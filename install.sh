#!/bin/sh
# tuiray installer -- clone, link, update, remove.
#
#   curl -fsSL https://raw.githubusercontent.com/XuliGan4eg2006/tuiray/master/install.sh | sh
#
# Or, if piping a script into a shell makes you uneasy (it should):
#
#   curl -fsSL https://raw.githubusercontent.com/XuliGan4eg2006/tuiray/master/install.sh -o install.sh
#   less install.sh && sh install.sh
#
# What it does: clones the repo with its submodule into ~/.local/share/tuiray
# and drops a `tuiray` launcher in ~/.local/bin. Nothing is written outside the
# home directory, nothing needs root, and no Python packages are installed --
# the TUI is stdlib-only on purpose.
#
# Env:  TUIRAY_REPO  TUIRAY_REF  TUIRAY_DIR  TUIRAY_BIN  TUIRAY_FORCE

set -eu

REPO=${TUIRAY_REPO:-https://github.com/XuliGan4eg2006/tuiray}
REF=${TUIRAY_REF:-master}
ROOT=${XDG_DATA_HOME:-$HOME/.local/share}/tuiray
# The checkout sits one level down, next to the exported configs in
# $ROOT/configs. The xray binary and geo data it downloads land inside the
# checkout itself (ignored by git, so updates leave them alone).
DIR=${TUIRAY_DIR:-$ROOT/app}
BIN=${TUIRAY_BIN:-$HOME/.local/bin}
LAUNCHER=$BIN/tuiray

if [ -t 1 ]; then
    B=$(printf '\033[1m'); DIM=$(printf '\033[2m'); R=$(printf '\033[0m')
    OK=$(printf '\033[32m'); WARN=$(printf '\033[33m'); ERR=$(printf '\033[31m')
else
    B=''; DIM=''; R=''; OK=''; WARN=''; ERR=''
fi

say()  { printf '%s\n' "$*"; }
step() { printf '%s==>%s %s\n' "$B" "$R" "$*"; }
note() { printf '    %s%s%s\n' "$DIM" "$*" "$R"; }
warn() { printf '%s !! %s%s\n' "$WARN" "$*" "$R"; }
die()  { printf '%s !! %s%s\n' "$ERR" "$*" "$R" >&2; exit 1; }

usage() {
    cat <<EOF
tuiray installer

  sh install.sh              install, or update an existing install
  sh install.sh --update     update in place
  sh install.sh --uninstall  remove the program (settings and cache stay)
  sh install.sh --purge      remove it along with settings, cache and profiles

Environment:
  TUIRAY_REPO   git URL          (default: $REPO)
  TUIRAY_REF    branch or tag    (default: $REF)
  TUIRAY_DIR    install dir      (default: $DIR)
  TUIRAY_BIN    launcher dir     (default: $BIN)
  TUIRAY_FORCE  1 to overwrite local changes when updating
EOF
}

have() { command -v "$1" >/dev/null 2>&1; }

check_tools() {
    have git || die "git is required. Install it and run this again."
    have python3 || die "python3 is required. Install it and run this again."

    python3 - <<'PY' || die "this python3 cannot import curses -- install your distro's full python3 (Debian/Ubuntu: python3, Alpine: python3, Fedora: python3-libs)."
import curses, sys
sys.exit(0)
PY

    python3 - <<'PY' || die "tuiray needs Python 3.8 or newer."
import sys
sys.exit(0 if sys.version_info >= (3, 8) else 1)
PY

    if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
        warn "Python $(python3 -c 'import platform; print(platform.python_version())') runs the TUI fine,"
        warn "but the subscription submodule needs 3.10+; that screen will fall back to reading profiles.json directly."
    fi
}

write_launcher() {
    mkdir -p "$BIN"
    cat > "$LAUNCHER" <<EOF
#!/bin/sh
# Written by tuiray's install.sh -- remove with: sh install.sh --uninstall
exec python3 "$DIR/main.py" "\$@"
EOF
    chmod +x "$LAUNCHER"
}

path_hint() {
    case ":${PATH}:" in
        *":$BIN:"*) return 0 ;;
    esac
    warn "$BIN is not on your PATH."
    case "${SHELL##*/}" in
        zsh)  rc=~/.zshrc ;;
        bash) rc=~/.bashrc ;;
        fish) note "fish:  fish_add_path $BIN"; return 0 ;;
        *)    rc=~/.profile ;;
    esac
    note "add it with:  echo 'export PATH=\"$BIN:\$PATH\"' >> $rc  &&  . $rc"
    note "or run it directly:  $LAUNCHER"
}

