"""OCTAREL-OPS-02 (issue #2): advance an accepted run to the *current* next eligible task.

After a runbook is finalized ``SUCCEEDED`` (acceptance passed), this module
re-reads the selected managed project's repository truth through the existing
task-source resolver (:func:`task_sources.next_eligible_task` semantics), gates
the result, records *why* it was selected (or why advancement stopped), and only
then prepares/starts the next task through the normal Quick Start path.

It is deliberately not a second scheduler: task discovery stays in
``task_sources``; worktree/branch/intake handling stays in ``quickstart``.
Nothing here caches a next-task decision as authority. A stored record is
only reused to (a) avoid starting/duplicating work once a next runbook was
already started and (b) avoid duplicate history events for an unchanged
outcome; every other call recomputes from the project's checkout.
"""

from __future__ import annotations

import re
import threading
from typing import Any, Callable

from ..registry import Registry
from .models import (
    RUNBOOK_DRAFT,
    RUNBOOK_SUCCEEDED,
    RUNBOOK_TERMINAL_STATES,
    Runbook,
    utc_now_iso,
)
from .provider_state import ROUTABLE_STATES
from .state import State
from .task_sources import (
    ADAPTER_GITHUB_ISSUES,
    ADAPTER_VIDEO_EDITOR_LEDGER,
    DiscoveredTask,
    GithubIssueFetcher,
    TaskSourceError,
    discover_tasks,
    task_source_adapter_name,
)

ADV_COMPLETED = "COMPLETED"
ADV_ADVANCING = "ADVANCING"
ADV_NEXT_SELECTED = "NEXT_SELECTED"
ADV_BLOCKED = "BLOCKED"
ADV_NO_ELIGIBLE_TASK = "NO_ELIGIBLE_TASK"
ADV_OWNER_DECISION_REQUIRED = "OWNER_DECISION_REQUIRED"

ADVANCEMENT_STATES = frozenset(
    {
        ADV_COMPLETED,
        ADV_ADVANCING,
        ADV_NEXT_SELECTED,
        ADV_BLOCKED,
        ADV_NO_ELIGIBLE_TASK,
        ADV_OWNER_DECISION_REQUIRED,
    }
)

# Stop-reason kinds persisted alongside a stop so the UI/API can be exact.
STOP_ACCEPTANCE_INCOMPLETE = "acceptance_incomplete"
STOP_DEPENDENCY_BLOCKED = "dependency_blocked"
STOP_ACTIVE_WRITER_CONFLICT = "active_writer_conflict"
STOP_PROVIDER_UNAVAILABLE = "provider_unavailable"
STOP_POLICY_BLOCKED = "policy_blocked"
STOP_STALE_REPOSITORY_STATE = "stale_repository_state"
STOP_START_FAILED = "start_failed"

COMPLETE_STATUSES = frozenset({"complete", "completed", "done", "closed", "merged", "superseded", "accepted"})
OWNER_DECISION_STATUSES = frozenset(
    {"owner-decision", "owner_decision", "owner-decision-required", "needs-owner", "needs-decision", "decision-required"}
)
# Project capability: "true" lets a selected next task start automatically.
# Absent/false means the next task is selected and prepared, then waits for the operator.
AUTO_ADVANCE_CAPABILITY = "auto_advance"

_DEPENDS_RE = re.compile(
    r"\b(?:depends[ _-]on|depends|requires|blocked[ _-]by)\s*:?\s*"
    r"(#?[A-Za-z0-9_.-]+(?:\s*(?:,|and|&)\s*#?[A-Za-z0-9_.-]+)*)",
    re.I,
)

# In-process serialization of the decide-then-start section. Durable
# idempotency across processes/restarts comes from the persisted record plus
# the active-runbook duplicate check in ``_evaluate``.
_ADVANCE_LOCK = threading.RLock()

Starter = Callable[[str, Any], Runbook]


def parse_dependencies(task: DiscoveredTask) -> tuple[str, ...]:
    """Task IDs a task declares it depends on, from its own title/notes."""

    found: list[str] = []
    for text in dict.fromkeys((task.notes or "", task.title or "")):
        for match in _DEPENDS_RE.finditer(text):
            for token in re.split(r"\s*(?:,|and|&)\s*", match.group(1).strip()):
                token = token.strip().rstrip(".")
                if token and token != task.task_id and token not in found:
                    found.append(token)
    return tuple(found)


