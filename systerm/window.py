"""The SysTerm window: a Gtk.Notebook of tabs, each tab a tree of Gtk.Paned
holding terminal panes (Terminator-style tiling). This module owns the pane tree
and implements split / close / focus-cycle / zoom / broadcast; the terminals
themselves (terminal.py) only run a shell and report title/exit."""

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gio, GLib, Gdk, Pango  # noqa: E402

from . import __version__
from .terminal import SysTermTerminal

# Shown in the window title so it's obvious at a glance which build is running
# (a freshly-built .deb does nothing until you relaunch — the title makes a
# stale still-open instance easy to spot).
_TITLE = f"SysTerm {__version__}"


class SysTermWindow(Gtk.ApplicationWindow):
    def __init__(self, app, config):
        super().__init__(application=app, title=_TITLE)
        self.config = config
        self.terminals = []          # every pane in this window (for broadcast + cycling)
        self.broadcast_active = False
        self._broadcasting = False   # re-entrancy guard for the key-press fan-out
        self._active = None          # last-focused terminal
        self._zoom_state = None      # bookkeeping for the zoom-pane toggle

        self.set_default_size(960, 600)
        self.notebook = Gtk.Notebook()
        self.notebook.set_scrollable(True)
        self.notebook.set_show_border(False)
        self.notebook.connect("switch-page", lambda *_: GLib.idle_add(self._focus_current))
        self.add(self.notebook)

        self._install_actions(app)
        self.new_tab()

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
        term = SysTermTerminal(self.config, on_exit=self._on_term_exit, on_title=self._on_term_title)
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
