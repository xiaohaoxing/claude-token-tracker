#!/usr/bin/env python3
"""
Claude Code Usage Dashboard — local Flask server.
Run: python3 server.py  (default port 5000)
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

from flask import Flask, jsonify, render_template, request

# Reused for the catch-up safety net (see start_catchup_thread below).
# tracker.py lives next to this file, so a plain import works.
import tracker
import updater

DB_PATH = Path(os.environ.get("CLAUDE_TRACKER_DB",
               Path.home() / ".claude" / "token-tracker" / "token_stats.db"))
TMPL_DIR = Path(__file__).parent / "templates"

app = Flask(__name__,
            template_folder=str(TMPL_DIR),
            static_folder=str(Path(__file__).parent / "static"))
app.config["JSON_SORT_KEYS"] = False
app.config["TEMPLATES_AUTO_RELOAD"] = True


# ── Logging ───────────────────────────────────────────────────────────────────
# Unhandled request exceptions (the "no such column" class of bug) otherwise
# exist only in whatever terminal or pm2 log started us — which a user cannot
# hand over. Persist them next to the tracker log so `--doctor` can include them.
def _setup_logging() -> None:
    try:
        tracker.LOG_DIR.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            tracker.DASHBOARD_LOG, maxBytes=tracker.LOG_MAX_BYTES,
            backupCount=tracker.LOG_BACKUPS, encoding="utf-8")
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-5s %(name)s: %(message)s"))
        app.logger.addHandler(handler)
        app.logger.setLevel(logging.INFO)
        # Werkzeug's per-request lines are noisy; keep warnings and errors only.
        wlog = logging.getLogger("werkzeug")
        wlog.addHandler(handler)
        wlog.setLevel(logging.WARNING)
    except OSError:
        pass


_setup_logging()


# ── DB helper ────────────────────────────────────────────────────────────────
# Columns the queries below rely on. If any is missing, the database was written
# by an older version of the tracker — migrate it before serving instead of
# returning a 500 on the first page load.
_REQUIRED_SESSION_COLS = {"session_id", "im_source", "session_name", "transcript_path"}
_REQUIRED_TABLES = {"sessions", "user_messages"}


def get_db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout = 15000")
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(sessions)")}
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    except sqlite3.DatabaseError:
        cols, tables = set(), set()
    if not _REQUIRED_SESSION_COLS <= cols or not _REQUIRED_TABLES <= tables:
        tracker.ensure_schema(con)
    return con


def rows_to_list(rows) -> list[dict]:
    return [dict(r) for r in rows]


def _clean_query(raw: str | None) -> str:
    """Normalise a user search term; cap it so a pathological paste stays cheap."""
    return (raw or "").strip()[:200]


def _search_terms(raw: str | None) -> list[str]:
    """Whitespace-separated keywords; every term must match (AND semantics)."""
    terms = [t for t in _clean_query(raw).split() if t]
    return terms[:6]


def _like_pattern(text: str) -> str:
    """Escape LIKE wildcards so a keyword containing % or _ is literal."""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _snippet(text: str, q: str, radius: int = 90) -> str:
    """One-line context around the first hit, for search result rows."""
    flat = re.sub(r"\s+", " ", (text or "").replace("\r", " ").replace("\n", " ")).strip()
    if not flat:
        return ""
    best = None
    lower = flat.lower()
    for term in (_search_terms(q) or [q]):
        idx = lower.find((term or "").lower())
        if idx >= 0 and (best is None or idx < best[0]):
            best = (idx, term)
    if best is None:
        return flat[:radius * 2] + ("…" if len(flat) > radius * 2 else "")
    idx, term = best
    start = max(0, idx - radius)
    end = min(len(flat), idx + len(term) + radius)
    return ("…" if start else "") + flat[start:end] + ("…" if end < len(flat) else "")


# ── Period helpers ────────────────────────────────────────────────────────────
def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def _ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")

def _period_stats(con: sqlite3.Connection, start: str, end: str) -> dict:
    row = con.execute("""
        SELECT
            COALESCE(SUM(sessions_count), 0)              AS sessions,
            COALESCE(SUM(api_calls_count), 0)             AS api_calls,
            COALESCE(SUM(total_input_tokens), 0)          AS input_tokens,
            COALESCE(SUM(total_output_tokens), 0)         AS output_tokens,
            COALESCE(SUM(total_cache_creation_tokens), 0) AS cache_write,
            COALESCE(SUM(total_cache_read_tokens), 0)     AS cache_read,
            COALESCE(SUM(total_cost_usd), 0)              AS cost
        FROM daily_summary
        WHERE date BETWEEN ? AND ?
    """, (start, end)).fetchone()
    return dict(row) if row else {}


# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/overview")
def overview():
    con = get_db()
    today = _today()
    result = {
        "today":  _period_stats(con, today, today),
        "week":   _period_stats(con, _ago(6), today),
        "month":  _period_stats(con, _ago(29), today),
        "total":  _period_stats(con, "2000-01-01", "2099-12-31"),
    }
    con.close()
    return jsonify(result)


@app.route("/api/daily")
def daily():
    days = min(int(request.args.get("days", 30)), 365)
    con = get_db()
    rows = con.execute("""
        SELECT date, sessions_count, api_calls_count,
               total_input_tokens, total_output_tokens,
               total_cache_creation_tokens, total_cache_read_tokens,
               total_cost_usd
        FROM daily_summary
        WHERE date BETWEEN ? AND ?
        ORDER BY date
    """, (_ago(days - 1), _today())).fetchall()
    con.close()
    return jsonify(rows_to_list(rows))


@app.route("/api/sessions")
def sessions():
    limit   = min(max(int(request.args.get("limit", 50)), 1), 200)
    offset  = max(int(request.args.get("offset", 0)), 0)
    project = request.args.get("project", "").strip()
    entry   = request.args.get("entrypoint", "").strip()
    im_only = request.args.get("im_only", "").strip()
    q       = _clean_query(request.args.get("q", ""))   # sessions + prompts

    clauses, params = [], []
    if project:
        clauses.append("project_slug LIKE ? ESCAPE '\\'")
        params.append(_like_pattern(project))
    if entry:
        clauses.append("entrypoint = ?")
        params.append(entry)
    if im_only == "1":
        clauses.append("im_source IS NOT NULL")
    if q:
        # Session fields plus every user prompt in the session. This is what
        # turns the search box from "find a session id" into "find what I asked".
        # Multiple whitespace-separated terms are ANDed.
        for term in _search_terms(q):
            clauses.append("""(
                session_id         LIKE ? ESCAPE '\\'
                OR project_slug    LIKE ? ESCAPE '\\'
                OR cwd             LIKE ? ESCAPE '\\'
                OR first_user_message LIKE ? ESCAPE '\\'
                OR git_branch      LIKE ? ESCAPE '\\'
                OR entrypoint      LIKE ? ESCAPE '\\'
                OR im_source       LIKE ? ESCAPE '\\'
                OR session_name    LIKE ? ESCAPE '\\'
                OR EXISTS (
                    SELECT 1 FROM user_messages um
                    WHERE um.session_id = sessions.session_id
                      AND um.content LIKE ? ESCAPE '\\'
                )
            )""")
            params.extend([_like_pattern(term)] * 9)

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    con = get_db()
    rows = con.execute(f"""
        SELECT session_id, started_at, ended_at, project_slug, cwd,
               entrypoint, git_branch, claude_version,
               total_input_tokens, total_output_tokens,
               total_cache_creation_tokens, total_cache_read_tokens,
               total_cost_usd, total_turns, total_api_calls, total_tool_calls,
               total_thinking_blocks, duration_seconds,
               models_used, first_user_message, im_source, session_name,
               (SELECT COUNT(*) FROM user_messages um
                WHERE um.session_id = sessions.session_id) AS prompt_count,
               (SELECT substr(um.content, 1, 500) FROM user_messages um
                WHERE um.session_id = sessions.session_id
                ORDER BY um.seq_in_session, um.id LIMIT 1) AS first_prompt
        FROM sessions {where}
        ORDER BY started_at DESC
        LIMIT ? OFFSET ?
    """, params + [limit, offset]).fetchall()

    total = con.execute(f"SELECT COUNT(*) FROM sessions {where}", params).fetchone()[0]
    data = rows_to_list(rows)

    if q:
        terms = _search_terms(q)
        sids = [r["session_id"] for r in data]
        if sids and terms:
            marks = ",".join("?" * len(sids))
            prompt_where = " AND ".join(["content LIKE ? ESCAPE '\\'"] * len(terms))
            prompt_params = [_like_pattern(t) for t in terms]
            counts = {
                row["session_id"]: row["n"]
                for row in con.execute(
                    f"SELECT session_id, COUNT(*) AS n FROM user_messages "
                    f"WHERE session_id IN ({marks}) AND {prompt_where} "
                    f"GROUP BY session_id",
                    sids + prompt_params)
            }
            for r in data:
                n = counts.get(r["session_id"], 0)
                r["prompt_matches"] = n
                r["matched_prompts"] = []
                if n:
                    hits = con.execute(
                        "SELECT uuid, seq_in_session, timestamp, content, is_command, source "
                        "FROM user_messages WHERE session_id = ? "
                        f"AND {prompt_where} "
                        "ORDER BY seq_in_session LIMIT 3",
                        [r["session_id"]] + prompt_params).fetchall()
                    r["matched_prompts"] = [
                        {**dict(h), "snippet": _snippet(h["content"], q)} for h in hits
                    ]

    con.close()
    return jsonify({"sessions": data, "total": total, "offset": offset, "limit": limit})


@app.route("/api/prompts")
def prompts():
    """Prompt-level search: one row per matching question, newest first."""
    q      = _clean_query(request.args.get("q", ""))
    limit  = min(max(int(request.args.get("limit", 20)), 1), 100)
    offset = max(int(request.args.get("offset", 0)), 0)
    if not q:
        return jsonify({"prompts": [], "total": 0, "offset": offset, "limit": limit, "q": q})

    con = get_db()
    terms = _search_terms(q)
    if not terms:
        con.close()
        return jsonify({"prompts": [], "total": 0, "offset": offset, "limit": limit, "q": q})
    where = " AND ".join(["content LIKE ? ESCAPE '\\'"] * len(terms))
    prompt_params = [_like_pattern(t) for t in terms]
    total = con.execute(
        f"SELECT COUNT(*) FROM user_messages WHERE {where}",
        prompt_params).fetchone()[0]
    rows = con.execute(f"""
        SELECT um.id, um.session_id, um.uuid, um.seq_in_session, um.timestamp,
               um.content, um.char_count, um.is_command, um.source,
               s.session_name, s.project_slug, s.cwd, s.started_at,
               s.entrypoint, s.im_source
        FROM user_messages um
        JOIN sessions s ON s.session_id = um.session_id
        WHERE {where}
        ORDER BY um.timestamp DESC, um.id DESC
        LIMIT ? OFFSET ?
    """, prompt_params + [limit, offset]).fetchall()
    con.close()

    data = rows_to_list(rows)
    for r in data:
        r["snippet"] = _snippet(r.get("content", ""), q)
        r.pop("content", None)   # result rows only need the snippet; the modal has the full text
    return jsonify({"prompts": data, "total": total, "offset": offset, "limit": limit, "q": q})


@app.route("/api/session/<sid>")
def session_detail(sid: str):
    con = get_db()
    # Support prefix match
    s = con.execute("SELECT * FROM sessions WHERE session_id = ?", (sid,)).fetchone()
    if not s:
        s = con.execute("SELECT * FROM sessions WHERE session_id LIKE ?", (sid + "%",)).fetchone()
    if not s:
        con.close()
        return jsonify({"error": "not found"}), 404

    full_sid = s["session_id"]
    calls = con.execute("""
        SELECT seq_in_session, timestamp, message_id, model, stop_reason, speed, service_tier,
               input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens,
               cache_1h_tokens, cache_5m_tokens, cost_usd,
               web_search_requests, web_fetch_requests,
               has_thinking, thinking_chars, tool_use_count, output_text_chars,
               is_sidechain, entrypoint
        FROM api_calls
        WHERE session_id = ?
        ORDER BY seq_in_session
    """, (full_sid,)).fetchall()

    tools = con.execute("""
        SELECT tool_name, COUNT(*) as calls,
               SUM(CASE WHEN result_type='error' THEN 1 ELSE 0 END) as errors,
               COALESCE(AVG(result_size_bytes), 0) as avg_result_bytes,
               COALESCE(AVG(tool_input_size), 0) as avg_input_bytes
        FROM tool_calls
        WHERE session_id = ?
        GROUP BY tool_name
        ORDER BY calls DESC
    """, (full_sid,)).fetchall()

    prompt_rows = con.execute("""
        SELECT uuid, seq_in_session, timestamp, content, char_count,
               is_command, source
        FROM user_messages
        WHERE session_id = ?
        ORDER BY seq_in_session, id
    """, (full_sid,)).fetchall()

    con.close()
    user_prompts = rows_to_list(prompt_rows)
    if not user_prompts and s["first_user_message"]:
        # The upgrade backfill has not run yet on this database; still show what
        # we know instead of an empty conversation panel.
        legacy = tracker._first_message_prompt(
            full_sid, s["started_at"], s["first_user_message"])
        if legacy:
            user_prompts = [legacy]
    return jsonify({
        "session": dict(s),
        "api_calls": rows_to_list(calls),
        "tool_summary": rows_to_list(tools),
        "user_prompts": user_prompts,
    })


@app.route("/api/projects")
def projects():
    con = get_db()
    rows = con.execute("""
        SELECT
            COALESCE(NULLIF(project_slug,''), cwd) AS project,
            cwd,
            COUNT(*)                          AS sessions,
            SUM(total_api_calls)              AS api_calls,
            SUM(total_tool_calls)             AS tool_calls,
            SUM(total_input_tokens)           AS input_tokens,
            SUM(total_output_tokens)          AS output_tokens,
            SUM(total_cache_creation_tokens)  AS cache_write,
            SUM(total_cache_read_tokens)      AS cache_read,
            SUM(total_cost_usd)               AS cost,
            SUM(duration_seconds)             AS total_seconds,
            MAX(started_at)                   AS last_active
        FROM sessions
        GROUP BY cwd
        ORDER BY cost DESC
    """).fetchall()
    con.close()
    return jsonify(rows_to_list(rows))


@app.route("/api/tools")
def tools():
    limit = min(int(request.args.get("limit", 30)), 100)
    con = get_db()
    rows = con.execute("""
        SELECT
            tool_name,
            COUNT(*)                                            AS total_calls,
            SUM(CASE WHEN result_type='error' THEN 1 ELSE 0 END) AS errors,
            ROUND(100.0 * SUM(CASE WHEN result_type='error' THEN 1 ELSE 0 END) / COUNT(*), 1) AS error_pct,
            COALESCE(ROUND(AVG(result_size_bytes)), 0)          AS avg_result_bytes,
            COALESCE(ROUND(AVG(tool_input_size)), 0)            AS avg_input_bytes,
            COUNT(DISTINCT session_id)                          AS sessions
        FROM tool_calls
        GROUP BY tool_name
        ORDER BY total_calls DESC
        LIMIT ?
    """, (limit,)).fetchall()
    con.close()
    return jsonify(rows_to_list(rows))


@app.route("/api/models")
def models():
    con = get_db()
    rows = con.execute("""
        SELECT
            model,
            COUNT(*)                        AS calls,
            SUM(input_tokens)               AS input_tokens,
            SUM(output_tokens)              AS output_tokens,
            SUM(cache_creation_tokens)      AS cache_write,
            SUM(cache_read_tokens)          AS cache_read,
            SUM(cost_usd)                   AS cost,
            COUNT(DISTINCT session_id)      AS sessions,
            SUM(CASE WHEN has_thinking=1 THEN 1 ELSE 0 END) AS thinking_calls,
            ROUND(AVG(output_tokens), 0)    AS avg_output_tokens
        FROM api_calls
        WHERE model IS NOT NULL AND model != '' AND model != '<synthetic>'
        GROUP BY model
        ORDER BY cost DESC
    """).fetchall()
    con.close()
    return jsonify(rows_to_list(rows))


@app.route("/api/entrypoints")
def entrypoints():
    con = get_db()
    rows = con.execute("""
        SELECT entrypoint,
               COUNT(DISTINCT session_id) AS sessions,
               SUM(cost_usd)              AS cost,
               COUNT(*)                   AS api_calls
        FROM api_calls
        WHERE entrypoint IS NOT NULL AND entrypoint != ''
        GROUP BY entrypoint
        ORDER BY sessions DESC
    """).fetchall()
    con.close()
    return jsonify(rows_to_list(rows))


@app.route("/api/filters")
def filters():
    """Return distinct values for filter dropdowns."""
    con = get_db()
    projects = con.execute(
        "SELECT DISTINCT COALESCE(NULLIF(project_slug,''), cwd) AS p FROM sessions ORDER BY p"
    ).fetchall()
    entries = con.execute(
        "SELECT DISTINCT entrypoint FROM sessions WHERE entrypoint IS NOT NULL AND entrypoint != '' ORDER BY entrypoint"
    ).fetchall()
    con.close()
    return jsonify({
        "projects":    [r[0] for r in projects if r[0]],
        "entrypoints": [r[0] for r in entries],
    })


# ── Version & one-click upgrade ───────────────────────────────────────────────
# The dashboard executes `git pull` for the user, so the endpoint is fenced in:
#   * only requests originating on this machine may call it — the dashboard
#     listens on 0.0.0.0, so a LAN neighbour must never be able to update code;
#   * the body must be JSON and carry the per-process token handed out by
#     /api/version. A cross-site page cannot read that response (no CORS) and
#     cannot send application/json without a preflight it would fail, so a
#     drive-by CSRF cannot restart the server under a new version.
UPGRADE_TOKEN = os.urandom(16).hex()
_PORT = 5001
_local_addrs_cache: list = []


def _local_addrs() -> list:
    global _local_addrs_cache
    if not _local_addrs_cache:
        addrs = ["127.0.0.1", "::1"]
        for probe in (lambda: socket.gethostbyname(socket.gethostname()), _lan_ip):
            try:
                ip = probe()
                if ip and ip not in addrs:
                    addrs.append(ip)
            except Exception:
                pass
        _local_addrs_cache = addrs
    return _local_addrs_cache


def _is_local_request() -> bool:
    addr = (request.remote_addr or "").strip()
    if addr.startswith("::ffff:"):       # IPv4-mapped IPv6
        addr = addr[7:]
    addr = addr.split("%", 1)[0]          # strip IPv6 zone id
    return addr in _local_addrs()


@app.route("/api/version")
def api_version():
    info = updater.status()
    local = _is_local_request()
    info["local"] = local
    # The token is only ever handed to a request coming from this machine.
    info["token"] = UPGRADE_TOKEN if (local and info.get("can_upgrade")) else None
    return jsonify(info)


@app.route("/api/upgrade", methods=["POST"])
def api_upgrade():
    if not _is_local_request():
        return jsonify({"ok": False, "error": "Upgrades are only allowed from the install machine"}), 403
    payload = request.get_json(silent=True) or {}
    if payload.get("token") != UPGRADE_TOKEN:
        return jsonify({"ok": False, "error": "Invalid upgrade token — reload the page and retry"}), 403

    result = updater.upgrade()
    tracker.log_event("INFO", "upgrade {} -> {} ok={} {}".format(
        result.get("from"), result.get("to"), result.get("ok"),
        result.get("error") or "").strip())
    if result.get("ok") and not result.get("up_to_date"):
        # Answer the request first: execv() would otherwise cut the response off.
        threading.Timer(1.0, _restart_for_upgrade).start()
    return jsonify(result), (200 if result.get("ok") else 409)


def _wait_for_port(port: int, timeout: float = 20.0) -> bool:
    """Block until we can bind `port` ourselves — i.e. the old process let go."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("0.0.0.0", port))
            return True
        except OSError:
            time.sleep(0.25)
        finally:
            probe.close()
    return False