def _mentions(runbook: Runbook, task_id: str) -> bool:
    pattern = re.compile(rf"(?<![A-Za-z0-9-]){re.escape(task_id)}(?![A-Za-z0-9-])", re.I)
    return any(pattern.search(text or "") for text in (runbook.name, runbook.source_ref, runbook.branch))


def _signature(record: dict[str, Any]) -> tuple[Any, ...]:
    return (
        record.get("state"),
        (record.get("next_task") or {}).get("task_id"),
        record.get("reason"),
        record.get("started_runbook_id"),
    )


def _base_record(runbook: Runbook) -> dict[str, Any]:
    return {
        "runbook_id": runbook.id,
        "project_id": runbook.project_id,
        "state": ADV_COMPLETED,
        "completed_task_id": None,
        "next_task": None,
        "selection_reason": None,
        "reason": None,
        "stop_kind": None,
        "dependency_status": [],
        "intended_worker": None,
        "intended_provider": None,
        "started_runbook_id": None,
        "evidence": {},
    }


def _stop(record: dict[str, Any], state: str, reason: str, kind: str | None = None) -> dict[str, Any]:
    record["state"] = state
    record["reason"] = reason
    record["stop_kind"] = kind
    return record


def _acceptance_problem(runbook: Runbook) -> str | None:
    if runbook.status != RUNBOOK_SUCCEEDED:
        return f"runbook {runbook.id} is {runbook.status}, not SUCCEEDED"
    failed = sorted(
        name for name, item in (runbook.acceptance_evidence or {}).items() if (item or {}).get("status") == "FAIL"
    )
    if failed:
        return f"acceptance stage(s) reported FAIL: {', '.join(failed)}"
    if runbook.acceptance_evidence and runbook.acceptance_stage != "DONE":
        return f"acceptance is at stage {runbook.acceptance_stage!r}, not DONE"
    return None


def _completed_task_id(runbook: Runbook, tasks: list[DiscoveredTask]) -> str | None:
    for task in tasks:
        if _mentions(runbook, task.task_id):
            return task.task_id
    return None


def accepted_run_for_task(state: State, project_id: str | None, task_id: str) -> Runbook | None:
    """The accepted implementation run for ``task_id`` in this project, if any.

    Accepted means an acceptance-pipeline run (implementation + review + test +
    checkpoint + PR readiness all recorded) that finished SUCCEEDED and DONE. A
    task source keeps listing such a task as eligible until its PR merges and its
    ledger is reconciled, so callers use this to avoid presenting it as startable.
    """

    for other in state.list_runbooks(project_id=project_id):
        if other.status == RUNBOOK_SUCCEEDED and other.acceptance_stage == "DONE" and _mentions(other, task_id):
            return other
    return None


_SETTLED_FOR_SUPERSESSION = frozenset({"FAILED", "BLOCKED", "CANCELLED", "OWNER_ACTION_REQUIRED", "DEADLINE_REACHED"})


def _work_ref(runbook: Runbook) -> str:
    """The stable work item a run is about (``V1-08`` from ``V1-08 (Split/trim…)``), else its own id."""

    head = (runbook.source_ref or "").split(maxsplit=1)
    return head[0].rstrip(":;,.()") if head else runbook.id


def completed_work_refs(project: Any) -> frozenset[str]:
    """Work items the project's own file-based task source records as complete right now.

    Read fresh from the project's checkout each call (never cached as truth). Network
    adapters (GitHub issues) are deliberately not consulted here: this runs on every
    dashboard poll.
    """

    from .task_sources import (
        ADAPTER_FILE_LEDGER,
        ADAPTER_VIDEO_EDITOR_LEDGER,
        discover_tasks,
        task_source_adapter_name,
    )

    if project is None:
        return frozenset()
    try:
        if task_source_adapter_name(project) not in {ADAPTER_FILE_LEDGER, ADAPTER_VIDEO_EDITOR_LEDGER}:
            return frozenset()
        return frozenset(t.task_id for t in discover_tasks(project) if t.status.lower() in COMPLETE_STATUSES)
    except (TaskSourceError, OSError):
        return frozenset()


