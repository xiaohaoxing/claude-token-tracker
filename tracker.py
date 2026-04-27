#!/usr/bin/env python3
"""
Claude Code Token Tracker
Parses session JSONL files and persists token usage to SQLite.

Usage (Stop hook):  stdin JSON → { session_id, transcript_path, cwd }
Usage (backfill):   python3 tracker.py --backfill <projects_dir>
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(os.environ.get("CLAUDE_TRACKER_DB",
               Path.home() / ".claude" / "token-tracker" / "token_stats.db"))

# ── Pricing table (USD per 1M tokens) ────────────────────────────────────────
PRICING: dict[str, dict[str, float]] = {
    "claude-opus-4-7":    {"input": 15.00, "output": 75.00, "cache_write": 18.75, "cache_read": 1.50},
    "claude-opus-4-6":    {"input": 15.00, "output": 75.00, "cache_write": 18.75, "cache_read": 1.50},
    "claude-sonnet-4-6":  {"input":  3.00, "output": 15.00, "cache_write":  3.75, "cache_read": 0.30},
    "claude-haiku-4-5":   {"input":  0.80, "output":  4.00, "cache_write":  1.00, "cache_read": 0.08},
    "claude-3-5-sonnet":  {"input":  3.00, "output": 15.00, "cache_write":  3.75, "cache_read": 0.30},
    "claude-3-5-haiku":   {"input":  0.80, "output":  4.00, "cache_write":  1.00, "cache_read": 0.08},
    "claude-3-opus":      {"input": 15.00, "output": 75.00, "cache_write": 18.75, "cache_read": 1.50},
}

def _get_pricing(model: str) -> dict[str, float]:
    if model:
        for key, p in PRICING.items():
            if key in model:
                return p
    return {"input": 3.00, "output": 15.00, "cache_write": 3.75, "cache_read": 0.30}

def calc_cost(model: str, inp: int, out: int, cache_write: int, cache_read: int) -> float:
    p = _get_pricing(model)
    return (
        inp        * p["input"]       / 1_000_000
        + out      * p["output"]      / 1_000_000
        + cache_write * p["cache_write"] / 1_000_000
        + cache_read  * p["cache_read"]  / 1_000_000
    )

# ── Schema ────────────────────────────────────────────────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id            TEXT PRIMARY KEY,
    started_at            TEXT,
    ended_at              TEXT,
    duration_seconds      INTEGER,
    cwd                   TEXT,
    project_slug          TEXT,       -- basename of cwd
    entrypoint            TEXT,       -- cli | sdk-ts | sdk-python | …
    git_branch            TEXT,
    claude_version        TEXT,
    user_type             TEXT,       -- external | internal | …

    -- aggregated token totals
    total_input_tokens          INTEGER DEFAULT 0,
    total_output_tokens         INTEGER DEFAULT 0,
    total_cache_creation_tokens INTEGER DEFAULT 0,
    total_cache_read_tokens     INTEGER DEFAULT 0,
    total_cache_1h_tokens       INTEGER DEFAULT 0,
    total_cache_5m_tokens       INTEGER DEFAULT 0,
    total_cost_usd              REAL    DEFAULT 0,

    -- conversation shape
    total_turns             INTEGER DEFAULT 0,  -- user messages
    total_api_calls         INTEGER DEFAULT 0,  -- assistant API responses
    total_tool_calls        INTEGER DEFAULT 0,  -- tool_use blocks
    total_thinking_blocks   INTEGER DEFAULT 0,
    total_output_chars      INTEGER DEFAULT 0,

    models_used           TEXT,   -- JSON array of distinct model ids
    first_user_message    TEXT,   -- first 500 chars of first human turn
    transcript_path       TEXT,

    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS api_calls (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id       TEXT NOT NULL REFERENCES sessions(session_id),
    message_id       TEXT UNIQUE,   -- msg_bdrk_xxx from Anthropic API
    uuid             TEXT UNIQUE,   -- Claude Code internal UUID
    parent_uuid      TEXT,
    seq_in_session   INTEGER,       -- 1-based position within session
    timestamp        TEXT,
    model            TEXT,
    stop_reason      TEXT,          -- end_turn | tool_use | max_tokens | stop_sequence
    service_tier     TEXT,          -- standard | priority | …
    speed            TEXT,          -- standard | fast

    -- token counts
    input_tokens          INTEGER DEFAULT 0,
    output_tokens         INTEGER DEFAULT 0,
    cache_creation_tokens INTEGER DEFAULT 0,
    cache_read_tokens     INTEGER DEFAULT 0,
    cache_1h_tokens       INTEGER DEFAULT 0,
    cache_5m_tokens       INTEGER DEFAULT 0,
    cost_usd              REAL    DEFAULT 0,

    -- web/server tool use (from usage.server_tool_use)
    web_search_requests   INTEGER DEFAULT 0,
    web_fetch_requests    INTEGER DEFAULT 0,

    -- content metadata
    has_thinking      INTEGER DEFAULT 0,
    thinking_chars    INTEGER DEFAULT 0,
    tool_use_count    INTEGER DEFAULT 0,
    output_text_chars INTEGER DEFAULT 0,
    is_sidechain      INTEGER DEFAULT 0,
    entrypoint        TEXT,
    cwd               TEXT,
    git_branch        TEXT,
    claude_version    TEXT,

    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tool_calls (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL REFERENCES sessions(session_id),
    api_call_uuid   TEXT,          -- the assistant message that issued this tool call
    tool_use_id     TEXT,          -- toolu_xxx from API
    tool_name       TEXT,
    tool_input_size INTEGER,       -- byte length of serialized input
    timestamp       TEXT,
    is_sidechain    INTEGER DEFAULT 0,

    -- result (from subsequent user tool_result block)
    result_type       TEXT,        -- success | error | cancel
    result_size_bytes INTEGER,

    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS daily_summary (
    date                        TEXT PRIMARY KEY,  -- YYYY-MM-DD
    sessions_count              INTEGER DEFAULT 0,
    api_calls_count             INTEGER DEFAULT 0,
    total_input_tokens          INTEGER DEFAULT 0,
    total_output_tokens         INTEGER DEFAULT 0,
    total_cache_creation_tokens INTEGER DEFAULT 0,
    total_cache_read_tokens     INTEGER DEFAULT 0,
    total_cost_usd              REAL    DEFAULT 0,
    unique_cwds                 TEXT,              -- JSON array
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_api_calls_session   ON api_calls(session_id);
CREATE INDEX IF NOT EXISTS idx_api_calls_timestamp ON api_calls(timestamp);
CREATE INDEX IF NOT EXISTS idx_api_calls_model     ON api_calls(model);
CREATE INDEX IF NOT EXISTS idx_tool_calls_session  ON tool_calls(session_id);
CREATE INDEX IF NOT EXISTS idx_tool_calls_name     ON tool_calls(tool_name);
CREATE INDEX IF NOT EXISTS idx_sessions_started    ON sessions(started_at);
CREATE INDEX IF NOT EXISTS idx_sessions_cwd        ON sessions(cwd);
CREATE INDEX IF NOT EXISTS idx_sessions_entrypoint ON sessions(entrypoint);
"""

