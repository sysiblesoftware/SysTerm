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
        try:
            win = SysTermWindow(self, self.config, command=command, cwd=cwd)
        except Exception:
            # A window must open even if something in its construction (a bad
            # config value, a GI hiccup) throws — otherwise the whole app
            # registers, then dies with no window and the user is locked out of
            # their terminal. Log it and fall back to a minimal window.
            import traceback
            traceback.print_exc()
            win = self._fallback_window(command=command, cwd=cwd)
        win.show_all()
        win.present()
        return win

    def _fallback_window(self, command=None, cwd=None):
        """Last-ditch window if the real one can't be built. A plain window with a
        single VTE shell, so the user still has a terminal."""
        from gi.repository import Vte, GLib
        win = Gtk.Window(title="SysTerm")
        win.set_default_size(900, 560)
        win.set_application(self)
        term = Vte.Terminal()
        argv = command or [os.environ.get("SHELL") or "/bin/bash"]
        term.spawn_async(
            Vte.PtyFlags.DEFAULT, cwd or os.environ.get("HOME") or "/",
            argv, None, GLib.SpawnFlags.SEARCH_PATH, None, None, -1, None, None, None)
        term.connect("child-exited", lambda *_a: win.close())
        win.add(term)
        return win

    def _install_css(self):
        try:
            provider = Gtk.CssProvider()
            provider.load_from_data(_CSS)
            screen = Gdk.Screen.get_default()
            if screen is not None:
                Gtk.StyleContext.add_provider_for_screen(
                    screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        except Exception:
            # A CSS parse error must never take the app down before any window
            # opens — the tint is cosmetic. Log and carry on.
            import traceback
            traceback.print_exc()

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
    # `--version` is a cheap liveness probe (used by the sysible-term fallback
    # shim to check SysTerm can import before handing it the session). It must
    # print and exit without a display or a window.
    if any(a in ("--version", "-V") for a in argv[1:]):
        from . import __version__
        sys.stdout.write("SysTerm %s\n" % __version__)
        return 0
    clean_argv, command, cwd = parse_terminal_args(argv)
    if not _has_display(clean_argv):
        sys.stderr.write(_NO_DISPLAY)
        return 1
    app = SysTermApp()
    app._initial_command = command
    app._initial_cwd = cwd
    return app.run(clean_argv)
