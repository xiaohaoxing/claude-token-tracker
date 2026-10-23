#!/usr/bin/env python3
"""
Release-driven version discovery and one-click upgrade.

The project ships releases as `vMAJOR.MINOR.PATCH` git tags. The dashboard never
follows the branch tip: "is there a newer version?" means "is there a tag I do
not contain yet?", and "upgrade" means fast-forwarding to that tag. Commits
pushed to `main` between releases are therefore invisible to users, which is the
point — they run what was tagged, not whatever landed last.

Everything here is read-only unless upgrade() is called, and that refuses
anything that could lose local work (dirty tree, wrong branch, diverged
history). Access control lives in server.py; process restart lives there too.
"""

from __future__ import annotations

import os
import re
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

TAG_GLOB = "v[0-9]*"
_TAG_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")

_STATE_TTL = 5.0
_lock = threading.Lock()
_local_cache: Dict[str, object] = {"at": 0.0, "state": None}
_remote_cache: Dict[str, object] = {
    "checked_at": 0.0, "ok": False, "error": "not checked yet",
    "latest_tag": None, "latest_version": None, "latest_sha": None,
    "ff_possible": None, "already": None,
}


# ── versions ──────────────────────────────────────────────────────────────────
def parse_version(tag: Optional[str]) -> Optional[Tuple[int, int, int]]:
    m = _TAG_RE.match((tag or "").strip())
    return tuple(int(x) for x in m.groups()) if m else None  # type: ignore[return-value]


def is_release_tag(tag: Optional[str]) -> bool:
    return parse_version(tag) is not None


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
        return 127, "", "git is not installed"
    except subprocess.TimeoutExpired:
        return 124, "", f"git {args[0]} timed out after {timeout:.0f}s"
    except OSError as exc:
        return 1, "", str(exc)


def _git_available() -> bool:
    return shutil.which("git") is not None and (REPO_DIR / ".git").exists()


def _is_ancestor(ancestor: str, descendant: str) -> bool:
    rc, _, _ = _git("merge-base", "--is-ancestor", ancestor, descendant)
    return rc == 0


def _invalidate_local() -> None:
    with _lock:
        _local_cache["at"] = 0.0
        _local_cache["state"] = None


# ── local state ───────────────────────────────────────────────────────────────
def local_state() -> Dict[str, object]:
    """Which release this checkout is on. Cached briefly; cheap to call often."""
    if not _git_available():
        return {
            "is_git": False, "head": None, "short": None, "date": None,
            "branch": None, "dirty": False, "tag": None, "ahead": 0,
            "version": None, "error": "git is not installed or this is not a git checkout",
        }
    with _lock:
        cached = _local_cache.get("state")
        fresh = time.time() - float(_local_cache.get("at") or 0) < _STATE_TTL
        if fresh and cached:
            return dict(cached)  # type: ignore[arg-type]

    rc_head, head, err = _git("rev-parse", "HEAD")
    if rc_head != 0:
        state = {"is_git": True, "head": None, "short": None, "date": None,
                 "branch": None, "dirty": False, "tag": None, "ahead": 0,
                 "version": None, "error": f"cannot read HEAD: {err}"}
    else:
        rc_b, branch, _ = _git("rev-parse", "--abbrev-ref", "HEAD")
        rc_d, date, _ = _git("show", "-s", "--format=%cI", "HEAD")
        rc_s, dirty_out, _ = _git("status", "--porcelain")
        rc_t, tag, _ = _git("describe", "--tags", "--match", TAG_GLOB, "--abbrev=0")
        tag = tag if rc_t == 0 and is_release_tag(tag) else None
        ahead = 0
        if tag:
            rc_n, n, _ = _git("rev-list", "--count", f"{tag}..HEAD")
            if rc_n == 0 and n.isdigit():
                ahead = int(n)
        state = {
            "is_git": True,
            "head": head,
            "short": head[:7],
            "date": (date[:10] if rc_d == 0 else None),
            "branch": branch if rc_b == 0 else None,
            "dirty": bool(dirty_out),
            "tag": tag,
            "ahead": ahead,
            # "+N" means: N commits past the last release, i.e. unreleased code.
            "version": (tag if ahead == 0 else f"{tag}+{ahead}") if tag else None,
            "error": None,
        }
    with _lock:
        _local_cache["state"] = state
        _local_cache["at"] = time.time()
    return dict(state)


# ── remote releases ───────────────────────────────────────────────────────────
def _remote_tags(timeout: float) -> Tuple[Dict[str, str], Optional[str]]:
    rc, out, err = _git("ls-remote", "--tags", REMOTE, timeout=timeout)
    if rc != 0:
        return {}, (err or f"git ls-remote failed ({rc})")
    tags: Dict[str, str] = {}
    for line in out.splitlines():
        fields = line.split()
        if len(fields) < 2 or not fields[1].startswith("refs/tags/"):
            continue
        sha, name = fields[0], fields[1][len("refs/tags/"):]
        peeled = name.endswith("^{}")
        if peeled:                      # annotated tag: prefer the commit it points at
            name = name[:-3]
        if not is_release_tag(name):
            continue
        if name not in tags or peeled:
            tags[name] = sha
    return tags, None


