#!/bin/sh
# SysTerm installer — works on any Linux with GTK3 + VTE (Debian/Ubuntu, Fedora,
# Arch, openSUSE). Installs the runtime deps, SysTerm itself (pure Python, no
# pip needed), the desktop entry + icons, and the Sysible Atlas shell hooks.
#
#   Quick install:  curl -fsSL https://raw.githubusercontent.com/sysiblesoftware/SysTerm/dev/install.sh | sh
#   From a checkout: sudo ./install.sh
#   Per-user:        ./install.sh --user      (no root; installs under ~/.local)
#   Remove:          sudo ./install.sh --uninstall   [--user]
#
set -eu

REPO="sysiblesoftware/SysTerm"
BRANCH="dev"
MODE="system"
ACTION="install"

for arg in "$@"; do
    case "$arg" in
        --user) MODE="user" ;;
        --uninstall|--remove) ACTION="uninstall" ;;
        -h|--help)
            sed -n '2,12p' "$0"; exit 0 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

say()  { printf '\033[1;32m▸\033[0m %s\n' "$1"; }
warn() { printf '\033[1;33m!\033[0m %s\n' "$1" >&2; }
die()  { printf '\033[1;31m✗\033[0m %s\n' "$1" >&2; exit 1; }

# ---- paths (system vs per-user) -------------------------------------------
if [ "$MODE" = user ]; then
    PREFIX="$HOME/.local"
    LIBDIR="$PREFIX/lib/systerm"
    BINDIR="$PREFIX/bin"
    APPS="$PREFIX/share/applications"
    ICONS="$PREFIX/share/icons/hicolor"
    SUDO=""
else
    [ "$(id -u)" = 0 ] || { command -v sudo >/dev/null 2>&1 && exec sudo sh "$0" "$@"; die "run as root or with --user"; }
    PREFIX="/usr/local"
    LIBDIR="/usr/local/lib/systerm"
    BINDIR="/usr/local/bin"
    APPS="/usr/share/applications"
    ICONS="/usr/share/icons/hicolor"
    PROFILED="/etc/profile.d/systerm-atlas.sh"
    SUDO=""
fi

