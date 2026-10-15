#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
SETTINGS="$HOME/.claude/settings.json"
TRACKER="$REPO_DIR/tracker.py"
LAUNCHER="$REPO_DIR/hook.sh"
DB_PATH="$HOME/.claude/token-tracker/token_stats.db"
PID_FILE="$HOME/.claude/token-tracker/dashboard.pid"
LOG_FILE="$HOME/.claude/token-tracker/dashboard.log"

# ── Options ───────────────────────────────────────────────────────────────────
START_MODE="ask"                                  # ask | always | never
PORT="${CLAUDE_TRACKER_PORT:-5001}"

usage() {
  cat <<'EOF'
Usage: ./install.sh [options]

  --start        start the dashboard in the background without asking
  --no-start     never start it (scripted / CI installs)
  --port N       dashboard port (default 5001)
  -h, --help     show this help

Environment:
  CLAUDE_TRACKER_PORT   same as --port
  CLAUDE_TRACKER_DB     database path (default ~/.claude/token-tracker/token_stats.db)
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --start)    START_MODE="always" ;;
    --no-start) START_MODE="never" ;;
    --port)     PORT="${2:-}"; shift ;;
    --port=*)   PORT="${1#*=}" ;;
    -h|--help)  usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

case "$PORT" in
  ''|*[!0-9]*) echo "Invalid port: ${PORT}" >&2; exit 2 ;;
esac

DASH_URL="http://localhost:${PORT}"

echo "Claude Token Tracker — install"
echo "  Repo    : $REPO_DIR"
echo "  DB      : $DB_PATH"
echo "  Port    : $PORT"
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

# ── Dashboard ─────────────────────────────────────────────────────────────────
dashboard_up() {
  command -v curl >/dev/null 2>&1 || return 1
  curl -s -m 2 "${DASH_URL}/" 2>/dev/null | grep -q "Claude Usage Dashboard"
}

# Is *anything* already answering on that port? (not necessarily our dashboard)
port_responds() {
  command -v curl >/dev/null 2>&1 || return 1
  curl -s -o /dev/null -m 2 "${DASH_URL}/" 2>/dev/null
}

start_dashboard() {
  mkdir -p "$(dirname "$LOG_FILE")"
  : > "$LOG_FILE"
  nohup "$PY" "$REPO_DIR/server.py" "$PORT" >>"$LOG_FILE" 2>&1 </dev/null &
  _pid=$!
  echo "$_pid" > "$PID_FILE"
  disown 2>/dev/null || true

  printf "  Starting the dashboard"
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    sleep 1
    printf "."
    if dashboard_up; then
      echo ""
      echo "  ✓ Up — open ${DASH_URL}"
      _lan=$(grep -m1 'LAN' "$LOG_FILE" 2>/dev/null | sed 's/.*→ *//')
      case "${_lan:-}" in
        ""|*127.0.0.1*|*0.0.0.0*) ;;          # not reachable from other devices
        *) echo "  ✓ From another device: ${_lan}" ;;
      esac
      echo "    Stop it with: kill \$(cat $PID_FILE)"
      return 0
    fi
    kill -0 "$_pid" 2>/dev/null || break
  done
  sleep 1                                      # let the child flush its log
  echo ""
  echo "  ✗ The dashboard did not come up. Last lines of $LOG_FILE:"
  tail -n 10 "$LOG_FILE" 2>/dev/null | sed 's/^/      /' || true
  return 1
}

echo ""
echo "✓ Install complete!"
echo ""
echo "  ── Dashboard ──────────────────────────────────────────────"
echo "     ${DASH_URL}"
echo "  ───────────────────────────────────────────────────────────"
echo ""

if dashboard_up; then
  echo "  It is already running — open ${DASH_URL} (refresh to see new data)."

elif [ "$START_MODE" = "never" ]; then
  echo "  Start it with:"
  echo ""
  echo "      $PY $REPO_DIR/server.py $PORT"
  echo ""
  echo "  then open ${DASH_URL}"

elif port_responds; then
  echo "  ✗ Port ${PORT} is already in use by another application — nothing was started."
  echo "    Use a different port, e.g.:  ./install.sh --start --port 8080"

else
  _do_start="no"
  if [ "$START_MODE" = "always" ]; then
    _do_start="yes"
  elif [ -t 0 ]; then
    printf "  Start the dashboard now in the background (port %s)? [Y/n] " "$PORT"
    if read -r _answer; then
      case "$_answer" in
        [nN]*) _do_start="no" ;;
        *)     _do_start="yes" ;;
      esac
    fi
    echo ""
  fi

  if [ "$_do_start" = "yes" ]; then
    start_dashboard || true
  else
    echo "  Start it with:"
    echo ""
    echo "      $PY $REPO_DIR/server.py $PORT"
    echo ""
    echo "  then open ${DASH_URL}"
  fi
fi

echo ""
echo "  Stats CLI       : $PY $REPO_DIR/stats.py today"
echo "  Backfill again  : $PY $REPO_DIR/tracker.py --backfill ~/.claude/projects/"
echo "  Catch up now    : $PY $REPO_DIR/tracker.py --catch-up"
echo "  Diagnostics     : sh $REPO_DIR/hook.sh --doctor"
echo "                    (add  --out ~/Desktop/report.txt  to save it for support)"
echo ""
echo "  Logs            : $HOME/.claude/token-tracker/logs/"
echo ""
echo "  Missed sessions are re-scanned automatically: after every tracked turn"
echo "  and every 5 minutes while the dashboard is running."
echo ""