def _restart_for_upgrade() -> None:
    """
    Restart onto the new code.

    execv() looked obvious but inherits the listening socket, so the fresh image
    failed to bind its own port. A detached child with close_fds=True starts with
    a clean descriptor table, waits for this process to exit (see _wait_for_port)
    and then serves; this process exits. The child keeps the same stdout/stderr,
    so nohup logging continues.
    """
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    env = dict(os.environ)
    env["CLAUDE_TRACKER_WAIT_PORT"] = str(_PORT)
    try:
        child = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), str(_PORT)],
            stdin=subprocess.DEVNULL, stdout=sys.stdout, stderr=sys.stderr,
            close_fds=True, start_new_session=True, env=env,
        )
    except Exception as e:
        tracker.log_exception("upgrade restart", e)
        return  # keep serving the old code rather than going dark
    tracker.log_event("INFO", f"upgrade restart old={os.getpid()} new={child.pid} port={_PORT}")
    os._exit(0)


# ── Catch-up safety net ───────────────────────────────────────────────────────
# The Stop hook is best-effort: headless/SDK sessions may never fire it, and when
# it does fire the transcript for the finishing turn is not always flushed yet.
# While the dashboard is running, re-scan the transcript directory periodically
# and ingest anything new or changed, so nothing stays missing.
CATCHUP_EVERY = float(os.environ.get("CLAUDE_TRACKER_CATCHUP_EVERY", "300"))