def superseded_ids(
    runbooks: list[Runbook], tasks: list[Any], completed_refs: frozenset[str] = frozenset()
) -> tuple[set[str], set[str]]:
    """(runbook ids, task ids) that are history, not current work.

    A failed/blocked run is history when a *later* accepted run took over the same
    work item, or when the project's own task source records that work item complete;
    and any failed/blocked task belonging to an accepted run (a review that failed
    before the recovered acceptance succeeded) is history. Only FAILED/BLOCKED/
    CANCELLED items are ever superseded: a live task or run is always current.
    """

    accepted = [r for r in runbooks if r.status == RUNBOOK_SUCCEEDED and r.acceptance_stage == "DONE"]
    newest_accepted: dict[str, str] = {}
    for run in accepted:
        ref = _work_ref(run)
        newest_accepted[ref] = max(newest_accepted.get(ref, ""), run.created_at)
    run_ids = {run.id for run in accepted}
    for run in runbooks:
        if run.status in _SETTLED_FOR_SUPERSESSION and (
            newest_accepted.get(_work_ref(run), "") > run.created_at or _work_ref(run) in completed_refs
        ):
            run_ids.add(run.id)
    owners = [r for r in runbooks if r.id in run_ids]
    task_ids: set[str] = set()
    for task in tasks:
        if task.state not in {"FAILED", "BLOCKED", "CANCELLED"}:
            continue
        if any(task.id == r.task_id or task.id.startswith(f"{r.id}-") for r in owners):
            task_ids.add(task.id)
    return {r.id for r in owners if r.status in _SETTLED_FOR_SUPERSESSION}, task_ids


def _accepted_task_ids(state: State, project_id: str | None, tasks: list[DiscoveredTask]) -> set[str]:
    accepted: set[str] = set()
    for other in state.list_runbooks(project_id=project_id):
        if other.status == RUNBOOK_SUCCEEDED:
            accepted.update(task.task_id for task in tasks if _mentions(other, task.task_id))
    return accepted


def _route_gate(
    registry: Registry | None, state: State, role: str, preferred: str
) -> tuple[str | None, str | None, str | None]:
    """(worker, provider, blocking_reason). Unknown provider truth never blocks; known-bad does."""

    if registry is None:
        return preferred, None, None
    try:
        route = registry.route(role)
    except Exception as exc:  # noqa: BLE001 - an unroutable role is a truthful stop
        return None, None, f"no route for role {role!r}: {exc}"
    known = {p.name: p for p in state.list_provider_states()}
    ordered = sorted(route, key=lambda name: (name != preferred, route.index(name)))
    problems: list[str] = []
    for name in ordered:
        worker = registry.workers.get(name)
        if worker is None or not worker.enabled:
            problems.append(f"{name}: disabled")
            continue
        if worker.allow_api_billing or worker.cost_class == "optional-overflow":
            problems.append(f"{name}: API/paid overflow is not eligible")
            continue
        provider = known.get(name)
        if known and (provider is None or not provider.configured or provider.state not in ROUTABLE_STATES):
            problems.append(f"{name}: {getattr(provider, 'state', 'UNKNOWN')}")
            continue
        return name, worker.provider, None
    return None, None, f"no eligible worker for {role}: " + "; ".join(problems or ["route is empty"])


