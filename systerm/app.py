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
/* Same navy as the terminal background (config default #0d1320) so the pane and
   the terminal read as one continuous surface (no black-vs-blue seam). */
.atlas-panel { background: #0d1320; border-left: 1px solid #1c2430; }
.atlas-header {
    padding: 10px 12px; background: #0b1017; border-bottom: 1px solid #1c2430;
}
.atlas-dot { color: #6ddb73; }
.atlas-dot-off { color: #6a7480; }
.atlas-header label { color: #dfeee6; font-size: 12.5px; }
/* Model selector pill on the right of the header (local + installed models). */
.atlas-badge {
    background: #0b1119; color: #9fb0c6; border: 1px solid #1c2430;
    border-radius: 999px; padding: 1px 6px 1px 10px; font-size: 11.5px; font-family: monospace;
}
.atlas-badge label { color: #9fb0c6; font-size: 11.5px; font-family: monospace; }
.atlas-model, .atlas-model button {
    background: transparent; color: #cfe6c4; border: none; box-shadow: none;
    text-shadow: none; font-family: monospace; font-size: 11.5px; min-height: 0;
    padding: 0 2px;
}
.atlas-model button:hover { background: rgba(109,219,115,.12); border-radius: 5px; }
.atlas-model cellview { color: #cfe6c4; }
.atlas-ghost, .atlas-run {
    background: #0b1119; color: #8ae88f; border: 1px solid rgba(109,219,115,.40);
    border-radius: 7px; padding: 3px 10px; font-size: 12px; box-shadow: none;
    text-shadow: none;
}
.atlas-ghost { color: #8ea1b8; border-color: #263041; }
.atlas-ghost:hover, .atlas-run:hover { background: rgba(109,219,115,.10); }
.atlas-close { color: #8ea1b8; padding: 3px 8px; font-size: 13px; }
.atlas-close:hover { background: rgba(229,72,77,.14); color: #e5a0a2; border-color: rgba(229,72,77,.40); }
.atlas-card {
    background: #0c121a; border: 1px solid #1c2430; border-radius: 10px;
    padding: 11px 13px; border-left: 3px solid #263041;
}
.atlas-card-err { border-left-color: #e5484d; }
.atlas-card-ans { border-left-color: #6ddb73; }
.atlas-card-title {
    color: #e7eef6; font-size: 12.5px; font-weight: bold; letter-spacing: .2px;
}
.atlas-card-sub { color: #4a5568; font-size: 11px; font-family: monospace; }
/* Small status pill next to the title: red "exit 127" for failures, muted tag
   for answers. Cleaner than an ALL-CAPS "COMMAND FAILED - EXIT 127" string. */
.atlas-exit {
    color: #e5a0a2; background: rgba(229,72,77,.14); border: 1px solid rgba(229,72,77,.34);
    border-radius: 999px; padding: 0 8px; font-size: 10.5px; font-family: monospace;
}
.atlas-tag {
    color: #8ea1b8; background: #0b1119; border: 1px solid #1c2430;
    border-radius: 999px; padding: 0 8px; font-size: 10.5px; font-family: monospace;
}
.atlas-stream, .atlas-code-text {
    font-family: monospace; font-size: 12.5px; color: #c7d2e0;
}
.atlas-wait { color: #55627a; font-style: italic; }
.atlas-fail { color: #e5a0a2; }
.atlas-prose { color: #b7c4d4; font-size: 13px; }
/* The user's question, echoed at the top of an answer card. */
.atlas-question {
    color: #9fb0c6; font-size: 12.5px; padding: 6px 9px; margin-bottom: 2px;
    background: #0b1119; border-radius: 7px; border-left: 2px solid #3560d4;
}
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
    border-radius: 7px; caret-color: #6ddb73;
}
.atlas-footer {
    padding: 6px 12px; background: #0a0e15; border-top: 1px solid #1c2430;
    color: #6f7d94; font-family: monospace; font-size: 11px;
}
.atlas-footer label { color: #6f7d94; font-family: monospace; font-size: 11px; }
/* Footer action hints (Analyze / Ask / Setup): quiet until hovered. */
.atlas-hint {
    background: transparent; border: none; box-shadow: none; text-shadow: none;
    color: #7d8aa0; padding: 1px 6px; font-family: monospace; font-size: 11px;
    min-height: 0;
}
.atlas-hint:hover { color: #8ae88f; background: rgba(109,219,115,.08); border-radius: 5px; }
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
            # config value, an Atlas/GI hiccup) throws — otherwise the whole app
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