# ── DB helpers ────────────────────────────────────────────────────────────────
def open_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    con.commit()
    return con

# ── JSONL parser ──────────────────────────────────────────────────────────────
def parse_transcript(path: str) -> dict:
    """
    Parse a Claude Code session JSONL file.
    Returns a dict with keys: session_meta, api_calls, tool_calls.
    """
    messages: list[dict] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    messages.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    except OSError:
        return {}

    if not messages:
        return {}

    # ── Collect assistant messages (carry usage) ──────────────────────────────
    assistant_msgs = [m for m in messages if m.get("type") == "assistant"]
    user_msgs      = [m for m in messages if m.get("type") == "user"]

    if not assistant_msgs and not user_msgs:
        return {}

    # Derive session-level metadata from first available record
    first = (assistant_msgs or user_msgs)[0]
    session_id    = first.get("sessionId", "")
    cwd           = first.get("cwd", "")
    entrypoint    = first.get("entrypoint", "")
    git_branch    = first.get("gitBranch", "")
    claude_version= first.get("version", "")
    user_type     = first.get("userType", "")

    # Timestamps
    all_ts = [m.get("timestamp") for m in messages if m.get("timestamp")]
    started_at = min(all_ts) if all_ts else None
    ended_at   = max(all_ts) if all_ts else None

    duration_seconds = None
    if started_at and ended_at:
        try:
            fmt = "%Y-%m-%dT%H:%M:%S.%fZ"
            t0 = datetime.strptime(started_at, fmt).replace(tzinfo=timezone.utc)
            t1 = datetime.strptime(ended_at,   fmt).replace(tzinfo=timezone.utc)
            duration_seconds = int((t1 - t0).total_seconds())
        except ValueError:
            pass

    # First user message text
    first_user_message = None
    for m in user_msgs:
        content = m.get("message", {}).get("content", "")
        if isinstance(content, str) and content.strip():
            first_user_message = content[:500]
            break
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    first_user_message = block.get("text", "")[:500]
                    break
            if first_user_message:
                break

    # ── Build api_call records ────────────────────────────────────────────────
    api_call_records: list[dict] = []
    tool_call_records: list[dict] = []

    # Map uuid → timestamp for tool result lookups
    uuid_to_ts: dict[str, str] = {m.get("uuid", ""): m.get("timestamp", "") for m in messages}

    # Track tool_result outcomes keyed by tool_use_id
    tool_results: dict[str, dict] = {}
    for m in user_msgs:
        content = m.get("message", {}).get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                tid = block.get("tool_use_id", "")
                result_content = block.get("content", "")
                if isinstance(result_content, list):
                    result_content = json.dumps(result_content)
                is_error = block.get("is_error", False)
                tool_results[tid] = {
                    "result_type":       "error" if is_error else "success",
                    "result_size_bytes": len(str(result_content).encode("utf-8")),
                }

    models_seen: set[str] = set()

    for seq, m in enumerate(assistant_msgs, start=1):
        msg     = m.get("message", {})
        usage   = msg.get("usage", {})
        content = msg.get("content", []) or []

        model       = msg.get("model", "")
        message_id  = msg.get("id", "")
        stop_reason = msg.get("stop_reason", "")
        stu         = usage.get("server_tool_use", {})

        inp          = usage.get("input_tokens", 0) or 0
        out          = usage.get("output_tokens", 0) or 0
        cache_create = usage.get("cache_creation_input_tokens", 0) or 0
        cache_read   = usage.get("cache_read_input_tokens", 0) or 0
        cc           = usage.get("cache_creation", {}) or {}
        cache_1h     = cc.get("ephemeral_1h_input_tokens", 0) or 0
        cache_5m     = cc.get("ephemeral_5m_input_tokens", 0) or 0
        service_tier = usage.get("service_tier", "")
        speed        = usage.get("speed", "")

        cost = calc_cost(model, inp, out, cache_create, cache_read)

        if model:
            models_seen.add(model)

        # Content analysis
        has_thinking = 0
        thinking_chars = 0
        tool_use_count = 0
        output_text_chars = 0

        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type", "")
            if btype == "thinking":
                has_thinking = 1
                thinking_chars += len(block.get("thinking", "") or "")
            elif btype == "tool_use":
                tool_use_count += 1
                tool_input = block.get("input", {})
                tool_use_id = block.get("id", "")
                tool_name   = block.get("name", "")
                tool_input_size = len(json.dumps(tool_input).encode("utf-8"))
                result = tool_results.get(tool_use_id, {})
                tool_call_records.append({
                    "session_id":      m.get("sessionId", session_id),
                    "api_call_uuid":   m.get("uuid", ""),
                    "tool_use_id":     tool_use_id,
                    "tool_name":       tool_name,
                    "tool_input_size": tool_input_size,
                    "timestamp":       m.get("timestamp", ""),
                    "is_sidechain":    1 if m.get("isSidechain") else 0,
                    "result_type":     result.get("result_type"),
                    "result_size_bytes": result.get("result_size_bytes"),
                })
            elif btype == "text":
                output_text_chars += len(block.get("text", "") or "")

        api_call_records.append({
            "session_id":          m.get("sessionId", session_id),
            "message_id":          message_id,
            "uuid":                m.get("uuid", ""),
            "parent_uuid":         m.get("parentUuid"),
            "seq_in_session":      seq,
            "timestamp":           m.get("timestamp", ""),
            "model":               model,
            "stop_reason":         stop_reason,
            "service_tier":        service_tier,
            "speed":               speed,
            "input_tokens":        inp,
            "output_tokens":       out,
            "cache_creation_tokens": cache_create,
            "cache_read_tokens":   cache_read,
            "cache_1h_tokens":     cache_1h,
            "cache_5m_tokens":     cache_5m,
            "cost_usd":            cost,
            "web_search_requests": stu.get("web_search_requests", 0) or 0,
            "web_fetch_requests":  stu.get("web_fetch_requests", 0) or 0,
            "has_thinking":        has_thinking,
            "thinking_chars":      thinking_chars,
            "tool_use_count":      tool_use_count,
            "output_text_chars":   output_text_chars,
            "is_sidechain":        1 if m.get("isSidechain") else 0,
            "entrypoint":          m.get("entrypoint", entrypoint),
            "cwd":                 m.get("cwd", cwd),
            "git_branch":          m.get("gitBranch", git_branch),
            "claude_version":      m.get("version", claude_version),
        })

    # ── Aggregate session totals ───────────────────────────────────────────────
    def _sum(key: str) -> int:
        return sum(r.get(key, 0) or 0 for r in api_call_records)

    session_meta = {
        "session_id":                  session_id,
        "started_at":                  started_at,
        "ended_at":                    ended_at,
        "duration_seconds":            duration_seconds,
        "cwd":                         cwd,
        "project_slug":                os.path.basename(cwd) if cwd else "",
        "entrypoint":                  entrypoint,
        "git_branch":                  git_branch,
        "claude_version":              claude_version,
        "user_type":                   user_type,
        "total_input_tokens":          _sum("input_tokens"),
        "total_output_tokens":         _sum("output_tokens"),
        "total_cache_creation_tokens": _sum("cache_creation_tokens"),
        "total_cache_read_tokens":     _sum("cache_read_tokens"),
        "total_cache_1h_tokens":       _sum("cache_1h_tokens"),
        "total_cache_5m_tokens":       _sum("cache_5m_tokens"),
        "total_cost_usd":              sum(r.get("cost_usd", 0) for r in api_call_records),
        "total_turns":                 len(user_msgs),
        "total_api_calls":             len(api_call_records),
        "total_tool_calls":            len(tool_call_records),
        "total_thinking_blocks":       sum(r.get("has_thinking", 0) for r in api_call_records),
        "total_output_chars":          _sum("output_text_chars"),
        "models_used":                 json.dumps(sorted(models_seen)),
        "first_user_message":          first_user_message,
        "transcript_path":             path,
    }

    return {"session_meta": session_meta, "api_calls": api_call_records, "tool_calls": tool_call_records}