def _evaluate(
    *,
    state: State,
    runbook: Runbook,
    registry: Registry | None,
    github_issue_fetcher: GithubIssueFetcher | None,
) -> tuple[dict[str, Any], Any]:
    """Recompute the outcome from current repository truth. Returns (record, quickstart option)."""

    record = _base_record(runbook)
    problem = _acceptance_problem(runbook)
    if problem:
        return _stop(record, ADV_BLOCKED, f"acceptance not actually complete: {problem}", STOP_ACCEPTANCE_INCOMPLETE), None

    from .policy_loader import PolicyLoadError, load_project_policy
    from .project_registry import UnknownProjectError, get_project
    from .quickstart import _repo_head_sha, resolve_quickstart_option

    if not runbook.project_id:
        return _stop(
            record, ADV_BLOCKED, "runbook has no managed project; refusing to guess a repository", STOP_STALE_REPOSITORY_STATE
        ), None
    try:
        project = get_project(state, runbook.project_id)
    except UnknownProjectError as exc:
        return _stop(record, ADV_BLOCKED, f"stale/inconsistent state: {exc}", STOP_STALE_REPOSITORY_STATE), None
    row = state.get_project(runbook.project_id)
    if row is not None and not row["enabled"]:
        return _stop(record, ADV_BLOCKED, f"project {project.project_id!r} is disabled", STOP_POLICY_BLOCKED), None
    try:
        load_project_policy(project)
    except PolicyLoadError as exc:
        return _stop(record, ADV_BLOCKED, f"repository policy blocks execution: {exc}", STOP_POLICY_BLOCKED), None

    try:
        adapter = task_source_adapter_name(project)
        tasks = discover_tasks(project, github_issue_fetcher=github_issue_fetcher)
    except TaskSourceError as exc:
        return _stop(
            record, ADV_BLOCKED, f"stale/inconsistent repository state: {exc}", STOP_STALE_REPOSITORY_STATE
        ), None
    record["evidence"] = {
        "repo_root": str(project.local_repo_root),
        "head_sha": _repo_head_sha(project.local_repo_root),
        "task_source_adapter": adapter,
        "resolved_at": utc_now_iso(),
        "tasks_seen": len(tasks),
    }
    record["completed_task_id"] = _completed_task_id(runbook, tasks)
    by_id = {task.task_id: task for task in tasks}
    nxt = next((task for task in tasks if task.eligible), None)

    if nxt is None:
        undecided = [t.task_id for t in tasks if t.status.lower() in OWNER_DECISION_STATUSES]
        if undecided:
            return _stop(record, ADV_OWNER_DECISION_REQUIRED, "owner decision required for: " + ", ".join(undecided)), None
        blocked = [t.task_id for t in tasks if t.status.lower() == "blocked"]
        if blocked:
            return _stop(
                record, ADV_BLOCKED, "no startable task; blocked: " + ", ".join(blocked), STOP_DEPENDENCY_BLOCKED
            ), None
        return _stop(record, ADV_NO_ELIGIBLE_TASK, "the project's task source has no eligible task"), None

    record["next_task"] = {"task_id": nxt.task_id, "title": nxt.title, "source_ref": nxt.source_ref, "status": nxt.status}

    # A pending owner decision earlier in the ordered source blocks what follows it.
    for task in tasks:
        if task.task_id == nxt.task_id:
            break
        if task.status.lower() in OWNER_DECISION_STATUSES:
            return _stop(
                record, ADV_OWNER_DECISION_REQUIRED, f"owner decision required for {task.task_id} before {nxt.task_id}"
            ), None

    # Never replay accepted work (including the run that just completed).
    if nxt.task_id == record["completed_task_id"] or nxt.task_id in _accepted_task_ids(state, runbook.project_id, tasks):
        return _stop(
            record,
            ADV_BLOCKED,
            f"stale/inconsistent repository state: {nxt.task_id} was already accepted by Octarel but its "
            "task source still lists it as eligible; not replaying it",
            STOP_STALE_REPOSITORY_STATE,
        ), None

    deps: list[dict[str, str]] = []
    for dep_id in parse_dependencies(nxt):
        dep = by_id.get(dep_id)
        if dep is not None:
            status = "satisfied" if dep.status.lower() in COMPLETE_STATUSES else f"unsatisfied ({dep.status})"
        elif adapter == ADAPTER_GITHUB_ISSUES:
            status = "satisfied"  # only open issues are listed; an absent one is closed
        else:
            status = "unsatisfied (not found in task source)"
        deps.append({"task_id": dep_id, "status": status})
    record["dependency_status"] = deps
    unmet = [d["task_id"] for d in deps if not d["status"].startswith("satisfied")]
    if unmet:
        return _stop(
            record, ADV_BLOCKED, f"{nxt.task_id} waits on unmet dependencies: {', '.join(unmet)}", STOP_DEPENDENCY_BLOCKED
        ), None

    key = "continue-video-editor" if adapter == ADAPTER_VIDEO_EDITOR_LEDGER else "continue-next-task"
    try:
        option = resolve_quickstart_option(project.local_repo_root, key, project=project)
    except Exception as exc:  # noqa: BLE001
        return _stop(record, ADV_BLOCKED, f"could not prepare {nxt.task_id}: {exc}", STOP_STALE_REPOSITORY_STATE), None
    if option.unavailable_reason or option.task_id != nxt.task_id:
        return _stop(
            record,
            ADV_BLOCKED,
            option.unavailable_reason or f"repository truth changed during advancement (now {option.task_id})",
            STOP_STALE_REPOSITORY_STATE,
        ), None

    # One write-capable owner per worktree; an already-active run of the same task is adopted, not duplicated.
    for other in state.list_runbooks(project_id=runbook.project_id):
        if other.id == runbook.id or other.status in RUNBOOK_TERMINAL_STATES or other.status == RUNBOOK_DRAFT:
            continue
        if _mentions(other, nxt.task_id):
            record["started_runbook_id"] = other.id
            record["selection_reason"] = f"{nxt.task_id} is already active as runbook {other.id}"
            break
        if option.worktree and other.worktree == option.worktree:
            return _stop(
                record, ADV_BLOCKED, f"worktree {option.worktree} is owned by active runbook {other.id}", STOP_ACTIVE_WRITER_CONFLICT
            ), None
    for task in state.list_tasks(state="RUNNING", project_id=runbook.project_id):
        if task.kind == "write" and option.worktree and task.worktree == option.worktree and task.id != runbook.task_id:
            return _stop(
                record, ADV_BLOCKED, f"worktree {option.worktree} is owned by running write task {task.id}", STOP_ACTIVE_WRITER_CONFLICT
            ), None

    worker, provider, blocked = _route_gate(registry, state, "primary-implementation", option.parent_worker)
    if blocked:
        return _stop(record, ADV_BLOCKED, f"provider/quota unavailable: {blocked}", STOP_PROVIDER_UNAVAILABLE), None
    record["intended_worker"] = worker
    record["intended_provider"] = provider

    record["state"] = ADV_NEXT_SELECTED
    if record["started_runbook_id"] is None:
        sha = (record["evidence"].get("head_sha") or "unknown")[:12]
        record["selection_reason"] = (
            f"{nxt.task_id} is the first eligible task in {nxt.source_ref or adapter} at {sha}; "
            + (f"{len(deps)} declared dependencies satisfied" if deps else "no dependencies declared")
            + "; no conflicting writer; provider route available"
        )
    return record, option


