#!/usr/bin/env python3
"""
Version discovery and one-click upgrade for the dashboard.

The dashboard is usually installed from a git checkout, so "is there a newer
version?" is a comparison between the local HEAD and the remote branch tip, and
"upgrade" is a fast-forward merge. Both are delegated to git rather than to a
hand-rolled downloader: a user's local commits, a dirty tree or a diverged
branch must all end in a clear refusal, never in lost work.

Everything here is read-only unless upgrade() is called; that function refuses
unless the checkout is clean, on the expected branch and behind the remote.
The caller is responsible for the access control (server.py limits it to
requests from this machine) and for restarting the process afterwards.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict, Optional, Tuple

REPO_DIR = Path(__file__).resolve().parent
REMOTE = os.environ.get("CLAUDE_TRACKER_REMOTE", "origin")
BRANCH = os.environ.get("CLAUDE_TRACKER_BRANCH", "main")
CHECK_EVERY = float(os.environ.get("CLAUDE_TRACKER_UPDATE_CHECK", "900"))
FETCH_TIMEOUT = float(os.environ.get("CLAUDE_TRACKER_GIT_TIMEOUT", "20"))

_STATE_TTL = 5.0
_lock = threading.Lock()
_local_cache: Dict[str, object] = {"at": 0.0, "state": None}
_remote_cache: Dict[str, object] = {
    "checked_at": 0.0, "head": None, "short": None, "ok": False, "error": "尚未检查",
}


# ── git plumbing ──────────────────────────────────────────────────────────────
def _git(*args: str, timeout: float = FETCH_TIMEOUT) -> Tuple[int, str, str]:
    env = dict(os.environ)
    # Never let a credential prompt hang the dashboard or a background thread.
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes -o ConnectTimeout=8")
    try:
        p = subprocess.run(["git", "-C", str(REPO_DIR), *args],
                           capture_output=True, text=True, timeout=timeout, env=env)
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except FileNotFoundError:
        return 127, "", "git 未安装"
    except subprocess.TimeoutExpired:
        return 124, "", f"git {args[0]} 超时（{timeout:.0f}s）"
    except OSError as exc:
        return 1, "", str(exc)


def _git_available() -> bool:
    return shutil.which("git") is not None and (REPO_DIR / ".git").exists()


def local_state() -> Dict[str, object]:
    """Where the checkout currently is. Cheap enough to call per request (cached)."""
    if not _git_available():
        return {
            "is_git": False, "head": None, "short": None, "date": None,
            "branch": None, "dirty": False,
            "error": "git 未安装或本目录不是 git 仓库（ZIP 安装）",
        }
    with _lock:
        cached = _local_cache.get("state")
        fresh = time.time() - float(_local_cache.get("at") or 0) < _STATE_TTL
        if fresh and cached:
            return dict(cached)  # type: ignore[arg-type]

    rc_head, head, err = _git("rev-parse", "HEAD")
    if rc_head != 0:
        state = {"is_git": True, "head": None, "short": None, "date": None,
                 "branch": None, "dirty": False, "error": f"无法读取 HEAD: {err}"}
    else:
        rc_b, branch, _ = _git("rev-parse", "--abbrev-ref", "HEAD")
        rc_d, date, _ = _git("show", "-s", "--format=%cI", "HEAD")
        rc_s, dirty_out, _ = _git("status", "--porcelain")
        state = {
            "is_git": True,
            "head": head,
            "short": head[:7],
            "date": (date[:10] if rc_d == 0 else None),
            "branch": branch if rc_b == 0 else None,
            "dirty": bool(dirty_out),
            "error": None,
        }
    with _lock:
        _local_cache["state"] = state
        _local_cache["at"] = time.time()
    return dict(state)


# ── remote tip ────────────────────────────────────────────────────────────────
def check_remote(force: bool = False, timeout: float = FETCH_TIMEOUT) -> Dict[str, object]:
    """Refresh the cached remote tip. Blocking — call it from a thread, not a request."""
    with _lock:
        age = time.time() - float(_remote_cache["checked_at"] or 0)
        if not force and age < CHECK_EVERY and _remote_cache["checked_at"]:
            return dict(_remote_cache)

    if not _git_available():
        result = {"checked_at": time.time(), "head": None, "short": None, "ok": False,
                  "error": "不是 git 仓库，无法检查更新"}
        with _lock:
            _remote_cache.update(result)
        return dict(result)

    rc, out, err = _git("ls-remote", REMOTE, f"refs/heads/{BRANCH}", timeout=timeout)
    head = out.split()[0] if rc == 0 and out else None
    result = {
        "checked_at": time.time(),
        "head": head,
        "short": head[:7] if head else None,
        "ok": rc == 0 and bool(head),
        "error": None if rc == 0 and head else (err or "远端没有该分支"),
    }
    with _lock:
        _remote_cache.update(result)
    return dict(result)


def remote_state() -> Dict[str, object]:
    with _lock:
        return dict(_remote_cache)


def start_background_checks() -> None:
    """Poll the remote in the background so no request ever waits on the network."""
    if not _git_available():
        return

    def loop() -> None:
        time.sleep(3)  # let the dashboard finish booting first
        while True:
            try:
                check_remote(force=True)
            except Exception:
                pass
            time.sleep(max(60.0, CHECK_EVERY))

    threading.Thread(target=loop, name="update-check", daemon=True).start()


# ── public API ────────────────────────────────────────────────────────────────
def _upgrade_blocker(local: Dict[str, object], remote: Dict[str, object]) -> Optional[str]:
    if not local.get("is_git"):
        return "该安装不是 git 仓库（ZIP 安装），请手动下载新版本"
    if local.get("error"):
        return str(local["error"])
    if local.get("branch") != BRANCH:
        return f"当前分支是 {local.get('branch')!r}，只支持在 {BRANCH} 上升级"
    if local.get("dirty"):
        return "工作区有未提交的改动，请先提交或 stash 后再升级"
    if not remote.get("ok"):
        return f"无法连接远端仓库：{remote.get('error') or '未知错误'}"
    if not remote.get("checked_at"):
        return "尚未完成版本检查"
    if local.get("head") == remote.get("head"):
        return None  # 已是最新，由 update_available 表达
    return None


def status() -> Dict[str, object]:
    """Everything the dashboard needs to render the version pill."""
    local = local_state()
    remote = remote_state()
    update_available = None
    if local.get("is_git") and remote.get("ok") and local.get("head") and remote.get("head"):
        update_available = local["head"] != remote["head"]
    blocker = _upgrade_blocker(local, remote)
    return {
        "current": local.get("short"),
        "current_date": local.get("date"),
        "branch": local.get("branch"),
        "dirty": bool(local.get("dirty")),
        "is_git": bool(local.get("is_git")),
        "latest": remote.get("short"),
        "update_available": update_available,
        "checked_at": remote.get("checked_at") or None,
        "check_error": remote.get("error") if not remote.get("ok") else None,
        "can_upgrade": bool(update_available) and blocker is None,
        "blocker": blocker,
    }


def upgrade() -> Dict[str, object]:
    """Fast-forward to the remote tip. Refuses anything that could lose work."""
    local = local_state()
    remote = remote_state()
    if not remote.get("ok") or not remote.get("checked_at"):
        remote = check_remote(force=True, timeout=30)
    blocker = _upgrade_blocker(local, remote)
    if blocker:
        return {"ok": False, "error": blocker}
    if local.get("head") == remote.get("head"):
        return {"ok": True, "from": local.get("short"), "to": local.get("short"),
                "up_to_date": True}

    rc, out, err = _git("fetch", REMOTE, BRANCH, timeout=60)
    if rc != 0:
        return {"ok": False, "error": f"git fetch 失败：{err or out}"}

    rc, out, err = _git("merge", "--ff-only", f"{REMOTE}/{BRANCH}", timeout=60)
    if rc != 0:
        detail = err or out
        return {"ok": False,
                "error": f"无法快进合并（本地可能有分叉提交），请在安装目录手动处理：{detail}"}

    # The merge output itself is noise for the UI; report the new position.
    with _lock:
        _local_cache["at"] = 0.0
        _local_cache["state"] = None
    after = local_state()
    check_remote(force=True)
    return {
        "ok": True,
        "from": local.get("short"),
        "to": after.get("short"),
        "up_to_date": local.get("head") == after.get("head"),
    }