# ── DB writers ────────────────────────────────────────────────────────────────
def upsert_session(con: sqlite3.Connection, meta: dict) -> None:
    con.execute("""
        INSERT INTO sessions (
            session_id, started_at, ended_at, duration_seconds,
            cwd, project_slug, entrypoint, git_branch, claude_version, user_type,
            total_input_tokens, total_output_tokens, total_cache_creation_tokens,
            total_cache_read_tokens, total_cache_1h_tokens, total_cache_5m_tokens,
            total_cost_usd, total_turns, total_api_calls, total_tool_calls,
            total_thinking_blocks, total_output_chars,
            models_used, first_user_message, transcript_path, updated_at
        ) VALUES (
            :session_id, :started_at, :ended_at, :duration_seconds,
            :cwd, :project_slug, :entrypoint, :git_branch, :claude_version, :user_type,
            :total_input_tokens, :total_output_tokens, :total_cache_creation_tokens,
            :total_cache_read_tokens, :total_cache_1h_tokens, :total_cache_5m_tokens,
            :total_cost_usd, :total_turns, :total_api_calls, :total_tool_calls,
            :total_thinking_blocks, :total_output_chars,
            :models_used, :first_user_message, :transcript_path, datetime('now')
        )
        ON CONFLICT(session_id) DO UPDATE SET
            ended_at                    = excluded.ended_at,
            duration_seconds            = excluded.duration_seconds,
            total_input_tokens          = excluded.total_input_tokens,
            total_output_tokens         = excluded.total_output_tokens,
            total_cache_creation_tokens = excluded.total_cache_creation_tokens,
            total_cache_read_tokens     = excluded.total_cache_read_tokens,
            total_cache_1h_tokens       = excluded.total_cache_1h_tokens,
            total_cache_5m_tokens       = excluded.total_cache_5m_tokens,
            total_cost_usd              = excluded.total_cost_usd,
            total_turns                 = excluded.total_turns,
            total_api_calls             = excluded.total_api_calls,
            total_tool_calls            = excluded.total_tool_calls,
            total_thinking_blocks       = excluded.total_thinking_blocks,
            total_output_chars          = excluded.total_output_chars,
            models_used                 = excluded.models_used,
            transcript_path             = excluded.transcript_path,
            updated_at                  = datetime('now')
    """, meta)


