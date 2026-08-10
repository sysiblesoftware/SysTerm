"""A single SysTerm terminal pane: a Vte.Terminal subclass that spawns the user's
shell and applies the profile. Splitting/closing and broadcast are handled by the
window (it owns the pane tree); this class just reports title changes and shell
exit back via callbacks, and exposes font-zoom + clipboard helpers."""

import os
import uuid
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Vte", "2.91")
from gi.repository import Gtk, Vte, GLib, Pango, Gdk  # noqa: E402


def _rgba(spec):
    c = Gdk.RGBA()
    c.parse(spec)
    return c


class SysTermTerminal(Vte.Terminal):
    def __init__(self, config, on_exit=None, on_title=None, command=None, cwd=None,
                 atlas_sock=None):
        super().__init__()
        self._config = config
        self._on_exit = on_exit
        self._on_title = on_title
        # Optional one-shot command (from `-e`/`--command`) and working directory
        # (from `--working-directory`); when unset a login shell opens in HOME.
        self._command = command
        self._cwd = cwd
        self._font_scale = 1.0
        # Sysible Atlas: a per-pane id + the control FIFO path, exported to the
        # shell so a failed command / `ai …` question from THIS pane is tagged
        # back to it (the companion then scrapes this pane's output for context).
        self.atlas_id = uuid.uuid4().hex
        self._atlas_sock = atlas_sock
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
        if self._atlas_sock:
            env["SYSIBLE_ATLAS_FIFO"] = self._atlas_sock
            env["SYSIBLE_ATLAS_ID"] = self.atlas_id
        envv = ["%s=%s" % kv for kv in env.items()]
        self.spawn_async(
            Vte.PtyFlags.DEFAULT,
            self._resolve_workdir(),
            self._resolve_argv(shell),
            envv,
            GLib.SpawnFlags.DEFAULT,
            None, None,          # child_setup, child_setup_data
            -1,                  # timeout: no limit
            None,                # cancellable
            self._spawn_done,    # callback
        )

    def _resolve_argv(self, shell):
        """Login shell by default; if a `-e`/`--command` was given, run that.
        A single command token is handed to the shell (`sh -c "…"`) so quoting
        and operators work; multiple tokens run as a literal argv (xterm-style)."""
        cmd = self._command
        if not cmd:
            return [shell]
        if len(cmd) == 1:
            return [shell, "-c", cmd[0]]
        return list(cmd)

    def _resolve_workdir(self):
        """An explicit --working-directory wins; otherwise use the process CWD
        (so "Open in SysTerm here" lands in that folder), falling back to HOME
        when launched from "/" (the usual cwd for a dock/menu launch)."""
        if self._cwd and os.path.isdir(self._cwd):
            return self._cwd
        home = os.environ.get("HOME")
        try:
            pcwd = os.getcwd()
        except OSError:
            pcwd = None
        if pcwd and pcwd != "/":
            return pcwd
        return home or pcwd or "/"

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

    def run_command(self, command):
        """Type a command into this pane and run it, as if entered by hand."""
        if not command:
            return
        data = command if command.endswith("\n") else command + "\n"
        for attempt in (data, data.encode()):   # VTE bindings vary: str vs bytes
            try:
                self.feed_child(attempt)
                break
            except TypeError:
                continue
        self.grab_focus()

    # ----- callbacks -------------------------------------------------------
    def _on_child_exited(self, _terminal, _status):
        if self._on_exit:
            self._on_exit(self)

    def _on_window_title_changed(self, _terminal):
        if self._on_title:
            self._on_title(self)

    def current_title(self):
        return self.get_window_title() or "SysTerm"

    # ----- Atlas: read what's on screen ------------------------------------
    def recent_text(self, max_lines=140):
        """Return the last ~max_lines of this pane's buffer as plain text, so the
        companion can read a command's output without any copy-paste. VTE's text
        API differs across versions, so try the reliable ones in order."""
        try:
            col = self.get_column_count()
            try:
                _, crow = self.get_cursor_position()
            except (TypeError, ValueError):
                crow = self.get_row_count()
            start = max(0, crow - max_lines)
            res = self.get_text_range(start, 0, crow, col)
            text = res[0] if isinstance(res, (tuple, list)) else res
            if text:
                return "\n".join(ln.rstrip() for ln in text.splitlines()).strip()
        except Exception:
            pass
        # Fallback: whole-buffer getter (older/newer bindings).
        for getter in ("get_text", "get_text_included_trailing_spaces"):
            fn = getattr(self, getter, None)
            if fn is None:
                continue
            try:
                res = fn()
                text = res[0] if isinstance(res, (tuple, list)) else res
                if text:
                    lines = [ln.rstrip() for ln in text.splitlines()]
                    return "\n".join(lines[-max_lines:]).strip()
            except Exception:
                continue
        return ""