def _catchup_loop() -> None:
    while True:
        time.sleep(CATCHUP_EVERY)
        try:
            n = tracker.run_catch_up(force=True)
            if n:
                print(f"[catch-up] ingested {n} session(s)", flush=True)
                tracker.log_event("INFO", f"dashboard catch-up ingested={n}")
        except Exception as e:  # never let the watcher kill the dashboard
            print(f"[catch-up] failed: {e}", flush=True)
            tracker.log_exception("dashboard catch-up", e)


def start_catchup_thread() -> None:
    if os.environ.get("CLAUDE_TRACKER_NO_CATCHUP") == "1":
        print("  catch-up : disabled (CLAUDE_TRACKER_NO_CATCHUP=1)")
        return
    threading.Thread(target=_catchup_loop, name="token-tracker-catchup",
                     daemon=True).start()
    print(f"  catch-up : every {int(CATCHUP_EVERY)}s")


def _lan_ip() -> str:
    """
    Best-effort LAN address. Resolving our own hostname returns 127.0.0.1 on
    plenty of machines, which makes the "open this from another device" line
    useless, so ask the routing table which local address would be used.
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))      # UDP connect sends no packets
            ip = s.getsockname()[0]
        finally:
            s.close()
        if ip and ip != "0.0.0.0" and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    try:
        return socket.gethostbyname(socket.gethostname())
    except Exception:
        return "0.0.0.0"


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5001
    _PORT = port
    lan_ip = _lan_ip()
    print(f"Claude Usage Dashboard")
    print(f"  Local  → http://localhost:{port}")
    print(f"  LAN    → http://{lan_ip}:{port}")
    _ver = updater.local_state()
    print(f"  Version → {_ver.get('version') or 'untagged'}"
          f" ({_ver.get('short') or '?'}, branch {_ver.get('branch') or '?'})")
    if not _ver.get("is_git"):
        print("  Update  → unavailable (not a git checkout), update by hand")
    # Bring an older database up to date now, so a schema problem shows up here
    # rather than as a 500 on the first page load.
    try:
        _con = get_db()
        # An upgraded install must show corrected prices immediately, not five
        # minutes later when the catch-up thread happens to run.
        tracker.migrate_pricing(_con)
        # Same for the prompt history: on the first start after this feature
        # lands, re-read existing transcripts now so the Sessions tab is useful.
        tracker.maybe_backfill_user_messages(_con)
        _con.close()
        print("  DB     → schema up to date")
    except Exception as e:
        print(f"  DB     → WARNING: {e}")
        tracker.log_exception("dashboard startup schema check", e)
    tracker.log_event("INFO", f"dashboard start port={port} pid={os.getpid()} "
                              f"db={DB_PATH}")
    _wait_port = os.environ.pop("CLAUDE_TRACKER_WAIT_PORT", "")
    if _wait_port:
        print(f"  Restart→ waiting for the previous process to release port {_wait_port}")
        if not _wait_for_port(int(_wait_port)):
            print(f"  Restart→ port {_wait_port} still busy, trying anyway")
    start_catchup_thread()
    updater.start_background_checks()
    app.run(host="0.0.0.0", port=port, debug=False)