def insert_api_calls(con: sqlite3.Connection, records: list[dict]) -> None:
    for r in records:
        con.execute("""
            INSERT OR IGNORE INTO api_calls (
                session_id, message_id, uuid, parent_uuid, seq_in_session, timestamp,
                model, stop_reason, service_tier, speed,
                input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens,
                cache_1h_tokens, cache_5m_tokens, cost_usd,
                web_search_requests, web_fetch_requests,
                has_thinking, thinking_chars, tool_use_count, output_text_chars,
                is_sidechain, entrypoint, cwd, git_branch, claude_version
            ) VALUES (
                :session_id, :message_id, :uuid, :parent_uuid, :seq_in_session, :timestamp,
                :model, :stop_reason, :service_tier, :speed,
                :input_tokens, :output_tokens, :cache_creation_tokens, :cache_read_tokens,
                :cache_1h_tokens, :cache_5m_tokens, :cost_usd,
                :web_search_requests, :web_fetch_requests,
                :has_thinking, :thinking_chars, :tool_use_count, :output_text_chars,
                :is_sidechain, :entrypoint, :cwd, :git_branch, :claude_version
            )
        """, r)


def insert_tool_calls(con: sqlite3.Connection, records: list[dict]) -> None:
    for r in records:
        con.execute("""
            INSERT INTO tool_calls (
                session_id, api_call_uuid, tool_use_id, tool_name, tool_input_size,
                timestamp, is_sidechain, result_type, result_size_bytes
            ) VALUES (
                :session_id, :api_call_uuid, :tool_use_id, :tool_name, :tool_input_size,
                :timestamp, :is_sidechain, :result_type, :result_size_bytes
            )
        """, r)


