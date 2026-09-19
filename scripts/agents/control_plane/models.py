"""Dataclasses and state-machine constants shared across the control plane.

Keeping these in one module means ``state.py`` (persistence), ``scheduler.py``
(task lifecycle), ``routing.py`` (provider selection) and ``dashboard_api.py``
(read models) all agree on one shape rather than drifting.
"""

from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass, field
from typing import Any

# Single source of truth for the permission-profile names shared by Task,
# Runbook, and the worker registry's ``build_command`` (ENG-AGENT-02-S5,
# issue #93/#94) — re-exported here rather than redefined so ``models.py``
# and ``registry.py`` can never disagree on the same string constant.
from ..registry import (
    PERMISSION_REPO_CONFIGURED_AUTO,
    PERMISSION_STANDARD,
    REASON_AVAILABLE,
)

# --------------------------------------------------------------------------- time


def utc_now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- task states

# Terminal "success-shaped" states a dependent task may treat as satisfied.
TASK_PENDING = "PENDING"
TASK_QUEUED = "QUEUED"
TASK_BLOCKED = "BLOCKED"
TASK_RUNNING = "RUNNING"
TASK_PAUSED = "PAUSED"
TASK_SUCCEEDED = "SUCCEEDED"
TASK_READY_LOCAL = "READY_LOCAL"
TASK_READY_BUT_UNMERGED = "READY_BUT_UNMERGED"
TASK_FAILED = "FAILED"
TASK_CANCELLED = "CANCELLED"

TASK_STATES = frozenset(
    {
        TASK_PENDING,
        TASK_QUEUED,
        TASK_BLOCKED,
        TASK_RUNNING,
        TASK_PAUSED,
        TASK_SUCCEEDED,
        TASK_READY_LOCAL,
        TASK_READY_BUT_UNMERGED,
        TASK_FAILED,
        TASK_CANCELLED,
    }
)

# A dependency is satisfied once its task reaches one of these. Matches
# ENG-AGENT-01/GITHUB_DEVELOPMENT_WORKFLOW.md vocabulary: a locally verified but
# unmerged branch (READY_BUT_UNMERGED) is a valid basis for downstream work, the
# same way this very task stacks on the unmerged PR #63.
DEPENDENCY_SATISFIED_STATES = frozenset({TASK_SUCCEEDED, TASK_READY_LOCAL, TASK_READY_BUT_UNMERGED})

# States that no longer occupy a concurrency slot.
TASK_TERMINAL_STATES = frozenset(
    {TASK_SUCCEEDED, TASK_READY_LOCAL, TASK_READY_BUT_UNMERGED, TASK_FAILED, TASK_CANCELLED}
)

# Canonical Control Center task projection.  These categories intentionally do
# not redefine scheduler state: they are the one presentation contract used by
# Overview, Tasks, workers, and worktree ownership views.
TASK_ACTIVE_STATES = frozenset({TASK_RUNNING})
TASK_QUEUED_STATES = frozenset({TASK_PENDING, TASK_QUEUED})
TASK_PAUSED_STATES = frozenset({TASK_PAUSED})
TASK_ATTENTION_STATES = frozenset({TASK_BLOCKED, TASK_FAILED})


def task_projection(state: str) -> str:
    if state in TASK_ACTIVE_STATES:
        return "ACTIVE"
    if state in TASK_QUEUED_STATES:
        return "QUEUED"
    if state in TASK_PAUSED_STATES:
        return "PAUSED"
    if state in TASK_ATTENTION_STATES:
        return "NEEDS_ATTENTION"
    return "HISTORICAL"

# Concurrency classes a task consumes a slot from (scheduler.py).
KIND_WRITE = "write"
KIND_READ = "read"
KIND_HEAVY = "heavy"
TASK_KINDS = frozenset({KIND_WRITE, KIND_READ, KIND_HEAVY})

ADMISSION_PENDING = "PENDING"
ADMISSION_ADMITTED = "ADMITTED"
ADMISSION_QUEUED = "QUEUED"
ADMISSION_BLOCKED = "BLOCKED"
ADMISSION_STATES = frozenset(
    {ADMISSION_PENDING, ADMISSION_ADMITTED, ADMISSION_QUEUED, ADMISSION_BLOCKED}
)