# ---- uninstall -------------------------------------------------------------
if [ "$ACTION" = uninstall ]; then
    say "Removing SysTerm…"
    rm -rf "$LIBDIR" "$BINDIR/systerm" "$APPS/systerm.desktop"
    rm -f "$ICONS"/*/apps/io.systerm.SysTerm.* 2>/dev/null || true
    [ "$MODE" = user ] || rm -f "$PROFILED"
    command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -f "$ICONS" 2>/dev/null || true
    say "Done. (Atlas hooks in ~/.bashrc, if added, can be removed by hand.)"
    exit 0
fi

# ---- locate the source (checkout or download) ------------------------------
SELF=$(cd "$(dirname "$0")" 2>/dev/null && pwd || echo "")
CLEANUP=""
if [ -n "$SELF" ] && [ -f "$SELF/systerm/app.py" ]; then
    SRC="$SELF"
else
    command -v curl >/dev/null 2>&1 || command -v wget >/dev/null 2>&1 || die "need curl or wget to download"
    TMP=$(mktemp -d); CLEANUP="$TMP"
    say "Downloading SysTerm ($BRANCH)…"
    url="https://github.com/$REPO/archive/refs/heads/$BRANCH.tar.gz"
    if command -v curl >/dev/null 2>&1; then curl -fsSL "$url" | tar xz -C "$TMP"
    else wget -qO- "$url" | tar xz -C "$TMP"; fi
    SRC=$(find "$TMP" -maxdepth 1 -type d -name 'SysTerm-*' | head -1)
    [ -n "$SRC" ] || die "download failed"
fi

# ---- runtime dependencies --------------------------------------------------
install_deps() {
    if command -v apt-get >/dev/null 2>&1; then
        say "Installing deps with apt…"
        $SUDO apt-get update -qq || true
        $SUDO apt-get install -y --no-install-recommends \
            python3-gi gir1.2-gtk-3.0 gir1.2-vte-2.91 gir1.2-pango-1.0
    elif command -v dnf >/dev/null 2>&1; then
        say "Installing deps with dnf…"
        $SUDO dnf install -y python3-gobject gtk3 vte291
    elif command -v pacman >/dev/null 2>&1; then
        say "Installing deps with pacman…"
        $SUDO pacman -S --needed --noconfirm python-gobject gtk3 vte3
    elif command -v zypper >/dev/null 2>&1; then
        say "Installing deps with zypper…"
        $SUDO zypper --non-interactive install python3-gobject gtk3 typelib-1_0-Vte-2_91
    else
        warn "Unknown package manager — install these yourself: GTK3, VTE 2.91 typelib, python3 GObject bindings."
    fi
}
[ "$MODE" = user ] && warn "--user: skipping system dep install; ensure GTK3+VTE2.91+python3-gi are present." || install_deps

# ---- verify the bindings actually import -----------------------------------
if ! python3 - <<'PY' 2>/dev/null
import gi
gi.require_version("Gtk", "3.0"); gi.require_version("Vte", "2.91")
from gi.repository import Gtk, Vte
PY
then
    warn "GTK/VTE Python bindings didn't import. Install the deps above, then re-run."
fi

# ---- install files ---------------------------------------------------------
say "Installing SysTerm to $LIBDIR…"
mkdir -p "$LIBDIR" "$BINDIR" "$APPS" "$ICONS"
rm -rf "$LIBDIR/systerm"
cp -r "$SRC/systerm" "$LIBDIR/systerm"
cp "$SRC/data/systerm-atlas.sh" "$LIBDIR/systerm-atlas.sh"

cat > "$BINDIR/systerm" <<LAUNCH
#!/bin/sh
exec python3 -c 'import sys; sys.path.insert(0, "$LIBDIR"); from systerm.app import main; sys.exit(main())' "\$@"
LAUNCH
chmod 0755 "$BINDIR/systerm"

cp "$SRC/data/systerm.desktop" "$APPS/systerm.desktop"
cp -r "$SRC/data/icons/hicolor/." "$ICONS/"
command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -f "$ICONS" 2>/dev/null || true

# ---- Sysible Atlas shell hooks --------------------------------------------
if [ "$MODE" = user ]; then
    RC="$HOME/.bashrc"; LINE=". \"$LIBDIR/systerm-atlas.sh\""
    if ! { [ -f "$RC" ] && grep -qF "systerm-atlas.sh" "$RC"; }; then
        printf '\n# SysTerm Atlas companion\n%s\n' "$LINE" >> "$RC"   # creates ~/.bashrc if absent
        say "Added Atlas hooks to ~/.bashrc"
    fi
else
    cp "$SRC/data/systerm-atlas.sh" "$PROFILED"
    # /etc/profile.d is sourced by LOGIN shells only, but SysTerm spawns an
    # interactive NON-login shell — which reads the system bashrc. Source our
    # hook from there too, or the auto-catch + `ai` command won't load. (Fedora
    # uses /etc/bashrc; Debian/Ubuntu/Arch/SUSE use /etc/bash.bashrc.)
    SYSBRC=""
    for c in /etc/bash.bashrc /etc/bashrc; do [ -f "$c" ] && SYSBRC="$c" && break; done
    if [ -n "$SYSBRC" ] && ! grep -qF 'systerm-atlas.sh' "$SYSBRC"; then
        printf '\n# SysTerm Atlas companion (interactive shells)\n[ -n "$PS1" ] && [ -f %s ] && . %s\n' \
            "$PROFILED" "$PROFILED" >> "$SYSBRC"
        say "Installed Atlas hooks ($PROFILED, sourced from $SYSBRC)"
    else
        say "Installed Atlas hooks to $PROFILED"
        [ -z "$SYSBRC" ] && warn "No system bashrc found — add '. $PROFILED' to it so \`ai\`/auto-catch load in terminals."
    fi
    # Make SysTerm a terminal option on Debian-family systems (best-effort).
    command -v update-alternatives >/dev/null 2>&1 && \
        update-alternatives --install /usr/bin/x-terminal-emulator x-terminal-emulator "$BINDIR/systerm" 40 2>/dev/null || true
fi

[ -n "$CLEANUP" ] && rm -rf "$CLEANUP"

say "SysTerm installed. Launch it from your app menu or run: systerm"
cat <<'NEXT'

  Sysible Atlas (the AI companion pane) opens on first launch and walks you
  through installing Ollama and downloading a model — or open it anytime:
  right-click → "Open Sysible Atlas", or press Alt+A. Ask in the pane's box;
  failed commands appear there automatically.

  (The auto-catch of failed commands is bash-only — open a NEW terminal, or run
  `exec bash`, so the shell hook loads. The pane itself works in any shell.)
NEXT
