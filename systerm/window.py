"""The SysTerm window: a Gtk.Notebook of tabs, each tab a tree of Gtk.Paned
holding terminal panes (Terminator-style tiling). This module owns the pane tree
and implements split / close / focus-cycle / zoom / broadcast; the terminals
themselves (terminal.py) only run a shell and report title/exit."""

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gio, GLib, Pango  # noqa: E402

from .terminal import SysTermTerminal


class SysTermWindow(Gtk.ApplicationWindow):
    def __init__(self, app, config):
        super().__init__(application=app, title="SysTerm")
        self.config = config
        self.terminals = []          # every pane in this window (for broadcast + cycling)
        self.broadcast_active = False
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
        term.connect("commit", self._on_commit)
        term.connect("focus-in-event", self._on_focus_in)
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

    def _on_term_exit(self, term):
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
        self.set_title("SysTerm — BROADCAST" if self.broadcast_active else "SysTerm")
        ctx = self.get_style_context()
        (ctx.add_class if self.broadcast_active else ctx.remove_class)("systerm-broadcast")

    def _on_commit(self, term, text, _size):
        if not self.broadcast_active or not text:
            return
        data = text.encode() if isinstance(text, str) else bytes(text)
        for t in self.terminals:
            if t is not term:
                _feed_child(t, data)

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


def _feed_child(term, data):
    """feed_child's signature changed across VTE versions (bytes vs (bytes,len))."""
    try:
        term.feed_child(data)
    except TypeError:
        term.feed_child(data, len(data))
