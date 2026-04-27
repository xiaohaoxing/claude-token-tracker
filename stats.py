#!/usr/bin/env python3
"""
Claude Code Token Stats CLI

Usage:
    stats.py today                   – today's usage summary
    stats.py yesterday               – yesterday's summary
    stats.py week                    – last 7 days
    stats.py month                   – last 30 days
    stats.py total                   – all-time totals
    stats.py session <session_id>    – single session detail
    stats.py sessions [--limit N]    – recent sessions list
    stats.py tools [--limit N]       – top tools by call count
    stats.py projects                – per-project breakdown
    stats.py models                  – per-model token breakdown
    stats.py daily [--days N]        – daily chart (last N days)
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path(os.environ.get("CLAUDE_TRACKER_DB",
               Path.home() / ".claude" / "token-tracker" / "token_stats.db"))


def open_db() -> sqlite3.Connection:
    if not DB_PATH.exists():
        print(f"Database not found at {DB_PATH}. Run tracker.py --backfill first.", file=sys.stderr)
        sys.exit(1)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


# ── Formatting helpers ────────────────────────────────────────────────────────
def fmt_tokens(n: int | float) -> str:
    n = int(n or 0)
    if n >= 1_000_000:
        return f"{n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)


def fmt_cost(v: float) -> str:
    return f"${v:.4f}"


def fmt_dur(seconds: int | None) -> str:
    if not seconds:
        return "-"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def print_table(headers: list[str], rows: list[list], col_sep: str = "  ") -> None:
    widths = [len(h) for h in headers]
    str_rows = [[str(c) for c in row] for row in rows]
    for row in str_rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    header_line = col_sep.join(h.ljust(widths[i]) for i, h in enumerate(headers))
    print(header_line)
    print("-" * len(header_line))
    for row in str_rows:
        print(col_sep.join(cell.ljust(widths[i]) for i, cell in enumerate(row)))


def print_summary_block(title: str, row: sqlite3.Row) -> None:
    inp   = row["total_input_tokens"] or 0
    out   = row["total_output_tokens"] or 0
    cw    = row["total_cache_creation_tokens"] or 0
    cr    = row["total_cache_read_tokens"] or 0
    cost  = row["total_cost_usd"] or 0
    calls = row["api_calls_count"] or 0
    sess  = row["sessions_count"] or 0

    print(f"\n{'─' * 50}")
    print(f"  {title}")
    print(f"{'─' * 50}")
    print(f"  Sessions   : {sess}")
    print(f"  API calls  : {calls}")
    print(f"  Input      : {fmt_tokens(inp)}")
    print(f"  Output     : {fmt_tokens(out)}")
    print(f"  Cache write: {fmt_tokens(cw)}")
    print(f"  Cache read : {fmt_tokens(cr)}")
    print(f"  Total cost : {fmt_cost(cost)}")
    print(f"{'─' * 50}\n")


# ── Commands ──────────────────────────────────────────────────────────────────
def cmd_period(con: sqlite3.Connection, label: str, start_date: str, end_date: str | None = None) -> None:
    if end_date:
        clause = "WHERE date BETWEEN ? AND ?"
        params = (start_date, end_date)
    else:
        clause = "WHERE date = ?"
        params = (start_date,)

    row = con.execute(f"""
        SELECT
            COALESCE(SUM(sessions_count),0)              AS sessions_count,
            COALESCE(SUM(api_calls_count),0)             AS api_calls_count,
            COALESCE(SUM(total_input_tokens),0)          AS total_input_tokens,
            COALESCE(SUM(total_output_tokens),0)         AS total_output_tokens,
            COALESCE(SUM(total_cache_creation_tokens),0) AS total_cache_creation_tokens,
            COALESCE(SUM(total_cache_read_tokens),0)     AS total_cache_read_tokens,
            COALESCE(SUM(total_cost_usd),0)              AS total_cost_usd
        FROM daily_summary {clause}
    """, params).fetchone()
    print_summary_block(label, row)


def cmd_today(con: sqlite3.Connection) -> None:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cmd_period(con, f"Today  ({today})", today)


def cmd_yesterday(con: sqlite3.Connection) -> None:
    d = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    cmd_period(con, f"Yesterday  ({d})", d)


def cmd_week(con: sqlite3.Connection) -> None:
    end   = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    start = (datetime.now(timezone.utc) - timedelta(days=6)).strftime("%Y-%m-%d")
    cmd_period(con, f"Last 7 days  ({start} → {end})", start, end)


def cmd_month(con: sqlite3.Connection) -> None:
    end   = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    start = (datetime.now(timezone.utc) - timedelta(days=29)).strftime("%Y-%m-%d")
    cmd_period(con, f"Last 30 days  ({start} → {end})", start, end)


def cmd_total(con: sqlite3.Connection) -> None:
    row = con.execute("""
        SELECT
            COALESCE(SUM(sessions_count),0)              AS sessions_count,
            COALESCE(SUM(api_calls_count),0)             AS api_calls_count,
            COALESCE(SUM(total_input_tokens),0)          AS total_input_tokens,
            COALESCE(SUM(total_output_tokens),0)         AS total_output_tokens,
            COALESCE(SUM(total_cache_creation_tokens),0) AS total_cache_creation_tokens,
            COALESCE(SUM(total_cache_read_tokens),0)     AS total_cache_read_tokens,
            COALESCE(SUM(total_cost_usd),0)              AS total_cost_usd
        FROM daily_summary
    """).fetchone()
    print_summary_block("All-time totals", row)


def cmd_session(con: sqlite3.Connection, session_id: str) -> None:
    s = con.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
    if not s:
        # partial match
        s = con.execute(
            "SELECT * FROM sessions WHERE session_id LIKE ?", (session_id + "%",)
        ).fetchone()
    if not s:
        print(f"Session not found: {session_id}")
        return

    sid = s["session_id"]
    print(f"\n{'─'*60}")
    print(f"  Session: {sid}")
    print(f"{'─'*60}")
    print(f"  Project    : {s['project_slug'] or s['cwd']}")
    print(f"  Entrypoint : {s['entrypoint']}")
    print(f"  Branch     : {s['git_branch']}")
    print(f"  Version    : {s['claude_version']}")
    print(f"  Started    : {s['started_at']}")
    print(f"  Ended      : {s['ended_at']}")
    print(f"  Duration   : {fmt_dur(s['duration_seconds'])}")
    print(f"  Turns      : {s['total_turns']}")
    print(f"  API calls  : {s['total_api_calls']}")
    print(f"  Tool calls : {s['total_tool_calls']}")
    print(f"  Thinking   : {s['total_thinking_blocks']} blocks")
    print(f"  Input      : {fmt_tokens(s['total_input_tokens'])}")
    print(f"  Output     : {fmt_tokens(s['total_output_tokens'])}")
    print(f"  Cache write: {fmt_tokens(s['total_cache_creation_tokens'])}")
    print(f"  Cache read : {fmt_tokens(s['total_cache_read_tokens'])}")
    print(f"  Cost       : {fmt_cost(s['total_cost_usd'])}")
    print(f"  Models     : {s['models_used']}")
    if s["first_user_message"]:
        preview = (s["first_user_message"] or "")[:100].replace("\n", " ")
        print(f"  First msg  : {preview}…")
    print()

    # Per-API-call breakdown
    calls = con.execute("""
        SELECT seq_in_session, timestamp, model, stop_reason, speed,
               input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens,
               cost_usd, has_thinking, tool_use_count, is_sidechain
        FROM api_calls WHERE session_id = ? ORDER BY seq_in_session
    """, (sid,)).fetchall()

    if calls:
        print("  API calls detail:")
        headers = ["#", "Time", "Model", "Stop", "In", "Out", "CW", "CR", "Cost", "Tools", "Think"]
        rows = []
        for c in calls:
            ts = (c["timestamp"] or "")[-8:-1]  # HH:MM:SS
            model_short = (c["model"] or "").replace("claude-", "").replace("-2024", "")[:20]
            rows.append([
                c["seq_in_session"],
                ts,
                model_short,
                c["stop_reason"] or "",
                fmt_tokens(c["input_tokens"]),
                fmt_tokens(c["output_tokens"]),
                fmt_tokens(c["cache_creation_tokens"]),
                fmt_tokens(c["cache_read_tokens"]),
                fmt_cost(c["cost_usd"]),
                c["tool_use_count"] or 0,
                "Y" if c["has_thinking"] else "",
            ])
        print_table(headers, rows)

    # Top tools in this session
    tools = con.execute("""
        SELECT tool_name, COUNT(*) as cnt,
               SUM(CASE WHEN result_type='error' THEN 1 ELSE 0 END) as errors
        FROM tool_calls WHERE session_id = ?
        GROUP BY tool_name ORDER BY cnt DESC LIMIT 15
    """, (sid,)).fetchall()
    if tools:
        print("\n  Tool call breakdown:")
        print_table(["Tool", "Calls", "Errors"], [[t["tool_name"], t["cnt"], t["errors"]] for t in tools])
    print()


def cmd_sessions(con: sqlite3.Connection, limit: int = 20) -> None:
    rows = con.execute("""
        SELECT session_id, started_at, project_slug, entrypoint,
               total_api_calls, total_tool_calls, total_cost_usd, duration_seconds
        FROM sessions ORDER BY started_at DESC LIMIT ?
    """, (limit,)).fetchall()
    if not rows:
        print("No sessions found.")
        return
    headers = ["Session", "Started", "Project", "Via", "Calls", "Tools", "Cost", "Dur"]
    table_rows = []
    for r in rows:
        table_rows.append([
            r["session_id"][:8],
            (r["started_at"] or "")[:16],
            (r["project_slug"] or "")[:20],
            r["entrypoint"] or "",
            r["total_api_calls"] or 0,
            r["total_tool_calls"] or 0,
            fmt_cost(r["total_cost_usd"]),
            fmt_dur(r["duration_seconds"]),
        ])
    print(f"\nRecent {limit} sessions:\n")
    print_table(headers, table_rows)
    print()


def cmd_tools(con: sqlite3.Connection, limit: int = 25) -> None:
    rows = con.execute("""
        SELECT tool_name,
               COUNT(*) as total_calls,
               SUM(CASE WHEN result_type='error' THEN 1 ELSE 0 END) as errors,
               ROUND(AVG(result_size_bytes)) as avg_result_bytes,
               COUNT(DISTINCT session_id) as sessions
        FROM tool_calls
        GROUP BY tool_name
        ORDER BY total_calls DESC
        LIMIT ?
    """, (limit,)).fetchall()
    if not rows:
        print("No tool call data found.")
        return
    print(f"\nTop {limit} tools by usage:\n")
    print_table(
        ["Tool", "Calls", "Errors", "Sessions", "Avg result"],
        [[r["tool_name"], r["total_calls"], r["errors"], r["sessions"],
          f"{int(r['avg_result_bytes'] or 0):,}B"] for r in rows]
    )
    print()


def cmd_projects(con: sqlite3.Connection) -> None:
    rows = con.execute("""
        SELECT project_slug, cwd,
               COUNT(*) as sessions,
               SUM(total_api_calls) as api_calls,
               SUM(total_input_tokens) as input_t,
               SUM(total_output_tokens) as output_t,
               SUM(total_cost_usd) as cost
        FROM sessions
        GROUP BY cwd
        ORDER BY cost DESC
    """).fetchall()
    if not rows:
        print("No project data found.")
        return
    print("\nPer-project breakdown:\n")
    print_table(
        ["Project", "Sessions", "API calls", "Input", "Output", "Cost"],
        [[r["project_slug"] or r["cwd"][:30], r["sessions"], r["api_calls"],
          fmt_tokens(r["input_t"]), fmt_tokens(r["output_t"]), fmt_cost(r["cost"])]
         for r in rows]
    )
    print()


def cmd_models(con: sqlite3.Connection) -> None:
    rows = con.execute("""
        SELECT model,
               COUNT(*) as calls,
               SUM(input_tokens) as inp,
               SUM(output_tokens) as out,
               SUM(cache_creation_tokens) as cw,
               SUM(cache_read_tokens) as cr,
               SUM(cost_usd) as cost
        FROM api_calls
        WHERE model IS NOT NULL AND model != ''
        GROUP BY model
        ORDER BY cost DESC
    """).fetchall()
    if not rows:
        print("No model data found.")
        return
    print("\nPer-model breakdown:\n")
    print_table(
        ["Model", "Calls", "Input", "Output", "Cache W", "Cache R", "Cost"],
        [[r["model"], r["calls"],
          fmt_tokens(r["inp"]), fmt_tokens(r["out"]),
          fmt_tokens(r["cw"]), fmt_tokens(r["cr"]),
          fmt_cost(r["cost"])]
         for r in rows]
    )
    print()


def cmd_daily(con: sqlite3.Connection, days: int = 14) -> None:
    end   = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    start = (datetime.now(timezone.utc) - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    rows = con.execute("""
        SELECT date, sessions_count, api_calls_count,
               total_input_tokens, total_output_tokens, total_cost_usd
        FROM daily_summary
        WHERE date BETWEEN ? AND ?
        ORDER BY date
    """, (start, end)).fetchall()

    print(f"\nDaily usage ({start} → {end}):\n")
    if not rows:
        print("  No data.")
        return

    max_cost = max((r["total_cost_usd"] or 0) for r in rows) or 1
    bar_width = 30
    print(f"  {'Date':<12} {'Cost':>8}  {'Bar':<{bar_width}}  {'In':>7} {'Out':>7} {'Sessions':>8}")
    print(f"  {'-'*12} {'-'*8}  {'-'*bar_width}  {'-'*7} {'-'*7} {'-'*8}")
    for r in rows:
        cost = r["total_cost_usd"] or 0
        bar_len = int(bar_width * cost / max_cost)
        bar = "█" * bar_len
        print(f"  {r['date']:<12} {fmt_cost(cost):>8}  {bar:<{bar_width}}  "
              f"{fmt_tokens(r['total_input_tokens']):>7} "
              f"{fmt_tokens(r['total_output_tokens']):>7} "
              f"{r['sessions_count']:>8}")
    print()


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="Claude Code token usage stats")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("today")
    sub.add_parser("yesterday")
    sub.add_parser("week")
    sub.add_parser("month")
    sub.add_parser("total")

    p_session = sub.add_parser("session")
    p_session.add_argument("session_id")

    p_sessions = sub.add_parser("sessions")
    p_sessions.add_argument("--limit", type=int, default=20)

    p_tools = sub.add_parser("tools")
    p_tools.add_argument("--limit", type=int, default=25)

    sub.add_parser("projects")
    sub.add_parser("models")

    p_daily = sub.add_parser("daily")
    p_daily.add_argument("--days", type=int, default=14)

    args = parser.parse_args()
    if not args.cmd:
        parser.print_help()
        return

    con = open_db()
    try:
        if   args.cmd == "today":     cmd_today(con)
        elif args.cmd == "yesterday": cmd_yesterday(con)
        elif args.cmd == "week":      cmd_week(con)
        elif args.cmd == "month":     cmd_month(con)
        elif args.cmd == "total":     cmd_total(con)
        elif args.cmd == "session":   cmd_session(con, args.session_id)
        elif args.cmd == "sessions":  cmd_sessions(con, args.limit)
        elif args.cmd == "tools":     cmd_tools(con, args.limit)
        elif args.cmd == "projects":  cmd_projects(con)
        elif args.cmd == "models":    cmd_models(con)
        elif args.cmd == "daily":     cmd_daily(con, args.days)
    finally:
        con.close()


if __name__ == "__main__":
    main()
