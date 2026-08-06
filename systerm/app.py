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

    def do_startup(self):
        Gtk.Application.do_startup(self)
        ensure_default_config()
        self.config.load()
        Gtk.Window.set_default_icon_name(APP_ID)   # themed icon for the window/dock
        self._install_css()

    def do_activate(self):
        self.new_window()

    def new_window(self):
        win = SysTermWindow(self, self.config)
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
    if not _has_display(argv):
        sys.stderr.write(_NO_DISPLAY)
        return 1
    return SysTermApp().run(argv)
