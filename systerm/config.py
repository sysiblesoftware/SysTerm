"""SysTerm configuration: a small INI at ~/.config/systerm/config.ini holding the
terminal profile (font, colours, scrollback, cursor) and the keybindings. Missing
file or keys fall back to the defaults below, and a commented default file is
written on first run so users have something to edit."""

import os
import configparser

CONFIG_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "systerm")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.ini")

# A 16-colour palette (normal 0-7 then bright 8-15). Green and blue are tuned to
# the Sysible logo (green #43a047, royal blue #3560d4) so shell prompts that use
# ANSI green/blue — e.g. the default user@host:path prompt — match the brand.
# Override under [profile] palette.
DEFAULT_PALETTE = [
    "#1a1a1a", "#c25b56", "#43a047", "#d0a060", "#3560d4", "#a07daf", "#5fa7a7", "#c0c0c0",
    "#4d4d4d", "#e07b76", "#52b657", "#f0c080", "#5580ee", "#c09dcf", "#7fc7c7", "#f0f0f0",
]

# action -> default accelerator (GTK accel syntax; <Primary> is Ctrl).
DEFAULT_KEYS = {
    "split-horizontal": "<Primary><Shift>o",   # panes stacked top/bottom
    "split-vertical": "<Primary><Shift>e",      # panes side by side
    "new-tab": "<Primary><Shift>t",
    "new-window": "<Primary><Shift>n",
    "close-pane": "<Primary><Shift>w",
    "close-tab": "<Primary><Shift>q",
    "copy": "<Primary><Shift>c",
    "paste": "<Primary><Shift>v",
    "next-tab": "<Primary>Page_Down",
    "prev-tab": "<Primary>Page_Up",
    "next-pane": "<Primary><Shift>Right",
    "prev-pane": "<Primary><Shift>Left",
    "zoom-pane": "<Primary><Shift>x",           # toggle: maximise this pane
    "toggle-broadcast": "<Primary><Shift>b",    # type once, send to every pane
    "zoom-in": "<Primary>plus",
    "zoom-out": "<Primary>minus",
    "zoom-reset": "<Primary>0",
}


class Config:
    """Loaded profile + keybindings. Attributes are plain values so the rest of
    the app never touches configparser."""

    def __init__(self):
        self.font = "Monospace 11"
        self.scrollback_lines = 10000
        self.cursor_shape = "block"          # block | ibeam | underline
        self.foreground = "#d0d0d0"
        self.background = "#141414"
        self.palette = list(DEFAULT_PALETTE)
        self.audible_bell = False
        self.keys = dict(DEFAULT_KEYS)

    def load(self, path=CONFIG_PATH):
        cp = configparser.ConfigParser()
        try:
            if not cp.read(path):
                return self
        except configparser.Error:
            return self
        p = cp["profile"] if cp.has_section("profile") else {}
        self.font = p.get("font", self.font)
        self.cursor_shape = p.get("cursor_shape", self.cursor_shape)
        self.foreground = p.get("foreground", self.foreground)
        self.background = p.get("background", self.background)
        self.audible_bell = _as_bool(p.get("audible_bell"), self.audible_bell)
        try:
            self.scrollback_lines = int(p.get("scrollback_lines", self.scrollback_lines))
        except (TypeError, ValueError):
            pass
        raw_pal = p.get("palette", "")
        cols = [c.strip() for c in raw_pal.split(",") if c.strip()]
        if len(cols) in (8, 16, 256):
            self.palette = cols
        if cp.has_section("keys"):
            for action, accel in cp["keys"].items():
                self.keys[action] = accel
        return self

    def accels_for(self, action):
        """GTK accel list for an action (empty string disables the binding)."""
        accel = self.keys.get(action, "")
        return [accel] if accel else []


def _as_bool(val, default):
    if val is None:
        return default
    return str(val).strip().lower() in ("1", "true", "yes", "on")


DEFAULT_CONFIG_TEXT = """\
# SysTerm configuration. Delete a line to fall back to its built-in default.
[profile]
font = Monospace 11
scrollback_lines = 10000
cursor_shape = block
foreground = #d0d0d0
background = #141414
audible_bell = false
# 16 comma-separated hex colours (normal 0-7, bright 8-15); leave blank for default.
# palette =

[keys]
split-horizontal = <Primary><Shift>o
split-vertical = <Primary><Shift>e
new-tab = <Primary><Shift>t
new-window = <Primary><Shift>n
close-pane = <Primary><Shift>w
close-tab = <Primary><Shift>q
copy = <Primary><Shift>c
paste = <Primary><Shift>v
next-tab = <Primary>Page_Down
prev-tab = <Primary>Page_Up
next-pane = <Primary><Shift>Right
prev-pane = <Primary><Shift>Left
zoom-pane = <Primary><Shift>x
toggle-broadcast = <Primary><Shift>b
zoom-in = <Primary>plus
zoom-out = <Primary>minus
zoom-reset = <Primary>0
"""


def ensure_default_config():
    """Write a commented default config on first run so there's something to edit."""
    try:
        if not os.path.exists(CONFIG_PATH):
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                f.write(DEFAULT_CONFIG_TEXT)
    except OSError:
        pass
