"""Bounded developer operations for ENG-AGENT-02-S9.

All Git targets are resolved from ``git worktree list --porcelain`` and every
subprocess uses a fixed argv.  This module intentionally offers no arbitrary
command/path/remote escape hatch.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import signal
import socket
import subprocess
import uuid
from pathlib import Path
from typing import Any

from scripts.ci.local_gate import read_latest_gate
from scripts.ci.runtime_paths import has_non_runtime_changes

from ..redaction import redact_text
from .models import TASK_TERMINAL_STATES, WorktreeRecord, task_projection, utc_now_iso
from .recovery import discover_git_worktrees, pid_is_alive, reconcile_worktree_locks

OP_STATES = frozenset({"IDLE", "QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "BLOCKED", "CONFLICT", "CANCELLED"})
GIT_ACTIONS = frozenset({"fetch", "pull", "push", "prepare_merge", "merge", "refresh"})
APP_ACTIONS = frozenset({"start", "stop", "restart"})


class OperationError(ValueError):
    pass


def _run(argv: list[str], cwd: Path, *, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False)


def _worktree_dirty(path: Path) -> bool:
    # ENG-AGENT-16 (issue #146): Control Plane runtime/evidence writes
    # (.agent-output/, .orchestrator-state/, .local-gate/) must never make a
    # worktree read as "dirty" here -- they are not product changes, and a
    # historical worktree's own tracked .gitignore may predate one or more of
    # them, so --untracked-files=all alone is not sufficient.
    return has_non_runtime_changes(path)


def _git(cwd: Path, *args: str, timeout: int = 15) -> str | None:
    try:
        result = _run(["git", *args], cwd, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _resolved_discovered(repo_root: Path, requested: str) -> WorktreeRecord:
    target = Path(requested).resolve()
    for record in discover_git_worktrees(repo_root):
        if Path(record.path).resolve() == target:
            return record
    raise OperationError("path is not a discovered worktree of this repository")


def worktree_status(repo_root: Path, record: WorktreeRecord, *, managed: bool, tasks: list[Any], registry: Any) -> dict[str, Any]:
    path = Path(record.path).resolve()
    head = _git(path, "rev-parse", "HEAD")
    branch_ref = record.branch or ""
    branch = branch_ref.removeprefix("refs/heads/") or None
    subject = _git(path, "show", "-s", "--format=%s", "HEAD")
    commit_at = _git(path, "show", "-s", "--format=%cI", "HEAD")
    dirty = _worktree_dirty(path)
    upstream = _git(path, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    ahead = behind = None
    remote_head = remote_time = None
    if upstream:
        counts = _git(path, "rev-list", "--left-right", "--count", f"{upstream}...HEAD")
        if counts and len(counts.split()) == 2:
            behind, ahead = (int(value) for value in counts.split())
        remote_head = _git(path, "rev-parse", upstream)
        remote_time = _git(path, "show", "-s", "--format=%cI", upstream)
    matching = [task for task in tasks if task.worktree and Path(task.worktree).resolve() == path]
    matching.sort(key=lambda item: item.updated_at, reverse=True)
    assigned = matching[0] if matching else None
    worker = registry.workers.get(assigned.worker) if assigned else None
    main_record = next(
        (item for item in discover_git_worktrees(repo_root) if (item.branch or "").removeprefix("refs/heads/") in {"main", "master"}),
        None,
    )
    main_head = _git(Path(main_record.path), "rev-parse", "HEAD") if main_record else None
    merged = None
    if head and main_head:
        merged = _run(["git", "merge-base", "--is-ancestor", head, main_head], path).returncode == 0
    process_alive = bool(assigned and assigned.pid and pid_is_alive(assigned.pid))
    canonical = bool(main_record and Path(main_record.path).resolve() == path)
    if canonical:
        classification, protected_reason = "UNRELATED_MANUAL", "canonical main checkout is never removable"
    elif assigned and assigned.state == "RUNNING" and process_alive:
        classification, protected_reason = "ACTIVE", "live task process owns this worktree"
    elif assigned and assigned.stale_recovered:
        classification, protected_reason = "STALE_RECOVERABLE", "dead task PID was reconciled and is safe to retry"
    elif assigned and assigned.state in {"PENDING", "QUEUED"}:
        classification, protected_reason = "QUEUED", "queued task owns this worktree"
    elif assigned and assigned.state == "PAUSED":
        classification, protected_reason = "PAUSED", "paused task owns this worktree"
    elif assigned and assigned.state == "RUNNING":
        classification, protected_reason = "STALE_RECOVERABLE", "durable RUNNING state has no live PID"
    elif dirty and (managed or assigned):
        classification, protected_reason = "FINISHED_DIRTY", "uncommitted changes are protected"
    elif not managed and not assigned:
        classification, protected_reason = "UNRELATED_MANUAL", "not adopted by the orchestrator"
    elif merged is True and not dirty and (not assigned or assigned.state in TASK_TERMINAL_STATES):
        classification, protected_reason = "FINISHED_CLEAN", None
    else:
        classification, protected_reason = "UNKNOWN", "merged/ownership state is not deterministically safe"
    return {
        "display_name": branch or path.name,
        "path": str(path),
        "branch": branch,
        "branch_ref": branch_ref or None,
        "management": "MANAGED" if managed or assigned else "DISCOVERED",
        "read_only": not (managed or assigned),
        "head_sha": head,
        "head_short": head[:8] if head else None,
        "commit_subject": subject,
        "dirty": dirty,
        "merged": merged,
        "upstream": upstream,
        "ahead": ahead,
        "behind": behind,
        "last_local_commit_at": commit_at,
        "remote_head_sha": remote_head,
        "remote_head_commit_at": remote_time,
        "remote_head_label": "Remote head commit time" if remote_time else "Remote head not available locally",
        "locked": record.locked,
        "lock_holder": record.lock_holder,
        "task": assigned.task_ref if assigned else None,
        "task_id": assigned.id if assigned else None,
        "task_state": assigned.state if assigned else None,
        "worker": assigned.worker if assigned else None,
        "execution_system": worker.execution_system if worker else None,
        "provider": worker.provider if worker else None,
        "model": worker.default_model if worker else None,
        "intensity": worker.default_intensity if worker else None,
        "pid": assigned.pid if assigned else None,
        "process_alive": process_alive,
        "task_projection": task_projection(assigned.state) if assigned else None,
        "classification": classification,
        "cleanup_eligible": classification == "FINISHED_CLEAN",
        "protected_reason": protected_reason,
        "canonical_checkout": canonical,
        "stale_lock": record.stale_lock,
        "stale_lock_holder": record.stale_lock_holder,
        # ENG-AGENT-14 (issue #140): an auto-provisioned review checkout's
        # origin, exact bound head, and reclaim eligibility, surfaced
        # honestly rather than the prior static "NOT_REPORTED" placeholder.
        "pr": record.review_pr if record.review_pr is not None else "NOT_REPORTED",
        "review_repository": record.review_repository,
        "review_pr": record.review_pr,
        "review_head_sha": record.review_head_sha,
        "review_head_matches_current": (
            (head == record.review_head_sha) if (record.review_head_sha and head) else None
        ),
        "local_gate": "PASS" if read_latest_gate(path).get("ready") else "NOT_READY",
        "updated_at": record.updated_at,
    }


def refresh_worktree_statuses(ctx: Any) -> list[dict[str, Any]]:
    """Observe the *selected project's* worktrees. ENG-CP-03 (issue #165).

    Every Git observation, persisted row, and cached row here is scoped to the
    selected project's own checkout (``ctx.project_root``, which falls back to
    ``ctx.repo_root`` only when no project is registered at all -- never to this
    process's cwd). Switching projects therefore re-observes the newly selected
    repository instead of showing the previous one's worktrees.
    """

    project_id = ctx.selected_project_id
    root = ctx.project_root
    discovered = discover_git_worktrees(root)
    if discovered:
        annotated = reconcile_worktree_locks(ctx.state, discovered, project_id=project_id)
    else:
        annotated = ctx.state.list_worktrees(project_id=project_id)
    persisted = {Path(w.path).resolve(): w for w in ctx.state.list_worktrees(project_id=project_id)}
    owned_elsewhere = {
        Path(w.path).resolve()
        for w in ctx.state.list_worktrees()
        if w.project_id is not None and w.project_id != project_id
    }
    tasks = ctx.state.list_tasks(project_id=project_id)
    rows = []
    for record in annotated:
        if Path(record.path).resolve() in owned_elsewhere:
            continue
        saved = persisted.get(Path(record.path).resolve())
        record.managed = bool(saved and saved.managed)
        if saved:
            record.review_repository = saved.review_repository
            record.review_pr = saved.review_pr
            record.review_head_sha = saved.review_head_sha
        rows.append(worktree_status(root, record, managed=record.managed, tasks=tasks, registry=ctx.registry))
    ctx.state.set_project_setting("worktree_status_cache", json.dumps(rows), project_id)
    return rows


def list_worktree_statuses(ctx: Any) -> list[dict[str, Any]]:
    """Read the last explicit/startup Git observation without spawning tools."""

    project_id = ctx.selected_project_id
    try:
        cached = json.loads(
            ctx.state.get_project_setting("worktree_status_cache", "[]", project_id=project_id) or "[]"
        )
    except (TypeError, ValueError):
        cached = []
    by_path = {Path(row["path"]).resolve(): row for row in cached if row.get("path")}
    tasks = ctx.state.list_tasks(project_id=project_id)
    rows: list[dict[str, Any]] = []
    for record in ctx.state.list_worktrees(project_id=project_id):
        path = Path(record.path).resolve()
        row = dict(by_path.get(path, {}))
        assigned = next((task for task in tasks if task.worktree and Path(task.worktree).resolve() == path), None)
        worker = ctx.registry.workers.get(assigned.worker) if assigned else None
        # Durable review-checkout origin lives on the persisted row. The Git
        # observation cache is optional and is empty after a restart or a
        # Playwright fixture reset; GET /api/worktrees must still render it.
        review_pr = record.review_pr
        review_repository = record.review_repository
        review_head_sha = record.review_head_sha
        cached_head = row.get("head_sha")
        row.update(
            {
                "display_name": row.get("display_name") or (record.branch or path.name).removeprefix("refs/heads/"),
                "path": str(path),
                "branch": row.get("branch") or (record.branch or "").removeprefix("refs/heads/") or None,
                "management": "MANAGED" if record.managed or assigned else "DISCOVERED",
                "read_only": not (record.managed or assigned),
                "locked": record.locked,
                "lock_holder": record.lock_holder,
                "task": assigned.task_ref if assigned else None,
                "task_id": assigned.id if assigned else None,
                "task_state": assigned.state if assigned else None,
                "worker": assigned.worker if assigned else None,
                "execution_system": worker.execution_system if worker else None,
                "provider": worker.provider if worker else None,
                "model": worker.default_model if worker else None,
                "intensity": worker.default_intensity if worker else None,
                "pid": assigned.pid if assigned else None,
                "process_alive": bool(assigned and assigned.pid and pid_is_alive(assigned.pid)),
                "task_projection": task_projection(assigned.state) if assigned else None,
                "stale_lock": record.stale_lock,
                "stale_lock_holder": record.stale_lock_holder,
                "pr": review_pr if review_pr is not None else "NOT_REPORTED",
                "review_repository": review_repository,
                "review_pr": review_pr,
                "review_head_sha": review_head_sha,
                "review_head_matches_current": (
                    (cached_head == review_head_sha)
                    if (review_head_sha and cached_head)
                    else row.get("review_head_matches_current")
                ),
            }
        )
        process_alive = bool(assigned and assigned.pid and pid_is_alive(assigned.pid))
        canonical = bool(row.get("canonical_checkout")) or row.get("branch") in {"main", "master"}
        if canonical:
            classification, protected_reason = "UNRELATED_MANUAL", "canonical main checkout is never removable"
        elif assigned and assigned.state == "RUNNING" and process_alive:
            classification, protected_reason = "ACTIVE", "live task process owns this worktree"
        elif assigned and assigned.stale_recovered:
            classification, protected_reason = "STALE_RECOVERABLE", "dead task PID was reconciled and is safe to retry"
        elif assigned and assigned.state in {"PENDING", "QUEUED"}:
            classification, protected_reason = "QUEUED", "queued task owns this worktree"
        elif assigned and assigned.state == "PAUSED":
            classification, protected_reason = "PAUSED", "paused task owns this worktree"
        elif assigned and assigned.state == "RUNNING":
            classification, protected_reason = "STALE_RECOVERABLE", "durable RUNNING state has no live PID"
        elif row.get("dirty") and (record.managed or assigned):
            classification, protected_reason = "FINISHED_DIRTY", "uncommitted changes are protected"
        elif not record.managed and not assigned:
            classification, protected_reason = "UNRELATED_MANUAL", "not adopted by the orchestrator"
        elif row.get("merged") is True and not row.get("dirty") and (not assigned or assigned.state in TASK_TERMINAL_STATES):
            classification, protected_reason = "FINISHED_CLEAN", None
        else:
            classification, protected_reason = "UNKNOWN", "merged/ownership state is not deterministically safe"
        row.update({
            "classification": classification,
            "cleanup_eligible": classification == "FINISHED_CLEAN",
            "protected_reason": protected_reason,
            "canonical_checkout": canonical,
        })
        rows.append(row)
    return rows


def cleanup_worktrees(ctx: Any, *, confirm: bool = False) -> dict[str, Any]:
    """Preview or remove only deterministically FINISHED_CLEAN worktrees."""

    rows = refresh_worktree_statuses(ctx)
    eligible = [row for row in rows if row.get("cleanup_eligible") and not row.get("canonical_checkout")]
    protected = [
        {"path": row["path"], "classification": row.get("classification", "UNKNOWN"),
         "reason": row.get("protected_reason") or "not cleanup eligible"}
        for row in rows if row not in eligible
    ]
    preview = {
        "mode": "APPLY" if confirm else "PREVIEW",
        "eligible": eligible,
        "protected": protected,
        "removed": [],
    }
    if not confirm:
        return preview
    # ENG-CP-03: Git worktree remove/prune must run in the selected project's
    # checkout, not the Control Plane's. Until CPX-05 those two may coincide;
    # once a second project is selected they must not. Never remove the live
    # Control Plane checkout even if it is not the selected project.
    git_root = ctx.project_root
    for row in eligible:
        target = Path(row["path"]).resolve()
        if target == git_root.resolve() or target == ctx.repo_root.resolve():
            protected.append({"path": str(target), "classification": "ACTIVE", "reason": "Control Center checkout is running here"})
            continue
        result = _run(["git", "worktree", "remove", str(target)], git_root, timeout=120)
        if result.returncode == 0:
            preview["removed"].append(str(target))
            ctx.state.record_event(
                category="worktrees",
                message=f"safely removed finished clean worktree {target}",
                project_id=ctx.selected_project_id,
            )
        else:
            protected.append({"path": str(target), "classification": row.get("classification"), "reason": redact_text(result.stderr or result.stdout or "git worktree remove failed")})
    _run(["git", "worktree", "prune"], git_root, timeout=30)
    refresh_repository_health(ctx)
    return preview


def refresh_repository_health(ctx: Any) -> dict[str, Any]:
    project_id = ctx.selected_project_id
    worktrees = refresh_worktree_statuses(ctx)
    default_branch = _selected_default_branch(ctx)
    origin_default = f"origin/{default_branch}"
    main_path = next((Path(row["path"]) for row in worktrees if row.get("branch") in {default_branch, "main", "master"}), ctx.project_root)
    remote_main = _git(main_path, "rev-parse", origin_default)
    remote_main_at = _git(main_path, "show", "-s", "--format=%cI", origin_default) if remote_main else None
    merge_lines = (_git(main_path, "log", origin_default, "--merges", "-5", "--format=%H%x1f%cI%x1f%s") or "").splitlines()
    recent_merges = []
    for line in merge_lines:
        parts = line.split("\x1f", 2)
        if len(parts) == 3:
            recent_merges.append({"sha": parts[0], "merged_at": parts[1], "subject": parts[2]})
    body = {
        "observed_at": utc_now_iso(),
        "main": {
            "remote_head_sha": remote_main,
            "remote_head_commit_at": remote_main_at,
            "remote_time_label": "Remote head commit time",
            "dirty_worktree_count": sum(1 for row in worktrees if row.get("dirty")),
            "active_worker_count": sum(1 for row in worktrees if row.get("pid")),
        },
        "branches": worktrees,
        "recent_merges": recent_merges,
        "open_task_branches": sum(1 for row in worktrees if row.get("branch") not in {"main", "master"}),
        "unpushed_worktrees": sum(1 for row in worktrees if (row.get("ahead") or 0) > 0),
    }
    ctx.state.set_project_setting("repository_health_cache", json.dumps(body), project_id)
    return body


def repository_health(ctx: Any) -> dict[str, Any]:
    """The selected project's cached repository health.

    ENG-CP-03: a project with no cache yet reports ``{}`` ("not observed yet")
    rather than falling back to another project's snapshot -- showing Project
    A's branch/merge state under Project B would be exactly the stale
    cross-project display this slice must make impossible.
    """

    try:
        return json.loads(
            ctx.state.get_project_setting("repository_health_cache", "{}", project_id=ctx.selected_project_id) or "{}"
        )
    except (TypeError, ValueError):
        return {}


def delegation_evidence(repo_root: Path, *, limit: int = 100) -> list[dict[str, Any]]:
    """Read existing redacted ENG-AGENT-01 manifests; never parse chat text/logs."""

    root = repo_root / ".agent-output"
    if not root.is_dir():
        return []
    manifests = sorted(root.glob("*/*/*/manifest.json"), key=lambda path: path.stat().st_mtime, reverse=True)[:limit]
    rows: list[dict[str, Any]] = []
    for path in manifests:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        planned = data.get("planned") or {}
        actual = data.get("actual") or {}
        rows.append(
            {
                "task": data.get("task"),
                "role": data.get("role"),
                "worker": data.get("worker"),
                "started_at": data.get("started_at"),
                "finished_at": data.get("finished_at"),
                "duration_seconds": data.get("duration_seconds"),
                "result": data.get("result"),
                "exit_status": data.get("exit_status"),
                "planned": planned,
                "actual": actual,
                "why_this_model": planned.get("why_this_worker"),
                "files_changed": data.get("files_changed") or [],
                "tests_or_checks": data.get("tests_or_checks") or [],
                "requested_command": data.get("requested_command") or [],
                "summary_path": (data.get("paths") or {}).get("summary"),
                "manifest_path": str(path.relative_to(repo_root)),
                "redaction_applied": data.get("redaction_applied") is True,
            }
        )
    return rows


def adopt_worktree(ctx: Any, *, path: str) -> dict[str, Any]:
    project_id = ctx.selected_project_id
    discovered = _resolved_discovered(ctx.project_root, path)
    if not discovered.branch:
        raise OperationError("detached worktrees cannot be adopted")
    target = Path(path).resolve()
    # Look across every project, not just the selected one. Two registered
    # roots can be worktrees of the same Git repository; a selected-only
    # lookup would treat the sibling as unmanaged and steal it via
    # ON CONFLICT(path) (fourth independent review, Grok/xAI).
    owned = next(
        (w for w in ctx.state.list_worktrees() if Path(w.path).resolve() == target),
        None,
    )
    if owned is not None and owned.project_id is not None and owned.project_id != project_id:
        raise OperationError(
            f"worktree {target} is already owned by project {owned.project_id!r} "
            "and cannot be adopted into another project"
        )
    saved = owned if owned is not None and owned.project_id == project_id else None
    if saved and saved.managed:
        raise OperationError("worktree is already managed")
    live = [t for t in ctx.state.list_tasks(project_id=project_id) if t.pid and pid_is_alive(t.pid)]
    if any(t.worktree and Path(t.worktree).resolve() == Path(path).resolve() for t in live):
        raise OperationError("worktree has an active writer/task and cannot be adopted")
    discovered.managed = True
    # ENG-CP-03: a freshly discovered record carries no project of its own;
    # adopting it into the selected project is what keeps it visible (and stops
    # it from nulling a previously stamped row on upsert).
    discovered.project_id = project_id
    ctx.state.upsert_worktree(discovered)
    refresh_worktree_statuses(ctx)
    ctx.state.record_event(
        category="worktrees", message=f"adopted worktree {Path(path).resolve()}", project_id=project_id
    )
    return {"path": str(Path(path).resolve()), "management": "MANAGED"}


def _record_operation(ctx: Any, operation_id: str, kind: str, action: str, target: str, state: str, stage: str, message: str) -> None:
    # ENG-CP-03: stamp the selected project on both the operation and its
    # event. An independent review (Grok/xAI) caught that the State-layer fix
    # alone was inert because this, the only production writer, never passed a
    # project -- so every new operation stayed NULL and invisible to the
    # project-scoped dashboard read.
    project_id = ctx.selected_project_id
    ctx.state.upsert_operation(operation_id, kind=kind, action=action, target=target, state=state, stage=stage, message=redact_text(message), project_id=project_id)
    ctx.state.record_event(category=kind, level="error" if state == "FAILED" else "info", message=f"{action} {state}: {redact_text(message)}", project_id=project_id)


def _selected_github_remote(ctx: Any) -> str | None:
    """The selected project's GitHub identity -- never a hard-coded OctaScene repo."""

    project = getattr(ctx, "selected_project", None)
    remote = getattr(project, "github_remote", None) if project is not None else None
    return remote or None


