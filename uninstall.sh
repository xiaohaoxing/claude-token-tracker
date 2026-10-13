#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
SETTINGS="$HOME/.claude/settings.json"
TRACKER="$REPO_DIR/tracker.py"

echo "Claude Token Tracker — uninstall"
echo ""

echo "==> Removing Stop hook from $SETTINGS..."

python3 - "$SETTINGS" "$TRACKER" <<'PYEOF'
import json, os, re, sys

settings_path = sys.argv[1]
tracker_path  = sys.argv[2]
repo_dir      = os.path.dirname(tracker_path)


def is_our_hook(cmd: str) -> bool:
    """Mirror of install.sh's check, so a renamed checkout can still be removed."""
    if not cmd:
        return False
    if repo_dir and repo_dir in cmd:
        return True
    if "token-tracker" in cmd:
        return True
    return bool(re.search(r"(?:^|[\s\"'=])(?:[^\s\"']*/)?(?:hook\.sh|tracker\.py)(?:\s|$)", cmd))


if not os.path.exists(settings_path):
    print(f"  No settings file at {settings_path} — nothing to remove")
    raise SystemExit(0)

with open(settings_path, encoding="utf-8") as f:
    settings = json.load(f)

stop_hooks = settings.get("hooks", {}).get("Stop", [])
for group in stop_hooks:
    kept, dropped = [], []
    for h in group.get("hooks", []):
        (dropped if is_our_hook(h.get("command", "")) else kept).append(h)
    group["hooks"] = kept
    if dropped:
        print(f"  Removed {len(dropped)} hook(s)")
        for h in dropped:
            print(f"    - {h.get('command', '')}")
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