LAUNCH_DELEGATED = "delegated"
LAUNCH_SESSION = "session"
TASK_LAUNCH_MODES = frozenset({LAUNCH_DELEGATED, LAUNCH_SESSION})


@dataclass
class Task:
    id: str
    task_ref: str
    role: str
    worker: str
    kind: str = KIND_WRITE
    state: str = TASK_PENDING
    priority: int = 0
    dependencies: tuple[str, ...] = ()
    pid: int | None = None
    worktree: str | None = None
    command: tuple[str, ...] = ()
    result: str | None = None
    last_error: str | None = None
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    launch_mode: str = LAUNCH_DELEGATED
    timeout_seconds: int | None = None
    runbook_id: str | None = None
    permission_profile: str = PERMISSION_STANDARD
    fallback_reason: str | None = None
    failed_worker_id: str | None = None
    failure_execution_system: str | None = None
    failure_provider: str | None = None
    failure_model: str | None = None
    failure_category: str | None = None
    failure_reason_sanitized: str | None = None
    failure_reset: str | None = None
    failure_reset_source: str | None = None
    fallback_alternatives: tuple[str, ...] = ()
    fallback_selected_worker: str | None = None
    fallback_automatic: bool | None = None
    stale_recovered: bool = False
    # ENG-AGENT-10 managed-dispatch facts. These are projections of the one
    # scheduler/registry path, not a second task or provider-state system.
    owner_ref: str | None = None
    required_capability: str | None = None
    changed_paths: tuple[str, ...] = ()
    integration_base: str | None = None
    avoid_provider: str | None = None
    codex_policy: str = "conserve"
    codex_auto_eligible: bool = False
    max_codex_invocations: int = 1
    admission_state: str = ADMISSION_PENDING
    admission_reason: str | None = None
    dependency_wave: int | None = None
    selected_worker_reason: str | None = None
    selected_provider: str | None = None
    selection_alternatives: tuple[str, ...] = ()
    # ENG-CP-03 (issue #165): which managed project this task belongs to.
    # ``None`` only for a legacy row written before the Control Plane knew
    # about projects; ``project_registry.migrate_legacy_state_to_project``
    # adopts those into the auto-migrated OctaScene project on first open.
    project_id: str | None = None

    def to_row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task_ref": self.task_ref,
            "role": self.role,
            "worker": self.worker,
            "kind": self.kind,
            "state": self.state,
            "priority": self.priority,
            "dependencies": json.dumps(list(self.dependencies)),
            "pid": self.pid,
            "worktree": self.worktree,
            "command": json.dumps(list(self.command)),
            "result": self.result,
            "last_error": self.last_error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "launch_mode": self.launch_mode,
            "timeout_seconds": self.timeout_seconds,
            "runbook_id": self.runbook_id,
            "permission_profile": self.permission_profile,
            "fallback_reason": self.fallback_reason,
            "failed_worker_id": self.failed_worker_id,
            "failure_execution_system": self.failure_execution_system,
            "failure_provider": self.failure_provider,
            "failure_model": self.failure_model,
            "failure_category": self.failure_category,
            "failure_reason_sanitized": self.failure_reason_sanitized,
            "failure_reset": self.failure_reset,
            "failure_reset_source": self.failure_reset_source,
            "fallback_alternatives": json.dumps(list(self.fallback_alternatives)),
            "fallback_selected_worker": self.fallback_selected_worker,
            "fallback_automatic": None if self.fallback_automatic is None else (1 if self.fallback_automatic else 0),
            "stale_recovered": 1 if self.stale_recovered else 0,
            "owner_ref": self.owner_ref,
            "required_capability": self.required_capability,
            "changed_paths": json.dumps(list(self.changed_paths)),
            "integration_base": self.integration_base,
            "avoid_provider": self.avoid_provider,
            "codex_policy": self.codex_policy,
            "codex_auto_eligible": 1 if self.codex_auto_eligible else 0,
            "max_codex_invocations": self.max_codex_invocations,
            "admission_state": self.admission_state,
            "admission_reason": self.admission_reason,
            "dependency_wave": self.dependency_wave,
            "selected_worker_reason": self.selected_worker_reason,
            "selected_provider": self.selected_provider,
            "selection_alternatives": json.dumps(list(self.selection_alternatives)),
            "project_id": self.project_id,
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Task":
        return cls(
            id=row["id"],
            task_ref=row["task_ref"],
            role=row["role"],
            worker=row["worker"],
            kind=row["kind"],
            state=row["state"],
            priority=row["priority"],
            dependencies=tuple(json.loads(row["dependencies"] or "[]")),
            pid=row["pid"],
            worktree=row["worktree"],
            command=tuple(json.loads(row["command"] or "[]")),
            result=row["result"],
            last_error=row["last_error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            launch_mode=row["launch_mode"] if "launch_mode" in row.keys() else LAUNCH_DELEGATED,
            timeout_seconds=row["timeout_seconds"] if "timeout_seconds" in row.keys() else None,
            runbook_id=row["runbook_id"] if "runbook_id" in row.keys() else None,
            permission_profile=row["permission_profile"] if "permission_profile" in row.keys() else PERMISSION_STANDARD,
            fallback_reason=row["fallback_reason"] if "fallback_reason" in row.keys() else None,
            failed_worker_id=row["failed_worker_id"] if "failed_worker_id" in row.keys() else None,
            failure_execution_system=row["failure_execution_system"] if "failure_execution_system" in row.keys() else None,
            failure_provider=row["failure_provider"] if "failure_provider" in row.keys() else None,
            failure_model=row["failure_model"] if "failure_model" in row.keys() else None,
            failure_category=row["failure_category"] if "failure_category" in row.keys() else None,
            failure_reason_sanitized=row["failure_reason_sanitized"] if "failure_reason_sanitized" in row.keys() else None,
            failure_reset=row["failure_reset"] if "failure_reset" in row.keys() else None,
            failure_reset_source=row["failure_reset_source"] if "failure_reset_source" in row.keys() else None,
            fallback_alternatives=tuple(json.loads(row["fallback_alternatives"] or "[]")) if "fallback_alternatives" in row.keys() else (),
            fallback_selected_worker=row["fallback_selected_worker"] if "fallback_selected_worker" in row.keys() else None,
            fallback_automatic=(bool(row["fallback_automatic"]) if row["fallback_automatic"] is not None else None) if "fallback_automatic" in row.keys() else None,
            stale_recovered=bool(row["stale_recovered"]) if "stale_recovered" in row.keys() else False,
            owner_ref=row["owner_ref"] if "owner_ref" in row.keys() else None,
            required_capability=row["required_capability"] if "required_capability" in row.keys() else None,
            changed_paths=tuple(json.loads(row["changed_paths"] or "[]")) if "changed_paths" in row.keys() else (),
            integration_base=row["integration_base"] if "integration_base" in row.keys() else None,
            avoid_provider=row["avoid_provider"] if "avoid_provider" in row.keys() else None,
            codex_policy=row["codex_policy"] if "codex_policy" in row.keys() else "conserve",
            codex_auto_eligible=bool(row["codex_auto_eligible"]) if "codex_auto_eligible" in row.keys() else False,
            max_codex_invocations=row["max_codex_invocations"] if "max_codex_invocations" in row.keys() else 1,
            admission_state=row["admission_state"] if "admission_state" in row.keys() else ADMISSION_PENDING,
            admission_reason=row["admission_reason"] if "admission_reason" in row.keys() else None,
            dependency_wave=row["dependency_wave"] if "dependency_wave" in row.keys() else None,
            selected_worker_reason=row["selected_worker_reason"] if "selected_worker_reason" in row.keys() else None,
            selected_provider=row["selected_provider"] if "selected_provider" in row.keys() else None,
            selection_alternatives=(
                tuple(json.loads(row["selection_alternatives"] or "[]"))
                if "selection_alternatives" in row.keys()
                else ()
            ),
            project_id=row["project_id"] if "project_id" in row.keys() else None,
        )


@dataclass
class ProviderState:
    name: str
    execution_system: str
    provider: str
    cost_class: str
    state: str
    configured: bool = True
    consecutive_failures: int = 0
    last_probe_at: str | None = None
    last_error: str | None = None
    updated_at: str = field(default_factory=utc_now_iso)
    # ENG-AGENT-02-S7 (issue #97): the truthful *why* behind `state`, from
    # registry.KNOWN_AVAILABILITY_REASONS (AVAILABLE, DISABLED, CLI_MISSING,
    # NOT_AUTHENTICATED, API_ONLY_NOT_AUTHORIZED, UNSUPPORTED,
    # CONFIGURATION_ERROR, CATALOG_ONLY). Distinct from `state`, which is the
    # routing-relevant bucket -- this is what the Control Center displays
    # instead of collapsing every unavailability cause into "NOT_CONFIGURED".
    reason: str = REASON_AVAILABLE

    def to_row(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "execution_system": self.execution_system,
            "provider": self.provider,
            "cost_class": self.cost_class,
            "state": self.state,
            "configured": 1 if self.configured else 0,
            "consecutive_failures": self.consecutive_failures,
            "last_probe_at": self.last_probe_at,
            "last_error": self.last_error,
            "updated_at": self.updated_at,
            "reason": self.reason,
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "ProviderState":
        return cls(
            name=row["name"],
            execution_system=row["execution_system"],
            provider=row["provider"],
            cost_class=row["cost_class"],
            state=row["state"],
            configured=bool(row["configured"]),
            consecutive_failures=row["consecutive_failures"],
            last_probe_at=row["last_probe_at"],
            last_error=row["last_error"],
            updated_at=row["updated_at"],
            reason=row["reason"] if row["reason"] is not None else REASON_AVAILABLE,
        )


@dataclass
class WorktreeRecord:
    path: str
    branch: str | None = None
    locked: bool = False
    lock_holder: str | None = None
    managed: bool = False
    discovered_at: str | None = None
    stale_lock: bool = False
    stale_lock_holder: str | None = None
    updated_at: str = field(default_factory=utc_now_iso)
    # ENG-AGENT-14 (issue #140): identifies an auto-provisioned, exact-head
    # read-only review checkout so it can be safely reused (only for the
    # exact same PR/head it was provisioned for -- never implicitly
    # substituted for main/DevCP/a product implementation worktree or another
    # historical task's checkout) and safely reclaimed once no longer needed.
    # Empty/None for every ordinary (non-review) worktree.
    review_repository: str | None = None
    review_pr: int | None = None
    review_head_sha: str | None = None
    # ENG-CP-03 (issue #165): the managed project whose repository this
    # worktree belongs to, so a worktree observed for Project A is never
    # listed, adopted, or cleaned up as if it belonged to Project B.
    project_id: str | None = None

    def to_row(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "branch": self.branch,
            "locked": 1 if self.locked else 0,
            "lock_holder": self.lock_holder,
            "managed": 1 if self.managed else 0,
            "discovered_at": self.discovered_at or self.updated_at,
            "stale_lock": 1 if self.stale_lock else 0,
            "stale_lock_holder": self.stale_lock_holder,
            "updated_at": self.updated_at,
            "review_repository": self.review_repository,
            "review_pr": self.review_pr,
            "review_head_sha": self.review_head_sha,
            "project_id": self.project_id,
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "WorktreeRecord":
        return cls(
            path=row["path"],
            branch=row["branch"],
            locked=bool(row["locked"]),
            lock_holder=row["lock_holder"],
            managed=bool(row["managed"]) if "managed" in row.keys() else False,
            discovered_at=row["discovered_at"] if "discovered_at" in row.keys() else None,
            stale_lock=bool(row["stale_lock"]) if "stale_lock" in row.keys() else False,
            stale_lock_holder=row["stale_lock_holder"] if "stale_lock_holder" in row.keys() else None,
            updated_at=row["updated_at"],
            review_repository=row["review_repository"] if "review_repository" in row.keys() else None,
            review_pr=row["review_pr"] if "review_pr" in row.keys() else None,
            review_head_sha=row["review_head_sha"] if "review_head_sha" in row.keys() else None,
            project_id=row["project_id"] if "project_id" in row.keys() else None,
        )


@dataclass
class Event:
    id: int | None
    ts: str
    category: str
    message: str
    task_id: str | None = None
    provider: str | None = None
    level: str = "info"
    # ENG-CP-03 (issue #165): the managed project this history entry belongs to.
    project_id: str | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Event":
        return cls(
            id=row["id"],
            ts=row["ts"],
            category=row["category"],
            message=row["message"],
            task_id=row["task_id"],
            provider=row["provider"],
            level=row["level"],
            project_id=row["project_id"] if "project_id" in row.keys() else None,
        )


# --------------------------------------------------------------------------- runbooks
# ENG-AGENT-02-S5 (issue #93): a Runbook is a durable, bounded, unattended
# development session. It never schedules its own subprocesses — it creates
# exactly one underlying ``Task`` (``launch_mode=LAUNCH_SESSION``) that the
# existing ``Supervisor``/``Scheduler`` own exactly like every other task, so
# there is still only one scheduler and one process-supervision mechanism in
# this control plane.

RUNBOOK_DRAFT = "DRAFT"
RUNBOOK_QUEUED = "QUEUED"
RUNBOOK_RUNNING = "RUNNING"
RUNBOOK_PAUSED = "PAUSED"
RUNBOOK_STOPPING = "STOPPING"
# ENG-AGENT-13 (issue #138): truthful non-terminal states between a successful
# implementation worker exit and a genuinely accepted runbook. Worker exit 0
# means the *implementation attempt* succeeded, not the runbook -- see
# acceptance.py for the stage machine that walks Test -> Review -> Checkpoint
# -> PR readiness -> Report before SUCCEEDED may be reported.
RUNBOOK_IMPLEMENTATION_COMPLETE = "IMPLEMENTATION_COMPLETE"
RUNBOOK_ACCEPTANCE_PENDING = "ACCEPTANCE_PENDING"
# Truthful halted states: acceptance cannot proceed without a human decision
# (OWNER_ACTION_REQUIRED, e.g. commit/push/PR authority) or without a repair
# to the candidate (BLOCKED, e.g. a failing required test/review). Both stop
# consuming a concurrency slot but are resumable via retry_runbook once the
# owner acts / the candidate is fixed, exactly like RUNBOOK_FAILED already is.
RUNBOOK_OWNER_ACTION_REQUIRED = "OWNER_ACTION_REQUIRED"
RUNBOOK_BLOCKED = "BLOCKED"
RUNBOOK_SUCCEEDED = "SUCCEEDED"
RUNBOOK_FAILED = "FAILED"
RUNBOOK_DEADLINE_REACHED = "DEADLINE_REACHED"
RUNBOOK_CANCELLED = "CANCELLED"

RUNBOOK_STATES = frozenset(
    {
        RUNBOOK_DRAFT,
        RUNBOOK_QUEUED,
        RUNBOOK_RUNNING,
        RUNBOOK_PAUSED,
        RUNBOOK_STOPPING,
        RUNBOOK_IMPLEMENTATION_COMPLETE,
        RUNBOOK_ACCEPTANCE_PENDING,
        RUNBOOK_OWNER_ACTION_REQUIRED,
        RUNBOOK_BLOCKED,
        RUNBOOK_SUCCEEDED,
        RUNBOOK_FAILED,
        RUNBOOK_DEADLINE_REACHED,
        RUNBOOK_CANCELLED,
    }
)

# A runbook whose underlying task has already been launched (used to decide
# whether the objective/instructions/preset/duration fields may still be edited).
RUNBOOK_LAUNCHED_STATES = frozenset(
    {
        RUNBOOK_QUEUED,
        RUNBOOK_RUNNING,
        RUNBOOK_PAUSED,
        RUNBOOK_STOPPING,
        RUNBOOK_IMPLEMENTATION_COMPLETE,
        RUNBOOK_ACCEPTANCE_PENDING,
        RUNBOOK_OWNER_ACTION_REQUIRED,
        RUNBOOK_BLOCKED,
        RUNBOOK_SUCCEEDED,
        RUNBOOK_FAILED,
        RUNBOOK_DEADLINE_REACHED,
        RUNBOOK_CANCELLED,
    }
)

# A runbook in one of these states no longer occupies a concurrency slot and
# will never resume on its own without an explicit retry/owner action.
RUNBOOK_TERMINAL_STATES = frozenset(
    {
        RUNBOOK_SUCCEEDED,
        RUNBOOK_FAILED,
        RUNBOOK_DEADLINE_REACHED,
        RUNBOOK_CANCELLED,
        RUNBOOK_OWNER_ACTION_REQUIRED,
        RUNBOOK_BLOCKED,
    }
)

# A runbook still actively working through the acceptance pipeline (implementation
# already succeeded; Test/Review/Checkpoint/PR readiness are not all resolved yet).
RUNBOOK_ACCEPTANCE_IN_PROGRESS_STATES = frozenset(
    {RUNBOOK_IMPLEMENTATION_COMPLETE, RUNBOOK_ACCEPTANCE_PENDING}
)

RUNBOOK_PERMISSION_PROFILES = frozenset({PERMISSION_STANDARD, PERMISSION_REPO_CONFIGURED_AUTO})

STOP_LOCALLY_REVIEW_READY = "LOCALLY_REVIEW_READY"
STOP_QUEUE_EXHAUSTED = "QUEUE_EXHAUSTED"
STOP_DEADLINE_REACHED = "DEADLINE_REACHED"
STOP_OWNER_DECISION_REQUIRED = "OWNER_DECISION_REQUIRED"
STOP_PROVIDER_QUOTA_EXHAUSTED = "PROVIDER_QUOTA_EXHAUSTED"
RUNBOOK_STOP_CONDITIONS = frozenset(
    {
        STOP_LOCALLY_REVIEW_READY,
        STOP_QUEUE_EXHAUSTED,
        STOP_DEADLINE_REACHED,
        STOP_OWNER_DECISION_REQUIRED,
        STOP_PROVIDER_QUOTA_EXHAUSTED,
    }
)

# Every field defaults to the maximally-restrictive (safe) value. A runbook can
# only ever be as permissive as the repository's own AGENTS.md/CI policy; this
# dict is a per-run explicit acknowledgement/summary, never a technical bypass.
DEFAULT_SAFETY_PROFILE: dict[str, bool] = {
    "forbid_auto_merge": True,
    "forbid_force_push": True,
    "forbid_reset_hard": True,
    "forbid_destructive_clean": True,
    "forbid_branch_worktree_deletion": True,
    "forbid_secret_exposure": True,
    "forbid_production_mutation": True,
    "forbid_unauthorized_billable_calls": True,
    "forbid_paid_ci_runners": True,
    "forbid_weakening_tests": True,
}

DEFAULT_STOP_CONDITIONS: tuple[str, ...] = (
    STOP_LOCALLY_REVIEW_READY,
    STOP_QUEUE_EXHAUSTED,
    STOP_DEADLINE_REACHED,
    STOP_OWNER_DECISION_REQUIRED,
    STOP_PROVIDER_QUOTA_EXHAUSTED,
)


@dataclass
class Runbook:
    id: str
    name: str
    preset: str
    objective: str
    source_ref: str
    branch: str
    worktree: str
    parent_worker: str
    max_duration_minutes: int
    worker_routes: dict[str, list[str]] = field(default_factory=dict)
    concurrency: dict[str, int] = field(default_factory=dict)
    safety_profile: dict[str, bool] = field(default_factory=lambda: dict(DEFAULT_SAFETY_PROFILE))
    checkpoint_policy: str = "after_each_green_slice"
    stop_conditions: tuple[str, ...] = DEFAULT_STOP_CONDITIONS
    permission_profile: str = PERMISSION_STANDARD
    codex_policy: str = "conserve"
    codex_auto_eligible: bool = False
    max_codex_invocations: int = 1
    phases: tuple[str, ...] = ()
    status: str = RUNBOOK_DRAFT
    task_id: str | None = None
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    started_at: str | None = None
    deadline_at: str | None = None
    ended_at: str | None = None
    recovery_note: str | None = None
    report_markdown: str | None = None
    # ENG-AGENT-13 (issue #138): which acceptance stage a code-writing runbook
    # is on (see acceptance.py's STAGE_* constants; "PENDING" before Test
    # starts, "DONE" once PR readiness passes) and the truthful PASS/FAIL/
    # NOT_APPLICABLE/NOT_REPORTED evidence recorded per stage so far, keyed by
    # stage name. A read-only runbook (e.g. the "review-only" preset) never
    # enters this pipeline and keeps the "PENDING"/{} defaults.
    acceptance_stage: str = "PENDING"
    acceptance_evidence: dict[str, Any] = field(default_factory=dict)
    # ENG-AGENT-13 final-review finding: stop_after_current_runbook used to be
    # purely informational (this runbook launches exactly one session task,
    # so "no further phases are separately queued" was true) -- false now
    # that a successful session task triggers further daemon-scheduled work
    # (the acceptance pipeline). This durable flag lets reconcile_runbooks
    # actually withhold that work once the current worker finishes, exactly
    # like an explicit Stop does, instead of silently letting it proceed.
    stop_after_current_requested: bool = False
    # ENG-CP-03 (issue #165): the managed project this runbook (and therefore
    # its acceptance state and evidence) belongs to. A runbook always executes
    # against its project's own repository root, never the Control Plane's cwd.
    project_id: str | None = None

    def to_row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "preset": self.preset,
            "objective": self.objective,
            "source_ref": self.source_ref,
            "branch": self.branch,
            "worktree": self.worktree,
            "parent_worker": self.parent_worker,
            "max_duration_minutes": self.max_duration_minutes,
            "worker_routes": json.dumps(self.worker_routes),
            "concurrency": json.dumps(self.concurrency),
            "safety_profile": json.dumps(self.safety_profile),
            "checkpoint_policy": self.checkpoint_policy,
            "stop_conditions": json.dumps(list(self.stop_conditions)),
            "permission_profile": self.permission_profile,
            "codex_policy": self.codex_policy,
            "codex_auto_eligible": 1 if self.codex_auto_eligible else 0,
            "max_codex_invocations": self.max_codex_invocations,
            "phases": json.dumps(list(self.phases)),
            "status": self.status,
            "task_id": self.task_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "deadline_at": self.deadline_at,
            "ended_at": self.ended_at,
            "recovery_note": self.recovery_note,
            "report_markdown": self.report_markdown,
            "acceptance_stage": self.acceptance_stage,
            "acceptance_evidence": json.dumps(self.acceptance_evidence),
            "stop_after_current_requested": 1 if self.stop_after_current_requested else 0,
            "project_id": self.project_id,
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Runbook":
        return cls(
            id=row["id"],
            name=row["name"],
            preset=row["preset"],
            objective=row["objective"],
            source_ref=row["source_ref"],
            branch=row["branch"],
            worktree=row["worktree"],
            parent_worker=row["parent_worker"],
            max_duration_minutes=row["max_duration_minutes"],
            worker_routes=json.loads(row["worker_routes"] or "{}"),
            concurrency=json.loads(row["concurrency"] or "{}"),
            safety_profile=json.loads(row["safety_profile"] or "{}"),
            checkpoint_policy=row["checkpoint_policy"],
            stop_conditions=tuple(json.loads(row["stop_conditions"] or "[]")),
            permission_profile=row["permission_profile"],
            codex_policy=row["codex_policy"] if "codex_policy" in row.keys() else "conserve",
            codex_auto_eligible=bool(row["codex_auto_eligible"]) if "codex_auto_eligible" in row.keys() else False,
            max_codex_invocations=row["max_codex_invocations"] if "max_codex_invocations" in row.keys() else 1,
            phases=tuple(json.loads(row["phases"] or "[]")),
            status=row["status"],
            task_id=row["task_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            started_at=row["started_at"],
            deadline_at=row["deadline_at"],
            ended_at=row["ended_at"],
            recovery_note=row["recovery_note"],
            report_markdown=row["report_markdown"],
            acceptance_stage=row["acceptance_stage"] if "acceptance_stage" in row.keys() else "PENDING",
            acceptance_evidence=(
                json.loads(row["acceptance_evidence"] or "{}") if "acceptance_evidence" in row.keys() else {}
            ),
            stop_after_current_requested=(
                bool(row["stop_after_current_requested"]) if "stop_after_current_requested" in row.keys() else False
            ),
            project_id=row["project_id"] if "project_id" in row.keys() else None,
        )
