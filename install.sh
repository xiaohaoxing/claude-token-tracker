#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
SETTINGS="$HOME/.claude/settings.json"
TRACKER="$REPO_DIR/tracker.py"
DB_PATH="$HOME/.claude/token-tracker/token_stats.db"

echo "Claude Token Tracker — install"
echo "  Repo    : $REPO_DIR"
echo "  DB      : $DB_PATH"
echo ""

# ── Prerequisites ─────────────────────────────────────────────────────────────
echo "==> Checking Python 3..."
python3 --version

echo "==> Checking Flask..."
if ! python3 -c "import flask" 2>/dev/null; then
  echo "  Flask not found — installing..."
  pip3 install --quiet flask
fi
python3 -c "import flask; print('  Flask', flask.__version__, '✓')"

# ── Ensure DB directory exists ────────────────────────────────────────────────
mkdir -p "$(dirname "$DB_PATH")"

# ── Register Stop hook ────────────────────────────────────────────────────────
echo "==> Registering Stop hook in $SETTINGS..."

python3 - "$SETTINGS" "$TRACKER" <<'PYEOF'
import json, sys, os

settings_path = sys.argv[1]
tracker_path  = sys.argv[2]
command       = f"python3 {tracker_path}"

with open(settings_path, encoding="utf-8") as f:
    settings = json.load(f)

hooks      = settings.setdefault("hooks", {})
stop_hooks = hooks.setdefault("Stop", [])

# Find the default (matcher="") group or create it
group = next((g for g in stop_hooks if g.get("matcher") == ""), None)
if group is None:
    group = {"matcher": "", "hooks": []}
    stop_hooks.append(group)

hook_cmds = [h.get("command", "") for h in group["hooks"]]

# Remove any old token-tracker hook (different path)
group["hooks"] = [
    h for h in group["hooks"]
    if "token-tracker/tracker.py" not in h.get("command", "")
]

if command not in hook_cmds:
    group["hooks"].append({"type": "command", "command": command})
    print(f"  Registered: {command}")
else:
    print(f"  Already registered: {command}")

with open(settings_path, "w", encoding="utf-8") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")
PYEOF

# ── Backfill existing sessions ────────────────────────────────────────────────
if [ -d "$HOME/.claude/projects" ]; then
  echo "==> Backfilling from ~/.claude/projects/ ..."
  DB_PATH="$DB_PATH" python3 "$TRACKER" --backfill "$HOME/.claude/projects/"
else
  echo "  No ~/.claude/projects/ found, skipping backfill."
fi

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo "✓ Install complete!"
echo ""
echo "  Start dashboard : python3 $REPO_DIR/server.py"
echo "  Query stats CLI : python3 $REPO_DIR/stats.py today"
echo "  Backfill again  : python3 $REPO_DIR/tracker.py --backfill ~/.claude/projects/"
echo ""