def rebuild_daily_summary(con: sqlite3.Connection, dates: list[str]) -> None:
    for date in set(dates):
        if not date:
            continue
        row = con.execute("""
            SELECT
                COUNT(DISTINCT s.session_id)             AS sessions_count,
                COUNT(a.id)                              AS api_calls_count,
                COALESCE(SUM(a.input_tokens), 0)         AS total_input_tokens,
                COALESCE(SUM(a.output_tokens), 0)        AS total_output_tokens,
                COALESCE(SUM(a.cache_creation_tokens), 0) AS total_cache_creation_tokens,
                COALESCE(SUM(a.cache_read_tokens), 0)    AS total_cache_read_tokens,
                COALESCE(SUM(a.cost_usd), 0)             AS total_cost_usd,
                json_group_array(DISTINCT s.cwd)         AS unique_cwds
            FROM sessions s
            JOIN api_calls a ON a.session_id = s.session_id
            WHERE substr(a.timestamp, 1, 10) = ?
        """, (date,)).fetchone()
        if row:
            con.execute("""
                INSERT INTO daily_summary (
                    date, sessions_count, api_calls_count,
                    total_input_tokens, total_output_tokens,
                    total_cache_creation_tokens, total_cache_read_tokens,
                    total_cost_usd, unique_cwds, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(date) DO UPDATE SET
                    sessions_count              = excluded.sessions_count,
                    api_calls_count             = excluded.api_calls_count,
                    total_input_tokens          = excluded.total_input_tokens,
                    total_output_tokens         = excluded.total_output_tokens,
                    total_cache_creation_tokens = excluded.total_cache_creation_tokens,
                    total_cache_read_tokens     = excluded.total_cache_read_tokens,
                    total_cost_usd              = excluded.total_cost_usd,
                    unique_cwds                 = excluded.unique_cwds,
                    updated_at                  = datetime('now')
            """, (date, row["sessions_count"], row["api_calls_count"],
                  row["total_input_tokens"], row["total_output_tokens"],
                  row["total_cache_creation_tokens"], row["total_cache_read_tokens"],
                  row["total_cost_usd"], row["unique_cwds"]))