do_install() {
    check_tools
    if [ -d "$DIR/.git" ]; then
        say "tuiray is already installed in $DIR"
        do_update
        return
    fi
    [ -e "$DIR" ] && die "$DIR exists but is not a git checkout. Move it aside, or set TUIRAY_DIR."

    step "cloning $REPO ($REF) into $DIR"
    mkdir -p "$(dirname "$DIR")"
    # --recurse-submodules is the whole point: subscriptions come from the
    # Happ-converter submodule, and a plain clone leaves that screen empty.
    git clone --depth 1 --branch "$REF" \
        --recurse-submodules --shallow-submodules "$REPO" "$DIR" 2>&1 | sed 's/^/    /'

    step "linking $LAUNCHER"
    write_launcher

    say ""
    printf '%s%stuiray installed.%s\n' "$OK" "$B" "$R"
    note "$(git -C "$DIR" log -1 --format='%h %s' 2>/dev/null)"
    say ""
    path_hint
    say ""
    say "Run it with:  ${B}tuiray${R}"
    note "no xray binary yet? the menu's 'Get xray' row downloads one for this machine"
    note "update later with:  tuiray-update   (or re-run this script)"
    write_updater
}

write_updater() {
    cat > "$BIN/tuiray-update" <<EOF
#!/bin/sh
# Written by tuiray's install.sh. Self-contained on purpose: the checkout it
# updates is also where install.sh lives, and that file may be renamed or gone.
set -eu
DIR="$DIR"
REF="\${TUIRAY_REF:-$REF}"
if [ -n "\$(git -C "\$DIR" status --porcelain -uno)" ] && [ "\${TUIRAY_FORCE:-0}" != "1" ]; then
    echo "\$DIR has local changes; re-run with TUIRAY_FORCE=1 to discard." >&2
    exit 1
fi
git -C "\$DIR" fetch --depth 1 origin "\$REF"
git -C "\$DIR" reset --hard FETCH_HEAD
git -C "\$DIR" submodule update --init --recursive --depth 1
git -C "\$DIR" log -1 --format='now at %h %s'
EOF
    chmod +x "$BIN/tuiray-update"
}

do_update() {
    check_tools
    [ -d "$DIR/.git" ] || die "nothing installed in $DIR -- run this script without --update first."

    # -uno: __pycache__ from a previous run is not a reason to refuse.
    if [ -n "$(git -C "$DIR" status --porcelain -uno 2>/dev/null)" ] && [ "${TUIRAY_FORCE:-0}" != "1" ]; then
        die "$DIR has local changes. Commit them, or re-run with TUIRAY_FORCE=1 to discard."
    fi

    step "updating $DIR"
    git -C "$DIR" fetch --depth 1 origin "$REF" 2>&1 | sed 's/^/    /'
    git -C "$DIR" reset --hard FETCH_HEAD 2>&1 | sed 's/^/    /'
    git -C "$DIR" submodule update --init --recursive --depth 1 2>&1 | sed 's/^/    /'

    step "refreshing $LAUNCHER"
    write_launcher
    write_updater
    printf '%s%snow at %s%s\n' "$OK" "$B" "$(git -C "$DIR" log -1 --format='%h %s')" "$R"
}

do_uninstall() {
    purge=${1:-no}
    [ -d "$DIR" ] || [ -f "$LAUNCHER" ] || die "nothing to remove."

    step "removing $DIR"
    rm -rf "$DIR"
    step "removing $LAUNCHER"
    rm -f "$LAUNCHER" "$BIN/tuiray-update"

    config=${XDG_CONFIG_HOME:-$HOME/.config}/tuiray
    cache=${XDG_CACHE_HOME:-$HOME/.cache}/tuiray
    if [ "$purge" = "purge" ]; then
        step "removing $ROOT, $config and $cache"
        rm -rf "$ROOT" "$config" "$cache"
    else
        [ -d "$ROOT" ] && note "exported configs kept in $ROOT"
        note "settings kept in $config"
        note "cache kept in    $cache"
        note "remove all of it with:  sh install.sh --purge"
    fi
    say ""
    warn "a running xray is not stopped by this. Check with:  pgrep -af 'xray run -c'"
    printf '%s%stuiray removed.%s\n' "$OK" "$B" "$R"
}

case "${1:-install}" in
    install)     do_install ;;
    -u|--update) do_update ;;
    --uninstall) do_uninstall ;;
    --purge)     do_uninstall purge ;;
    -h|--help)   usage ;;
    *)           usage; die "unknown option: $1" ;;
esac
