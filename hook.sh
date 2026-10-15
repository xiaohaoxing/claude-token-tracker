#!/bin/sh
# ─────────────────────────────────────────────────────────────────────────────
# claude-token-tracker — portable Stop-hook launcher
#
# Claude Code runs the hook command through the user's shell, so the hook must
# not hardcode an absolute interpreter path: the tracker gets installed on
# machines with completely different Python setups. This wrapper locates a
# usable Python 3 on PATH at run time and fails loudly (non-zero exit) when
# there is none — Claude Code surfaces a non-zero hook exit code to the user,
# so a broken setup is visible instead of silently dropping usage data.
#
# stdin (the hook JSON payload) is passed straight through to tracker.py, and
# any arguments are forwarded, so support can ask for:
#
#     sh hook.sh --doctor
#     sh hook.sh --doctor --out ~/Desktop/token-tracker-report.txt
#
# --doctor still answers when no usable Python exists, because that is exactly
# the situation where its output matters most.
# ─────────────────────────────────────────────────────────────────────────────
set -u

MIN_MAJOR=3
MIN_MINOR=7
CANDIDATES="python3 python3.13 python3.12 python3.11 python3.10 python3.9 python3.8 python3.7 python"

# Resolve this script's directory using only shell builtins, so the launcher
# still reports a useful error on a machine with a broken PATH.
case "$0" in
    */*) REPO_DIR="${0%/*}" ;;
    *)   REPO_DIR="." ;;
esac
REPO_DIR=$(CDPATH= cd -- "$REPO_DIR" && pwd)
TRACKER="$REPO_DIR/tracker.py"

WANT_DOCTOR=no
for arg in "$@"; do
    [ "$arg" = "--doctor" ] && WANT_DOCTOR=yes
done

list_interpreters() {
    found=0
    for cand in $CANDIDATES; do
        path=$(command -v "$cand" 2>/dev/null) || continue
        [ -n "$path" ] || continue
        found=1
        echo "    $cand -> $path  [$("$path" -V 2>&1)]"
    done
    [ "$found" = "1" ] || echo "    (none on PATH)"
}

short_path() {
    if [ -n "${HOME:-}" ]; then
        case "$1" in
            "$HOME"/*) printf '~%s' "${1#"$HOME"}" ; return ;;
            "$HOME")   printf '~' ; return ;;
        esac
    fi
    printf '%s' "$1"
}

shell_diagnostics() {
    uname_out=$(uname -a 2>/dev/null) || uname_out=""
    date_out=$(date '+%Y-%m-%dT%H:%M:%S%z' 2>/dev/null) || date_out=""
    echo "Claude Token Tracker — diagnostics (shell level)"
    echo "generated  : ${date_out:-（unavailable）}"
    echo "reason     : no Python ${MIN_MAJOR}.${MIN_MINOR}+ is usable, so tracker.py cannot run"
    echo
    echo "── Launcher ─────────────────────────────────────────────────"
    echo "  invoked as  : $(short_path "$0")"
    echo "  repo dir    : $(short_path "$REPO_DIR")"
    echo "  tracker.py  : $([ -f "$TRACKER" ] && echo 'present' || echo 'MISSING')"
    echo "  hook.sh     : $([ -x "$0" ] && echo 'executable' || echo 'NOT executable')"
    echo
    echo "── Environment ──────────────────────────────────────────────"
    echo "  uname       : ${uname_out:-（unavailable）}"
    echo "  PATH        : ${PATH:-（empty）}"
    echo "  HOME        : $(short_path "${HOME:-（empty）}")"
    echo
    echo "── Interpreters found on PATH ───────────────────────────────"
    list_interpreters
    echo
    echo "── Next step ────────────────────────────────────────────────"
    echo "  Install Python ${MIN_MAJOR}.${MIN_MINOR}+, make sure 'python3' resolves to it,"
    echo "  then re-run ./install.sh"
    echo
    echo "  This report is safe to share (no prompts, messages or keys)."
}

if [ ! -f "$TRACKER" ]; then
    echo "claude-token-tracker: tracker.py not found next to this launcher" >&2
    echo "  looked for: $TRACKER" >&2
    exit 1
fi

# Prefer the plain `python3` name, then explicit versioned names, then `python`
# (which may be Python 2 — the version probe below rejects it).
PY=""
for cand in $CANDIDATES; do
    path=$(command -v "$cand" 2>/dev/null) || continue
    [ -n "$path" ] || continue
    if "$path" -c "import sys; sys.exit(0 if sys.version_info[:2] >= ($MIN_MAJOR, $MIN_MINOR) else 1)" \
            >/dev/null 2>&1; then
        PY="$path"
        break
    fi
done

if [ -z "$PY" ]; then
    if [ "$WANT_DOCTOR" = "yes" ]; then
        shell_diagnostics
        exit 1
    fi
    echo "claude-token-tracker: no Python ${MIN_MAJOR}.${MIN_MINOR}+ found on PATH" >&2
    echo "  tried: python3, python3.7 … python3.13, python" >&2
    echo "  Run 'sh $0 --doctor' for a shareable report of this machine." >&2
    echo "  Usage is NOT being tracked until this is fixed." >&2
    exit 1
fi

exec "$PY" "$TRACKER" "$@"
