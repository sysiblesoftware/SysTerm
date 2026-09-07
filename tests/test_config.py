"""Config parsing and persistence — the paths that can take SysTerm off the air.

Found by pentesting the save/load round trip. Two defects, both reachable through
SysTerm's own "Add Command…" dialog with no attacker involved:

  1. configparser's DEFAULT interpolation made a '%' in any value raise
     InterpolationSyntaxError — not on read(), but on the later .get()/.items()
     calls, which sat outside load()'s try/except. App.do_startup() calls load()
     unguarded, so SysTerm would not start at all, and the user had no terminal
     left to edit the config back out. Saved commands are shell lines, so this
     fired on entirely ordinary ones: `date +%Y-%m-%d`, `ps --sort=-%cpu`,
     `git log --pretty=format:'%h'`, `curl -w '%{http_code}'`.
  2. Labels were written straight in as INI keys. A newline or a leading '['
     produced a file configparser refuses, and load() answered that by silently
     returning defaults — so one stray character cost the user their font,
     colours, keybindings and every other saved command, with no message.
"""
import os
import stat

import pytest

from systerm.config import Config, DEFAULT_CONFIG_TEXT, DEFAULT_COMMANDS, safe_label


def _roundtrip(tmp_path, label, cmd):
    """Save a command the way the Add Command dialog does, then load it back the
    way the next launch does."""
    p = str(tmp_path / "config.ini")
    c = Config()
    c.commands = [(label, cmd)]
    c.save_commands(p)
    return p, Config().load(p)


# ---- 1. a '%' in a command must not take the app down ----------------------
@pytest.mark.parametrize("cmd", [
    "date +%Y-%m-%d",
    "ps -eo pcpu,args --sort=-%cpu | head",
    "git log --pretty=format:'%h %s'",
    "curl -o /dev/null -w '%{http_code}' https://example.com",
    "df -h | awk '{printf \"%s %s\\n\", $1, $5}'",
    "printf '100%%\\n'",
])
def test_a_percent_in_a_saved_command_survives_a_restart(tmp_path, cmd):
    _, loaded = _roundtrip(tmp_path, "Cmd", cmd)
    assert loaded.commands == [("Cmd", cmd)], (
        "the command must come back BYTE FOR BYTE — it is a shell line, and "
        "anything that rewrites '%' silently changes what the user runs")


def test_a_percent_in_a_profile_value_does_not_break_startup(tmp_path):
    p = str(tmp_path / "config.ini")
    open(p, "w").write("[profile]\nfont = Mono 100%\nforeground = #ffffff\n")
    c = Config().load(p)                       # must not raise
    assert c.font == "Mono 100%"
    assert c.foreground == "#ffffff"


def test_load_never_raises_on_a_hostile_config(tmp_path):
    """Whatever is in the file, startup gets a usable Config. do_startup() has no
    handler of its own, so anything escaping here is a dead app."""
    for text in ("[profile\nbroken", "%(", "[commands]\na = %(nope)s\n",
                 "[profile]\nscrollback_lines = not-a-number\n",
                 "[profile]\npalette = #fff,#000\n", "\x00\x01binary", ""):
        p = str(tmp_path / "c.ini")
        open(p, "w").write(text)
        c = Config().load(p)
        assert c.font and c.commands, f"unusable config from {text!r}"


# ---- 2. labels must not be able to corrupt the file ------------------------
@pytest.mark.parametrize("label", [
    "Harmless\nfont = Comic Sans 40",          # newline injects other settings
    "[profile]",                               # looks like a section header
    "key = value",                             # contains the delimiter
    "with: colon",                             # the other delimiter
    "# comment",
    "; comment",
    "\r\n\t",                                  # nothing usable at all
])
def test_a_label_cannot_corrupt_the_config(tmp_path, label):
    p, loaded = _roundtrip(tmp_path, label, "echo hi")
    # The file still parses, and everything else survived.
    assert loaded.commands and loaded.commands[0][1] == "echo hi"
    assert loaded.font == Config().font, "the profile was lost to a bad label"
    assert loaded.keys == Config().keys, "the keybindings were lost to a bad label"
    # And it is genuinely one INI line, not several.
    body = open(p).read().split("[commands]", 1)[1]
    entries = [l for l in body.splitlines() if l.strip() and not l.strip().startswith("#")]
    assert len(entries) == 1, f"label produced {len(entries)} lines: {entries}"


def test_safe_label_keeps_something_readable():
    assert safe_label("apt update") == "apt update"
    assert safe_label("Top CPU") == "Top CPU"
    assert "\n" not in safe_label("two\nlines")
    assert "=" not in safe_label("a = b")
    assert safe_label("", "df -h") == "df -h"        # falls back to the command
    assert safe_label("\n\n", "df -h") == "df -h"


# ---- 3. the file must not become world-readable ----------------------------
def test_saving_commands_creates_a_private_file(tmp_path):
    p = str(tmp_path / "config.ini")
    assert not os.path.exists(p)
    c = Config()
    c.commands = [("Deploy", "ssh prod 'TOKEN=abc123 ./deploy.sh'")]
    c.save_commands(p)                       # creates it — must not use the umask
    mode = stat.S_IMODE(os.stat(p).st_mode)
    assert mode == 0o600, f"config.ini created {oct(mode)}; it holds saved commands"


def test_saving_commands_does_not_widen_an_existing_file(tmp_path):
    p = str(tmp_path / "config.ini")
    open(p, "w").write(DEFAULT_CONFIG_TEXT)
    os.chmod(p, 0o600)
    c = Config()
    c.commands = list(DEFAULT_COMMANDS)
    c.save_commands(p)
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600


# ---- 4. what ships must load ----------------------------------------------
def test_the_shipped_default_config_loads(tmp_path):
    p = str(tmp_path / "config.ini")
    open(p, "w").write(DEFAULT_CONFIG_TEXT)
    c = Config().load(p)
    assert c.commands and c.font


def test_every_shipped_default_command_survives_a_round_trip(tmp_path):
    p = str(tmp_path / "config.ini")
    c = Config()
    c.commands = list(DEFAULT_COMMANDS)
    c.save_commands(p)
    assert Config().load(p).commands == list(DEFAULT_COMMANDS)