def _default_starter(*, state: State, registry: Registry | None, supervisor: Any, scheduler: Any) -> Starter | None:
    if registry is None or supervisor is None:
        return None

    def start(key: str, project: Any) -> Runbook:
        from .quickstart import start_quickstart_option

        return start_quickstart_option(
            state=state, registry=registry, supervisor=supervisor, repo_root=project.local_repo_root,
            key=key, scheduler=scheduler, project_id=project.project_id, project=project,
        )

    return start


def _persist(state: State, record: dict[str, Any], previous: dict[str, Any] | None) -> None:
    state.upsert_advancement(record)
    if previous is not None and _signature(previous) == _signature(record):
        return
    nxt = (record.get("next_task") or {}).get("task_id")
    state.record_event(
        category="advancement",
        level="warning" if record["state"] in (ADV_BLOCKED, ADV_OWNER_DECISION_REQUIRED) else "info",
        message=(
            f"runbook {record['runbook_id']} advancement {record['state']}"
            + (f" next={nxt}" if nxt else "")
            + (f": {record['reason']}" if record.get("reason") else "")
        ),
        project_id=record.get("project_id"),
    )


def advance_after_success(
    *,
    state: State,
    runbook: Runbook,
    registry: Registry | None = None,
    supervisor: Any = None,
    scheduler: Any = None,
    starter: Starter | None = None,
    auto_start: bool | None = None,
    github_issue_fetcher: GithubIssueFetcher | None = None,
) -> dict[str, Any]:
    """Decide (and, when authorized, start) the successor of an accepted runbook.

    Safe to call repeatedly: once a successor runbook has been started or
    adopted the stored record is returned unchanged; otherwise the outcome is
    recomputed from current repository truth each time.
    """

    with _ADVANCE_LOCK:
        previous = state.get_advancement(runbook.id)
        if previous and previous.get("started_runbook_id"):
            return previous
        record, option = _evaluate(
            state=state, runbook=runbook, registry=registry, github_issue_fetcher=github_issue_fetcher
        )
        if record["state"] != ADV_NEXT_SELECTED or record["started_runbook_id"]:
            _persist(state, record, previous)
            return state.get_advancement(runbook.id) or record

        from .project_registry import get_project

        project = get_project(state, runbook.project_id)
        if auto_start is None:
            auto_start = (project.capabilities.get(AUTO_ADVANCE_CAPABILITY) or "").strip().lower() in {"true", "1", "yes"}
        start = starter or (
            _default_starter(state=state, registry=registry, supervisor=supervisor, scheduler=scheduler)
            if auto_start
            else None
        )
        if not auto_start or start is None:
            _persist(state, record, previous)  # selected and prepared; the operator starts it
            return state.get_advancement(runbook.id) or record

        record["state"] = ADV_ADVANCING
        _persist(state, record, previous)
        previous = dict(record)
        try:
            started = start(option.key, project)
        except Exception as exc:  # noqa: BLE001 - a failed start is a truthful stop, never a retry loop
            failed = dict(record)
            _stop(failed, ADV_BLOCKED, f"could not start {record['next_task']['task_id']}: {exc}", STOP_START_FAILED)
            _persist(state, failed, previous)
            return state.get_advancement(runbook.id) or failed
        record["state"] = ADV_NEXT_SELECTED
        record["started_runbook_id"] = started.id
        _persist(state, record, previous)
        return state.get_advancement(runbook.id) or record