def _selected_default_branch(ctx: Any) -> str:
    project = getattr(ctx, "selected_project", None)
    branch = getattr(project, "default_branch", None) if project is not None else None
    return branch or "main"


def git_operation(ctx: Any, *, action: str, path: str, confirm: bool = False, prepare_id: str | None = None) -> dict[str, Any]:
    if action not in GIT_ACTIONS:
        raise OperationError("unsupported Git action")
    record = _resolved_discovered(ctx.project_root, path)
    target = Path(record.path).resolve()
    operation_id = uuid.uuid4().hex
    _record_operation(ctx, operation_id, "git", action, str(target), "RUNNING", "Preparing", "validating worktree")
    dirty = _worktree_dirty(target)
    branch = (record.branch or "").removeprefix("refs/heads/")
    upstream = _git(target, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if action == "refresh":
        refresh_repository_health(ctx)
        _record_operation(ctx, operation_id, "git", action, str(target), "SUCCEEDED", "Complete", "status refreshed")
    elif action == "fetch":
        _record_operation(ctx, operation_id, "git", action, str(target), "RUNNING", "Fetching", "fetching origin")
        result = _run(["git", "fetch", "--prune", "origin"], target, timeout=180)
        _record_operation(ctx, operation_id, "git", action, str(target), "SUCCEEDED" if result.returncode == 0 else "FAILED", "Complete", result.stderr or result.stdout or "fetch complete")
    elif action == "pull":
        if dirty or not upstream:
            reason = "dirty worktree" if dirty else "branch has no upstream"
            _record_operation(ctx, operation_id, "git", action, str(target), "BLOCKED", "Validating", reason)
        elif not confirm:
            counts = _git(target, "rev-list", "--left-right", "--count", f"{upstream}...HEAD") or "unknown"
            _record_operation(ctx, operation_id, "git", action, str(target), "BLOCKED", "Validating", f"preview: upstream={upstream}; behind/ahead={counts}; confirm to apply ff-only pull")
        else:
            counts = _git(target, "rev-list", "--left-right", "--count", f"{upstream}...HEAD") or ""
            behind, ahead = ([int(v) for v in counts.split()] if len(counts.split()) == 2 else [0, 0])
            if ahead and behind:
                _record_operation(ctx, operation_id, "git", action, str(target), "CONFLICT", "Validating", "branch has diverged; pull refused")
            else:
                result = _run(["git", "pull", "--ff-only"], target, timeout=180)
                state = "SUCCEEDED" if result.returncode == 0 else "CONFLICT"
                _record_operation(ctx, operation_id, "git", action, str(target), state, "Complete", result.stderr or result.stdout or "pull complete")
    elif action == "push":
        if dirty or not branch:
            _record_operation(ctx, operation_id, "git", action, str(target), "BLOCKED", "Validating", "push requires a clean named branch")
        elif not confirm:
            counts = _git(target, "rev-list", "--left-right", "--count", f"{upstream}...HEAD") if upstream else None
            _record_operation(ctx, operation_id, "git", action, str(target), "BLOCKED", "Validating", f"preview: target=origin/{branch}; behind/ahead={counts or 'no upstream'}; confirm to push without force")
        else:
            argv = ["git", "push", "origin", f"HEAD:refs/heads/{branch}"]
            result = _run(argv, target, timeout=180)
            _record_operation(ctx, operation_id, "git", action, str(target), "SUCCEEDED" if result.returncode == 0 else "FAILED", "Complete", result.stderr or result.stdout or "push complete")
    elif action == "prepare_merge":
        blockers = []
        if dirty:
            blockers.append("worktree is dirty")
        if not branch or branch in {"main", "master"}:
            blockers.append("source must be a named feature branch")
        task = next(
            (
                t
                for t in ctx.state.list_tasks(project_id=ctx.selected_project_id)
                if t.worktree and Path(t.worktree).resolve() == target
            ),
            None,
        )
        if not task or task.state not in {"SUCCEEDED", "READY_LOCAL", "READY_BUT_UNMERGED"}:
            blockers.append("final task/local gate evidence is not ready")
        tasks_by_id = {item.id: item for item in ctx.state.list_tasks(project_id=ctx.selected_project_id)}
        unsatisfied = [dep for dep in getattr(task, "dependencies", ()) if dep not in tasks_by_id or tasks_by_id[dep].state not in {"SUCCEEDED", "READY_LOCAL", "READY_BUT_UNMERGED"}]
        if unsatisfied:
            blockers.append(f"dependency order is not satisfied: {', '.join(unsatisfied)}")
        events = ctx.state.list_events(limit=1000, project_id=ctx.selected_project_id)
        task_id = getattr(task, "id", None)
        if not any(e.category in {"review", "independent_review"} and e.task_id == task_id for e in events):
            blockers.append("independent review evidence is not recorded")
        if not any(e.category in {"documentation", "docs"} and e.task_id == task_id for e in events):
            blockers.append("documentation reconciliation evidence is not recorded")
        expected_tree = _git(target, "rev-parse", "HEAD^{tree}")
        from .validation_adapter import (
            ADAPTER_EXACT_TREE_LOCAL_GATE,
            validation_adapter_name,
        )

        project = getattr(ctx, "selected_project", None)
        if project is None or validation_adapter_name(project) == ADAPTER_EXACT_TREE_LOCAL_GATE:
            local_gate = read_latest_gate(target, expected_tree=expected_tree)
            if not local_gate.get("ready"):
                blockers.append(f"authoritative exact-tree local gate is not ready: {local_gate.get('reason')}")
        elif not project.validation_command:
            blockers.append("selected project declares no validation_command")
        default_branch = _selected_default_branch(ctx)
        origin_default = f"origin/{default_branch}"
        if _git(target, "rev-parse", origin_default):
            conflict = _run(["git", "merge-tree", "--write-tree", origin_default, "HEAD"], target)
            if conflict.returncode != 0:
                blockers.append(f"merge conflict detected against {origin_default}")
        github_remote = _selected_github_remote(ctx)
        if not github_remote:
            blockers.append("selected project has no github_remote")
            pr = {}
        else:
            try:
                pr_result = _run(["gh", "pr", "view", branch, "--repo", github_remote, "--json", "number,state,mergeable,reviewDecision,url"], target, timeout=30)
                pr = json.loads(pr_result.stdout) if pr_result.returncode == 0 else {}
            except (OSError, subprocess.TimeoutExpired, ValueError):
                pr = {}
        if not pr:
            blockers.append("open PR metadata is not resolvable")
        elif pr.get("state") != "OPEN" or pr.get("mergeable") not in {"MERGEABLE", "UNKNOWN"}:
            blockers.append(f"PR is not mergeable ({pr.get('state')}/{pr.get('mergeable')})")
        state = "BLOCKED" if blockers else "SUCCEEDED"
        _record_operation(ctx, operation_id, "git", action, str(target), state, "Complete", "; ".join(blockers) or "merge preparation checks passed")
        if not blockers:
            ctx.state.set_control_setting(f"merge_prepare:{operation_id}", json.dumps({"path": str(target), "head": _git(target, "rev-parse", "HEAD"), "branch": branch, "at": utc_now_iso()}))
    else:
        if not confirm or not prepare_id:
            _record_operation(ctx, operation_id, "git", action, str(target), "BLOCKED", "Validating", "merge requires explicit confirmation and a successful preparation")
        else:
            raw = ctx.state.get_control_setting(f"merge_prepare:{prepare_id}")
            prepared = json.loads(raw) if raw else {}
            if prepared.get("path") != str(target) or prepared.get("head") != _git(target, "rev-parse", "HEAD"):
                _record_operation(ctx, operation_id, "git", action, str(target), "BLOCKED", "Validating", "prepared merge is missing or stale")
            else:
                github_remote = _selected_github_remote(ctx)
                if not github_remote:
                    _record_operation(ctx, operation_id, "git", action, str(target), "BLOCKED", "Validating", "selected project has no github_remote")
                else:
                    result = _run(["gh", "pr", "merge", branch, "--repo", github_remote, "--merge"], target, timeout=180)
                    _record_operation(ctx, operation_id, "git", action, str(target), "SUCCEEDED" if result.returncode == 0 else "BLOCKED", "Complete", result.stderr or result.stdout or "merge complete")
    return ctx.state.get_operation(operation_id) or {"id": operation_id}


_SENSITIVE_COMMAND = re.compile(r"(?i)(password|passwd|token|api[_-]?key|authorization|private[_-]?key|secret)\s*(=|:)|(^|\s)(export|set)\s+[^\s]*(key|token|secret|password)")


def sanitize_terminal_command(command: str) -> str:
    stripped = command.strip()
    if not stripped:
        return ""
    if _SENSITIVE_COMMAND.search(stripped) or "-----BEGIN " in stripped:
        return "[sensitive command hidden]"
    return redact_text(stripped)


class AppLifecycleManager:
    """Own only the canonical local development process launched by this instance.

    ENG-CP-04: launch argv/port/cwd come from the selected project's
    capabilities (``app_lifecycle_command``, ``app_lifecycle_port``) and
    ``local_repo_root``. Generic CP code does not assume ``make run``, port
    8765, or the OctaScene display name. A project that does not declare a
    lifecycle command cannot be started.
    """

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self.process: subprocess.Popen[str] | None = None
        self.started_at: str | None = ctx.state.get_control_setting("app_started_at")
        raw_exit = ctx.state.get_control_setting("app_last_exit_code")
        self.last_exit_code: int | None = int(raw_exit) if raw_exit not in {None, ""} else None

    def _lifecycle_spec(self) -> tuple[list[str], int, Path, str]:
        project = getattr(self.ctx, "selected_project", None)
        if project is None:
            return ["make", "run"], 8765, self.ctx.repo_root, "OctaScene"
        command = (project.capabilities.get("app_lifecycle_command") or "").strip()
        if not command:
            raise OperationError(
                f"project {project.project_id!r} does not declare app_lifecycle_command; "
                "generic Control Plane code will not assume make run"
            )
        port_raw = project.capabilities.get("app_lifecycle_port") or "8765"
        try:
            port = int(port_raw)
        except ValueError as exc:
            raise OperationError(f"project {project.project_id!r} app_lifecycle_port is not an integer: {port_raw!r}") from exc
        return command.split(), port, project.local_repo_root, project.display_name

    def _persist(self, pid: int | None, create_time: float | None = None) -> None:
        self.ctx.state.set_control_setting("app_pid", str(pid or ""))
        self.ctx.state.set_control_setting("app_started_at", self.started_at or "")
        self.ctx.state.set_control_setting("app_process_create_time", str(create_time or ""))
        self.ctx.state.set_control_setting("app_last_exit_code", "" if self.last_exit_code is None else str(self.last_exit_code))

    def _owned_pid(self) -> int | None:
        raw = self.ctx.state.get_control_setting("app_pid")
        if not raw or not raw.isdigit() or not pid_is_alive(int(raw)):
            return None
        pid = int(raw)
        try:
            import psutil

            process = psutil.Process(pid)
            expected = self.ctx.state.get_control_setting("app_process_create_time")
            if expected and abs(process.create_time() - float(expected)) > 1:
                return None
            command = " ".join(process.cmdline()).lower()
            cwd = Path(process.cwd()).resolve()
            try:
                argv, _port, launch_root, _name = self._lifecycle_spec()
            except OperationError:
                return None
            expected_cwd = launch_root.resolve()
            if cwd != expected_cwd or not all(part.lower() in command for part in argv):
                return None
        except (ImportError, OSError, ValueError):
            if not self.process or self.process.pid != pid:
                return None
        return pid

    def status(self) -> dict[str, Any]:
        pid = self.process.pid if self.process else self._owned_pid()
        if self.process and self.process.poll() is not None:
            self.last_exit_code = self.process.returncode
            self.process = None
            pid = None
            self._persist(None)
        running = bool(pid and pid_is_alive(pid))
        uptime = None
        if running and self.started_at:
            uptime = max(0, int((dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(self.started_at)).total_seconds()))
        try:
            argv, port, _root, _name = self._lifecycle_spec()
            launch_source = " ".join(argv)
        except OperationError:
            port, launch_source = 8765, "undeclared"
        return {"status": "RUNNING" if running else "STOPPED", "pid": pid, "port": port, "uptime_seconds": uptime, "started_at": self.started_at, "launch_source": launch_source, "last_exit_code": self.last_exit_code}

    def action(self, action: str, actor: str) -> dict[str, Any]:
        if action not in APP_ACTIONS:
            raise OperationError("unsupported app lifecycle action")
        if action == "restart":
            if self.process:
                self._stop(actor)
            argv, _port, _root, display = self._lifecycle_spec()
            return self._start(actor, event=f"{display} Restarted")
        if action == "start":
            return self._start(actor)
        return self._stop(actor)

    def shutdown_owned(self) -> bool:
        """Stop a development process only when durable identity proves ownership."""

        if self._owned_pid() is None:
            return False
        self._stop("control-center-shutdown")
        return True

    def _start(self, actor: str, event: str | None = None) -> dict[str, Any]:
        argv, port, launch_root, display = self._lifecycle_spec()
        if event is None:
            event = f"{display} Started"
        if self.status()["status"] == "RUNNING":
            raise OperationError(f"the managed {display} development process is already running")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                raise OperationError(f"port {port} is already active; refusing to start a duplicate process")
        except (ConnectionRefusedError, TimeoutError, OSError):
            pass
        log_path = self.ctx.state.db_path.parent / "managed-app.log"
        stream = log_path.open("a", encoding="utf-8")
        self.process = subprocess.Popen(argv, cwd=launch_root, stdout=stream, stderr=subprocess.STDOUT, text=True, start_new_session=True)
        stream.close()
        self.started_at = utc_now_iso()
        create_time = None
        try:
            import psutil

            create_time = psutil.Process(self.process.pid).create_time()
        except (ImportError, OSError):
            pass
        self._persist(self.process.pid, create_time)
        self.ctx.state.record_event(category="app_lifecycle", message=f"{event}; actor={actor}; pid={self.process.pid}; launch=make-run")
        return self.status()

    def _stop(self, actor: str) -> dict[str, Any]:
        old_pid = self.process.pid if self.process and self.process.poll() is None else self._owned_pid()
        if not old_pid:
            raise OperationError("no Control-Center-managed development process is running")
        os.killpg(old_pid, signal.SIGTERM)
        if self.process:
            try:
                self.process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                raise OperationError("process did not stop after SIGTERM; refusing to force-kill it") from None
            self.last_exit_code = self.process.returncode
        else:
            deadline = dt.datetime.now().timestamp() + 8
            while pid_is_alive(old_pid) and dt.datetime.now().timestamp() < deadline:
                import time as _time

                _time.sleep(0.1)
            if pid_is_alive(old_pid):
                raise OperationError("process did not stop after SIGTERM; refusing to force-kill it")
        self.process = None
        self._persist(None)
        try:
            _argv, _port, _root, display = self._lifecycle_spec()
        except OperationError:
            display = "app"
        self.ctx.state.record_event(category="app_lifecycle", message=f"{display} Stopped; actor={actor}; pid={old_pid}; exit={self.last_exit_code}")
        return self.status()


_PROGRAM_SOURCES = {
    "Core production app": "V1_CHECKLIST.md",
    "Standalone Video Editor": "docs/video-editor/IMPLEMENTATION_STATUS_V2.md",
    "Generation QA": "docs/OCTASCENE_GENERATION_QA_IMPLEMENTATION_PLAN.md",
    "Long-Form Director / Direction Brain": "docs/engineering/longform-director-progress",
    "Provider/model/spend platform": None,
    "AI assistant/agents": None,
    "Voice AI Director": None,
    "Engineering governance / CI / Control Center": "docs/PRODUCT_ROADMAP.md",
}
_TASK_ROW = re.compile(r"^\|\s*`?([A-Z][A-Z0-9-]+)`?\s*\|\s*([^|]+)\|", re.MULTILINE)


def derived_roadmap(repo_root: Path, index_rows: list[dict[str, str]], *, include_catalog: bool = True) -> list[dict[str, Any]]:
    """Return doc-derived counts only when a maintained ledger has explicit rows."""

    existing = {row.get("Program / feature family", ""): row for row in index_rows}
    output: list[dict[str, Any]] = []
    programs = _PROGRAM_SOURCES if include_catalog else {row.get("Program / feature family", "Unknown"): None for row in index_rows}
    for program, source in programs.items():
        row = existing.get(program, {})
        files: list[Path] = []
        if source:
            target = repo_root / source
            files = sorted(target.glob("*.md")) if target.is_dir() else [target]
        tasks: dict[str, str] = {}
        latest: float | None = None
        for file in files:
            if not file.is_file():
                continue
            latest = max(latest or 0, file.stat().st_mtime)
            for task_id, status in _TASK_ROW.findall(file.read_text(encoding="utf-8")):
                if task_id not in {"ID", "TASK", "TASK-ID"}:
                    tasks[task_id] = status.strip().lower().strip("*`")
        completed = sum(1 for status in tasks.values() if status in {"complete", "completed", "done", "merged"})
        active = sum(1 for status in tasks.values() if status in {"active", "in progress", "implemented", "ready-but-unmerged"})
        blocked = sum(1 for status in tasks.values() if "block" in status)
        pending = max(0, len(tasks) - completed - active - blocked)
        pct = round(completed * 100 / len(tasks), 1) if tasks else None
        output.append(
            {
                **row,
                "program": program,
                "version": row.get("Product version", "—"),
                "status_source": row.get("Status source", source or "Repository docs/code"),
                "implementation_source": row.get("Implementation source", source or "Repository docs/code"),
                "canonical_source": source,
                "total": len(tasks) if tasks else None,
                "completed": completed if tasks else None,
                "active": active if tasks else None,
                "pending": pending if tasks else None,
                "blocked": blocked if tasks else None,
                "percentage": pct,
                "percentage_label": f"{pct:g}% DERIVED" if pct is not None else "Progress: not computable from current ledger",
                "latest_update_at": dt.datetime.fromtimestamp(latest, dt.timezone.utc).isoformat() if latest else None,
            }
        )
    return output
