"""The SysTerm window: a Gtk.Notebook of tabs, each tab a tree of Gtk.Paned
holding terminal panes (Terminator-style tiling). This module owns the pane tree
and implements split / close / focus-cycle / zoom / broadcast; the terminals
themselves (terminal.py) only run a shell and report title/exit."""

import os

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gio, GLib, Gdk, Pango  # noqa: E402

from . import __version__
from .terminal import SysTermTerminal
from . import atlas as _atlas
from .config import CONFIG_DIR

# Shown in the window title so it's obvious at a glance which build is running
# (a freshly-built .deb does nothing until you relaunch — the title makes a
# stale still-open instance easy to spot).
_TITLE = f"SysTerm {__version__}"


class SysTermWindow(Gtk.ApplicationWindow):
    def __init__(self, app, config, command=None, cwd=None):
        super().__init__(application=app, title=_TITLE)
        self.config = config
        # A `-e` command / `--working-directory` applies to the FIRST pane only;
        # tabs and splits opened later get a normal login shell. Consumed once by
        # _make_terminal().
        self._pending_command = command
        self._pending_cwd = cwd
        self.terminals = []          # every pane in this window (for broadcast + cycling)
        self.broadcast_active = False
        self._broadcasting = False   # re-entrancy guard for the key-press fan-out
        self._active = None          # last-focused terminal
        self._zoom_state = None      # bookkeeping for the zoom-pane toggle

        self.set_default_size(1120, 640)
        self.notebook = Gtk.Notebook()
        self.notebook.set_scrollable(True)
        self.notebook.set_show_border(False)
        self.notebook.connect("switch-page", lambda *_: GLib.idle_add(self._focus_current))

        # Sysible Atlas companion: a control FIFO the shells write to, a local
        # model client, and the companion pane docked to the right of the tabs.
        # The pane is hidden until you open it (Alt+A) or a command fails. ALL of
        # this is optional: if any part fails to initialise (missing GLib bits, a
        # sandboxed FIFO, etc.) SysTerm must still open as a plain terminal, so it
        # is wrapped and the window falls back to just the notebook.
        self._atlas = None
        self._atlas_ctl = None
        self._atlas_sock = None
        self._atlas_paned = None
        self._atlas_term = None
        # The PANE (ask box, analyze, right-click "Open Sysible Atlas") is the
        # core companion and must survive on its own. The control FIFO — which
        # lets the shell auto-report failed commands — is a SEPARATE, optional
        # feature: if it can't be created (sandboxed /tmp, no mkfifo), we still
        # want the pane and its menu entry, just without hands-free auto-catch.
        try:
            self._atlas_client = _atlas.AtlasClient()
            self._atlas = _atlas.AtlasPanel(self._atlas_client)
            self._atlas.on_ask = self._atlas_ask
            self._atlas.on_run = self._atlas_run
            self._atlas.on_analyze = self._atlas_analyze_active
            self._atlas.on_close = self._hide_atlas
        except Exception as e:
            self._atlas = None
            print("SysTerm: Atlas companion disabled (%s)" % e)

        if self._atlas is not None:
            try:
                self._atlas_ctl = _atlas.AtlasControl(self._on_atlas_event)
                self._atlas_sock = self._atlas_ctl.start()
            except Exception as e:
                self._atlas_sock = None
                try:
                    if self._atlas_ctl is not None:
                        self._atlas_ctl.stop()
                except Exception:
                    pass
                self._atlas_ctl = None
                print("SysTerm: Atlas auto-catch disabled (%s)" % e)

        if self._atlas is not None:
            self._atlas_paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
            self._atlas_paned.set_wide_handle(True)
            self._atlas_paned.pack1(self.notebook, True, True)
            self._atlas_paned.pack2(self._atlas, False, False)
            self.add(self._atlas_paned)
            self.connect("destroy", lambda *_: self._atlas_ctl and self._atlas_ctl.stop())
        else:
            self.add(self.notebook)   # plain terminal — no companion

        self._install_actions(app)
        self.new_tab()
        if self._atlas is not None:
            # Reveal the companion (with its Setup card) the FIRST time SysTerm is
            # ever launched, so new users are walked through installing Ollama and
            # downloading a model; collapsed by default thereafter.
            if self._first_run():
                GLib.idle_add(self._show_atlas)
            else:
                GLib.idle_add(self._atlas.hide)

    def _first_run(self):
        """True once — records a flag so the Atlas welcome shows only on the very
        first launch."""
        flag = os.path.join(CONFIG_DIR, ".atlas-welcomed")
        if os.path.exists(flag):
            return False
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            open(flag, "w").close()
        except OSError:
            pass
        return True

    # ===== tabs ============================================================
    def new_tab(self):
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)   # holds exactly one child: term or paned
        term = self._make_terminal()
        root.pack_start(term, True, True, 0)
        self.notebook.append_page(root, self._make_tab_label(root))
        self.notebook.set_tab_reorderable(root, True)
        self.notebook.show_all()
        self.notebook.set_show_tabs(self.notebook.get_n_pages() > 1)
        self.notebook.set_current_page(self.notebook.page_num(root))
        GLib.idle_add(term.grab_focus)

    def close_tab(self):
        root = self._current_root()
        if root is None:
            return
        for t in self._terminals_in(root):
            self._forget(t)
        self._remove_tab(root)

    def _remove_tab(self, root):
        idx = self.notebook.page_num(root)
        if idx != -1:
            self.notebook.remove_page(idx)
        if self.notebook.get_n_pages() == 0:
            self.destroy()
        else:
            self.notebook.set_show_tabs(self.notebook.get_n_pages() > 1)
            GLib.idle_add(self._focus_current)

    def _make_tab_label(self, root):
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        lbl = Gtk.Label(label="Terminal")
        lbl.set_ellipsize(Pango.EllipsizeMode.END)
        lbl.set_max_width_chars(22)
        btn = Gtk.Button(relief=Gtk.ReliefStyle.NONE)
        btn.set_focus_on_click(False)
        btn.add(Gtk.Image.new_from_icon_name("window-close-symbolic", Gtk.IconSize.MENU))
        btn.connect("clicked", lambda *_: self._close_tab_root(root))
        box.pack_start(lbl, True, True, 0)
        box.pack_start(btn, False, False, 0)
        box.show_all()
        root._systerm_label = lbl
        return box

    def _close_tab_root(self, root):
        for t in self._terminals_in(root):
            self._forget(t)
        self._remove_tab(root)

    # ===== terminals =======================================================
    def _make_terminal(self):
        command, cwd = self._pending_command, self._pending_cwd
        self._pending_command = self._pending_cwd = None   # first pane only
        term = SysTermTerminal(self.config, on_exit=self._on_term_exit,
                               on_title=self._on_term_title, command=command, cwd=cwd,
                               atlas_sock=self._atlas_sock)
        # Broadcast at the key-press layer, NOT via VTE's "commit" signal.
        # feed_child() makes a pane re-emit "commit", so a commit-based broadcast
        # feeds back on itself (one keystroke avalanches across every pane).
        # key-press-event fires ONLY for real keyboard input and is never emitted
        # by feed_child(), so replaying keys to the other panes cannot loop.
        term.connect("key-press-event", self._on_key_press)
        term.connect("focus-in-event", self._on_focus_in)
        term.connect("button-press-event", self._on_term_button_press)
        self.terminals.append(term)
        return term

    def _forget(self, term):
        if term in self.terminals:
            self.terminals.remove(term)
        if self._active is term:
            self._active = None

    def _on_focus_in(self, term, _event):
        self._active = term
        return False

    # ===== right-click context menu ========================================
    def _on_term_button_press(self, term, event):
        if event.button == 3 and event.type == Gdk.EventType.BUTTON_PRESS:
            term.grab_focus()          # so split/close target the pane you clicked
            self._active = term
            self._show_context_menu(term, event)
            return True                # we handled it; don't start a VTE selection
        return False

    def _show_context_menu(self, term, event):
        menu = Gtk.Menu()
        accels = self._accel_labels()

        def add(label, callback, *, action=None, enabled=True):
            item = Gtk.MenuItem()
            item.add(self._menu_row(label, accels.get(action)))
            item.set_sensitive(enabled)
            item.connect("activate", lambda *_: callback())
            menu.append(item)

        def sep():
            menu.append(Gtk.SeparatorMenuItem())

        # Run Command submenu: quick commands (click to run in this pane) plus
        # add/manage of your own. Lives at the top since it's the common action.
        run_item = Gtk.MenuItem(label="Run Command")
        submenu = Gtk.Menu()
        for label, cmd in self.config.commands:
            mi = Gtk.MenuItem(label=label)
            mi.set_tooltip_text(cmd)
            mi.connect("activate", lambda _w, c=cmd, t=term: t.run_command(c))
            submenu.append(mi)
        submenu.append(Gtk.SeparatorMenuItem())
        add_mi = Gtk.MenuItem(label="Add Command…")
        add_mi.connect("activate", lambda *_: self._add_command_dialog())
        submenu.append(add_mi)
        manage_mi = Gtk.MenuItem(label="Manage Commands…")
        manage_mi.connect("activate", lambda *_: self._manage_commands_dialog())
        submenu.append(manage_mi)
        run_item.set_submenu(submenu)
        menu.append(run_item)
        sep()

        # Sysible Atlas: open the AI companion, or analyze THIS pane's last output
        # in it (the right-click already set _active to this pane). Only shown when
        # the companion initialised.
        if self._atlas is not None:
            add("Open Sysible Atlas", self.open_atlas, action="toggle-atlas")
            add("Analyze in Sysible Atlas", self._atlas_analyze_active)
            sep()

        # Terminator wording: "horizontal" = top/bottom (a VERTICAL paned).
        add("Split Horizontally", lambda: self.split(Gtk.Orientation.VERTICAL),
            action="split-horizontal")
        add("Split Vertically", lambda: self.split(Gtk.Orientation.HORIZONTAL),
            action="split-vertical")
        sep()
        add("Open Tab", self.new_tab, action="new-tab")
        add("Open Window", lambda: self.get_application().new_window(), action="new-window")
        sep()
        add("Copy", term.copy, action="copy", enabled=term.get_has_selection())
        add("Paste", term.paste, action="paste")
        sep()
        multi = len(self._terminals_in(self._current_root())) > 1
        zoomed = bool(self._zoom_state) and self._zoom_state["root"] is self._current_root()
        add("Restore All Terminals" if zoomed else "Zoom Terminal", self.toggle_zoom,
            action="zoom-pane", enabled=multi or zoomed)
        bcast = Gtk.CheckMenuItem(label="Broadcast to All Terminals")
        bcast.set_active(self.broadcast_active)
        bcast.connect("toggled", lambda *_: self.toggle_broadcast())
        menu.append(bcast)
        sep()
        add("Close Terminal", lambda: self.close_pane(term), action="close-pane")

        menu.show_all()
        menu.attach_to_widget(term, None)
        menu.connect("selection-done", lambda m: m.destroy())
        if hasattr(menu, "popup_at_pointer"):
            menu.popup_at_pointer(event)
        else:  # GTK < 3.22
            menu.popup(None, None, None, None, event.button, event.time)

    def _menu_row(self, label, accel):
        """A menu-item child that right-aligns the shortcut hint, Terminator-style."""
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=18)
        row.pack_start(Gtk.Label(label=label, xalign=0.0), True, True, 0)
        if accel:
            hint = Gtk.Label(label=accel, xalign=1.0)
            hint.get_style_context().add_class("dim-label")
            row.pack_end(hint, False, False, 0)
        return row

    def _accel_labels(self):
        """Map action name → human shortcut (e.g. "Ctrl+Shift+O") for the menu."""
        out = {}
        for name in ("split-horizontal", "split-vertical", "new-tab", "new-window",
                     "copy", "paste", "zoom-pane", "close-pane"):
            accels = self.config.accels_for(name)
            if accels:
                key, mods = Gtk.accelerator_parse(accels[0])
                if key:
                    out[name] = Gtk.accelerator_get_label(key, mods)
        return out

    # ===== Run Command menu: add / manage =================================
    def _add_command_dialog(self, label="", command=""):
        dlg = Gtk.Dialog(title="Add Command", transient_for=self, modal=True)
        dlg.add_button("Cancel", Gtk.ResponseType.CANCEL)
        ok = dlg.add_button("Add", Gtk.ResponseType.OK)
        ok.get_style_context().add_class("suggested-action")
        box = dlg.get_content_area()
        box.set_spacing(8)
        grid = Gtk.Grid(column_spacing=10, row_spacing=8, margin=14)
        e_label = Gtk.Entry(text=label, width_chars=34)
        e_cmd = Gtk.Entry(text=command, width_chars=44)
        e_label.set_placeholder_text("Menu label — e.g. apt update")
        e_cmd.set_placeholder_text("Command — e.g. sudo apt update -y")
        e_cmd.set_activates_default(True)
        dlg.set_default_response(Gtk.ResponseType.OK)
        grid.attach(Gtk.Label(label="Label", xalign=0), 0, 0, 1, 1)
        grid.attach(e_label, 1, 0, 1, 1)
        grid.attach(Gtk.Label(label="Command", xalign=0), 0, 1, 1, 1)
        grid.attach(e_cmd, 1, 1, 1, 1)
        box.add(grid)
        dlg.show_all()
        ok_clicked = dlg.run() == Gtk.ResponseType.OK
        cmd = e_cmd.get_text().strip()
        lbl = e_label.get_text().strip() or cmd
        dlg.destroy()
        if ok_clicked and cmd:
            self.config.commands.append((lbl, cmd))
            self.config.save_commands()
            return True
        return False

    def _manage_commands_dialog(self):
        dlg = Gtk.Dialog(title="Manage Commands", transient_for=self, modal=True)
        dlg.add_button("Close", Gtk.ResponseType.CLOSE)
        box = dlg.get_content_area()
        box.set_spacing(8)
        listbox = Gtk.ListBox(margin=12)

        def refresh():
            for c in listbox.get_children():
                listbox.remove(c)
            for i, (label, cmd) in enumerate(self.config.commands):
                row = Gtk.ListBoxRow()
                h = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10, margin=4)
                text = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
                text.set_markup("<b>%s</b>  <span alpha='60%%'>%s</span>"
                                % (GLib.markup_escape_text(label),
                                   GLib.markup_escape_text(cmd)))
                rm = Gtk.Button(label="Remove")
                rm.connect("clicked", lambda _b, idx=i: self._remove_command(idx, refresh))
                h.pack_start(text, True, True, 0)
                h.pack_end(rm, False, False, 0)
                row.add(h)
                listbox.add(row)
            listbox.show_all()

        refresh()
        sw = Gtk.ScrolledWindow(min_content_height=240, min_content_width=440)
        sw.add(listbox)
        box.add(sw)
        add_btn = Gtk.Button(label="Add Command…", margin=10)
        add_btn.connect("clicked", lambda *_: (self._add_command_dialog() and refresh()))
        box.add(add_btn)
        dlg.show_all()
        dlg.run()
        dlg.destroy()

    def _remove_command(self, idx, refresh):
        if 0 <= idx < len(self.config.commands):
            self.config.commands.pop(idx)
            self.config.save_commands()
            refresh()

    def _on_term_exit(self, term):
        # Destroying a pane kills its shell, so VTE fires "child-exited" during
        # close_pane's .destroy(). close_pane forgets the terminal before
        # destroying it, so a terminal we no longer track is already being torn
        # down — acting again would re-enter close_pane with a detached widget.
        if term not in self.terminals:
            return
        self.close_pane(term)

    def _on_term_title(self, term):
        root = self._tab_root_of(term)
        if root is not None and getattr(root, "_systerm_label", None) is not None:
            root._systerm_label.set_text(term.current_title())

    # ===== splitting =======================================================
    def split(self, orientation):
        term = self._active_terminal()
        if term is None:
            return
        parent = term.get_parent()
        new_term = self._make_terminal()
        paned = Gtk.Paned(orientation=orientation)
        paned.set_wide_handle(True)
        if isinstance(parent, Gtk.Paned):
            in_first = parent.get_child1() is term
            parent.remove(term)
            paned.pack1(term, True, True)
            paned.pack2(new_term, True, True)
            if in_first:
                parent.pack1(paned, True, True)
            else:
                parent.pack2(paned, True, True)
        else:  # tab-root Box: term is its only child
            parent.remove(term)
            paned.pack1(term, True, True)
            paned.pack2(new_term, True, True)
            parent.pack_start(paned, True, True, 0)
        paned.show_all()
        self._even_paned(paned)
        GLib.idle_add(new_term.grab_focus)

    def _even_paned(self, paned):
        """Set a fresh split to 50/50 once GTK has allocated it a size."""
        state = {"n": 0}

        def do_set():
            state["n"] += 1
            alloc = paned.get_allocation()
            horiz = paned.get_orientation() == Gtk.Orientation.HORIZONTAL
            size = alloc.width if horiz else alloc.height
            if size > 1:
                paned.set_position(size // 2)
                return False
            return state["n"] < 50   # keep trying briefly until it's on screen

        GLib.idle_add(do_set)

    # ===== closing a pane ==================================================
    def close_pane(self, term=None):
        term = term or self._active_terminal()
        if term is None:
            return
        self._forget(term)
        parent = term.get_parent()
        if parent is None:          # already detached (defensive) — just tear it down
            term.destroy()
            return
        if isinstance(parent, Gtk.Paned):
            sibling = parent.get_child2() if parent.get_child1() is term else parent.get_child1()
            grand = parent.get_parent()
            parent.remove(term)
            parent.remove(sibling)
            self._replace_child(grand, parent, sibling)
            term.destroy()
            focus = self._first_terminal(sibling)
            if focus:
                GLib.idle_add(focus.grab_focus)
        else:  # sole terminal in a tab-root Box → the tab goes away
            term.destroy()
            self._remove_tab(parent)

    def _replace_child(self, parent, old, new):
        if isinstance(parent, Gtk.Paned):
            in_first = parent.get_child1() is old
            parent.remove(old)
            if in_first:
                parent.pack1(new, True, True)
            else:
                parent.pack2(new, True, True)
        elif isinstance(parent, Gtk.Box):
            parent.remove(old)
            parent.pack_start(new, True, True, 0)
        new.show_all()

    # ===== focus cycling ===================================================
    def cycle_pane(self, step):
        terms = self._terminals_in(self._current_root())
        if not terms:
            return
        cur = self._active if self._active in terms else terms[0]
        terms[(terms.index(cur) + step) % len(terms)].grab_focus()

    # ===== zoom one pane to fill the tab (toggle) ==========================
    def toggle_zoom(self):
        root = self._current_root()
        if root is None:
            return
        if self._zoom_state and self._zoom_state["root"] is root:
            self._unzoom()
            return
        term = self._active_terminal()
        if term is None:
            return
        tree = root.get_children()[0]
        if tree is term:
            return  # only one pane — nothing to zoom
        parent = term.get_parent()
        in_first = isinstance(parent, Gtk.Paned) and parent.get_child1() is term

        parent.remove(term)          # detach the pane from the tree (tree kept alive by ref)
        root.remove(tree)            # detach the tree from the tab root
        root.pack_start(term, True, True, 0)
        term.show_all()
        self._zoom_state = {"root": root, "term": term, "tree": tree,
                            "parent": parent, "in_first": in_first}
        GLib.idle_add(term.grab_focus)

    def _unzoom(self):
        st = self._zoom_state
        self._zoom_state = None
        root, term, tree, parent, in_first = (st["root"], st["term"], st["tree"],
                                              st["parent"], st["in_first"])
        root.remove(term)
        if in_first:
            parent.pack1(term, True, True)
        else:
            parent.pack2(term, True, True)
        root.pack_start(tree, True, True, 0)
        tree.show_all()
        GLib.idle_add(term.grab_focus)

    # ===== broadcast (type once, send to every pane) =======================
    def toggle_broadcast(self):
        self.broadcast_active = not self.broadcast_active
        self.set_title(f"{_TITLE} — BROADCAST" if self.broadcast_active else _TITLE)
        ctx = self.get_style_context()
        (ctx.add_class if self.broadcast_active else ctx.remove_class)("systerm-broadcast")

    def _on_key_press(self, term, event):
        # Broadcast a real keystroke to every OTHER pane by replaying the key
        # event to them. Two things keep this from looping:
        #   1. Only the pane that holds the keyboard focus originates a broadcast.
        #   2. A hard re-entrancy guard: while we're fanning a keystroke out, any
        #      key-press this triggers on another pane (e.g. if replaying the event
        #      re-dispatches synchronously) is ignored. feed_child() also never
        #      emits "key-press-event", so there is no echo path at all.
        # Returns False so the focused pane still processes the key itself.
        if not self.broadcast_active or self._broadcasting or not term.has_focus():
            return False
        self._broadcasting = True
        try:
            for t in self.terminals:
                if t is term:
                    continue
                win = t.get_window()
                if win is None:
                    continue                 # not realized yet; skip
                ev = event.copy()
                ev.window = win
                t.event(ev)
        finally:
            self._broadcasting = False
        return False

    # ===== Sysible Atlas ===================================================
    def toggle_atlas(self):
        if self._atlas is None:
            return
        if self._atlas.get_visible():
            self._atlas.hide()
            self._focus_current()
        else:
            self._show_atlas()
            self._atlas.focus_ask()

    def open_atlas(self):
        """Reveal the companion and focus the ask box (right-click / Alt+A)."""
        if self._atlas is None:
            return
        self._show_atlas()
        self._atlas.focus_ask()

    def _hide_atlas(self):
        """Hide the companion (the pane's ✕ button). hide() keeps the widget and
        all its cards, so the conversation history is intact when it reopens."""
        if self._atlas is None:
            return
        self._atlas.hide()
        self._focus_current()

    def _show_atlas(self):
        if self._atlas is None:
            return
        if not self._atlas.get_visible():
            self._atlas.show()
            alloc = self._atlas_paned.get_allocation()
            if alloc.width > 1:
                self._atlas_paned.set_position(max(360, alloc.width - 420))

    def _term_by_id(self, pane_id):
        for t in self.terminals:
            if getattr(t, "atlas_id", None) == pane_id:
                return t
        return None

    def _atlas_run_target(self, term):
        """Label for the card's Run button: 'terminal N' naming the exact pane the
        command will be typed into (1-based, matching left-to-right order)."""
        try:
            return "terminal %d" % (self.terminals.index(term) + 1)
        except (ValueError, AttributeError):
            return "terminal"

    def _atlas_messages(self, term, command=None, exit_code=None, question=None):
        ctx = []
        if command:
            # The specific failed command — emphasise it so the model fixes THIS
            # one, not some other command elsewhere in the scrollback.
            ctx.append("The command that failed (fix THIS one only):\n%s" % command)
        if exit_code is not None:
            ctx.append("Its exit code: %s" % exit_code)
        # A focused slice of the buffer. A caught failure needs only the last few
        # lines (the command + its own output); sending the whole scrollback makes
        # a small model grab the wrong command. A manual analysis gets a bit more.
        lines = 24 if command else 60
        output = term.recent_text(max_lines=lines) if term is not None else ""
        if output:
            if len(output) > 4000:
                output = "…(truncated)…\n" + output[-4000:]
            ctx.append("Recent terminal output (context only):\n%s" % output)
        if question:
            ctx.append("Question: %s" % question)
        return [
            {"role": "system", "content": _atlas.SYSTEM_PROMPT},
            {"role": "user", "content": "\n\n".join(ctx) or "Explain the last output."},
        ]

    def _on_atlas_event(self, kind, pane_id, exit_code, text):
        """Fired from the control FIFO (main loop). text is the command (error)
        or the question (ask)."""
        if self._atlas is None:
            return False
        # Auto-catch is opt-in by *having Atlas open*. A failed command must NOT
        # pop the pane open on its own — the user opens Atlas when they want it
        # watching (Alt+A / right-click), and only then do caught errors appear.
        if not self._atlas.get_visible():
            return False
        term = self._term_by_id(pane_id) or self._active_terminal()
        self._atlas_term = term
        rt = self._atlas_run_target(term)
        if kind == "error":
            title = "CAUGHT · EXIT %s" % exit_code
            self._atlas.start_card("error", title, text,
                                   self._atlas_messages(term, command=text,
                                                        exit_code=exit_code),
                                   run_target=rt)
        else:  # ask
            self._atlas.start_card("answer", "ANSWER", "local",
                                   self._atlas_messages(term, question=text),
                                   run_target=rt)
        return False   # in case invoked via idle_add

    def _atlas_ask(self, question):
        if self._atlas is None:
            return
        term = self._atlas_term or self._active_terminal()
        self._atlas_term = term
        self._show_atlas()
        self._atlas.start_card("answer", "ANSWER", "local",
                               self._atlas_messages(term, question=question),
                               run_target=self._atlas_run_target(term))

    def _atlas_analyze_active(self):
        if self._atlas is None:
            return
        term = self._active_terminal()
        if term is None:
            return
        self._atlas_term = term
        self._show_atlas()
        self._atlas.start_card("answer", "ANALYSIS", "local",
                               self._atlas_messages(term),
                               run_target=self._atlas_run_target(term))

    def _atlas_run(self, command):
        term = self._atlas_term or self._active_terminal()
        if term is not None:
            term.run_command(command)
            term.grab_focus()

    # ===== helpers =========================================================
    def _current_root(self):
        idx = self.notebook.get_current_page()
        return self.notebook.get_nth_page(idx) if idx != -1 else None

    def _tab_root_of(self, term):
        for i in range(self.notebook.get_n_pages()):
            page = self.notebook.get_nth_page(i)
            if term in self._terminals_in(page):
                return page
        return None

    def _terminals_in(self, widget):
        out = []

        def walk(w):
            if isinstance(w, SysTermTerminal):
                out.append(w)
            elif isinstance(w, Gtk.Container):
                for ch in w.get_children():
                    walk(ch)

        if widget is not None:
            walk(widget)
        return out

    def _first_terminal(self, widget):
        found = self._terminals_in(widget)
        return found[0] if found else None

    def _active_terminal(self):
        root = self._current_root()
        terms = self._terminals_in(root)
        if self._active in terms:
            return self._active
        return terms[0] if terms else None

    def _active_do(self, fn):
        t = self._active_terminal()
        if t is not None:
            fn(t)

    def _focus_current(self):
        t = self._active_terminal()
        if t is not None:
            t.grab_focus()
        return False

    # ===== actions / accelerators =========================================
    def _install_actions(self, app):
        specs = {
            # Terminator semantics: "split horizontal" = panes stacked top/bottom
            # = a VERTICAL GtkPaned; "split vertical" = side-by-side = HORIZONTAL.
            "split-horizontal": lambda *_: self.split(Gtk.Orientation.VERTICAL),
            "split-vertical": lambda *_: self.split(Gtk.Orientation.HORIZONTAL),
            "new-tab": lambda *_: self.new_tab(),
            "new-window": lambda *_: self.get_application().new_window(),
            "close-pane": lambda *_: self.close_pane(),
            "close-tab": lambda *_: self.close_tab(),
            "copy": lambda *_: self._active_do(lambda t: t.copy()),
            "paste": lambda *_: self._active_do(lambda t: t.paste()),
            "next-tab": lambda *_: self.notebook.next_page(),
            "prev-tab": lambda *_: self.notebook.prev_page(),
            "next-pane": lambda *_: self.cycle_pane(1),
            "prev-pane": lambda *_: self.cycle_pane(-1),
            "zoom-pane": lambda *_: self.toggle_zoom(),
            "toggle-broadcast": lambda *_: self.toggle_broadcast(),
            "toggle-atlas": lambda *_: self.toggle_atlas(),
            "zoom-in": lambda *_: self._active_do(lambda t: t.zoom(1)),
            "zoom-out": lambda *_: self._active_do(lambda t: t.zoom(-1)),
            "zoom-reset": lambda *_: self._active_do(lambda t: t.zoom(0)),
        }
        for name, cb in specs.items():
            act = Gio.SimpleAction.new(name, None)
            act.connect("activate", cb)
            self.add_action(act)
            accels = self.config.accels_for(name)
            if accels:
                app.set_accels_for_action("win.%s" % name, accels)
