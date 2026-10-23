#!/usr/bin/env python3
"""
Claude Code Token Tracker
Parses session JSONL files and persists token usage to SQLite.

Usage (Stop hook):  stdin JSON → { session_id, transcript_path, cwd }
Usage (backfill):   python3 tracker.py --backfill <projects_dir>
Usage (catch-up):   python3 tracker.py --catch-up
"""

# Keep annotations lazy so this file also runs on interpreters that predate
# PEP 604 unions (`X | Y` in annotations) — e.g. the Python 3.9 that macOS
# ships at /usr/bin/python3, which used to crash this file on import.
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import sqlite3
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import pricing

# ── Interpreter guard ─────────────────────────────────────────────────────────
# The Stop hook runs whatever `python3` resolves to on the user's PATH, and that
# differs from machine to machine. Never die with an obscure SyntaxError or
# TypeError: explain the problem and exit non-zero, because a non-zero hook exit
# code is what Claude Code surfaces back to the user.
MIN_PYTHON = (3, 7)

if sys.version_info < MIN_PYTHON:
    sys.stderr.write(
        "claude-token-tracker: unsupported interpreter "
        "Python {}.{}.{} ({})\n"
        "  Python {}.{}+ is required. Install a newer Python 3, make sure\n"
        "  `python3` on PATH resolves to it, then re-run ./install.sh\n"
        "  Usage is NOT being tracked until this is fixed.\n".format(
            sys.version_info[0], sys.version_info[1], sys.version_info[2],
            sys.executable, MIN_PYTHON[0], MIN_PYTHON[1],
        )
    )
    sys.exit(1)

DB_PATH = Path(os.environ.get("CLAUDE_TRACKER_DB",
               Path.home() / ".claude" / "token-tracker" / "token_stats.db"))

# Where Claude Code keeps its session transcripts (overridable for tests).
PROJECTS_DIR = Path(os.environ.get("CLAUDE_TRACKER_PROJECTS",
                  Path.home() / ".claude" / "projects"))

# Minimum seconds between two automatic catch-up scans (0 = every hook run).
CATCHUP_INTERVAL = float(os.environ.get("CLAUDE_TRACKER_CATCHUP_INTERVAL", "60"))

# ── Logging ───────────────────────────────────────────────────────────────────
# Without a log of its own the tool is undebuggable: the hook's stderr is
# swallowed by Claude Code and the dashboard's traceback only exists in whatever
# terminal (or pm2) happened to start it. Everything worth knowing is appended
# here instead, so `--doctor` can hand a support report to the user.
LOG_DIR          = Path(os.environ.get("CLAUDE_TRACKER_LOG_DIR", DB_PATH.parent / "logs"))
LOG_PATH         = LOG_DIR / "tracker.log"
DASHBOARD_LOG    = LOG_DIR / "dashboard.log"
LOG_MAX_BYTES    = int(os.environ.get("CLAUDE_TRACKER_LOG_MAX_BYTES", "1000000"))
LOG_BACKUPS      = 2
LOG_TAIL_LINES   = 120


def _rotate_log(path: Path) -> None:
    """Size-based rotation, best effort. Called right before an append."""
    try:
        if not path.exists() or path.stat().st_size < LOG_MAX_BYTES:
            return
        oldest = Path(f"{path}.{LOG_BACKUPS}")
        if oldest.exists():
            oldest.unlink()
        for i in range(LOG_BACKUPS - 1, 0, -1):
            src = Path(f"{path}.{i}")
            if src.exists():
                src.rename(Path(f"{path}.{i + 1}"))
        path.rename(Path(f"{path}.1"))
    except OSError:
        pass


