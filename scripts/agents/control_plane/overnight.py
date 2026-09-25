"""ENG-AO-05 (issue #9): bounded, restart-safe continuous project advancement.

An *overnight session* is a durable, operator-created record that binds the
existing pieces -- Quick Start runbooks, the daemon loop, ``advance_after_success``
task resolution, worktree/write ownership, the acceptance pipeline and the event
log -- into "keep advancing this project until a bound or stop condition".

It is deliberately not a queue or a second scheduler:

* The daemon calls :func:`tick` once per poll; the dashboard/API/CLI only create and
  control the durable record (``create_session``/``pause_session``/...).
* Nothing is planned in advance. Every product task is resolved from the project's
  *current* repository truth through :mod:`advancement` immediately before it
  starts, after the previous task's merge has been verified and the checkout
  refreshed. The session stores counters and the current pointer, never a task list.
* At most one product runbook of the session's project is active at a time.
  Provider choice, context/bot/free-fallback/model-freshness behaviour, review and
  exact-tree acceptance all stay inside the normal runbook path; the session never
  names a provider and never enables API/paid fallback (a route that only offers
  paid/API workers stops the session).
* Merging is never assumed. It happens only when the operator explicitly authorised
  it for this session *and* the project opted in via the ``overnight_merge``
  capability, and then only through the existing ``prepare_merge``/``merge``
  operations with all of their blockers. Otherwise the session stops at
  ``owner_action_required`` once a task is accepted.
"""

from __future__ import annotations

import datetime as _dt
import re
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..registry import Registry
from .advancement import (
    ADV_BLOCKED,
    ADV_NEXT_SELECTED,
    ADV_NO_ELIGIBLE_TASK,
    ADV_OWNER_DECISION_REQUIRED,
    STOP_ACTIVE_WRITER_CONFLICT,
    Starter,
    _acceptance_problem,
    _default_starter,
    advance_after_success,
    resolve_next_task,
)
from .advancement_lease import advancement_lease
from .models import (
    RUNBOOK_CANCELLED,
    RUNBOOK_DRAFT,
    RUNBOOK_OWNER_ACTION_REQUIRED,
    RUNBOOK_SUCCEEDED,
    RUNBOOK_TERMINAL_STATES,
    Runbook,
    utc_now_iso,
)
from .state import State

SESSION_ACTIVE = "ACTIVE"
SESSION_PAUSED = "PAUSED"
SESSION_STOPPING = "STOPPING"
SESSION_STOPPED = "STOPPED"
SESSION_COMPLETE = "COMPLETE"
LIVE_STATES = frozenset({SESSION_ACTIVE, SESSION_PAUSED, SESSION_STOPPING})
FINAL_STATES = frozenset({SESSION_STOPPED, SESSION_COMPLETE})

# Machine-readable stop reasons (``stop_kind``); ``stop_reason`` carries the prose.
KIND_DEADLINE = "deadline_reached"
KIND_TASK_LIMIT = "task_limit_reached"
KIND_NO_ELIGIBLE = "no_eligible_task"
KIND_OWNER_ACTION = "owner_action_required"
KIND_MERGE_BLOCKED = "merge_blocked"
KIND_BLOCKING_FAILURE = "blocking_failure"
KIND_PROVIDER = "provider_unavailable"
KIND_POLICY = "policy_blocked"
KIND_PROJECT = "project_unavailable"
KIND_OPERATOR_STOP = "operator_stop"
KIND_OPERATOR_CANCEL = "operator_cancelled"
KIND_WRITER_CONFLICT = "active_writer_conflict"
KIND_STALE_TRUTH = "stale_repository_state"
KIND_INTERNAL = "internal_error"

MERGE_CAPABILITY = "overnight_merge"
MAX_DURATION_SECONDS = 48 * 3600
ONE_WRITER_NOTE = "one write-capable product task at a time; read-only stages keep normal scheduler caps"
NO_PAID_NOTE = "paid/API fallback is never used; only-paid routes stop the session"

_DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([hms])", re.I)


class OvernightError(ValueError):
    pass


def parse_duration(value: str | int | float) -> int:
    """``"10h"``, ``"90m"``, ``"2h30m"`` -> seconds; a bare number means hours."""

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value) * 3600
    else:
        text = str(value).strip().lower()
        if re.fullmatch(r"\d+(?:\.\d+)?", text):
            seconds = float(text) * 3600
        else:
            parts = _DURATION_RE.findall(text)
            if not parts or _DURATION_RE.sub("", text).strip():
                raise OvernightError(f"unrecognised duration {value!r}; use e.g. 10h, 90m, 2h30m")
            if len({u for _, u in parts}) != len(parts):
                raise OvernightError(f"unrecognised duration {value!r}; each unit may appear once")
            seconds = sum(float(n) * {"h": 3600, "m": 60, "s": 1}[u] for n, u in parts)
    seconds = int(seconds)
    if seconds <= 0:
        raise OvernightError("duration must be positive")
    if seconds > MAX_DURATION_SECONDS:
        raise OvernightError(f"duration exceeds the {MAX_DURATION_SECONDS // 3600}h maximum")
    return seconds


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _iso(when: _dt.datetime) -> str:
    return when.isoformat(timespec="seconds")


