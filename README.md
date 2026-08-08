<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset=".github/sysible-logo-dark.png">
    <img alt="Sysible" src=".github/sysible-logo-light.png" width="340">
  </picture>
</p>

# SysTerm

A native **GTK + VTE** tiling terminal for Debian-based systems, in the spirit of
[Terminator](https://gnome-terminator.org/): split the window into as many shell
panes as you like, arrange them in tabs, and **broadcast** your typing to every
pane at once. Built on the same battle-tested VTE widget GNOME Terminal and
Terminator use, so the terminal core (PTY, escape sequences, rendering, URLs,
scrollback) is solid — SysTerm adds the tiling, tabs, broadcast, and config.

> Status: **v0.1 (MVP)** — usable daily. Splits, tabs, broadcast, per-pane font
> zoom, pane zoom, configurable keys and profile all work. See the roadmap.

## Install (Debian / Ubuntu)

SysTerm's GTK/VTE bindings are **system packages**, not pip wheels:

```bash
sudo apt install python3 python3-gi gir1.2-gtk-3.0 gir1.2-vte-2.91
```

Then either run it in place:

```bash
git clone <your-fork-url> systerm && cd systerm
python3 -m systerm
```

or install the `systerm` command with **pipx** (it manages the virtualenv for
you; `--system-site-packages` lets that venv import the system GTK/VTE bindings):

```bash
sudo apt install pipx
pipx install --system-site-packages .
# add it to your application menu (desktop entry + icons):
install -Dm644 data/systerm.desktop ~/.local/share/applications/systerm.desktop
cp -r data/icons/hicolor ~/.local/share/icons/
# refresh the caches so the launcher shows it immediately (no re-login):
update-desktop-database ~/.local/share/applications 2>/dev/null || true
gtk-update-icon-cache -f -t ~/.local/share/icons/hicolor 2>/dev/null || true
```

> **Icon not showing?** Two common causes on servers/minimal desktops:
> 1. **Menu cache** — re-run the two cache commands above, then log out and
>    back in (or `killall -HUP gnome-shell` on X11). New entries rarely appear
>    live.
> 2. **No SVG loader** — GTK renders themed icons via gdk-pixbuf; without
>    `librsvg2-common` an SVG icon silently fails. SysTerm ships **PNG** icons
>    (16–256px) as well as the SVG for exactly this reason, so the raster icon
>    resolves even with no SVG loader. Check with
>    `gdk-pixbuf-query-loaders | grep -qi svg` if you want the scalable one too.

Once the desktop entry is installed, **SysTerm is a normal standalone app** —
launch it from your Activities / application menu, pin it to the dock, or bind
`systerm` to a keyboard shortcut in your desktop settings. You never need to
start it from another terminal; running `python3 -m systerm` from a shell is
just the from-source convenience.

> **Not `pip install --user .`** — Debian 12+/Python 3.11+ mark the system
> interpreter *externally managed* (PEP 668), so it refuses with
> `error: externally-managed-environment`. Use pipx (above) or a
> `python3 -m venv --system-site-packages <dir>` you install into. As a last
> resort you can force the old behaviour with
> `pip install --user --break-system-packages .`. Running in place with
> `python3 -m systerm` needs no install at all — there are no pip dependencies.

### Build a Debian package (recommended for a system install)

A `debian/` directory ships in the repo, so you can build a proper `.deb`. This
is the cleanest install: it pulls the GTK/VTE dependencies automatically and
registers the desktop entry and icon for you (cache refresh included).

```bash
sudo apt install build-essential debhelper dh-python pybuild-plugin-pyproject
dpkg-buildpackage -us -uc -b        # produces ../systerm_0.1.0_all.deb
sudo apt install ../systerm_0.1.0_all.deb
```

Now `systerm` is on `PATH`, appears in your application menu with its icon, and
uninstalls cleanly with `sudo apt remove systerm`.

## Usage

Launch `systerm` (or `python3 -m systerm`). You start with one shell pane. Split
it, open tabs, and work.

**Right-click any pane** for the context menu — Split Horizontally / Vertically,
Open Tab / Window, Copy / Paste, Zoom, Broadcast, and Close — the same actions as
the keyboard shortcuts below, each shown with its shortcut.

### Default keybindings

| Action | Shortcut |
| --- | --- |
| Split horizontal (top/bottom) | `Ctrl+Shift+O` |
| Split vertical (side by side) | `Ctrl+Shift+E` |
| New tab | `Ctrl+Shift+T` |
| New window | `Ctrl+Shift+N` |
| Close pane | `Ctrl+Shift+W` |
| Close tab | `Ctrl+Shift+Q` |
| Copy / Paste | `Ctrl+Shift+C` / `Ctrl+Shift+V` |
| Next / previous tab | `Ctrl+PageDown` / `Ctrl+PageUp` |
| Cycle pane focus | `Ctrl+Shift+→` / `Ctrl+Shift+←` |
| Zoom pane to fill tab (toggle) | `Ctrl+Shift+X` |
| **Broadcast input to all panes (toggle)** | `Ctrl+Shift+B` |
| Font bigger / smaller / reset | `Ctrl++` / `Ctrl+-` / `Ctrl+0` |

**Broadcast**: press `Ctrl+Shift+B` and the window gets an amber frame — now every
keystroke goes to *all* panes in the window (run the same command across a row of
`ssh` sessions). Press again to disarm. The amber border is the safety cue.

## Configuration

On first run SysTerm writes a commented default to
`~/.config/systerm/config.ini`. Edit the profile (font, colours, scrollback,
cursor) and rebind any key under `[keys]` using GTK accelerator syntax
(`<Primary>` = Ctrl). Restart to apply.

```ini
[profile]
font = Monospace 12
background = #141414
foreground = #d0d0d0
scrollback_lines = 20000

[keys]
toggle-broadcast = <Primary><Shift>a
```

## Roadmap

- Directional focus (`Alt+Arrows`) and drag-to-resize handles (VTE gives us the
  panes; geometry-aware navigation is next).
- Saved layouts / named profiles.
- Preferences UI (config is file-only for now).
- Session groups: broadcast to a *subset* of panes, not just all.
- Debian packaging (`debian/` rules → a proper `.deb` for the distro default).
- Optional C+VTE port for a leaner single binary if we want it in the base image.

## License

MIT — see [LICENSE](LICENSE).
