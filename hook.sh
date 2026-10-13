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
# stdin (the hook JSON payload) is passed straight through to tracker.py.
# ─────────────────────────────────────────────────────────────────────────────
set -u

MIN_MAJOR=3
MIN_MINOR=7

# Resolve this script's directory using only shell builtins, so the launcher
# still reports a useful error on a machine with a broken PATH.
case "$0" in
    */*) REPO_DIR="${0%/*}" ;;
    *)   REPO_DIR="." ;;
esac
REPO_DIR=$(CDPATH= cd -- "$REPO_DIR" && pwd)
TRACKER="$REPO_DIR/tracker.py"

if [ ! -f "$TRACKER" ]; then
    echo "claude-token-tracker: tracker.py not found next to this launcher" >&2
    echo "  looked for: $TRACKER" >&2
    exit 1
fi

# Prefer the plain `python3` name, then explicit versioned names, then `python`
# (which may be Python 2 — the version probe below rejects it).
PY=""
for cand in python3 python3.13 python3.12 python3.11 python3.10 python3.9 \
            python3.8 python3.7 python; do
    path=$(command -v "$cand" 2>/dev/null) || continue
    [ -n "$path" ] || continue
    if "$path" -c "import sys; sys.exit(0 if sys.version_info[:2] >= ($MIN_MAJOR, $MIN_MINOR) else 1)" \
            >/dev/null 2>&1; then
        PY="$path"
        break
    fi
done

if [ -z "$PY" ]; then
    echo "claude-token-tracker: no Python ${MIN_MAJOR}.${MIN_MINOR}+ found on PATH" >&2
    echo "  tried: python3, python3.7 … python3.13, python" >&2
    echo "  Install Python ${MIN_MAJOR}.${MIN_MINOR}+, make sure 'python3' resolves to it," >&2
    echo "  then re-run ./install.sh." >&2
    echo "  Usage is NOT being tracked until this is fixed." >&2
    exit 1
fi

exec "$PY" "$TRACKER"
