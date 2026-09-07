"""SysTerm configuration: a small INI at ~/.config/systerm/config.ini holding the
terminal profile (font, colours, scrollback, cursor) and the keybindings. Missing
file or keys fall back to the defaults below, and a commented default file is
written on first run so users have something to edit."""

import os
import configparser
import re
import sys

CONFIG_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "systerm")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.ini")

# A 16-colour palette (normal 0-7 then bright 8-15). Green and blue match the
# Sysible brand accent — green #5cc746 (bright #6ddb73), royal blue
# #3560d4 — so shell prompts that use ANSI green/blue (e.g. the default
# user@host:path prompt) use the same green. Override under
# [profile] palette.
DEFAULT_PALETTE = [
    "#1a1a1a", "#c25b56", "#5cc746", "#d0a060", "#3560d4", "#a07daf", "#5fa7a7", "#c0c0c0",
    "#4d4d4d", "#e07b76", "#6ddb73", "#f0c080", "#5580ee", "#c09dcf", "#7fc7c7", "#f0f0f0",
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

# (label, command) pairs offered in the terminal right-click "Run Command" menu.
# Users add/remove their own under [commands] (see save_commands / the menu).
DEFAULT_COMMANDS = [
    ("apt update", "sudo apt update -y"),
    ("apt update && upgrade", "sudo apt update -y && sudo apt upgrade -y"),
    ("apt full-upgrade", "sudo apt update -y && sudo apt full-upgrade -y"),
    ("apt autoremove", "sudo apt autoremove -y"),
    ("Disk usage", "df -h"),
    ("Memory usage", "free -h"),
    ("Failed services", "systemctl --failed"),
]


# An INI key must be ONE line and must not contain the key/value delimiter. A menu
# label that breaks either rule makes the whole file unparseable — and load() then
# falls back to defaults, so one stray character in a label silently costs the user
# their font, colours, keybindings AND every other saved command. Normalise here so
# that cannot happen, keeping the label as close to what was typed as the format
# allows.
_LABEL_UNSAFE = re.compile(r"[\r\n\x00-\x1f]+")


def safe_label(label, fallback="Command"):
    s = _LABEL_UNSAFE.sub(" ", label or "").strip()
    s = s.replace("=", "-").replace(":", "-")
    s = s.lstrip("[#;").strip()
    return s or fallback


class Config:
    """Loaded profile + keybindings. Attributes are plain values so the rest of
    the app never touches configparser."""

    def __init__(self):
        # Crisp, compact monospace for a sharp look (keeps
        # column alignment, unlike a proportional font). Bump with Ctrl++ if you
        # prefer larger.
        self.font = "Monospace 9"
        self.scrollback_lines = 10000
        self.cursor_shape = "block"          # block | ibeam | underline
        # Cohesive dark-navy backdrop (no black-vs-blue
        # split): the terminal and the companion read as one surface.
        self.foreground = "#cdd6e3"
        self.background = "#0d1320"
        self.palette = list(DEFAULT_PALETTE)
        self.audible_bell = False
        self.keys = dict(DEFAULT_KEYS)
        self.commands = list(DEFAULT_COMMANDS)

    def load(self, path=CONFIG_PATH):
        # interpolation=None is LOAD-BEARING. With configparser's default
        # BasicInterpolation a '%' in any value raises InterpolationSyntaxError —
        # not on read(), but later on every .get()/.items() call. Saved commands
        # are full shell lines, so that fires on completely ordinary ones:
        #   date +%Y-%m-%d ... ps --sort=-%cpu ... git log --pretty=format:'%h'
        #   curl -w '%{http_code}' ... awk '{printf "%s\n", $1}'
        # and the exception escaped load() into App.do_startup(), so SysTerm would
        # not start AT ALL — with no terminal left to edit the config back out.
        # Values are taken literally now; nothing here wants interpolation.
        cp = configparser.ConfigParser(interpolation=None)
        cp.optionxform = str            # preserve case (command labels are shown verbatim)
        try:
            if not cp.read(path):
                return self
            return self._apply(cp)
        except (configparser.Error, ValueError, TypeError) as e:
            # A broken config must never be fatal: fall back to defaults and SAY
            # so, rather than silently dropping the user's whole profile.
            print("SysTerm: ignoring unreadable config %s (%s)" % (path, e), file=sys.stderr)
            return self

    def _apply(self, cp):
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
        if cp.has_section("commands"):
            cmds = [(label, cmd) for label, cmd in cp["commands"].items() if cmd.strip()]
            if cmds:
                self.commands = cmds
        return self


    def save_commands(self, path=CONFIG_PATH):
        """Persist the current command list to the [commands] section, leaving the
        rest of the file (profile, keys, comments) untouched."""
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    text = f.read()
            else:
                text = DEFAULT_CONFIG_TEXT
            # Drop any existing [commands] section (up to the next section / EOF).
            text = re.sub(r"(?ms)^\[commands\].*?(?=^\[|\Z)", "", text).rstrip() + "\n"
            out = ["", "[commands]",
                   "# label = command  — shown in the terminal right-click Run Command menu."]
            for label, cmd in self.commands:
                # A command is written VERBATIM (it is a shell line — '%', quotes
                # and pipes all have to survive); only the label is constrained,
                # because it becomes the INI key.
                out.append("%s = %s" % (safe_label(label, cmd.strip() or "Command"),
                                        cmd.replace("\n", " ").rstrip()))
            body = text + "\n".join(out) + "\n"
            # Create 0600 rather than whatever the umask says. The file holds the
            # user's saved commands (hostnames, flags, sometimes tokens) and
            # ensure_default_config already takes care to make it private —
            # a plain open() here would hand that back on any path where the file
            # does not already exist.
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(body)
        except OSError:
            pass

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
font = Monospace 9
scrollback_lines = 10000
cursor_shape = block
foreground = #cdd6e3
background = #0d1320
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

# Right-click "Run Command" menu. "label = command"; edit here or via the menu's
# Add / Manage Commands dialogs. Remove the whole section for the built-in list.
[commands]
apt update = sudo apt update -y
apt update && upgrade = sudo apt update -y && sudo apt upgrade -y
apt full-upgrade = sudo apt update -y && sudo apt full-upgrade -y
apt autoremove = sudo apt autoremove -y
Disk usage = df -h
Memory usage = free -h
Failed services = systemctl --failed

# keep_alive = 30s       # how long the local model stays loaded after a reply (e.g. 10m; "0" = unload now)
# anthropic_api_key =
# openai_api_key =
"""


def ensure_default_config():
    """Write a commented default config on first run so there's something to edit.
    Create the dir and
    file PRIVATE from birth (0700 / 0600) — never world-readable."""
    try:
        if not os.path.exists(CONFIG_PATH):
            os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
            # Open with 0600 from the start (umask-independent) so a key pasted in
            # later never sits in a 0644 file even briefly.
            fd = os.open(CONFIG_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(DEFAULT_CONFIG_TEXT)
        try:
            os.chmod(CONFIG_PATH, 0o600)   # tighten a pre-existing 0644 file too
        except OSError:
            pass
    except OSError:
        pass