def _parse(value: str | None) -> _dt.datetime | None:
    try:
        return _dt.datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


# ----------------------------------------------------------------- default ops


@dataclass
class OvernightOps:
    """The three repository/merge seams. Production defaults shell out to git/gh;
    tests substitute deterministic fakes (no network, no provider)."""

    refresh_truth: Callable[[Any], dict[str, Any]]
    merge_state: Callable[[Any, Runbook], str]  # "merged" | "open" | "unknown"
    merge: Callable[[State, Any, Runbook], tuple[bool, str]]


def _git(root: Path, *args: str, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=timeout, check=False)


def default_refresh_truth(project: Any) -> dict[str, Any]:
    """Fetch, and fast-forward the project's clean default-branch checkout only.

    The checkout is what the task source is read from, so it must reflect merged truth.
    Never merges, resets, cleans or touches a dirty/other-branch checkout. ``ok`` is False
    (fail closed) when a configured ``origin`` cannot be fetched, the checkout is dirty or not
    on the default branch (it is left untouched), or it cannot be fast-forwarded; a project
    with no ``origin`` is local-only and stays ok.
    """

    root = Path(project.local_repo_root)
    branch = project.default_branch or "main"
    out: dict[str, Any] = {"ok": True, "fetched": False, "fast_forwarded": False, "note": None}
    try:
        if _git(root, "remote", "get-url", "origin").returncode != 0:
            out["note"] = "no origin remote; local-only project"
        else:
            out["fetched"] = _git(root, "fetch", "--prune", "origin").returncode == 0
            if not out["fetched"]:
                out.update(ok=False, note="git fetch origin failed; repository truth could not be refreshed")
            else:
                current = _git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
                dirty = bool(_git(root, "status", "--porcelain").stdout.strip())
                if current != branch or dirty:
                    out.update(
                        ok=False,
                        note=f"canonical checkout is {'dirty' if dirty else 'on ' + current}, not a clean {branch}; "
                        "it is left untouched and cannot be treated as current repository truth",
                    )
                elif _git(root, "merge", "--ff-only", f"origin/{branch}").returncode == 0:
                    out["fast_forwarded"] = True
                else:
                    out.update(ok=False, note=f"could not fast-forward {branch} to origin/{branch}")
        out["head_sha"] = _git(root, "rev-parse", "HEAD").stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        out.update(ok=False, note=f"git refresh failed: {exc}")
    return out


def default_merge_state(project: Any, runbook: Runbook) -> str:
    root = Path(project.local_repo_root)
    pr = (runbook.acceptance_evidence or {}).get("pr_readiness") or {}
    head = pr.get("head_sha")
    try:
        if head and _git(root, "merge-base", "--is-ancestor", head, f"origin/{project.default_branch or 'main'}").returncode == 0:
            return "merged"
        if pr.get("pr_number") and project.github_remote:
            view = subprocess.run(
                ["gh", "pr", "view", str(pr["pr_number"]), "--repo", project.github_remote, "--json", "state", "--jq", ".state"],
                cwd=root, capture_output=True, text=True, timeout=30, check=False,
            )
            if view.returncode == 0:
                return "merged" if view.stdout.strip() == "MERGED" else "open"
    except (OSError, subprocess.TimeoutExpired):
        pass
    return "unknown"


class _MergeContext:
    """The slice of CommandContext ``git_operation`` reads, pinned to the session's project
    (not whichever project the dashboard has selected)."""

    def __init__(self, state: State, project: Any) -> None:
        self.state = state
        self.selected_project = project
        self.selected_project_id = project.project_id
        self.project_root = Path(project.local_repo_root)


def default_merge(state: State, project: Any, runbook: Runbook) -> tuple[bool, str]:
    """The existing gated two-step merge (prepare_merge -> confirmed merge) -- no shortcut."""

    from .operations import OperationError, git_operation

    ctx = _MergeContext(state, project)
    try:
        prepared = git_operation(ctx, action="prepare_merge", path=runbook.worktree)
        if prepared.get("state") != "SUCCEEDED":
            return False, f"merge preparation blocked: {prepared.get('message')}"
        merged = git_operation(ctx, action="merge", path=runbook.worktree, confirm=True, prepare_id=prepared["id"])
    except OperationError as exc:
        return False, str(exc)
    return merged.get("state") == "SUCCEEDED", str(merged.get("message") or merged.get("state"))


def default_ops() -> OvernightOps:
    return OvernightOps(refresh_truth=default_refresh_truth, merge_state=default_merge_state, merge=default_merge)


# ------------------------------------------------------------------- recording


