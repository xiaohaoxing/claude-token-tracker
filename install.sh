#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
SETTINGS="$HOME/.claude/settings.json"
TRACKER="$REPO_DIR/tracker.py"
LAUNCHER="$REPO_DIR/hook.sh"
DB_PATH="$HOME/.claude/token-tracker/token_stats.db"

echo "Claude Token Tracker — install"
echo "  Repo    : $REPO_DIR"
echo "  DB      : $DB_PATH"
echo ""

# ── Locate a usable Python 3 ──────────────────────────────────────────────────
# The hook resolves its own interpreter at run time (see hook.sh), so the
# installed command stays portable. We still need one here for Flask and the
# initial backfill, and we refuse to register a hook that cannot work at all.
echo "==> Looking for Python 3.7+..."
PY=""
for cand in python3 python3.13 python3.12 python3.11 python3.10 python3.9 \
            python3.8 python3.7 python; do
  path="$(command -v "$cand" 2>/dev/null || true)"
  [ -n "$path" ] || continue
  if "$path" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 7) else 1)' 2>/dev/null; then
    PY="$path"
    break
  fi
done

if [ -z "$PY" ]; then
  echo "  ✗ No Python 3.7+ found on PATH (tried python3, python3.7-3.13, python)." >&2
  echo "    Install Python 3 and re-run ./install.sh — nothing was registered." >&2
  exit 1
fi
echo "  Using: $PY ($("$PY" -c 'import platform; print(platform.python_version())'))"

# Tell the user when their default `python3` is not what the hook will run.
if command -v python3 >/dev/null 2>&1 \
   && ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 7) else 1)' 2>/dev/null; then
  echo "  ! 'python3' on PATH is too old ($(python3 -V 2>&1)) — the hook will use $PY instead."
fi

echo "==> Checking Flask..."
if ! "$PY" -c "import flask" 2>/dev/null; then
  echo "  Flask not found — installing..."
  "$PY" -m pip install --quiet flask
fi
"$PY" -c "import flask; print('  Flask', flask.__version__, '✓')"

# ── Ensure DB directory exists ────────────────────────────────────────────────
mkdir -p "$(dirname "$DB_PATH")"

# ── Register Stop hook ────────────────────────────────────────────────────────
echo "==> Registering Stop hook in $SETTINGS..."

"$PY" - "$SETTINGS" "$LAUNCHER" <<'PYEOF'
import json, os, re, sys

settings_path = sys.argv[1]
launcher      = sys.argv[2]
command       = f"sh {launcher}"
repo_dir      = os.path.dirname(launcher)


def is_our_hook(cmd: str) -> bool:
    """True for a hook registered by this tool.

    Must keep working when the checkout lives in a differently named directory
    (custom `git clone` target, renamed folder, …), otherwise an upgrade leaves
    the previous hook behind and both of them fire on every turn.
    """
    if not cmd:
        return False
    if repo_dir and repo_dir in cmd:
        return True
    if "token-tracker" in cmd:          # default clone / zip directory names
        return True
    # Fall back to our entry-point filenames: …/hook.sh, …/tracker.py
    return bool(re.search(r"(?:^|[\s\"'=])(?:[^\s\"']*/)?(?:hook\.sh|tracker\.py)(?:\s|$)", cmd))


# Tolerate a machine where Claude Code has never been started: create the
# directory and start from an empty settings object instead of crashing with a
# traceback half way through the install.
os.makedirs(os.path.dirname(settings_path), exist_ok=True)
settings = {}
if os.path.exists(settings_path):
    with open(settings_path, encoding="utf-8") as f:
        settings = json.load(f)

hooks      = settings.setdefault("hooks", {})
stop_hooks = hooks.setdefault("Stop", [])

# Find the default (matcher="") group or create it
group = next((g for g in stop_hooks if g.get("matcher") == ""), None)
if group is None:
    group = {"matcher": "", "hooks": []}
    stop_hooks.append(group)

# Drop any previous token-tracker hook, whatever form it took (older installs
# registered `python3 …/tracker.py`; this launcher is form-independent). Print
# what was removed so an over-eager match is never silent.
kept, removed = [], []
for h in group["hooks"]:
    (removed if is_our_hook(h.get("command", "")) else kept).append(h)
group["hooks"] = kept
for h in removed:
    # Re-registering the same command is normal on a re-run — no need to shout.
    if h.get("command", "") != command:
        print(f"  Removed previous hook: {h.get('command', '')}")

group["hooks"].append({"type": "command", "command": command})
print(f"  Registered: {command}")

with open(settings_path, "w", encoding="utf-8") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")
PYEOF

chmod +x "$LAUNCHER" 2>/dev/null || true

# ── Backfill existing sessions ────────────────────────────────────────────────
if [ -d "$HOME/.claude/projects" ]; then
  echo "==> Backfilling from ~/.claude/projects/ ..."
  CLAUDE_TRACKER_DB="$DB_PATH" "$PY" "$TRACKER" --backfill "$HOME/.claude/projects/"
else
  echo "  No ~/.claude/projects/ found, skipping backfill."
fi

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo "✓ Install complete!"
echo ""
echo "  Start dashboard : $PY $REPO_DIR/server.py"
echo "  Query stats CLI : $PY $REPO_DIR/stats.py today"
echo "  Backfill again  : $PY $REPO_DIR/tracker.py --backfill ~/.claude/projects/"
echo "  Catch up now    : $PY $REPO_DIR/tracker.py --catch-up"
echo ""
echo "  Missed sessions are re-scanned automatically: after every tracked turn"
echo "  and every 5 minutes while the dashboard is running."
echo ""
