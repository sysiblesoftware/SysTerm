"""A single SysTerm terminal pane: a Vte.Terminal subclass that spawns the user's
shell and applies the profile. Splitting/closing and broadcast are handled by the
window (it owns the pane tree); this class just reports title changes and shell
exit back via callbacks, and exposes font-zoom + clipboard helpers."""

import os
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Vte", "2.91")
from gi.repository import Gtk, Vte, GLib, Pango, Gdk  # noqa: E402


def _rgba(spec):
    c = Gdk.RGBA()
    c.parse(spec)
    return c


class SysTermTerminal(Vte.Terminal):
    def __init__(self, config, on_exit=None, on_title=None):
        super().__init__()
        self._config = config
        self._on_exit = on_exit
        self._on_title = on_title
        self._font_scale = 1.0
        self.apply_profile(config)
        self.set_scroll_on_output(False)
        self.set_scroll_on_keystroke(True)
        self.set_mouse_autohide(True)
        self.set_audible_bell(config.audible_bell)
        try:
            self.set_allow_hyperlink(True)
        except Exception:
            pass  # older VTE without hyperlink support
        self.connect("child-exited", self._on_child_exited)
        self.connect("window-title-changed", self._on_window_title_changed)
        self.spawn_shell()

    # ----- profile ---------------------------------------------------------
    def apply_profile(self, config):
        self._base_font = Pango.FontDescription.from_string(config.font)
        self._apply_font_scale()
        self.set_scrollback_lines(config.scrollback_lines)
        shape = {
            "block": Vte.CursorShape.BLOCK,
            "ibeam": Vte.CursorShape.IBEAM,
            "underline": Vte.CursorShape.UNDERLINE,
        }.get(config.cursor_shape.lower(), Vte.CursorShape.BLOCK)
        self.set_cursor_shape(shape)
        palette = [_rgba(c) for c in config.palette] or None
        self.set_colors(_rgba(config.foreground), _rgba(config.background), palette)

    def _apply_font_scale(self):
        fd = self._base_font.copy()
        size = fd.get_size() or (11 * Pango.SCALE)
        fd.set_size(int(size * self._font_scale))
        self.set_font(fd)

    def zoom(self, step):
        """step > 0 bigger, < 0 smaller, 0 resets to the profile size."""
        if step == 0:
            self._font_scale = 1.0
        else:
            self._font_scale = max(0.4, min(4.0, self._font_scale + step * 0.1))
        self._apply_font_scale()

    # ----- shell -----------------------------------------------------------
    def spawn_shell(self):
        shell = os.environ.get("SHELL") or "/bin/bash"
        env = dict(os.environ)
        env.setdefault("TERM", "xterm-256color")
        env["SYSTERM"] = "1"
        envv = ["%s=%s" % kv for kv in env.items()]
        workdir = os.environ.get("HOME") or os.getcwd()
        self.spawn_async(
            Vte.PtyFlags.DEFAULT,
            workdir,
            [shell],
            envv,
            GLib.SpawnFlags.DEFAULT,
            None, None,          # child_setup, child_setup_data
            -1,                  # timeout: no limit
            None,                # cancellable
            self._spawn_done,    # callback
        )

    def _spawn_done(self, terminal, pid, error, *_):
        if error is not None:
            self.feed(("\r\n\033[31mSysTerm: failed to start shell: %s\033[0m\r\n"
                       % error.message).encode())

    # ----- clipboard -------------------------------------------------------
    def copy(self):
        if self.get_has_selection():
            self.copy_clipboard_format(Vte.Format.TEXT)

    def paste(self):
        self.paste_clipboard()

    # ----- callbacks -------------------------------------------------------
    def _on_child_exited(self, _terminal, _status):
        if self._on_exit:
            self._on_exit(self)

    def _on_window_title_changed(self, _terminal):
        if self._on_title:
            self._on_title(self)

    def current_title(self):
        return self.get_window_title() or "SysTerm"