def _event(state: State, session: dict[str, Any], message: str, level: str = "info") -> None:
    state.record_event(
        category="overnight", level=level, project_id=session["project_id"],
        message=f"overnight {session['session_id']}: {message}",
    )


# Fields the operator commands and the daemon both write. The daemon only changes them when it
# deliberately transitions the session (deadline stop, final stop); ordinary progress saves
# keep whatever the operator committed meanwhile.
_CONTROL_FIELDS = frozenset(
    {"state", "stop_requested", "stop_kind", "stop_reason", "ended_at", "resumable", "next_advancement"}
)


def _save(state: State, session: dict[str, Any], *, own_state: bool = False) -> None:
    """Persist the daemon's view atomically, merged onto the latest committed record.

    A session already in a final state is never resurrected by a stale in-memory copy.
    ``session`` is refreshed in place with the merged result.
    """

    def apply(fresh: dict[str, Any]) -> dict[str, Any]:
        if fresh["state"] in FINAL_STATES:
            return fresh
        merged = dict(fresh)
        for key, value in session.items():
            if key == "resume_count" or key == "updated_at" or (key in _CONTROL_FIELDS and not own_state):
                continue
            merged[key] = value
        return merged

    merged = state.mutate_overnight_session(session["session_id"], apply)
    if merged is not None:
        session.clear()
        session.update(merged)


