"""ENG-AO-07 (issue #17): durable single-writer lease for runbook advancement.

Two Octarel processes (the daemon and a dashboard) once advanced the same runbook into acceptance and
ran the exact-tree gate concurrently in one managed worktree. Exactly one process may advance a given
runbook at a time; this module is the ownership mechanism.

The lease is an OS advisory ``flock`` on a per-runbook file beside the state database. The kernel
releases it when the owning process exits for any reason (crash, ``kill -9``), so a stale owner can
never wedge a runbook -- there is no pid/heartbeat bookkeeping to go stale. The file body is only an
informational owner record for observers. The lease is re-entrant within one thread so that
``reconcile_runbooks`` may call ``advance_after_success`` while already holding it. A non-owner never
waits: it observes state and skips, then re-reads the runbook after it (later) wins the lease.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import threading
from collections.abc import Iterator
from pathlib import Path

from .models import utc_now_iso
from .state import State

LEASE_DIRNAME = "advancement-leases"
_HELD = threading.local()
_MEMORY_LOCKS: dict[str, threading.Lock] = {}
_MEMORY_GUARD = threading.Lock()


def lease_dir(state: State) -> Path | None:
    """Lease directory beside the state DB; ``None`` for an in-memory database."""

    if str(state.db_path) == ":memory:":
        return None
    return state.db_path.parent / LEASE_DIRNAME


def _lease_path(state: State, runbook_id: str) -> Path | None:
    base = lease_dir(state)
    if base is None:
        return None
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in runbook_id)[:80]
    digest = hashlib.sha256(runbook_id.encode("utf-8")).hexdigest()[:12]  # distinct ids never share a lease
    return base / f"{safe}-{digest}.lock"


def _held() -> set[str]:
    if not hasattr(_HELD, "keys"):
        _HELD.keys = set()
    return _HELD.keys


def lease_owner(state: State, runbook_id: str) -> dict[str, object] | None:
    """Best-effort informational owner record (never authoritative; the flock is)."""

    path = _lease_path(state, runbook_id)
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


@contextlib.contextmanager
def advancement_lease(state: State, runbook_id: str, *, holder: str = "reconcile") -> Iterator[bool]:
    """Try to become the sole advancer of ``runbook_id``; yields ``True`` only for the owner.

    Never blocks. On ``False`` the caller must not mutate or advance the runbook.
    """

    path = _lease_path(state, runbook_id)
    key = f"{path}" if path is not None else f"mem:{id(state)}:{runbook_id}"
    held = _held()
    if key in held:  # re-entrant within this thread
        yield True
        return
    if path is None:
        with _MEMORY_GUARD:
            lock = _MEMORY_LOCKS.setdefault(key, threading.Lock())
        if not lock.acquire(blocking=False):
            yield False
            return
        held.add(key)
        try:
            yield True
        finally:
            held.discard(key)
            lock.release()
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        record = json.dumps({"pid": os.getpid(), "holder": holder, "acquired_at": utc_now_iso()})
        os.ftruncate(fd, 0)
        os.pwrite(fd, record.encode("utf-8"), 0)
        held.add(key)
        try:
            yield True
        finally:
            held.discard(key)
            with contextlib.suppress(OSError):
                os.ftruncate(fd, 0)
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# --- daemon authority ---------------------------------------------------------------------------------
# The daemon is the authoritative advancement owner. It holds this flock for its whole lifetime (the
# kernel drops it if the daemon dies), so a dashboard can cheaply tell "a live daemon advances runbooks"
# and act as a pure observer. The per-runbook lease above remains the correctness guarantee either way.

DAEMON_AUTHORITY_FILENAME = "daemon-authority.lock"
_DAEMON_FDS: dict[str, int] = {}


def _authority_path(state: State) -> Path | None:
    if str(state.db_path) == ":memory:":
        return None
    return state.db_path.parent / DAEMON_AUTHORITY_FILENAME


def claim_daemon_authority(state: State) -> bool:
    """Called by the daemon at start-up; ``False`` means another live daemon already owns it."""

    path = _authority_path(state)
    if path is None:
        return True
    if str(path) in _DAEMON_FDS:
        return True
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return False
    os.ftruncate(fd, 0)
    os.pwrite(fd, json.dumps({"pid": os.getpid(), "acquired_at": utc_now_iso()}).encode("utf-8"), 0)
    _DAEMON_FDS[str(path)] = fd
    return True


def release_daemon_authority(state: State) -> None:
    path = _authority_path(state)
    fd = _DAEMON_FDS.pop(str(path), None) if path is not None else None
    if fd is not None:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def daemon_authority_active(state: State) -> bool:
    """True when a *different* process currently holds daemon authority (this process's own claim does not count)."""

    path = _authority_path(state)
    if path is None or str(path) in _DAEMON_FDS:
        return False
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:  # never claimed, or removed meanwhile: no live daemon
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)
