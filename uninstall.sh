#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
SETTINGS="$HOME/.claude/settings.json"
TRACKER="$REPO_DIR/tracker.py"

echo "Claude Token Tracker — uninstall"
echo ""

echo "==> Removing Stop hook from $SETTINGS..."

python3 - "$SETTINGS" "$TRACKER" <<'PYEOF'
import json, sys

settings_path = sys.argv[1]
tracker_path  = sys.argv[2]

with open(settings_path, encoding="utf-8") as f:
    settings = json.load(f)

stop_hooks = settings.get("hooks", {}).get("Stop", [])
for group in stop_hooks:
    before = len(group.get("hooks", []))
    group["hooks"] = [
        h for h in group.get("hooks", [])
        if tracker_path not in h.get("command", "")
        and "token-tracker/tracker.py" not in h.get("command", "")
    ]
    removed = before - len(group["hooks"])
    if removed:
        print(f"  Removed {removed} hook(s)")
    else:
        print("  No hook found to remove")

with open(settings_path, "w", encoding="utf-8") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")
PYEOF

echo ""
echo "✓ Hook removed."
echo ""
echo "  The database at ~/.claude/token-tracker/token_stats.db is preserved."
echo "  Delete it manually if you want to remove all data."
echo ""
