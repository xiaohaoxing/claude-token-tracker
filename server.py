#!/usr/bin/env python3
"""
Claude Code Usage Dashboard — local Flask server.
Run: python3 server.py  (default port 5000)
"""

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, jsonify, render_template, request

DB_PATH = Path(os.environ.get("CLAUDE_TRACKER_DB",
               Path.home() / ".claude" / "token-tracker" / "token_stats.db"))
TMPL_DIR = Path(__file__).parent / "templates"

app = Flask(__name__,
            template_folder=str(TMPL_DIR),
            static_folder=str(Path(__file__).parent / "static"))
app.config["JSON_SORT_KEYS"] = False
app.config["TEMPLATES_AUTO_RELOAD"] = True


# ── DB helper ────────────────────────────────────────────────────────────────
def get_db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def rows_to_list(rows) -> list[dict]:
    return [dict(r) for r in rows]


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
    limit   = min(int(request.args.get("limit", 50)), 200)
    offset  = int(request.args.get("offset", 0))
    project = request.args.get("project", "").strip()
    entry   = request.args.get("entrypoint", "").strip()
    im_only = request.args.get("im_only", "").strip()

    clauses, params = [], []
    if project:
        clauses.append("project_slug LIKE ?")
        params.append(f"%{project}%")
    if entry:
        clauses.append("entrypoint = ?")
        params.append(entry)
    if im_only == "1":
        clauses.append("im_source IS NOT NULL")

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    con = get_db()
    rows = con.execute(f"""
        SELECT session_id, started_at, ended_at, project_slug, cwd,
               entrypoint, git_branch, claude_version,
               total_input_tokens, total_output_tokens,
               total_cache_creation_tokens, total_cache_read_tokens,
               total_cost_usd, total_turns, total_api_calls, total_tool_calls,
               total_thinking_blocks, duration_seconds,
               models_used, first_user_message, im_source
        FROM sessions {where}
        ORDER BY started_at DESC
        LIMIT ? OFFSET ?
    """, params + [limit, offset]).fetchall()

    total = con.execute(f"SELECT COUNT(*) FROM sessions {where}", params).fetchone()[0]
    con.close()
    return jsonify({"sessions": rows_to_list(rows), "total": total, "offset": offset, "limit": limit})


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

    con.close()
    return jsonify({
        "session": dict(s),
        "api_calls": rows_to_list(calls),
        "tool_summary": rows_to_list(tools),
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


if __name__ == "__main__":
    import socket
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5001
    try:
        lan_ip = socket.gethostbyname(socket.gethostname())
    except Exception:
        lan_ip = "0.0.0.0"
    print(f"Claude Usage Dashboard")
    print(f"  Local  → http://localhost:{port}")
    print(f"  LAN    → http://{lan_ip}:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
