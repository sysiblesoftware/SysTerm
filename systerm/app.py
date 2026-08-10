"""SysTerm application: a Gtk.Application that owns the shared config, applies a
little CSS (the broadcast-mode window tint), and opens windows. Multiple windows
share one process."""

import os

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, Gio  # noqa: E402

from .config import Config, ensure_default_config
from .window import SysTermWindow

APP_ID = "io.systerm.SysTerm"

_CSS = b"""
/* A clear amber frame while broadcast is armed, so you never type to the whole
   fleet by accident. */
.systerm-broadcast { border: 2px solid #d0a060; }

/* ---- Sysible Atlas companion pane ---- */
.atlas-panel { background: #0e131b; border-left: 1px solid #1c2430; }
.atlas-header {
    padding: 10px 12px; background: #0b1017; border-bottom: 1px solid #1c2430;
}
.atlas-dot { color: #5cc746; }
.atlas-header label { color: #dfeee6; font-size: 12.5px; }
.atlas-ghost, .atlas-run {
    background: #0b1119; color: #7ed268; border: 1px solid rgba(92,199,70,.40);
    border-radius: 7px; padding: 3px 10px; font-size: 12px; box-shadow: none;
    text-shadow: none;
}
.atlas-ghost { color: #8ea1b8; border-color: #263041; }
.atlas-ghost:hover, .atlas-run:hover { background: rgba(92,199,70,.10); }
.atlas-card {
    background: #0c121a; border: 1px solid #1c2430; border-radius: 10px;
    padding: 11px 13px; border-left: 3px solid #263041;
}
.atlas-card-err { border-left-color: #e5484d; }
.atlas-card-ans { border-left-color: #5cc746; }
.atlas-card-title {
    color: #e7eef6; font-size: 11px; font-weight: bold; letter-spacing: 1px;
}
.atlas-card-sub { color: #4a5568; font-size: 11px; font-family: monospace; }
.atlas-stream, .atlas-code-text {
    font-family: monospace; font-size: 12.5px; color: #c7d2e0;
}
.atlas-fail { color: #e5a0a2; }
.atlas-prose { color: #b7c4d4; font-size: 13px; }
.atlas-code {
    background: #080c12; border: 1px solid #1c2430; border-radius: 7px;
    padding: 8px 10px; margin: 4px 0;
}
.atlas-code-text { color: #eaf1f8; }
.atlas-empty { color: #55627a; font-size: 12.5px; padding: 24px 16px; }
.atlas-ask {
    padding: 9px 12px; background: #0b1017; border-top: 1px solid #1c2430;
}
.atlas-ask entry {
    background: #0c121a; color: #dbe6f2; border: 1px solid #263041;
    border-radius: 7px; caret-color: #5cc746;
}
.atlas-footer {
    padding: 7px 12px; background: #0a0e15; border-top: 1px solid #1c2430;
    color: #6f7d94; font-family: monospace; font-size: 11px;
}
"""


class SysTermApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.config = Config()
        # First-window command/cwd parsed from `-e` / `--working-directory`.
        self._initial_command = None
        self._initial_cwd = None

    def do_startup(self):
        Gtk.Application.do_startup(self)
        ensure_default_config()
        self.config.load()
        Gtk.Window.set_default_icon_name(APP_ID)   # themed icon for the window/dock
        self._install_css()

    def do_activate(self):
        # The first window honors any `-e`/`--working-directory`; consume them so
        # a second window (Ctrl+Shift+N / "Open Window") gets a plain shell.
        command, cwd = self._initial_command, self._initial_cwd
        self._initial_command = self._initial_cwd = None
        self.new_window(command=command, cwd=cwd)

    def new_window(self, command=None, cwd=None):
        win = SysTermWindow(self, self.config, command=command, cwd=cwd)
        win.show_all()
        win.present()
        return win

    def _install_css(self):
        provider = Gtk.CssProvider()
        provider.load_from_data(_CSS)
        screen = Gdk.Screen.get_default()
        if screen is not None:
            Gtk.StyleContext.add_provider_for_screen(
                screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


_NO_DISPLAY = """\
SysTerm is a graphical terminal — it needs a desktop session (X11 or Wayland),
but no display was found.

  • On a desktop machine, launch it from your application menu, or run `systerm`
    inside that graphical session.
  • Over SSH, forward X first:   ssh -X user@host    then run `systerm`.

(No $DISPLAY or $WAYLAND_DISPLAY, or the display could not be opened.)
"""


def parse_terminal_args(argv):
    """Split off the x-terminal-emulator-style options so GApplication never sees
    them (it would abort on the unknown `-e`). Recognizes:

        -e / -x / --command CMD [ARGS…]   run CMD instead of a login shell
        --command=CMD                     (single-token form)
        --                                everything after is the command
        --working-directory[=]DIR         open in DIR (also --workdir)

    Returns (clean_argv_for_gapp, command_tokens_or_None, cwd_or_None)."""
    prog, rest = argv[:1], list(argv[1:])
    command = None
    cwd = None
    passthrough = []
    i = 0
    while i < len(rest):
        a = rest[i]
        if a in ("-e", "-x", "--command", "--"):
            command = rest[i + 1:]
            break
        if a.startswith("--command="):
            command = [a.split("=", 1)[1]]
        elif a in ("--working-directory", "--workdir"):
            if i + 1 < len(rest):
                cwd = rest[i + 1]
                i += 2
                continue
        elif a.startswith("--working-directory=") or a.startswith("--workdir="):
            cwd = a.split("=", 1)[1]
        else:
            passthrough.append(a)
        i += 1
    if command is not None and len(command) == 0:
        command = None   # a bare `-e` with no command → just open a shell
    return prog + passthrough, command, cwd


def _has_display(argv):
    """True if a usable display is reachable. Uses Gtk.init_check so a set-but-
    dead $DISPLAY (e.g. broken SSH forwarding) is caught, not just an unset one."""
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return False
    try:
        ok, _ = Gtk.init_check(argv)
        return bool(ok)
    except Exception:
        return False


def main(argv=None):
    import sys
    argv = argv if argv is not None else sys.argv
    clean_argv, command, cwd = parse_terminal_args(argv)
    if not _has_display(clean_argv):
        sys.stderr.write(_NO_DISPLAY)
        return 1
    app = SysTermApp()
    app._initial_command = command
    app._initial_cwd = cwd
    return app.run(clean_argv)