def _control(state: State, session_id: str, change: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    """Apply an operator change atomically to the latest committed record."""

    def apply(fresh: dict[str, Any]) -> dict[str, Any]:
        change(fresh)
        return fresh

    record = state.mutate_overnight_session(session_id, apply)
    if record is None:
        raise OvernightError(f"no overnight session {session_id!r}")
    return record


def _finish(
    state: State, session: dict[str, Any], final: str, kind: str, reason: str, *, level: str = "info",
    resumable: bool = False,
) -> None:
    """Record the final stop. ``resumable`` marks an owner-action stop the operator can clear and resume."""

    session.update(
        state=final, stop_kind=kind, stop_reason=reason, stop_requested=None, ended_at=utc_now_iso(),
        resumable=resumable, next_advancement=f"{final}: {kind}",
    )
    _save(state, session, own_state=True)
    if session["state"] == final and session.get("stop_kind") == kind:
        _event(state, session, f"final stop {final} [{kind}]: {reason}", level)


def _fingerprint(project: Any) -> dict[str, Any]:
    return {
        "local_repo_root": str(project.local_repo_root),
        "github_remote": project.github_remote,
        "default_branch": project.default_branch,
    }


# --------------------------------------------------------------- session control


def active_session(state: State, project_id: str) -> dict[str, Any] | None:
    return next((s for s in state.list_overnight_sessions(project_id=project_id) if s["state"] in LIVE_STATES), None)


def get_session(state: State, session_id: str | None, project_id: str | None = None) -> dict[str, Any]:
    if session_id:
        session = state.get_overnight_session(session_id)
    elif project_id:
        sessions = state.list_overnight_sessions(project_id=project_id)
        session = active_session(state, project_id) or (sessions[0] if sessions else None)
    else:
        session = None
    if session is None:
        raise OvernightError(f"no overnight session found ({session_id or project_id or 'none specified'})")
    return session


def create_session(
    state: State,
    *,
    project_id: str,
    duration: str | int | float,
    max_tasks: int | None = None,
    merge_authorized: bool = False,
    now: _dt.datetime | None = None,
) -> dict[str, Any]:
    from .project_registry import UnknownProjectError, get_project

    seconds = parse_duration(duration)
    if max_tasks is not None:
        if isinstance(max_tasks, bool) or int(max_tasks) < 1:
            raise OvernightError("max_tasks must be a positive integer")
        max_tasks = int(max_tasks)
    try:
        project = get_project(state, project_id)
    except UnknownProjectError as exc:
        raise OvernightError(str(exc)) from None
    row = state.get_project(project_id)
    if row is not None and not row["enabled"]:
        raise OvernightError(f"project {project_id!r} is disabled")
    if active_session(state, project_id):
        raise OvernightError(f"project {project_id!r} already has a live overnight session")
    if merge_authorized and (project.capabilities.get(MERGE_CAPABILITY) or "").strip().lower() not in {"true", "1", "yes"}:
        raise OvernightError(
            f"project {project_id!r} does not opt in to unattended merges (capability {MERGE_CAPABILITY!r}); "
            "start without merge authorization and merge the accepted PR yourself"
        )
    started = now or _now()
    session = {
        "session_id": f"ovn-{uuid.uuid4().hex[:12]}",
        "project_id": project_id,
        "state": SESSION_ACTIVE,
        "created_at": _iso(started),
        "started_at": _iso(started),
        "duration_seconds": seconds,
        "deadline_at": _iso(started + _dt.timedelta(seconds=seconds)),
        "max_tasks": max_tasks,
        "accepted_count": 0,
        "accepted_runbook_ids": [],
        "current_runbook_id": None,
        "current_task": None,
        "last_accepted": None,
        "merge_authorized": bool(merge_authorized),
        "merge_authorized_at": _iso(started) if merge_authorized else None,
        "project_fingerprint": _fingerprint(project),
        "stop_reason": None,
        "stop_kind": None,
        "stop_requested": None,
        "next_advancement": "resolving the first eligible task",
        "resume_count": 0,
        "restart_count": 0,
        "notes": {"one_writer": ONE_WRITER_NOTE, "paid_fallback": NO_PAID_NOTE},
    }
    if not state.insert_overnight_session_if_none_live(session, LIVE_STATES):
        raise OvernightError(f"project {project_id!r} already has a live overnight session")
    _event(
        state, session,
        f"session created for {project_id}: duration {seconds}s (deadline {session['deadline_at']}), "
        f"max_tasks={max_tasks}, merge_authorized={session['merge_authorized']}",
    )
    _event(state, session, "session started")
    return session


def pause_session(state: State, session_id: str) -> dict[str, Any]:
    """Withhold further advancement. The in-flight runbook is deliberately untouched
    (its acceptance pipeline must not be interrupted); it settles and the session waits."""

    def change(record: dict[str, Any]) -> None:
        if record["state"] != SESSION_ACTIVE:
            raise OvernightError(f"session is {record['state']}; only an ACTIVE session can be paused")
        record.update(state=SESSION_PAUSED, next_advancement="paused by operator")

    session = _control(state, get_session(state, session_id)["session_id"], change)
    _event(state, session, "paused by operator")
    return session


def resume_session(state: State, session_id: str, *, now: _dt.datetime | None = None) -> dict[str, Any]:
    def change(record: dict[str, Any]) -> None:
        if any(
            other["session_id"] != record["session_id"] and other["state"] in LIVE_STATES
            for other in state.list_overnight_sessions(project_id=record["project_id"])
        ):
            raise OvernightError("another live overnight session exists for this project; stop it first")
        if record["state"] == SESSION_STOPPED and record.get("resumable"):
            deadline = _parse(record["deadline_at"])
            if deadline and (now or _now()) >= deadline:
                raise OvernightError("session deadline has passed; start a new session")
            record.update(stop_kind=None, stop_reason=None, ended_at=None, resumable=False)
        elif record["state"] != SESSION_PAUSED:
            raise OvernightError(f"session is {record['state']} ({record.get('stop_kind')}); it cannot be resumed")
        record.update(
            state=SESSION_ACTIVE, resume_count=int(record.get("resume_count", 0)) + 1,
            next_advancement="resolving from current repository truth",
        )

    session = _control(state, get_session(state, session_id)["session_id"], change)
    _event(state, session, "resumed by operator")
    return session


def stop_after_current(state: State, session_id: str) -> dict[str, Any]:
    def change(record: dict[str, Any]) -> None:
        if record["state"] not in {SESSION_ACTIVE, SESSION_PAUSED}:
            raise OvernightError(f"session is {record['state']}; nothing to stop")
        record.update(
            state=SESSION_STOPPING, stop_requested="operator", next_advancement="stopping after the current task"
        )

    session = _control(state, get_session(state, session_id)["session_id"], change)
    _event(state, session, "stop-after-current requested; the current task continues to its checkpoint")
    return session


def _cancel_runbook(state: State, runbook: Runbook, supervisor: Any) -> None:
    """Cancel a live runbook through the existing owned-process termination + stop path."""

    from . import runbooks as runbooks_module

    if runbook.status in RUNBOOK_TERMINAL_STATES or runbook.status == RUNBOOK_DRAFT:
        return
    if supervisor is not None:
        # The current implementation task plus any live acceptance-stage task (reviewer/tester) of this run.
        live = {t.id for t in state.list_tasks(state="RUNNING") if t.runbook_id == runbook.id}
        for task_id in ([runbook.task_id] if runbook.task_id else []) + sorted(live - {runbook.task_id}):
            supervisor.terminate_task(task_id)
    try:
        runbooks_module.stop_runbook(state=state, runbook_id=runbook.id)
    except runbooks_module.RunbookError as exc:
        raise OvernightError(str(exc)) from None


def stop_now(state: State, session_id: str, *, supervisor: Any = None) -> dict[str, Any]:
    """Safe stop: end the session and cancel its current runbook through the existing
    owned-process termination path (never kills by name).

    The final state is committed first, from the latest record, so a daemon tick can no longer
    start or record another run afterwards (a tick that already started one sees the final state
    and cancels it). Then whatever runbook the *committed* record points at is cancelled.
    """

    target = get_session(state, session_id)["session_id"]
    was_final: list[bool] = []

    def change(record: dict[str, Any]) -> None:
        if record["state"] in FINAL_STATES:
            was_final.append(True)
            return
        record.update(
            state=SESSION_STOPPED, stop_kind=KIND_OPERATOR_STOP, stop_requested=None, resumable=False,
            stop_reason="stopped by operator (current runbook cancelled)", ended_at=utc_now_iso(),
            next_advancement=f"{SESSION_STOPPED}: {KIND_OPERATOR_STOP}",
        )

    session = _control(state, target, change)
    if was_final:
        return session
    rb_id = session.get("current_runbook_id")
    runbook = state.get_runbook(rb_id) if rb_id else None
    try:
        if runbook is not None:
            _cancel_runbook(state, runbook, supervisor)
    finally:
        _event(state, session, f"final stop {SESSION_STOPPED} [{KIND_OPERATOR_STOP}]: stopped by operator")
    return session


# ------------------------------------------------------------------------ view


def session_view(state: State, session: dict[str, Any], *, now: _dt.datetime | None = None) -> dict[str, Any]:
    view = dict(session)
    now = now or _now()
    deadline = _parse(session.get("deadline_at"))
    live = session["state"] in LIVE_STATES
    view["time_remaining_seconds"] = max(0, int((deadline - now).total_seconds())) if deadline and live else 0
    runbook = state.get_runbook(session["current_runbook_id"]) if session.get("current_runbook_id") else None
    task = state.get_task(runbook.task_id) if runbook is not None and runbook.task_id else None
    view["current_runbook"] = (
        {
            "id": runbook.id, "status": runbook.status, "stage": runbook.acceptance_stage,
            "worker": (task.worker if task else runbook.parent_worker),
            "provider": (task.selected_provider if task else None),
        }
        if runbook is not None
        else None
    )
    return view


def list_views(state: State, project_id: str | None = None, *, now: _dt.datetime | None = None) -> list[dict[str, Any]]:
    return [session_view(state, s, now=now) for s in state.list_overnight_sessions(project_id=project_id)]


def owns_runbook(state: State, runbook: Runbook) -> bool:
    """True while a live session is driving ``runbook`` (so generic auto-advance stays out)."""

    return any(
        s.get("current_runbook_id") == runbook.id and s["state"] in LIVE_STATES
        for s in state.list_overnight_sessions(project_id=runbook.project_id)
    )


def recover_on_restart(state: State) -> int:
    """Daemon start: durable sessions simply continue. Record it; nothing is relaunched here --
    the next tick re-inspects runbook/process ownership and refreshes truth before any new task."""

    count = 0
    for session in state.list_overnight_sessions():
        if session["state"] in LIVE_STATES:
            session["restart_count"] = int(session.get("restart_count", 0)) + 1
            _save(state, session)  # progress-only merge: control fields stay as committed
            _event(state, session, "daemon restart: session recovered from durable state", "warning")
            count += 1
    return count


# ------------------------------------------------------------------------ tick


def tick(
    *,
    state: State,
    registry: Registry | None = None,
    supervisor: Any = None,
    scheduler: Any = None,
    now: _dt.datetime | None = None,
    ops: OvernightOps | None = None,
    starter: Starter | None = None,
    allow_launch: bool = True,
    github_issue_fetcher: Any = None,
) -> int:
    """Advance every live session by at most one transition. Returns sessions touched."""

    ops = ops or default_ops()
    now = now or _now()
    touched = 0
    for session in state.list_overnight_sessions():
        session = state.get_overnight_session(session["session_id"]) or session  # latest committed control state
        if session["state"] not in LIVE_STATES:
            continue
        before = (session["state"], session.get("current_runbook_id"), session.get("accepted_count"))
        try:
            _step(
                state, session, now, ops=ops, registry=registry, supervisor=supervisor, scheduler=scheduler,
                starter=starter, allow_launch=allow_launch, github_issue_fetcher=github_issue_fetcher,
            )
        except Exception as exc:  # noqa: BLE001 - fail closed; never a retry loop
            _finish(state, session, SESSION_STOPPED, KIND_INTERNAL, f"internal error: {exc}", level="error")
        touched += 1 if (session["state"], session.get("current_runbook_id"), session.get("accepted_count")) != before else 0
    return touched


def _project_problem(state: State, session: dict[str, Any]) -> tuple[Any, str | None]:
    from .project_registry import UnknownProjectError, get_project

    try:
        project = get_project(state, session["project_id"])
    except UnknownProjectError:
        return None, f"project {session['project_id']!r} was removed"
    row = state.get_project(session["project_id"])
    if row is not None and not row["enabled"]:
        return project, f"project {session['project_id']!r} is disabled"
    if _fingerprint(project) != session["project_fingerprint"]:
        return project, "project identity (repository root/remote/default branch) changed since the session started"
    if not Path(project.local_repo_root).is_dir():
        return project, f"project checkout {project.local_repo_root} is unavailable"
    return project, None


def _sync_control(state: State, session: dict[str, Any]) -> None:
    """Adopt the operator's latest committed control state after slow work (fetch/merge/start)."""

    fresh = state.get_overnight_session(session["session_id"])
    if fresh is not None:
        session["state"] = fresh["state"]
        session["stop_requested"] = fresh.get("stop_requested")


def _bound_reached(session: dict[str, Any], now: _dt.datetime) -> tuple[str, str, str] | None:
    """(final_state, kind, reason) when a bound or stop request means no further task may begin."""

    if session.get("stop_requested") == "operator":
        return SESSION_STOPPED, KIND_OPERATOR_STOP, "stopped after the current task at the operator's request"
    deadline = _parse(session["deadline_at"])
    if session.get("stop_requested") == "deadline" or (deadline and now >= deadline):
        return SESSION_COMPLETE, KIND_DEADLINE, "duration limit reached; no further task was started"
    if session.get("max_tasks") and session["accepted_count"] >= session["max_tasks"]:
        return SESSION_COMPLETE, KIND_TASK_LIMIT, f"accepted-task limit ({session['max_tasks']}) reached"
    return None


def _step(
    state: State, session: dict[str, Any], now: _dt.datetime, *, ops: OvernightOps, registry: Any,
    supervisor: Any, scheduler: Any, starter: Starter | None, allow_launch: bool, github_issue_fetcher: Any,
) -> None:
    project, problem = _project_problem(state, session)
    if problem:
        _finish(state, session, SESSION_STOPPED, KIND_PROJECT, problem, level="warning")
        return
    if session["state"] == SESSION_PAUSED:
        return  # hold everything (counting, merge, next task) until the operator resumes
    rb_id = session.get("current_runbook_id")
    runbook = state.get_runbook(rb_id) if rb_id else None
    if rb_id and runbook is None:
        _finish(state, session, SESSION_STOPPED, KIND_BLOCKING_FAILURE, f"runbook {rb_id} no longer exists", level="warning")
        return

    if runbook is not None and runbook.status not in RUNBOOK_TERMINAL_STATES:
        # The one write-capable task is still live (also the restart case): never launch another.
        deadline = _parse(session["deadline_at"])
        if deadline and now >= deadline and not session.get("stop_requested"):
            session.update(
                state=SESSION_STOPPING, stop_requested="deadline",
                next_advancement="deadline reached; current task continues to its checkpoint",
            )
            _save(state, session, own_state=True)
            _event(
                state, session,
                f"deadline reached during {runbook.id} ({runbook.status}); stop-after-current: the worker is not killed and "
                "no further task will start",
                "warning",
            )
        return

    if not allow_launch:
        return  # global stop-after-current: observe only; settle/merge/launch wait for the daemon to be released
    if runbook is not None:
        if not _settle_runbook(state, session, runbook, project, now, ops):
            return  # stopped/finished, or waiting on the owner
    bound = _bound_reached(session, now)
    if bound:
        _finish(state, session, *bound)
        return
    if not allow_launch:
        return
    _launch_next(
        state, session, project, runbook, now, ops=ops, registry=registry, supervisor=supervisor,
        scheduler=scheduler, starter=starter, github_issue_fetcher=github_issue_fetcher,
    )


def _settle_runbook(
    state: State, session: dict[str, Any], runbook: Runbook, project: Any, now: _dt.datetime, ops: OvernightOps
) -> bool:
    """Handle the terminal current runbook. Returns True when the session may go on to the
    bounds check / next task, False when it stopped (or is holding at a pause)."""

    if runbook.status == RUNBOOK_SUCCEEDED:
        problem = _acceptance_problem(runbook)
        if problem:
            _finish(state, session, SESSION_STOPPED, KIND_BLOCKING_FAILURE, f"acceptance not complete: {problem}", level="warning")
            return False
    elif runbook.status == RUNBOOK_OWNER_ACTION_REQUIRED:
        _finish(state, session, SESSION_STOPPED, KIND_OWNER_ACTION, f"runbook {runbook.id} requires owner action: {runbook.recovery_note or 'see runbook report'}", level="warning", resumable=False)
        return False
    elif runbook.status == RUNBOOK_CANCELLED:
        _finish(state, session, SESSION_STOPPED, KIND_OPERATOR_CANCEL, f"runbook {runbook.id} was cancelled", level="warning")
        return False
    else:
        note = runbook.recovery_note or ""
        kind = KIND_BLOCKING_FAILURE
        _finish(state, session, SESSION_STOPPED, kind, f"runbook {runbook.id} ended {runbook.status}; bounded handling did not resolve it. {note}".strip(), level="warning")
        return False

    if runbook.id not in session["accepted_runbook_ids"]:
        session["accepted_runbook_ids"].append(runbook.id)
        session["accepted_count"] += 1
        task_id = (session.get("current_task") or {}).get("task_id")
        session["last_accepted"] = {"runbook_id": runbook.id, "task_id": task_id, "accepted_at": utc_now_iso(), "merged": False}
        _save(state, session)
        _event(state, session, f"task {task_id or runbook.id} accepted via {runbook.id} ({session['accepted_count']} this session)")

    ops.refresh_truth(project)
    status = ops.merge_state(project, runbook)
    _sync_control(state, session)  # the fetch can be slow; honour a pause/stop committed meanwhile
    if session["state"] == SESSION_PAUSED or session["state"] in FINAL_STATES:
        return False  # pause holds merge and next-task work; resume re-checks from truth
    if status != "merged" and session["merge_authorized"]:
        if (project.capabilities.get(MERGE_CAPABILITY) or "").strip().lower() not in {"true", "1", "yes"}:
            _finish(state, session, SESSION_STOPPED, KIND_POLICY, f"project no longer enables {MERGE_CAPABILITY!r}", level="warning")
            return False
        ok, message = ops.merge(state, project, runbook)
        _event(state, session, f"authorised merge of {runbook.id} {'succeeded' if ok else 'blocked'}: {message}", "info" if ok else "warning")
        if not ok:
            _finish(state, session, SESSION_STOPPED, KIND_MERGE_BLOCKED, f"merge/reconciliation not completed: {message}", level="warning")
            return False
        ops.refresh_truth(project)
        status = ops.merge_state(project, runbook)
        if status != "merged":
            _finish(state, session, SESSION_STOPPED, KIND_MERGE_BLOCKED, "merge reported success but the repository does not show it merged", level="warning")
            return False
    elif status != "merged":
        bound = _bound_reached(session, now)
        note = (
            f"task accepted (runbook {runbook.id}) but its PR is not merged and this session is not authorised to merge; "
            f"merge it, then resume (merge state: {status})"
        )
        if bound:
            session["last_accepted"]["pending_merge"] = True
            _finish(state, session, *bound[:2], f"{bound[2]}. {note}")
        else:
            _finish(state, session, SESSION_STOPPED, KIND_OWNER_ACTION, note, level="warning", resumable=True)
        return False

    session["last_accepted"]["merged"] = True
    session["last_accepted"].pop("pending_merge", None)
    _event(state, session, f"merge/reconciliation of {runbook.id} verified in repository truth")
    return True


def _launch_next(
    state: State, session: dict[str, Any], project: Any, after: Runbook | None, now: _dt.datetime, *,
    ops: OvernightOps, registry: Any, supervisor: Any, scheduler: Any, starter: Starter | None,
    github_issue_fetcher: Any,
) -> None:
    truth = ops.refresh_truth(project)
    if not truth.get("ok", True):
        _finish(
            state, session, SESSION_STOPPED, KIND_STALE_TRUTH,
            f"cannot start another task from stale repository truth: {truth.get('note')}", level="warning",
        )
        return
    _event(
        state, session,
        f"repository truth refreshed (head {str(truth.get('head_sha') or 'unknown')[:12]}"
        f"{'; ' + truth['note'] if truth.get('note') else ''})",
    )

    conflict: list[str] = []
    refused: list[str] = []
    started_here: list[str] = []  # runbooks this very call started through the guarded starter

    def guarded(key: str, proj: Any) -> Runbook:
        fresh = state.get_overnight_session(session["session_id"]) or session
        deadline = _parse(fresh["deadline_at"])
        if fresh["state"] != SESSION_ACTIVE or (deadline and now >= deadline):
            refused.append(fresh["state"])  # an operator pause/stop or the deadline landed mid-tick
            raise OvernightError("session is no longer active or its deadline has passed")
        foreign = _foreign_writer(state, proj.project_id, since=session["started_at"])
        if foreign:
            conflict.append(foreign)
            raise OvernightError(f"one writer at a time: {foreign}")
        launched = (
            starter or _default_starter(state=state, registry=registry, supervisor=supervisor, scheduler=scheduler) or _no_starter
        )(key, proj)
        started_here.append(launched.id)
        return launched

    if after is not None:
        # ENG-AO-07: successor advancement of an accepted runbook is single-writer like reconcile.
        with advancement_lease(state, after.id, holder="overnight_tick") as owner:
            if not owner:
                _event(state, session, f"advancement of {after.id} withheld: another Octarel process owns it; retrying next tick")
                return
            record = advance_after_success(
                state=state, runbook=after, registry=registry, supervisor=supervisor, scheduler=scheduler,
                starter=guarded, auto_start=True, github_issue_fetcher=github_issue_fetcher,
            )
    else:
        record, option = resolve_next_task(
            state=state, project_id=project.project_id, registry=registry, github_issue_fetcher=github_issue_fetcher
        )
        if record["state"] == ADV_NEXT_SELECTED and not record.get("started_runbook_id"):
            try:
                record["started_runbook_id"] = guarded(option.key, project).id
            except Exception as exc:  # noqa: BLE001
                record.update(state=ADV_BLOCKED, reason=f"could not start {record['next_task']['task_id']}: {exc}", stop_kind="start_failed")

    if refused:
        # Not a failed start: the committed control state (PAUSED/STOPPING/final) or the deadline
        # wins and the next tick applies it. Never turn a pause into a dead session.
        _event(state, session, f"task start withheld: session became {refused[0]} while resolving")
        return
    session["last_resolution"] = {
        k: record.get(k) for k in ("state", "next_task", "selection_reason", "reason", "stop_kind", "dependency_status", "evidence")
    }
    nxt = record.get("next_task") or {}
    started = record.get("started_runbook_id")
    if record["state"] == ADV_NEXT_SELECTED and started:
        if started not in started_here:
            # Never adopt a run this call did not start itself. A live run for the next task that we
            # cannot prove is ours (someone else's, or ours orphaned by a crash before its pointer was
            # saved) is left running untouched and the session fails closed -- no takeover, no duplicate.
            _finish(
                state, session, SESSION_STOPPED, KIND_WRITER_CONFLICT,
                f"a live run for the next task already exists ({started}) and is not provably owned by this "
                "session; it is left running untouched. Verify it, then start a new session", level="warning",
            )
            return
        stray = _foreign_writer(state, project.project_id, since=session["started_at"], exclude=started)
        if stray:
            try:
                _cancel_runbook(state, state.get_runbook(started), supervisor)  # our own fresh run
            except OvernightError as exc:
                _event(state, session, f"could not cancel {started} after a writer conflict: {exc}", "error")
            _finish(state, session, SESSION_STOPPED, KIND_WRITER_CONFLICT, stray, level="warning")
            return
        session.update(
            current_runbook_id=started, current_task=nxt,
            next_advancement=f"running {nxt.get('task_id')} as {started}",
        )
        _save(state, session)
        if session["state"] in FINAL_STATES:
            # The operator stopped the session while the start was in flight: the just-started
            # runbook must not be left running unowned.
            try:
                started_rb = state.get_runbook(started)
                if started_rb is not None:
                    _cancel_runbook(state, started_rb, supervisor)
                _event(state, session, f"stop arrived during task start; cancelled the just-started runbook {started}", "warning")
            except OvernightError as exc:
                _event(state, session, f"stop arrived during task start; could not cancel {started}: {exc}", "error")
            return
        _event(state, session, f"task selected: {nxt.get('task_id')} ({record.get('selection_reason')})")
        _event(state, session, f"runbook started: {started}")
        _event(state, session, f"provider selected by normal routing: {record.get('intended_worker') or 'unresolved'} / {record.get('intended_provider') or 'unresolved'}")
        return

    reason = record.get("reason") or "next task could not be started"
    if record["state"] == ADV_NO_ELIGIBLE_TASK:
        _finish(state, session, SESSION_COMPLETE, KIND_NO_ELIGIBLE, reason)
    elif record["state"] == ADV_OWNER_DECISION_REQUIRED:
        _finish(state, session, SESSION_STOPPED, KIND_OWNER_ACTION, reason, level="warning", resumable=True)
    elif conflict or record.get("stop_kind") == STOP_ACTIVE_WRITER_CONFLICT:
        _finish(state, session, SESSION_STOPPED, KIND_WRITER_CONFLICT, conflict[0] if conflict else reason, level="warning")
    elif record.get("stop_kind") == "provider_unavailable":
        _finish(state, session, SESSION_STOPPED, KIND_PROVIDER, f"{reason} (no eligible provider; {NO_PAID_NOTE})", level="warning")
    elif record.get("stop_kind") == "policy_blocked":
        _finish(state, session, SESSION_STOPPED, KIND_POLICY, reason, level="warning")
    else:
        _finish(state, session, SESSION_STOPPED, record.get("stop_kind") or KIND_BLOCKING_FAILURE, reason, level="warning")


def _no_starter(key: str, project: Any) -> Runbook:
    raise OvernightError("no runbook starter is available (registry/supervisor missing)")


def _foreign_writer(state: State, project_id: str, *, since: str, exclude: str | None = None) -> str | None:
    """Any other live product runbook or running write task of this project.

    A DRAFT created during this session is what a crash between "create runbook" and "start"
    leaves behind; treating it as a live writer means a restart can never launch a duplicate.
    Older operator-authored drafts are not launched work and do not block.
    """

    for other in state.list_runbooks(project_id=project_id):
        if other.id == exclude:
            continue
        if other.status == RUNBOOK_DRAFT:
            if (other.created_at or "") >= since:
                return f"runbook {other.id} (DRAFT created this session) may be an interrupted start"
        elif other.status not in RUNBOOK_TERMINAL_STATES:
            return f"runbook {other.id} ({other.status}) is already active in this project"
    for task in state.list_tasks(state="RUNNING", project_id=project_id):
        if exclude and (task.runbook_id == exclude or task.id.startswith(f"{exclude}-")):
            continue
        if task.kind == "write":
            return f"write task {task.id} is already running in this project"
    return None