def log_event(level: str, message: str) -> None:
    """Append one line to the tracker log. Never raises, never blocks work."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        _rotate_log(LOG_PATH)
        stamp = datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{stamp} {level.upper():<5} {message}\n")
    except Exception:
        pass


def log_exception(context: str, exc: BaseException, limit: int = 2000) -> None:
    """Log an exception with its traceback, truncated so the log stays small."""
    try:
        detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).strip()
    except Exception:
        detail = repr(exc)
    if len(detail) > limit:
        detail = detail[:limit] + " …[truncated]"
    log_event("ERROR", f"{context}: {detail}")

# ── Pricing ──────────────────────────────────────────────────────────────────
# The price book itself is data (pricing.json) so rates can be corrected
# without a code change, and a model may have different peak / idle prices
# depending on when the call happened. pricing.py owns the matching rules.
_PRICING_WARNED = False


def calc_cost(model: str, inp: int, out: int, cache_write: int, cache_read: int,
              when=None) -> float:
    """Cost in USD. `when` is the API call timestamp (peak/idle pricing)."""
    return pricing.DEFAULT.cost(model, inp, out, cache_write, cache_read, when=when)


def reprice_all(con: sqlite3.Connection) -> int:
    """
    Recompute every stored api_call cost from the current price book.

    Called once per price-book change (see migrate_pricing) and available as
    `--reprice`, so corrections to prices or the CNY/USD rate also fix history
    instead of leaving a mixed database behind.
    """
    rows = con.execute("""
        SELECT id, model, timestamp, input_tokens, output_tokens,
               cache_creation_tokens, cache_read_tokens, cost_usd
        FROM api_calls
    """).fetchall()
    updates = []
    for r in rows:
        new = calc_cost(r["model"], r["input_tokens"] or 0, r["output_tokens"] or 0,
                        r["cache_creation_tokens"] or 0, r["cache_read_tokens"] or 0,
                        when=r["timestamp"])
        if abs(new - (r["cost_usd"] or 0.0)) > 1e-9:
            updates.append((new, r["id"]))
    if not updates:
        return 0
    con.executemany("UPDATE api_calls SET cost_usd = ? WHERE id = ?", updates)
    con.execute("""
        UPDATE sessions SET total_cost_usd = COALESCE((
            SELECT SUM(cost_usd) FROM api_calls
            WHERE api_calls.session_id = sessions.session_id
        ), 0)
    """)
    dates = [r[0] for r in con.execute(
        "SELECT DISTINCT substr(timestamp, 1, 10) FROM api_calls WHERE timestamp <> ''")]
    rebuild_daily_summary(con, dates)
    con.commit()
    return len(updates)


def migrate_pricing(con: sqlite3.Connection) -> None:
    """Reprice when the price book changed; no-op on every other startup."""
    signature = pricing.DEFAULT.signature()
    if get_meta(con, "pricing_signature") == signature:
        return
    changed = reprice_all(con)
    set_meta(con, "pricing_signature", signature)
    con.commit()
    log_event("INFO", f"pricing migrate signature={signature} rows_repriced={changed}")

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
    im_source             TEXT,   -- 'cti' if launched via claude-to-im, else NULL
    session_name          TEXT,   -- user-renamed session name via /rename

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

-- Bookkeeping for the catch-up safety net: which transcript files have already
-- been ingested, and at what (mtime, size). Lets catch-up skip unchanged files
-- so it stays cheap enough to run from a hook or from the dashboard.
CREATE TABLE IF NOT EXISTS ingest_state (
    path        TEXT PRIMARY KEY,
    mtime       REAL,
    size        INTEGER,
    ingested_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tracker_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
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
def ensure_schema(con: sqlite3.Connection) -> None:
    """
    Create missing tables/indexes and apply column migrations.

    Any entry point (Stop hook, CLI, dashboard) can be the first thing to run
    after an upgrade, so every one of them must be able to bring an older
    database up to date on its own. Otherwise the dashboard 500s on a column the
    new queries expect but the old database does not have yet.
    """
    con.executescript(SCHEMA)
    cols = {r[1] for r in con.execute("PRAGMA table_info(sessions)")}
    if "im_source" not in cols:
        con.execute("ALTER TABLE sessions ADD COLUMN im_source TEXT")
    if "session_name" not in cols:
        con.execute("ALTER TABLE sessions ADD COLUMN session_name TEXT")
    con.commit()


def open_db(migrate: bool = True) -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # The hook, the CLI and the dashboard can all write at the same time now, so
    # wait for a busy database instead of failing instantly.
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout = 15000")
    ensure_schema(con)
    global _PRICING_WARNED
    if pricing.DEFAULT.error and not _PRICING_WARNED:
        _PRICING_WARNED = True
        log_event("WARN", f"pricing book unusable ({pricing.DEFAULT.error}); "
                          f"falling back to embedded Sonnet rates")
    if migrate:
        migrate_pricing(con)
    return con


# ── tracker_meta helpers ──────────────────────────────────────────────────────
def get_meta(con: sqlite3.Connection, key: str, default=None):
    row = con.execute("SELECT value FROM tracker_meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute("""
        INSERT INTO tracker_meta (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
    """, (key, str(value)))

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

    # Extract session name from /rename command
    session_name = None
    for m in messages:
        if m.get("type") == "system" and m.get("subtype") == "local_command":
            msg_content = m.get("content", "")
            if "/rename" in msg_content:
                match = re.search(r'<command-args>(.*?)</command-args>', msg_content, re.DOTALL)
                if match:
                    session_name = match.group(1).strip()
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

    # Newer Claude Code versions write ONE JSONL record per content block
    # (`apiBlockIndex` 0, 1, …). Every record of the same API response repeats
    # the same `message.id` and carries the SAME, complete `usage` object.
    # Counting each record separately multiplies the session total, so collapse
    # them: usage is read once, content blocks are merged for the content stats.
    grouped: dict[str, dict] = {}
    ordered_keys: list[str] = []
    for raw in assistant_msgs:
        raw_msg = raw.get("message", {}) or {}
        key = raw_msg.get("id") or raw.get("uuid") or ""
        if key not in grouped:
            grouped[key] = {"first": raw, "blocks": []}
            ordered_keys.append(key)
        blocks = raw_msg.get("content")
        if isinstance(blocks, list):
            grouped[key]["blocks"].extend(blocks)

    for seq, key in enumerate(ordered_keys, start=1):
        m       = grouped[key]["first"]
        msg     = m.get("message", {})
        usage   = msg.get("usage", {})
        content = grouped[key]["blocks"]

        model       = msg.get("model", "")
        message_id  = msg.get("id", "")
        stop_reason = msg.get("stop_reason", "")
        stu         = usage.get("server_tool_use", {})
        timestamp   = m.get("timestamp", "")

        inp          = usage.get("input_tokens", 0) or 0
        out          = usage.get("output_tokens", 0) or 0
        cache_create = usage.get("cache_creation_input_tokens", 0) or 0
        cache_read   = usage.get("cache_read_input_tokens", 0) or 0
        cc           = usage.get("cache_creation", {}) or {}
        cache_1h     = cc.get("ephemeral_1h_input_tokens", 0) or 0
        cache_5m     = cc.get("ephemeral_5m_input_tokens", 0) or 0
        service_tier = usage.get("service_tier", "")
        speed        = usage.get("speed", "")

        # `timestamp` matters: time-of-day models (DeepSeek) bill peak vs idle.
        cost = calc_cost(model, inp, out, cache_create, cache_read, when=timestamp)

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
        "im_source":                   None,  # filled in by run_hook() when CTI_RUNTIME is set
        "session_name":                session_name,
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
            models_used, first_user_message, transcript_path, im_source, session_name, updated_at
        ) VALUES (
            :session_id, :started_at, :ended_at, :duration_seconds,
            :cwd, :project_slug, :entrypoint, :git_branch, :claude_version, :user_type,
            :total_input_tokens, :total_output_tokens, :total_cache_creation_tokens,
            :total_cache_read_tokens, :total_cache_1h_tokens, :total_cache_5m_tokens,
            :total_cost_usd, :total_turns, :total_api_calls, :total_tool_calls,
            :total_thinking_blocks, :total_output_chars,
            :models_used, :first_user_message, :transcript_path, :im_source, :session_name, datetime('now')
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
            im_source                   = COALESCE(excluded.im_source, sessions.im_source),
            session_name                = COALESCE(excluded.session_name, sessions.session_name),
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


def process_session(con: sqlite3.Connection, session_id: str, transcript_path: str,
                    im_source: str | None = None) -> bool:
    parsed = parse_transcript(transcript_path)
    if not parsed or not parsed.get("session_meta"):
        return False

    meta       = parsed["session_meta"]
    api_calls  = parsed["api_calls"]
    tool_calls = parsed["tool_calls"]

    if not meta.get("session_id"):
        meta["session_id"] = session_id

    if im_source and not meta.get("im_source"):
        meta["im_source"] = im_source
    elif "im_source" not in meta:
        meta["im_source"] = None

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

    dates = list({c["timestamp"][:10] for c in api_calls if c.get("timestamp")})
    rebuild_daily_summary(con, dates)
    con.commit()
    return True


# ── Catch-up safety net ───────────────────────────────────────────────────────
# The Stop hook is best-effort: headless/SDK runs may never fire it, and when it
# does fire the transcript for the turn being finished is not always flushed to
# disk yet. Catch-up closes both gaps — it re-scans ~/.claude/projects and
# ingests every transcript that is new or has changed since the previous scan.
def _mark_ingested(con: sqlite3.Connection, path: Path, st: os.stat_result) -> None:
    con.execute("""
        INSERT INTO ingest_state (path, mtime, size, ingested_at)
        VALUES (?, ?, ?, datetime('now'))
        ON CONFLICT(path) DO UPDATE SET
            mtime       = excluded.mtime,
            size        = excluded.size,
            ingested_at = datetime('now')
    """, (str(path), st.st_mtime, st.st_size))


def _needs_ingest(con: sqlite3.Connection, path: Path, st: os.stat_result) -> bool:
    row = con.execute(
        "SELECT mtime, size FROM ingest_state WHERE path = ?", (str(path),)
    ).fetchone()
    if row is None:
        return True
    if abs((row["mtime"] or 0.0) - st.st_mtime) > 1e-6:
        return True
    return (row["size"] or -1) != st.st_size


def catch_up(con: sqlite3.Connection, force: bool = False,
             projects_dir: Path | str | None = None) -> int:
    """
    Ingest every transcript under the projects dir that is new or modified since
    the last scan. Idempotent (upsert + INSERT OR IGNORE), so it doubles as a
    repair pass for sessions the hook recorded too early.

    `force` skips the minimum-interval throttle; unchanged files are still
    skipped (use --backfill to re-read everything unconditionally).
    Returns the number of transcripts ingested.
    """
    base = Path(projects_dir) if projects_dir else PROJECTS_DIR
    if not base.is_dir():
        return 0

    if not force:
        try:
            last = float(get_meta(con, "last_catchup", "0") or 0)
        except (TypeError, ValueError):
            last = 0.0
        if time.time() - last < CATCHUP_INTERVAL:
            return 0

    ingested = 0
    try:
        files = sorted(base.rglob("*.jsonl"))
    except OSError:
        return 0

    for f in files:
        try:
            st = f.stat()
        except OSError:
            continue
        if not _needs_ingest(con, f, st):
            continue
        try:
            if process_session(con, f.stem, str(f)):
                ingested += 1
        except Exception as e:
            # Do not mark this file as ingested — retry on the next scan.
            print(f"claude-token-tracker: catch-up failed for {f.name}: {e}",
                  file=sys.stderr)
            log_exception(f"catch-up {f.name}", e)
            continue
        _mark_ingested(con, f, st)

    set_meta(con, "last_catchup", str(time.time()))
    con.commit()
    if ingested:
        log_event("INFO", f"catch-up ingested={ingested}")
    return ingested


def run_reprice() -> None:
    """Recompute every stored cost from the current price book (--reprice)."""
    con = open_db(migrate=False)
    try:
        changed = reprice_all(con)
        set_meta(con, "pricing_signature", pricing.DEFAULT.signature())
        con.commit()
        total = con.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM api_calls").fetchone()[0]
    finally:
        con.close()
    log_event("INFO", f"reprice signature={pricing.DEFAULT.signature()} rows={changed}")
    print(f"Pricing book  : {pricing.DEFAULT.version} ({pricing.DEFAULT.signature()})")
    print(f"Rows repriced : {changed}")
    print(f"Total cost    : ${total:.4f}")


def run_catch_up(force: bool = True, projects_dir: Path | str | None = None) -> int:
    """Open the DB and run one catch-up scan (used by --catch-up and the dashboard)."""
    con = open_db()
    try:
        return catch_up(con, force=force, projects_dir=projects_dir)
    finally:
        con.close()


# ── Diagnostics (--doctor) ────────────────────────────────────────────────────
# Goal: a user who cannot debug anything can run one command and hand back a
# file that answers "what version, what state, what failed" — without leaking
# their prompts, transcripts, or credentials.
_REDACT_PATTERNS = (
    (re.compile(r"(?i)\b(sk-[A-Za-z0-9_\-]{8,})"), "sk-***"),
    (re.compile(r"\b(gh[pousr])_[A-Za-z0-9]{16,}"), r"\1_***"),
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-]{8,}"), r"\1 ***"),
    # KEY=value — the key may carry a prefix (ANTHROPIC_AUTH_TOKEN), where a
    # leading \b does not match before the `_`, so match the identifier instead.
    (re.compile(r"(?i)([A-Za-z0-9_]{0,40}(?:api[_-]?key|auth[_-]?token|access[_-]?token"
                r"|secret|password|credential)[A-Za-z0-9_]{0,20})\"?\s*[:=]\s*[^\s,;]+"),
     r"\1=***"),
    # Weaker names (token / key / pwd …) but only when the value is quoted, so
    # diagnostics such as total_cache_read_tokens=42 survive untouched.
    (re.compile(r"(?i)\b([A-Za-z0-9_]{0,40}(?:token|key|secret|password|passwd|pwd)"
                r"[A-Za-z0-9_]{0,20})\"?\s*[:=]\s*([\"'])[^\"'\n]*\2"),
     r"\1=***"),
)


def redact(text: str) -> str:
    """Strip credentials and user-identifying paths from anything we export."""
    if not text:
        return ""
    out = str(text).replace(str(Path.home()), "~")
    out = re.sub(r"/(?:Users|home)/[^/\s:\"']+", "~", out)
    for pat, rep in _REDACT_PATTERNS:
        out = pat.sub(rep, out)
    return out


def _tail(path: Path, lines: int = LOG_TAIL_LINES) -> list[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            rows = f.readlines()
        return [r.rstrip("\n") for r in rows[-lines:]]
    except OSError:
        return []


def _hook_report() -> list[str]:
    settings = Path.home() / ".claude" / "settings.json"
    lines = [f"settings file : {settings} ({'present' if settings.exists() else 'MISSING'})"]
    if not settings.exists():
        return lines
    try:
        with open(settings, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        lines.append(f"  cannot parse  : {e}")
        return lines

    groups = data.get("hooks", {}).get("Stop", []) or []
    ours, others = [], 0
    for g in groups:
        for h in g.get("hooks", []) or []:
            cmd = h.get("command", "")
            if "token-tracker" in cmd or re.search(r"hook\.sh|tracker\.py", cmd):
                ours.append(cmd)
            else:
                others += 1
    lines.append(f"Stop hooks    : {len(ours)} from this tool, {others} from something else")
    for cmd in ours:
        lines.append(f"  registered  : {cmd}")
    if not ours:
        lines.append("  registered  : NONE — the Stop hook is not installed")
    return lines


def _claude_code_report() -> list[str]:
    lines = []
    upd = Path.home() / ".claude" / ".last-update-result.json"
    if upd.exists():
        try:
            with open(upd, encoding="utf-8") as f:
                d = json.load(f)
            lines.append(f"last update   : {d.get('version_from')} → {d.get('version_to')} "
                         f"at {d.get('timestamp')} ({d.get('outcome')})")
        except Exception:
            pass
    lines.append(f"projects dir  : {PROJECTS_DIR} "
                 f"({'present' if PROJECTS_DIR.is_dir() else 'MISSING'})")
    if PROJECTS_DIR.is_dir():
        files = list(PROJECTS_DIR.rglob("*.jsonl"))
        lines.append(f"transcripts   : {len(files)}")
        if files:
            import datetime as _dt
            newest = max(files, key=lambda p: p.stat().st_mtime)
            oldest = min(files, key=lambda p: p.stat().st_mtime)
            lines.append(f"  newest      : {_dt.datetime.fromtimestamp(newest.stat().st_mtime)}")
            lines.append(f"  oldest      : {_dt.datetime.fromtimestamp(oldest.stat().st_mtime)}")
            lines.append("  note        : Claude Code deletes transcripts after ~30 days")
    return lines


def _dir_writable(path: Path) -> bool:
    """
    Ground truth beats os.access: sandboxes and ACLs can disagree with it, and a
    false "not writable" sends support down the wrong path. The probe file is
    removed immediately and holds no data.
    """
    if not path.exists():
        return False
    probe = path / ".token-tracker-write-probe"
    try:
        with open(probe, "a"):
            pass
        return True
    except OSError:
        return os.access(path, os.W_OK)
    finally:
        try:
            probe.unlink()
        except OSError:
            pass


def _db_report() -> list[str]:
    lines = [f"path          : {DB_PATH}",
             f"exists        : {DB_PATH.exists()}"
             + (f" ({DB_PATH.stat().st_size / 1048576:.1f} MB)" if DB_PATH.exists() else ""),
             f"dir writable  : {_dir_writable(DB_PATH.parent)}"]
    if not DB_PATH.exists():
        return lines
    try:
        con = sqlite3.connect(DB_PATH, timeout=10)
        con.row_factory = sqlite3.Row
        # Diagnostics must never modify anything.
        con.execute("PRAGMA query_only = ON")
    except Exception as e:
        lines.append(f"cannot open   : {e}")
        return lines
    try:
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        lines.append(f"tables        : {', '.join(sorted(tables)) or '(none)'}")
        for t in ("sessions", "api_calls", "tool_calls", "daily_summary",
                  "ingest_state", "tracker_meta"):
            if t not in tables:
                lines.append(f"  MISSING     : {t}")
        if "sessions" in tables:
            cols = {r[1] for r in con.execute("PRAGMA table_info(sessions)")}
            missing = sorted({"session_id", "im_source", "session_name", "transcript_path"} - cols)
            lines.append(f"sessions cols : {len(cols)}"
                         + (f", MISSING {', '.join(missing)}" if missing else ", complete"))
        for t in ("sessions", "api_calls", "tool_calls", "daily_summary", "ingest_state"):
            if t in tables:
                lines.append(f"  rows {t:<14}: {con.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]}")
        if "sessions" in tables:
            r = con.execute("SELECT MAX(started_at), MAX(updated_at) FROM sessions").fetchone()
            lines.append(f"latest session: {r[0]} (row updated {r[1]})")
        if "api_calls" in tables:
            lines.append(f"latest api call: {con.execute('SELECT MAX(timestamp) FROM api_calls').fetchone()[0]}")
        if "tracker_meta" in tables:
            r = con.execute("SELECT value FROM tracker_meta WHERE key='last_catchup'").fetchone()
            if r and r[0]:
                import datetime as _dt
                lines.append("last catch-up : "
                             f"{_dt.datetime.fromtimestamp(float(r[0]))}")
    except Exception as e:
        lines.append(f"query failed  : {type(e).__name__}: {e}")
    finally:
        con.close()
    return lines


def _pricing_report() -> list[str]:
    """Which price book is in force, and which stored models it does not know."""
    info = pricing.DEFAULT.summary()
    lines = [f"book          : {info['path']}",
             f"version       : {info['version']} (signature {info['signature']})",
             f"models        : {len(info['models'])} — {', '.join(info['models'])}",
             f"time-of-day   : {', '.join(info['timed_models']) or '(none)'}",
             f"fallback      : {info['fallback']} (used for unmatched model names)"]
    fx = info.get("fx") or {}
    if fx:
        lines.append("fx            : "
                     + ", ".join(f"1 {pricing.DEFAULT.base_currency} = {v} {k}"
                                 for k, v in fx.items()))
    if info.get("error"):
        lines.append(f"  ERROR       : {info['error']} — using embedded rates")
    for name, url in (pricing.DEFAULT.sources or {}).items():
        lines.append(f"source        : {name} {url}")
    if not DB_PATH.exists():
        return lines
    try:
        con = sqlite3.connect(DB_PATH, timeout=5)
        con.execute("PRAGMA query_only = ON")
        rows = con.execute(
            "SELECT model, COUNT(*) FROM api_calls GROUP BY model").fetchall()
        unknown = [(m or "(empty)", n) for m, n in rows if not pricing.DEFAULT.known(m or "")]
        if unknown:
            lines.append("unpriced      : "
                         + ", ".join(f"{m} ×{n}" for m, n in unknown))
            lines.append("                these are billed at the fallback model — "
                         "add them to pricing.json if the number matters")
    except Exception as e:
        lines.append(f"  query failed: {type(e).__name__}: {e}")
    finally:
        try:
            con.close()
        except Exception:
            pass
    return lines


def build_report() -> str:
    out: list[str] = []

    def add(text: str = "") -> None:
        out.append(redact(text))

    def section(title: str) -> None:
        add()
        add(f"── {title} " + "─" * max(3, 60 - len(title)))

    add("Claude Token Tracker — diagnostics report")
    add(f"generated  : {datetime.now().astimezone().strftime('%Y-%m-%dT%H:%M:%S%z')}")
    add("contents   : versions, file/schema state, counters and log lines only.")
    add("             No prompts, messages, transcript contents or API keys.")

    section("Runtime")
    add(f"python        : {sys.version.split()[0]} ({sys.implementation.name})")
    add(f"executable    : {sys.executable}")
    add(f"platform      : {platform.platform()}")
    add(f"cwd           : {os.getcwd()}")
    for var in ("CLAUDE_TRACKER_DB", "CLAUDE_TRACKER_PROJECTS", "CLAUDE_TRACKER_LOG_DIR",
                "CLAUDE_TRACKER_CATCHUP_INTERVAL", "CLAUDE_TRACKER_NO_CATCHUP", "CTI_RUNTIME"):
        add(f"{var:<14}: {os.environ.get(var, '(unset)')}")

    section("Installation")
    repo = Path(__file__).resolve().parent
    add(f"repo dir      : {repo}")
    try:
        import updater as _updater
        _v = _updater.local_state()
        add(f"version       : {_v.get('version') or 'untagged'}"
            f" ({_v.get('short') or '?'}, {_v.get('date') or 'n/a'},"
            f" branch {_v.get('branch') or '?'}"
            f"{', dirty' if _v.get('dirty') else ''})")
    except Exception as e:
        add(f"version       : unavailable ({type(e).__name__}: {e})")
    for name in ("tracker.py", "hook.sh", "server.py", "stats.py", "install.sh",
                 "pricing.json", "pricing.py", "updater.py", "templates/index.html"):
        p = repo / name
        if not p.exists():
            add(f"  {name:<20}: MISSING")
        else:
            mode = oct(p.stat().st_mode)[-3:]
            add(f"  {name:<20}: present (mode {mode})")

    section("Hook registration")
    for line in _hook_report():
        add(line)

    section("Claude Code")
    for line in _claude_code_report():
        add(line)

    section("Database")
    for line in _db_report():
        add(line)

    section("Pricing")
    for line in _pricing_report():
        add(line)

    tlog = _tail(LOG_PATH)
    dlog = _tail(DASHBOARD_LOG, 60)
    hook_runs = [l for l in tlog if "hook session=" in l]
    errors = [l for l in tlog + dlog if " ERROR " in l or "Traceback" in l]

    section("Activity")
    add(f"tracker log   : {LOG_PATH} ({'present' if LOG_PATH.exists() else 'not created yet'})")
    add(f"dashboard log : {DASHBOARD_LOG} ({'present' if DASHBOARD_LOG.exists() else 'not created yet'})")
    add(f"hook runs     : {len(hook_runs)} in the last {LOG_TAIL_LINES} log lines")
    add(f"last hook run : {hook_runs[-1] if hook_runs else '(none logged)'}")
    add(f"errors        : {len(errors)} in the last {LOG_TAIL_LINES} log lines")
    for e in errors[-8:]:
        add(f"  {e}")

    section(f"{LOG_PATH.name} (last {LOG_TAIL_LINES} lines)")
    for line in tlog or ["(empty)"]:
        add(f"  {line}")

    section(f"{DASHBOARD_LOG.name} (last 60 lines)")
    for line in dlog or ["(empty)"]:
        add(f"  {line}")

    section("Send this to support")
    add("Everything above is safe to share. To save it as a file:")
    add(f"  python3 {Path(__file__).resolve()} --doctor --out ~/Desktop/token-tracker-report.txt")
    return "\n".join(out)


def run_doctor(out_path: str | None = None) -> None:
    report = build_report()
    if not out_path:
        print(report)
        return
    target = Path(os.path.expanduser(out_path))
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(report + "\n", encoding="utf-8")
    except OSError as e:
        print(f"claude-token-tracker: could not write {target}: {e}", file=sys.stderr)
        print(report)
        sys.exit(1)
    print(f"Diagnostics written to {target}")
    print("Send that file to support — it contains no prompts, messages or API keys.")


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
        log_event("WARN", f"hook payload missing session_id/transcript_path "
                          f"(keys={sorted(data)})")
        sys.exit(0)

    # Detect IM origin: claude-to-im passes CTI_* env vars to the Claude subprocess,
    # which the Stop hook inherits. CTI_RUNTIME being set is the reliable signal.
    im_source = "cti" if os.environ.get("CTI_RUNTIME") else None

    try:
        con = open_db()
        try:
            processed = process_session(con, session_id, transcript_path,
                                        im_source=im_source)

            # Remember the transcript we just handled so the catch-up below does
            # not parse it a second time.
            try:
                p = Path(transcript_path)
                _mark_ingested(con, p, p.stat())
                con.commit()
            except OSError:
                pass

            # Safety net: pick up sessions whose Stop hook never fired (headless/SDK
            # runs) plus turns that were not flushed to disk yet when this hook ran.
            try:
                catch_up(con)
            except Exception as e:
                print(f"claude-token-tracker: catch-up failed: {e}", file=sys.stderr)
                log_exception("hook catch-up", e)

            row = con.execute(
                "SELECT session_id, total_api_calls, total_input_tokens, total_output_tokens "
                "FROM sessions WHERE transcript_path = ?", (transcript_path,)).fetchone()
            if row:
                log_event("INFO", f"hook session={row['session_id']} "
                                  f"calls={row['total_api_calls']} "
                                  f"in={row['total_input_tokens']} "
                                  f"out={row['total_output_tokens']}")
            else:
                log_event("WARN", f"hook session={session_id} processed={int(bool(processed))} "
                                  f"no row written for {Path(transcript_path).name}")
        finally:
            con.close()
    except Exception as e:
        log_exception(f"hook session={session_id}", e)
        # Non-zero exit so Claude Code surfaces the failure instead of hiding it.
        print(f"claude-token-tracker: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)


def run_backfill(projects_dir: str) -> None:
    """Scan all JSONL files under projects_dir and ingest them."""
    log_event("INFO", f"backfill start dir={projects_dir}")
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
            # Record the revision we just read so catch-up can skip it later.
            try:
                _mark_ingested(con, f, f.stat())
            except OSError:
                pass
        except Exception as e:
            print(f"  WARN {f.name}: {e}", file=sys.stderr)
            log_exception(f"backfill {f.name}", e)
            skip += 1
    set_meta(con, "last_catchup", str(time.time()))
    con.commit()
    con.close()
    log_event("INFO", f"backfill done ingested={ok} skipped={skip}")
    print(f"Backfill done: {ok} sessions ingested, {skip} skipped.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Claude Code token tracker")
    parser.add_argument("--backfill", metavar="PROJECTS_DIR",
                        help="Scan all JSONL files in PROJECTS_DIR and ingest into SQLite")
    parser.add_argument("--catch-up", action="store_true",
                        help="Ingest transcripts that are new or changed since the last scan")
    parser.add_argument("--doctor", action="store_true",
                        help="Print a shareable diagnostics report (no prompts or secrets)")
    parser.add_argument("--out", metavar="FILE",
                        help="With --doctor: write the report to FILE instead of stdout")
    parser.add_argument("--reprice", action="store_true",
                        help="Recompute every stored cost from pricing.json (after a price edit)")
    args = parser.parse_args()

    if args.doctor:
        run_doctor(args.out)
    elif args.reprice:
        run_reprice()
    elif args.backfill:
        run_backfill(args.backfill)
    elif args.catch_up:
        n = run_catch_up(force=True)
        print(f"Catch-up done: {n} session(s) ingested.")
    else:
        run_hook()
