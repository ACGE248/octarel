"""One managed dispatch/admission path for CLI, Control Plane, and Runbooks.

This module composes existing task state, registry, provider state, scheduler,
usage policy and supervisor. It deliberately owns none of those concerns.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..policy import PolicyError, compose_policy_bundle, validate_policy_preservation
from ..registry import PERMISSION_STANDARD, Registry
from .intake import IntakeCollision, check_and_claim
from .models import (
    ADMISSION_ADMITTED,
    ADMISSION_BLOCKED,
    ADMISSION_QUEUED,
    DEPENDENCY_SATISFIED_STATES,
    KIND_READ,
    KIND_WRITE,
    TASK_BLOCKED,
    TASK_QUEUED,
    Task,
)
from .provider_state import refresh_stale_routable_candidates
from .scheduler import Scheduler
from .state import State
from .usage_policy import classify_task, codex_allowed

_COST_RANK = {
    "free-verified": 0,
    "supplemental-configured": 1,
    "metered-configured": 2,
    "premium-subscription": 3,
}


@dataclass(frozen=True)
class ManagedAdmission:
    task: Task
    launched: bool
    outcome: str
    reason: str
    scores: dict[str, tuple[Any, ...]]


def _usage_for_task(state: State, task: Task) -> dict[str, Any]:
    return state.get_usage_governance(task.runbook_id) if task.runbook_id else {}


def _candidate_scores(*, state: State, registry: Registry, task: Task) -> tuple[list[str], dict[str, tuple[Any, ...]], list[str]]:
    tasks = state.list_tasks()
    providers = {item.name: item for item in state.list_provider_states()}
    has_provider_truth = bool(providers)
    usage = _usage_for_task(state, task) or {}
    history = state.list_dispatch_decisions(limit=10000)
    selected_counts: dict[str, int] = {}
    for item in history:
        worker = item.get("selected_worker")
        if worker:
            selected_counts[str(worker)] = selected_counts.get(str(worker), 0) + 1
    provider_load: dict[str, int] = {}
    worker_load: dict[str, int] = {}
    for running in tasks:
        if running.state != "RUNNING":
            continue
        worker_load[running.worker] = worker_load.get(running.worker, 0) + 1
        worker = registry.workers.get(running.worker)
        if worker:
            provider_load[worker.provider] = provider_load.get(worker.provider, 0) + 1

    eligible: list[str] = []
    blocked: list[str] = []
    scores: dict[str, tuple[Any, ...]] = {}
    try:
        route = registry.route(task.role)
        # ENG-AGENT-07/08 already made and durably reserved this exact
        # fallback decision. Admission revalidates it; it never substitutes a
        # different worker behind the fallback state machine's back.
        if task.fallback_selected_worker and task.fallback_automatic is not None:
            route = tuple(name for name in route if name == task.fallback_selected_worker)
    except ValueError as exc:
        return [], {}, [str(exc)]
    for route_index, name in enumerate(route):
        worker = registry.get(name)
        provider = providers.get(name)
        reasons: list[str] = []
        if task.kind == KIND_WRITE and not worker.is_write_capable:
            reasons.append("not write-capable")
        if task.kind == KIND_READ and not worker.is_read_only:
            reasons.append("not read-only")
        if task.required_capability and task.required_capability not in {worker.capability, "any"}:
            reasons.append(f"capability {worker.capability} does not meet {task.required_capability}")
        if task.permission_profile != PERMISSION_STANDARD and not worker.supports_permission_profile(task.permission_profile):
            reasons.append(f"permission profile {task.permission_profile} unsupported")
        if has_provider_truth and (not provider or not provider.configured or provider.state not in {"AVAILABLE", "BUSY"}):
            reasons.append(f"provider state {getattr(provider, 'state', 'UNKNOWN')}")
        if not worker.enabled:
            reasons.append("disabled by registry")
        if worker.allow_api_billing or worker.cost_class == "optional-overflow":
            reasons.append("API/paid overflow is not eligible")
        if not registry.repository_data_reuse_allowed(name):
            reasons.append("repository-data authorization is not eligible")
        if task.avoid_provider and worker.provider == task.avoid_provider:
            reasons.append(f"provider diversity excludes {worker.provider}")
        if task.role == "diff-review" and provider and provider.consecutive_failures and provider.last_error:
            reasons.append("reviewer route has an unresolved failed adapter result")
        codex_pressure = 0.0
        if worker.provider == "OpenAI" or name.startswith("codex"):
            invocations = int(usage.get("codex_invocations", 0))
            maximum = int(usage.get("max_codex_invocations", task.max_codex_invocations))
            # ENG-AGENT-07 reserves a selected Codex fallback invocation
            # before process creation. Rechecking as though it were a new
            # unreserved request would reject the legitimate 1/1 attempt.
            reserved_fallback = task.fallback_selected_worker == name and task.fallback_automatic is not None
            if not reserved_fallback:
                allowed, why = codex_allowed(
                    policy=str(usage.get("codex_policy", task.codex_policy)),
                    classification=str(usage.get("classification", classify_task(role=task.role))),
                    auto_eligible=bool(usage.get("codex_auto_eligible", task.codex_auto_eligible)),
                    invocations=invocations,
                    max_invocations=maximum,
                )
                if not allowed:
                    reasons.append(why)
            codex_pressure = 1.0 if maximum <= 0 else invocations / maximum
        if reasons:
            blocked.append(f"{name}: {', '.join(reasons)}")
            continue
        score = (
            _COST_RANK.get(worker.cost_class, 99),
            codex_pressure,
            provider_load.get(worker.provider, 0),
            worker_load.get(name, 0),
            selected_counts.get(name, 0),
            route_index,
            name,
        )
        scores[name] = score
        eligible.append(name)
    eligible.sort(key=lambda name: scores[name])
    return eligible, scores, blocked


def _preserves_policy(*, state: State, registry: Registry, task: Task, worker_name: str, repo_root: Path) -> tuple[bool, str]:
    if not task.runbook_id or worker_name == task.worker:
        return True, ""
    usage = state.get_usage_governance(task.runbook_id) or {}
    original = (usage.get("context_manifest") or {}).get("policy_manifest")
    if not original:
        return True, ""
    try:
        replacement = compose_policy_bundle(
            root=Path(task.worktree or repo_root),
            registry=registry,
            worker_name=worker_name,
            route_role=task.role,
            acceptance_criteria="managed dispatch preserves the existing task acceptance contract",
        ).manifest
        validate_policy_preservation(original, replacement)
    except PolicyError as exc:
        return False, str(exc)
    return True, ""


def managed_admit(
    *,
    state: State,
    registry: Registry,
    scheduler: Scheduler,
    supervisor,
    repo_root: Path,
    task: Task,
    dry_run: bool = False,
) -> ManagedAdmission:
    """Check intake, select a provider, admit against the DAG/caps, then launch."""

    owner_ref = task.owner_ref or f"task:{task.task_ref.split(maxsplit=1)[0]}"
    preferred = registry.workers.get(task.worker)
    if (
        preferred is not None
        and preferred.is_read_only
        and task.kind == KIND_WRITE
        and task.required_capability is None
        and task.admission_state == "PENDING"
    ):
        # Forward-compatible reading of tasks persisted before kind inference
        # moved into enqueue. New managed requests are normalized there.
        task.kind = KIND_READ
    try:
        intake = check_and_claim(
            state=state, repo_root=repo_root, task_ref=task.task_ref, owner_ref=owner_ref,
            source=f"task:{task.id}", project_id=task.project_id,
        )
    except IntakeCollision as exc:
        return _persist_block(state, task, owner_ref, str(exc), permanent=True)
    task.owner_ref = intake.owner_ref

    # Dependency truth precedes provider/cost selection. A task that cannot
    # yet enter a real wave consumes no provider fairness/load decision.
    all_tasks = [item for item in state.list_tasks() if item.id != task.id] + [task]
    tasks_by_id = {item.id: item for item in all_tasks}
    wave = scheduler.dependency_wave(task, tasks_by_id)
    if wave is None:
        return _persist_block(
            state,
            task,
            intake.owner_ref,
            "dependency graph is missing a task or contains a cycle",
            permanent=True,
        )
    task.dependency_wave = wave
    unsatisfied = [
        dep_id
        for dep_id in task.dependencies
        if tasks_by_id[dep_id].state not in DEPENDENCY_SATISFIED_STATES
    ]
    if unsatisfied:
        return _persist_block(
            state,
            task,
            intake.owner_ref,
            f"waiting for dependencies: {', '.join(sorted(unsatisfied))}",
            permanent=False,
            wave=wave,
        )

    # ENG-AGENT-12 (issue #136): a persisted AVAILABLE/BUSY provider row can
    # be stale (e.g. an auth session expired, or a CLI stopped being
    # reachable, since the last probe). Refresh only the workers this task's
    # role would actually route to, and only those whose cached state is
    # both stale and currently routable, with the same non-billable CLI
    # probe the explicit `probe` command already uses -- never a live model
    # call, and never a provider already excluded from routing (that cannot
    # regress into being wrongly selected by staying stale). Matches the
    # existing dry-run preflight contract (ENG-AGENT-05): a `dry_run=True`
    # admission is a zero-subprocess planning check and must not spawn a
    # real CLI probe either; only an admission that can actually launch a
    # worker refreshes state first.
    if not dry_run:
        try:
            route_candidates = registry.route(task.role)
        except ValueError:
            route_candidates = ()
        refresh_stale_routable_candidates(state, registry, route_candidates)

    eligible, scores, blocked = _candidate_scores(state=state, registry=registry, task=task)
    selected = None
    policy_blocks: list[str] = []
    for candidate in eligible:
        preserved, reason = _preserves_policy(
            state=state, registry=registry, task=task, worker_name=candidate, repo_root=repo_root
        )
        if preserved:
            selected = candidate
            break
        policy_blocks.append(f"{candidate}: policy preservation failed ({reason})")
    if selected is None:
        return _persist_block(
            state,
            task,
            intake.owner_ref,
            "no eligible provider: " + "; ".join([*blocked, *policy_blocks]),
            permanent=True,
            scores=scores,
        )

    task.worker = selected
    task.selected_provider = registry.get(selected).provider
    ordered = [name for name in eligible if name != selected]
    task.selection_alternatives = tuple(ordered)
    score = scores[selected]
    task.selected_worker_reason = (
        "capability/safety/cost/load/fairness/diversity gates selected "
        f"{selected}; score={score}"
    )

    all_tasks = [item for item in state.list_tasks() if item.id != task.id] + [task]
    fact = next(item for item in scheduler.admission_facts(all_tasks) if item.task_id == task.id)
    task.dependency_wave = fact.wave
    if not fact.runnable:
        permanent = "cycle" in fact.reason or "overlap" in fact.reason
        return _persist_block(
            state,
            task,
            intake.owner_ref,
            fact.reason,
            permanent=permanent,
            selected_worker=selected,
            alternatives=ordered,
            wave=fact.wave,
            scores=scores,
        )

    task.admission_state = ADMISSION_ADMITTED
    task.admission_reason = fact.reason
    state.upsert_task(task)
    state.record_dispatch_decision(
        task_id=task.id,
        stable_task_id=intake.stable_task_id,
        owner_ref=intake.owner_ref,
        outcome=ADMISSION_ADMITTED,
        reason=task.selected_worker_reason,
        selected_worker=selected,
        alternatives=ordered,
        wave=fact.wave,
        scores={name: list(value) for name, value in scores.items()},
        project_id=task.project_id,
    )
    launched = supervisor.launch_task(task, dry_run=True) if dry_run else supervisor.launch_task(task)
    return ManagedAdmission(
        launched,
        launched.state == "RUNNING",
        launched.admission_state,
        launched.last_error or task.admission_reason or "admitted",
        scores,
    )


def _persist_block(
    state: State,
    task: Task,
    owner_ref: str,
    reason: str,
    *,
    permanent: bool,
    selected_worker: str | None = None,
    alternatives: list[str] | tuple[str, ...] = (),
    wave: int | None = None,
    scores: dict[str, tuple[Any, ...]] | None = None,
) -> ManagedAdmission:
    task.admission_state = ADMISSION_BLOCKED if permanent else ADMISSION_QUEUED
    task.admission_reason = reason[:1000]
    task.last_error = task.admission_reason if permanent else None
    task.state = TASK_BLOCKED if permanent else TASK_QUEUED
    task.dependency_wave = wave
    if selected_worker:
        task.worker = selected_worker
    task.selection_alternatives = tuple(alternatives)
    state.upsert_task(task)
    try:
        stable_id = task.task_ref.split(maxsplit=1)[0]
        state.record_dispatch_decision(
            task_id=task.id,
            stable_task_id=stable_id,
            owner_ref=owner_ref,
            outcome=task.admission_state,
            reason=task.admission_reason,
            selected_worker=selected_worker,
            alternatives=alternatives,
            wave=wave,
            scores={name: list(value) for name, value in (scores or {}).items()},
            project_id=task.project_id,
        )
    except Exception:
        # Persistence of the task block is authoritative; malformed legacy
        # task refs must not turn a safety block into a launch.
        pass
    return ManagedAdmission(task, False, task.admission_state, task.admission_reason, scores or {})