def check_remote(force: bool = False, timeout: float = FETCH_TIMEOUT) -> Dict[str, object]:
    """Refresh the cached newest release. Blocking — call from a thread, not a request."""
    with _lock:
        age = time.time() - float(_remote_cache["checked_at"] or 0)
        if not force and age < CHECK_EVERY and _remote_cache["checked_at"]:
            return dict(_remote_cache)

    if not _git_available():
        result = {"checked_at": time.time(), "ok": False,
                  "error": "not a git checkout", "latest_tag": None,
                  "latest_version": None, "latest_sha": None,
                  "ff_possible": None, "already": None}
        with _lock:
            _remote_cache.update(result)
        return dict(result)

    tags, err = _remote_tags(timeout)
    latest = max(tags, key=lambda t: (parse_version(t), t)) if tags else None
    result: Dict[str, object] = {
        "checked_at": time.time(),
        "ok": bool(latest) and err is None,
        "error": err if latest is None else (None if latest else "no release tag on the remote"),
        "latest_tag": latest,
        "latest_version": latest,
        "latest_sha": tags.get(latest) if latest else None,
        "ff_possible": None,
        "already": None,
    }
    if latest:
        local = local_state()
        behind = (local.get("tag") is None
                  or parse_version(latest) > parse_version(local.get("tag")))  # type: ignore[operator]
        if behind:
            # Make the tag's commit available so we can answer "can we ff onto it?"
            _git("fetch", "--tags", "--quiet", REMOTE, timeout=max(60.0, timeout))
            result["ff_possible"] = _is_ancestor("HEAD", latest)
            result["already"] = _is_ancestor(latest, "HEAD")
        else:
            result["ff_possible"] = False
            result["already"] = True
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
def _blocker(local: Dict[str, object], remote: Dict[str, object],
             update_available: bool) -> Optional[str]:
    if not local.get("is_git"):
        return "This install is not a git checkout — update it by hand"
    if local.get("error"):
        return str(local["error"])
    if local.get("branch") != BRANCH:
        return f"On branch {local.get('branch')!r}; releases are installed on {BRANCH}"
    if local.get("dirty"):
        return "Working tree has uncommitted changes"
    if not remote.get("checked_at"):
        return "The version check has not run yet"
    if not remote.get("ok"):
        return f"Cannot reach the remote: {remote.get('error')}"
    if update_available and remote.get("ff_possible") is False:
        return "Local commits are not part of that release — resolve by hand"
    return None


def status() -> Dict[str, object]:
    """Everything the dashboard needs to render the version pill."""
    local = local_state()
    remote = remote_state()
    latest = remote.get("latest_tag")
    update_available = None
    if latest:
        update_available = bool(local.get("tag") != latest and not remote.get("already"))
    blocker = _blocker(local, remote, bool(update_available))
    return {
        "current": local.get("version"),
        "current_date": local.get("date"),
        "branch": local.get("branch"),
        "dirty": bool(local.get("dirty")),
        "is_git": bool(local.get("is_git")),
        "latest": latest,
        "update_available": update_available,
        "checked_at": remote.get("checked_at") or None,
        "check_error": remote.get("error") if not remote.get("ok") else None,
        "can_upgrade": bool(update_available) and blocker is None,
        "blocker": blocker,
    }


def upgrade() -> Dict[str, object]:
    """Fast-forward onto the newest release tag. Refuses anything that loses work."""
    local = local_state()
    remote = remote_state()
    if not remote.get("checked_at") or not remote.get("ok"):
        remote = check_remote(force=True, timeout=30)
    latest = remote.get("latest_tag")
    if not latest:
        return {"ok": False, "error": "No release tag found on the remote"}

    update_available = bool(local.get("tag") != latest and not remote.get("already"))
    if not update_available:
        return {"ok": True, "from": local.get("version"), "to": local.get("version"),
                "up_to_date": True}

    blocker = _blocker(local, remote, True)
    if blocker:
        return {"ok": False, "error": blocker}

    rc, out, err = _git("fetch", "--tags", "--quiet", REMOTE, timeout=60)
    if rc != 0:
        return {"ok": False, "error": f"git fetch failed: {err or out}"}
    rc, _, _ = _git("rev-parse", "-q", "--verify", f"refs/tags/{latest}^{{commit}}")
    if rc != 0:
        return {"ok": False, "error": f"Release tag {latest} is missing after fetch"}
    if not _is_ancestor("HEAD", latest):
        return {"ok": False,
                "error": f"HEAD is not an ancestor of {latest}; upgrading would drop local commits"}

    rc, out, err = _git("merge", "--ff-only", latest, timeout=60)
    if rc != 0:
        return {"ok": False, "error": f"Fast-forward failed: {err or out}"}

    _invalidate_local()
    after = local_state()
    check_remote(force=True)
    return {
        "ok": True,
        "from": local.get("version"),
        "to": after.get("version"),
        "up_to_date": False,
    }
