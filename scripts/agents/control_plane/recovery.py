"""Startup reconciliation: real git worktrees, real PIDs, real write locks.

Run once when the daemon starts (and safe to re-run any time) so a restart
never spawns a duplicate agent for a task the durable store still thinks is
``RUNNING`` when its process actually died, and never leaves a task claiming a
live worktree lock that a crashed process abandoned.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from .models import TASK_QUEUED, TASK_RUNNING, WorktreeRecord
from .state import State

_WRITE_LOCK_NAME = ".write-lock"
_AGENT_OUTPUT_DIRNAME = ".agent-output"


def pid_is_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but is owned by someone else; treat as alive.
        return True
    return True


def discover_git_worktrees(repo_root: Path) -> list[WorktreeRecord]:
    """Parse ``git worktree list --porcelain`` into durable rows.

    Never raises on a missing/broken git: an empty list is returned and the
    caller keeps whatever worktree rows are already on disk, since this is
    reconciliation, not the sole source of truth.
    """

    try:
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return []

    records: list[WorktreeRecord] = []
    path: str | None = None
    branch: str | None = None
    locked = False
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            if path is not None:
                records.append(WorktreeRecord(path=path, branch=branch, locked=locked))
            path = line.removeprefix("worktree ").strip()
            branch = None
            locked = False
        elif line.startswith("branch "):
            branch = line.removeprefix("branch ").strip()
        elif line == "locked" or line.startswith("locked "):
            locked = True
    if path is not None:
        records.append(WorktreeRecord(path=path, branch=branch, locked=locked))
    return records


def _stale_write_lock_holder(lock_path: Path) -> tuple[str | None, bool]:
    """Return ``(holder_text, is_stale)`` for a write-lock file, if present.

    Mirrors ``runner._active_lock_holder``'s PID-liveness technique without
    importing that private helper, so recovery can report a stale lock even
    when it chooses not to delete it itself (deletion stays ``runner.py``'s
    job during an actual run, to avoid two modules racing to remove one file).
    """

    if not lock_path.exists():
        return None, False
    try:
        holder = lock_path.read_text(encoding="utf-8").strip()
    except OSError:  # pragma: no cover - unreadable lock file
        return None, False
    match = re.search(r"\bpid=(\d+)\b", holder)
    if not match:
        return holder or "unknown holder", False
    return holder, not pid_is_alive(int(match.group(1)))


def reconcile_worktree_locks(
    state: State, worktrees: list[WorktreeRecord], *, project_id: str | None = None
) -> list[WorktreeRecord]:
    """Annotate each discovered worktree with its real write-lock state and persist it.

    ENG-CP-03 (issue #165): ``worktrees`` is always a snapshot of a *single*
    repository, so both the prior-state lookup and the persisted replacement are
    scoped to ``project_id``. Without that scoping, observing Project A's
    worktrees would delete every worktree row belonging to Project B.
    """

    prior = {Path(item.path).resolve(): item for item in state.list_worktrees(project_id=project_id)}
    annotated: list[WorktreeRecord] = []
    for worktree in worktrees:
        lock_path = Path(worktree.path) / _AGENT_OUTPUT_DIRNAME / _WRITE_LOCK_NAME
        holder, stale = _stale_write_lock_holder(lock_path)
        saved = prior.get(Path(worktree.path).resolve())
        released = False
        # Only an already-adopted worktree and a normal orchestrator lock
        # signature prove ownership strongly enough for automatic release.
        if stale and saved and saved.managed and holder and re.match(r"^[\w.-]+\s+pid=\d+\s+at=\d+$", holder):
            try:
                lock_path.unlink()
                released = True
                state.record_event(
                    category="recovery", level="warning",
                    message=f"released stale orchestrator write lock at {lock_path} (holder was {holder})",
                    project_id=project_id,
                )
            except OSError:
                released = False
        record = WorktreeRecord(
            path=worktree.path,
            branch=worktree.branch,
            locked=bool(holder) and not stale,
            lock_holder=holder if (holder and not stale) else None,
            managed=bool(saved and saved.managed),
            stale_lock=bool(stale),
            stale_lock_holder=holder if stale else None,
            project_id=project_id,
            # ENG-AGENT-14: Git discovery does not observe PR/head origin.
            # Copy durable stamps onto the in-memory snapshot so refresh
            # and GET /api/worktrees do not depend on replace_worktrees
            # mutating these objects later.
            review_repository=saved.review_repository if saved else None,
            review_pr=saved.review_pr if saved else None,
            review_head_sha=saved.review_head_sha if saved else None,
        )
        if released:
            record.locked = False
        annotated.append(record)
    state.replace_worktrees(annotated, project_id=project_id)
    return annotated


def reconcile_tasks(state: State, *, project_id: str | None = None) -> dict[str, int]:
    """Reclaim tasks marked ``RUNNING`` whose recorded PID is no longer alive.

    A task with a live PID is left untouched (never duplicated). A task with a
    dead/missing PID is requeued if it has none, or marked ``FAILED`` if a
    successful requeue is unsafe to infer automatically (default: requeue, on
    the theory that a killed daemon should retry rather than silently drop
    work; the scheduler's normal concurrency/priority rules apply on retry).
    """

    reclaimed = 0
    left_running = 0
    for task in state.list_tasks(state=TASK_RUNNING, project_id=project_id):
        if pid_is_alive(task.pid):
            left_running += 1
            continue
        task.state = TASK_QUEUED
        task.pid = None
        task.stale_recovered = True
        task.last_error = (task.last_error or "") + " | reclaimed on daemon restart: recorded PID was not alive"
        state.upsert_task(task)
        state.record_event(
            category="recovery",
            task_id=task.id,
            level="warning",
            message="reclaimed RUNNING task with a dead PID on daemon restart",
            project_id=task.project_id,
        )
        reclaimed += 1
    return {"reclaimed": reclaimed, "left_running": left_running}


def run_recovery(state: State, repo_root: Path, *, project_id: str | None = None) -> dict[str, object]:
    """Full startup reconciliation pass for one project. Safe to call repeatedly.

    ENG-CP-03 (issue #165): ``project_id`` names the managed project whose
    ``repo_root`` is being observed, and is threaded all the way into
    :func:`reconcile_worktree_locks` -> ``State.replace_worktrees``. Passing it
    is what makes startup recovery *scoped*: an independent review (Grok/xAI)
    caught that omitting it here made every daemon start run an unscoped
    ``DELETE FROM worktrees``, which both destroyed a second project's
    persisted worktree rows and rewrote the just-migrated OctaScene rows back
    to ``project_id IS NULL`` -- hiding them from every project-scoped read for
    the life of the process.
    """

    worktrees = discover_git_worktrees(repo_root)
    if worktrees:
        annotated = reconcile_worktree_locks(state, worktrees, project_id=project_id)
    else:
        # ``discover_git_worktrees`` returns [] when the Git observation itself
        # failed (a real repository always reports at least its main worktree),
        # so an empty snapshot means "could not observe", never "there are
        # none". Replacing rows from it would delete the persisted worktrees
        # and insert nothing. ``refresh_worktree_statuses`` already made this
        # distinction; an independent review (Grok/xAI) caught that startup
        # recovery did not, leaving the same data-loss class this slice's
        # scoping was meant to close.
        annotated = state.list_worktrees(project_id=project_id)
    task_summary = reconcile_tasks(state, project_id=project_id)
    state.record_event(
        category="recovery",
        level="info",
        message=f"startup recovery: {len(annotated)} worktrees observed, {task_summary}",
        project_id=project_id,
    )
    return {"worktrees": annotated, "tasks": task_summary}