def process_session(con: sqlite3.Connection, session_id: str, transcript_path: str) -> bool:
    parsed = parse_transcript(transcript_path)
    if not parsed or not parsed.get("session_meta"):
        return False

    meta       = parsed["session_meta"]
    api_calls  = parsed["api_calls"]
    tool_calls = parsed["tool_calls"]

    if not meta.get("session_id"):
        meta["session_id"] = session_id

    upsert_session(con, meta)
    insert_api_calls(con, api_calls)

    # tool_calls table: avoid duplicates on re-runs by checking (api_call_uuid, tool_use_id)
    existing_tool_keys = set()
    if tool_calls:
        rows = con.execute(
            "SELECT api_call_uuid || '|' || COALESCE(tool_use_id,'') FROM tool_calls WHERE session_id = ?",
            (meta["session_id"],)
        ).fetchall()
        existing_tool_keys = {r[0] for r in rows}

    new_tool_calls = [
        t for t in tool_calls
        if (t["api_call_uuid"] + "|" + (t["tool_use_id"] or "")) not in existing_tool_keys
    ]
    insert_tool_calls(con, new_tool_calls)

    dates = list({ts[:10] for c in api_calls if (ts := c.get("timestamp", ""))[:10]})
    rebuild_daily_summary(con, dates)
    con.commit()
    return True


# ── Entry points ──────────────────────────────────────────────────────────────
def run_hook() -> None:
    """Called as Stop hook: reads JSON from stdin."""
    raw = sys.stdin.read().strip()
    if not raw:
        sys.exit(0)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        sys.exit(0)

    session_id      = data.get("session_id", "")
    transcript_path = data.get("transcript_path", "")

    if not session_id or not transcript_path:
        sys.exit(0)

    con = open_db()
    try:
        process_session(con, session_id, transcript_path)
    finally:
        con.close()


def run_backfill(projects_dir: str) -> None:
    """Scan all JSONL files under projects_dir and ingest them."""
    con = open_db()
    base = Path(projects_dir)
    files = sorted(base.rglob("*.jsonl"))
    ok = skip = 0
    for f in files:
        # session_id is the stem of the file
        session_id = f.stem
        try:
            success = process_session(con, session_id, str(f))
            if success:
                ok += 1
            else:
                skip += 1
        except Exception as e:
            print(f"  WARN {f.name}: {e}", file=sys.stderr)
            skip += 1
    con.close()
    print(f"Backfill done: {ok} sessions ingested, {skip} skipped.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Claude Code token tracker")
    parser.add_argument("--backfill", metavar="PROJECTS_DIR",
                        help="Scan all JSONL files in PROJECTS_DIR and ingest into SQLite")
    args = parser.parse_args()

    if args.backfill:
        run_backfill(args.backfill)
    else:
        run_hook()
