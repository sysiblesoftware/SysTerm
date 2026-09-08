#!/bin/sh
# SysTerm installer — works on any Linux with GTK3 + VTE (Debian/Ubuntu, Fedora,
# Arch, openSUSE). Installs the runtime deps, SysTerm itself (pure Python, no
# pip needed), and the desktop entry + icons.
#
#   Quick install:  curl -fsSL https://raw.githubusercontent.com/sysiblesoftware/SysTerm/dev/install.sh | sh
#   From a checkout: sudo ./install.sh
#   Per-user:        ./install.sh --user      (no root; installs under ~/.local)
#   Remove:          sudo ./install.sh --uninstall   [--user]
#   Verified:        sudo ./install.sh --ref=v0.3.0 --sha256=<digest>
#
# Installing from a local checkout downloads nothing. The download path fetches a
# tarball and installs it as root, so pin --ref to a tag and --sha256 to the digest
# published with it; without a digest the install trusts TLS to github.com alone
# and says so rather than implying a check it did not make.
#
set -eu

REPO="sysiblesoftware/SysTerm"
# What to download when there is no local checkout. A branch moves, so it can
# never have a stable digest — pass --ref=<tag> (plus the digest published with
# that tag) for an install you can actually verify.
REF="${SYSTERM_REF:-refs/heads/dev}"
WANT_SHA256="${SYSTERM_SHA256:-}"
MODE="system"
ACTION="install"

for arg in "$@"; do
    case "$arg" in
        --user) MODE="user" ;;
        --uninstall|--remove) ACTION="uninstall" ;;
        --ref=*) REF="${arg#--ref=}" ;;
        --sha256=*) WANT_SHA256="${arg#--sha256=}" ;;
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
    SUDO=""
fi

# ---- uninstall -------------------------------------------------------------
if [ "$ACTION" = uninstall ]; then
    say "Removing SysTerm…"
    rm -rf "$LIBDIR" "$BINDIR/systerm" "$APPS/systerm.desktop"
    rm -f "$ICONS"/*/apps/io.systerm.SysTerm.* 2>/dev/null || true
    # Also clear the shell hook older versions installed for the removed
    # Atlas companion, so an upgrade-then-uninstall leaves nothing behind.
    [ "$MODE" = user ] || rm -f /etc/profile.d/systerm-atlas.sh
    command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -f "$ICONS" 2>/dev/null || true
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
    say "Downloading SysTerm ($REF)…"
    url="https://github.com/$REPO/archive/$REF.tar.gz"
    tarball="$TMP/systerm.tar.gz"

    # Download to a FILE rather than piping straight into tar. `set -e` does not
    # fail on a non-final pipeline element and sh has no pipefail, so
    # `curl … | tar xz` SWALLOWED a failed or truncated download: tar saw a short
    # stream, and a partial tree could still be installed over /usr/local as root.
    # Downloading first makes the transfer's exit status the gate.
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL --proto '=https' --tlsv1.2 -o "$tarball" "$url" \
            || die "download failed: $url"
    else
        wget -q --https-only -O "$tarball" "$url" || die "download failed: $url"
    fi
    [ -s "$tarball" ] || die "downloaded an empty archive from $url"

    # Integrity. This runs as root and installs into /usr/local, so say plainly
    # what is and is not being verified instead of implying a check that never
    # happened. Pin a digest (published with each release) and it is enforced:
    #   SYSTERM_SHA256=<sha256> sudo ./install.sh        or  --sha256=<sha256>
    if [ -n "$WANT_SHA256" ]; then
        command -v sha256sum >/dev/null 2>&1 || die "sha256sum is needed to verify --sha256"
        got=$(sha256sum "$tarball" | cut -d' ' -f1)
        [ "$got" = "$WANT_SHA256" ] || die "CHECKSUM MISMATCH for $url
    expected $WANT_SHA256
    got      $got
  Refusing to install. If you did not mistype the digest, do not retry — the
  archive you were served is not the one that digest describes."
        say "Checksum verified ($got)."
    else
        warn "No --sha256/SYSTERM_SHA256 pinned: this install trusts TLS to github.com alone."
        warn "Pin the digest published with the release to make that verifiable."
    fi

    # Refuse an archive that tries to write outside the temp dir. GNU tar strips
    # a leading '/' and skips '..' members with a warning, but that behaviour is
    # not universal and a warning is not a refusal — and this is running as root.
    if tar tzf "$tarball" | grep -qE '(^/|(^|/)\.\.(/|$))'; then
        die "archive contains an absolute or parent-directory path — refusing to extract"
    fi
    # Take our own ownership and permissions, never the archive's claims.
    tar xzf "$tarball" -C "$TMP" --no-same-owner --no-same-permissions \
        || die "could not extract $tarball"
    SRC=$(find "$TMP" -maxdepth 1 -type d -name 'SysTerm-*' | head -1)
    [ -n "$SRC" ] && [ -f "$SRC/systerm/app.py" ] \
        || die "the downloaded archive does not contain a SysTerm source tree"
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

cat > "$BINDIR/systerm" <<LAUNCH
#!/bin/sh
exec python3 -c 'import sys; sys.path.insert(0, "$LIBDIR"); from systerm.app import main; sys.exit(main())' "\$@"
LAUNCH
chmod 0755 "$BINDIR/systerm"

cp "$SRC/data/systerm.desktop" "$APPS/systerm.desktop"
cp -r "$SRC/data/icons/hicolor/." "$ICONS/"
command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -f "$ICONS" 2>/dev/null || true

# ---- desktop integration ----------------------------------------------------
# Make SysTerm a terminal option on Debian-family systems (best-effort).
command -v update-alternatives >/dev/null 2>&1 && \
    update-alternatives --install /usr/bin/x-terminal-emulator x-terminal-emulator "$BINDIR/systerm" 40 2>/dev/null || true

[ -n "$CLEANUP" ] && rm -rf "$CLEANUP"

say "SysTerm installed. Launch it from your app menu or run: systerm"
