"""One shared command-application layer used by both the CLI and the dashboard.

Every mutating verb the operator can invoke — from ``orchestrator.py``'s
argparse subcommands or from ``dashboard_api.py``'s control endpoints — goes
through :func:`apply_command` so the two surfaces can never drift into
duplicated, subtly-different logic.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..registry import REASON_AVAILABLE, REASON_DISABLED, Registry, RegistryError
from . import runbooks as runbooks_module
from .dispatch import managed_admit
from .intake import IntakeCollision, check_and_claim
from .models import (
    KIND_WRITE,
    TASK_BLOCKED,
    TASK_CANCELLED,
    TASK_PAUSED,
    TASK_PENDING,
    TASK_QUEUED,
    Runbook,
    Task,
    utc_now_iso,
)
from .provider_state import (
    STATE_AVAILABLE,
    STATE_DISABLED,
    STATE_FAILED,
    state_for_reason,
)
from .quickstart import QuickStartError, start_quickstart_option
from .runbooks import RunbookError
from .scheduler import ConcurrencyPolicy, Scheduler
from .state import State
from .supervisor import Supervisor
from .usage_policy import classify_task, new_usage_record

COMMAND_ENQUEUE = "enqueue"
COMMAND_START = "start"
COMMAND_PAUSE = "pause"
COMMAND_RESUME = "resume"
COMMAND_STOP = "stop"
COMMAND_STOP_AFTER_CURRENT = "stop_after_current"
COMMAND_PROVIDER_ENABLE = "provider_enable"
COMMAND_PROVIDER_DISABLE = "provider_disable"
COMMAND_PROVIDER_DRAIN = "provider_drain"
COMMAND_PROVIDER_COST_BLOCK = "provider_cost_block"
COMMAND_PROVIDER_COST_CLEAR = "provider_cost_clear"
COMMAND_PROVIDER_FAILURE_CLEAR = "provider_failure_clear"
COMMAND_PROBE = "probe"
COMMAND_SET_MAX_WRITERS = "set_max_writers"
COMMAND_PRIORITIZE = "prioritize"
COMMAND_DEFER = "defer"
COMMAND_DRY_RUN = "dry_run"
COMMAND_RUNBOOK_CREATE = "runbook_create"
COMMAND_RUNBOOK_UPDATE = "runbook_update"
COMMAND_RUNBOOK_START = "runbook_start"
COMMAND_RUNBOOK_PAUSE = "runbook_pause"
COMMAND_RUNBOOK_RESUME = "runbook_resume"
COMMAND_RUNBOOK_STOP = "runbook_stop"
COMMAND_RUNBOOK_STOP_AFTER_CURRENT = "runbook_stop_after_current"
COMMAND_RUNBOOK_RETRY = "runbook_retry"
COMMAND_RUNBOOK_RETRY_ACCEPTANCE = "runbook_retry_acceptance"
COMMAND_QUICKSTART_START = "quickstart_start"
COMMAND_WORKTREE_ADOPT = "worktree_adopt"
COMMAND_WORKTREE_CLEANUP = "worktree_cleanup"
COMMAND_GIT_OPERATION = "git_operation"
COMMAND_TERMINAL_HISTORY_CLEAR = "terminal_history_clear"
COMMAND_USAGE_OVERRIDE = "usage_override"
COMMAND_MANAGED_DISPATCH = "managed_dispatch"
COMMAND_SET_CONCURRENCY = "set_concurrency"
COMMAND_OVERNIGHT_START = "overnight_start"
COMMAND_OVERNIGHT_PAUSE = "overnight_pause"
COMMAND_OVERNIGHT_RESUME = "overnight_resume"
COMMAND_OVERNIGHT_STOP_AFTER_CURRENT = "overnight_stop_after_current"
COMMAND_OVERNIGHT_STOP = "overnight_stop"

ALL_COMMANDS = (
    COMMAND_ENQUEUE,
    COMMAND_START,
    COMMAND_PAUSE,
    COMMAND_RESUME,
    COMMAND_STOP,
    COMMAND_STOP_AFTER_CURRENT,
    COMMAND_PROVIDER_ENABLE,
    COMMAND_PROVIDER_DISABLE,
    COMMAND_PROVIDER_DRAIN,
    COMMAND_PROVIDER_COST_BLOCK,
    COMMAND_PROVIDER_COST_CLEAR,
    COMMAND_PROVIDER_FAILURE_CLEAR,
    COMMAND_PROBE,
    COMMAND_SET_MAX_WRITERS,
    COMMAND_PRIORITIZE,
    COMMAND_DEFER,
    COMMAND_DRY_RUN,
    COMMAND_RUNBOOK_CREATE,
    COMMAND_RUNBOOK_UPDATE,
    COMMAND_RUNBOOK_START,
    COMMAND_RUNBOOK_PAUSE,
    COMMAND_RUNBOOK_RESUME,
    COMMAND_RUNBOOK_STOP,
    COMMAND_RUNBOOK_STOP_AFTER_CURRENT,
    COMMAND_RUNBOOK_RETRY,
    COMMAND_QUICKSTART_START,
    COMMAND_WORKTREE_ADOPT,
    COMMAND_WORKTREE_CLEANUP,
    COMMAND_GIT_OPERATION,
    COMMAND_TERMINAL_HISTORY_CLEAR,
    COMMAND_USAGE_OVERRIDE,
    COMMAND_MANAGED_DISPATCH,
    COMMAND_SET_CONCURRENCY,
    COMMAND_OVERNIGHT_START,
    COMMAND_OVERNIGHT_PAUSE,
    COMMAND_OVERNIGHT_RESUME,
    COMMAND_OVERNIGHT_STOP_AFTER_CURRENT,
    COMMAND_OVERNIGHT_STOP,
)


@dataclass
class CommandResult:
    ok: bool
    message: str
    data: dict[str, Any] | None = None


@dataclass
class CommandContext:
    """Everything a command needs. Built once per process, shared by both surfaces."""

    state: State
    registry: Registry
    scheduler: Scheduler
    supervisor: Supervisor
    repo_root: Path

    # Tasks explicitly held by the operator (pause) never get scheduled, even
    # if dependencies/slots would otherwise allow it, until resumed.
    @property
    def held_task_ids(self) -> set[str]:
        raw = self.state.get_control_setting("held_task_ids", "[]") or "[]"
        try:
            return {str(item) for item in json.loads(raw)}
        except (TypeError, ValueError):
            return set()

    def set_held_task_ids(self, task_ids: set[str]) -> None:
        self.state.set_control_setting("held_task_ids", json.dumps(sorted(task_ids)))

    @property
    def stop_after_current(self) -> bool:
        return self.state.get_control_setting("stop_after_current", "0") == "1"

    # ----------------------------------------------------------- ENG-CP-03
    # The selected managed project (issue #165). Deliberately resolved from
    # durable state on every read rather than captured at construction: the
    # operator can switch projects at runtime from the Control Center, and a
    # cached contract would keep serving the previous project's root to
    # whatever was built before the switch.

    @property
    def selected_project(self) -> Any | None:
        """The selected :class:`~.project.ProjectContract`, or ``None`` if unregistered."""

        from .project_registry import selected_project

        return selected_project(self.state)

    @property
    def selected_project_id(self) -> str | None:
        project = self.selected_project
        return project.project_id if project is not None else None

    @property
    def project_root(self) -> Path:
        """The selected project's checkout, falling back to ``repo_root``.

        The fallback is the pre-ENG-CP-03 behavior and applies only when no
        project is registered/selected at all (a fresh database, or a context
        built directly by a test). It is never a fallback to this process's
        ``cwd`` or to the Control Plane's own code checkout.
        """

        project = self.selected_project
        return project.local_repo_root if project is not None else self.repo_root


class CommandError(ValueError):
    pass


def _require_task(ctx: CommandContext, task_id: str) -> Task:
    task = ctx.state.get_task(task_id)
    if task is None:
        raise CommandError(f"unknown task id {task_id!r}")
    return task


def cmd_enqueue(
    ctx: CommandContext,
    *,
    task_id: str,
    task_ref: str,
    role: str,
    worker: str,
    kind: str | None = None,
    priority: int = 0,
    dependencies: list[str] | None = None,
    scopes: list[str] | None = None,
    prompt: list[str] | None = None,
    owner_ref: str | None = None,
    required_capability: str | None = None,
    changed_paths: list[str] | None = None,
    integration_base: str | None = None,
    avoid_provider: str | None = None,
    permission_profile: str = "standard",
    codex_policy: str = "conserve",
    codex_auto_eligible: bool = False,
    max_codex_invocations: int = 1,
) -> CommandResult:
    """Create a new durable task row. Never launches anything by itself."""

    if ctx.state.get_task(task_id) is not None:
        raise CommandError(f"task id {task_id!r} already exists")
    try:
        worker_obj = ctx.registry.get(worker)
    except RegistryError as exc:
        raise CommandError(str(exc)) from None
    if role not in worker_obj.roles:
        raise CommandError(f"worker {worker!r} does not declare role {role!r}")
    try:
        intake = check_and_claim(
            state=ctx.state,
            repo_root=ctx.repo_root,
            task_ref=task_ref,
            owner_ref=owner_ref,
            source=f"enqueue:{task_id}",
            project_id=ctx.selected_project_id,
        )
    except IntakeCollision as exc:
        # Persist a visible blocked task: a collision must not disappear as a
        # transient HTTP/CLI error that an operator could unknowingly retry.
        task = Task(
            id=task_id,
            task_ref=task_ref,
            role=role,
            worker=worker,
            kind=kind or KIND_WRITE,
            state=TASK_BLOCKED,
            owner_ref=owner_ref,
            admission_state="BLOCKED",
            admission_reason=str(exc),
            last_error=str(exc),
            project_id=ctx.selected_project_id,
        )
        ctx.state.upsert_task(task)
        ctx.state.record_dispatch_decision(
            task_id=task_id,
            stable_task_id=task_ref.split(maxsplit=1)[0],
            owner_ref=owner_ref or f"task:{task_ref}",
            outcome="BLOCKED",
            reason=str(exc),
            project_id=ctx.selected_project_id,
        )
        return CommandResult(ok=False, message=str(exc), data={"task_id": task_id, "state": TASK_BLOCKED})
    resolved_kind = kind or (KIND_WRITE if worker_obj.is_write_capable else "read")
    # ``scope:`` is the persisted discriminator used by Supervisor. Escape a
    # literal prompt argument with that prefix so user text can never become a
    # repository path implicitly (existing stored commands remain compatible).
    prompt_parts = [f"literal:{part}" if part.startswith("scope:") else part for part in (prompt or [])]
    command = [f"scope:{s}" for s in (scopes or [])] + prompt_parts
    task = Task(
        id=task_id,
        task_ref=task_ref,
        role=role,
        worker=worker,
        kind=resolved_kind,
        state=TASK_PENDING,
        priority=priority,
        dependencies=tuple(dependencies or ()),
        command=tuple(command),
        owner_ref=intake.owner_ref,
        required_capability=required_capability,
        changed_paths=tuple(changed_paths or ()),
        integration_base=integration_base,
        avoid_provider=avoid_provider,
        permission_profile=permission_profile,
        codex_policy=codex_policy,
        codex_auto_eligible=bool(codex_auto_eligible),
        max_codex_invocations=max_codex_invocations,
        # ENG-CP-03 (issue #165): stamp the project this task belongs to at
        # creation. Without this a task created after the registry migration
        # would carry a NULL project_id and disappear from every project-scoped
        # view -- the one way project scoping could silently lose real work.
        project_id=ctx.selected_project_id,
    )
    ctx.state.upsert_task(task)
    ctx.state.record_event(
        category="command",
        task_id=task_id,
        message=f"enqueued ({task_ref}/{role}/{worker})",
        project_id=ctx.selected_project_id,
    )
    return CommandResult(ok=True, message=f"{task_id} enqueued", data={"task_id": task_id})


def cmd_start(ctx: CommandContext, *, task_id: str | None = None, dry_run: bool = False) -> CommandResult:
    """Start one explicit task, or the next scheduler-eligible task when omitted.

    Bare ``/start`` is a documented steering shortcut. It must never throw just
    because the queue is empty; instead it returns a normal non-success result
    that the dashboard can display. Operator-held tasks remain excluded until
    explicitly resumed.
    """

    if task_id is None:
        tasks = [task for task in ctx.state.list_tasks() if task.id not in ctx.held_task_ids]
        runnable = ctx.scheduler.next_runnable(tasks)
        if not runnable:
            return CommandResult(ok=False, message="no runnable tasks")
        task = runnable[0]
        task_id = task.id
    else:
        task = _require_task(ctx, task_id)
        held = ctx.held_task_ids
        held.discard(task_id)
        ctx.set_held_task_ids(held)

    admission = managed_admit(
        state=ctx.state,
        registry=ctx.registry,
        scheduler=ctx.scheduler,
        supervisor=ctx.supervisor,
        repo_root=ctx.repo_root,
        task=task,
        dry_run=dry_run,
    )
    return CommandResult(
        ok=admission.launched,
        message=admission.reason,
        data={"task": task_id, "state": admission.task.state, "admission": admission.outcome},
    )


def cmd_managed_dispatch(ctx: CommandContext, **fields: Any) -> CommandResult:
    """Operator-facing create+admit entrypoint using the same Control Plane path."""

    dry_run = bool(fields.pop("dry_run", False))
    created = cmd_enqueue(ctx, **fields)
    if not created.ok:
        return created
    return cmd_start(ctx, task_id=str(created.data["task_id"]), dry_run=dry_run)


def cmd_pause(ctx: CommandContext, *, task_id: str) -> CommandResult:
    task = _require_task(ctx, task_id)
    held = ctx.held_task_ids
    held.add(task_id)
    ctx.set_held_task_ids(held)
    if task.state in (TASK_PENDING, TASK_QUEUED, TASK_BLOCKED):
        task.state = TASK_PAUSED
        ctx.state.upsert_task(task)
    ctx.state.record_event(category="command", task_id=task_id, message="paused by operator")
    return CommandResult(ok=True, message=f"{task_id} paused (will not be scheduled until resumed)")


def cmd_resume(ctx: CommandContext, *, task_id: str) -> CommandResult:
    task = _require_task(ctx, task_id)
    held = ctx.held_task_ids
    held.discard(task_id)
    ctx.set_held_task_ids(held)
    if task.state == TASK_PAUSED:
        task.state = TASK_PENDING
        ctx.state.upsert_task(task)
    ctx.state.record_event(category="command", task_id=task_id, message="resumed by operator")
    return CommandResult(ok=True, message=f"{task_id} resumed")


def cmd_stop(ctx: CommandContext, *, task_id: str) -> CommandResult:
    task = _require_task(ctx, task_id)
    terminated = ctx.supervisor.terminate_task(task_id)
    task.state = TASK_CANCELLED
    task.pid = None if terminated else task.pid
    ctx.state.upsert_task(task)
    ctx.state.record_event(category="command", task_id=task_id, level="warning", message="stopped by operator")
    return CommandResult(
        ok=True,
        message=f"{task_id} cancelled" + (" and its owned process group terminated" if terminated else ""),
    )


def cmd_stop_after_current(ctx: CommandContext) -> CommandResult:
    ctx.state.set_control_setting("stop_after_current", "1")
    ctx.state.record_event(category="command", message="stop-after-current requested by operator")
    return CommandResult(ok=True, message="daemon will stop scheduling new tasks after current work finishes")


def cmd_provider_enable(ctx: CommandContext, *, name: str) -> CommandResult:
    provider = ctx.state.get_provider_state(name)
    if provider is None:
        raise CommandError(f"unknown provider {name!r}")
    if not provider.configured:
        return CommandResult(ok=False, message=f"{name} is NOT_CONFIGURED (catalog-only); cannot be enabled")
    provider.state = STATE_AVAILABLE
    provider.reason = REASON_AVAILABLE
    ctx.state.upsert_provider_state(provider)
    ctx.state.record_event(category="command", provider=name, message="provider enabled by operator")
    return CommandResult(ok=True, message=f"{name} enabled")


def cmd_provider_disable(ctx: CommandContext, *, name: str) -> CommandResult:
    provider = ctx.state.get_provider_state(name)
    if provider is None:
        raise CommandError(f"unknown provider {name!r}")
    provider.state = STATE_DISABLED
    provider.reason = REASON_DISABLED
    ctx.state.upsert_provider_state(provider)
    ctx.state.record_event(category="command", provider=name, message="provider disabled by operator")
    return CommandResult(ok=True, message=f"{name} disabled")


def cmd_provider_drain(ctx: CommandContext, *, name: str) -> CommandResult:
    """Stop routing *new* work to a provider without marking it failed/disabled."""

    provider = ctx.state.get_provider_state(name)
    if provider is None:
        raise CommandError(f"unknown provider {name!r}")
    from .provider_state import STATE_COOLING_DOWN

    provider.state = STATE_COOLING_DOWN
    ctx.state.upsert_provider_state(provider)
    ctx.state.record_event(category="command", provider=name, message="provider draining by operator")
    return CommandResult(ok=True, message=f"{name} draining (no new routed work; in-flight work is unaffected)")


def cmd_provider_cost_block(ctx: CommandContext, *, name: str, reason: str = "") -> CommandResult:
    """Hard-block a provider's routing after :func:`telemetry.check_budget` trips.

    Distinct from ``provider_disable`` (an operator choice) and
    ``QUOTA_EXHAUSTED`` (a provider-reported limit): this is this
    control plane's own spend gate.
    """

    provider = ctx.state.get_provider_state(name)
    if provider is None:
        raise CommandError(f"unknown provider {name!r}")
    from .provider_state import STATE_COST_BLOCKED

    provider.state = STATE_COST_BLOCKED
    provider.last_error = reason or "hard budget exceeded"
    ctx.state.upsert_provider_state(provider)
    ctx.state.record_event(
        category="command",
        provider=name,
        level="warning",
        message=f"provider cost-blocked ({reason or 'hard budget exceeded'})",
    )
    return CommandResult(ok=True, message=f"{name} cost-blocked")


def cmd_provider_cost_clear(ctx: CommandContext, *, name: str) -> CommandResult:
    """Release a ``COST_BLOCKED`` provider back to routable, e.g. after a budget reset."""

    provider = ctx.state.get_provider_state(name)
    if provider is None:
        raise CommandError(f"unknown provider {name!r}")
    from .provider_state import STATE_AVAILABLE, STATE_COST_BLOCKED

    if provider.state != STATE_COST_BLOCKED:
        return CommandResult(ok=False, message=f"{name} is not COST_BLOCKED (currently {provider.state})")
    provider.state = STATE_AVAILABLE
    provider.last_error = None
    ctx.state.upsert_provider_state(provider)
    ctx.state.record_event(category="command", provider=name, message="cost block cleared by operator")
    return CommandResult(ok=True, message=f"{name} cost block cleared")


def cmd_provider_failure_clear(ctx: CommandContext, *, name: str) -> CommandResult:
    """Recover a provider stuck ``FAILED`` after a resolved transient failure.

    Symmetric to ``provider_cost_clear``, but for the plain (non-cost) failure
    path: nothing else ever resets ``consecutive_failures``/``last_error``, so
    one transient flake (e.g. a headless-mode tool-permission auto-denial)
    permanently excluded a provider from every future dispatch that checks
    those fields, such as ``dispatch.py``'s diff-review eligibility rule
    (issue #159). ``provider_enable`` deliberately leaves this state alone --
    it only ever flips ``state``/``reason`` -- so an operator judgment call
    that the underlying flake is resolved needs its own explicit command.
    """

    provider = ctx.state.get_provider_state(name)
    if provider is None:
        raise CommandError(f"unknown provider {name!r}")
    if provider.state != STATE_FAILED:
        return CommandResult(ok=False, message=f"{name} is not FAILED (currently {provider.state})")
    provider.consecutive_failures = 0
    provider.last_error = None
    if provider.configured:
        provider.state = STATE_AVAILABLE
        provider.reason = REASON_AVAILABLE
    ctx.state.upsert_provider_state(provider)
    ctx.state.record_event(category="command", provider=name, message="failure state cleared by operator")
    return CommandResult(ok=True, message=f"{name} failure state cleared")


def cmd_probe(ctx: CommandContext, *, name: str) -> CommandResult:
    """Check whether a provider's CLI is installed/authorized. Never a billable call."""

    from .provider_state import STATE_COST_BLOCKED

    try:
        worker = ctx.registry.get(name)
    except RegistryError as exc:
        raise CommandError(str(exc)) from None
    cli_available = worker.cli_available()
    reason = worker.availability_reason()
    available = reason == REASON_AVAILABLE
    provider = ctx.state.get_provider_state(name)
    if provider is not None:
        provider.last_probe_at = utc_now_iso()
        # A hard budget block is this control plane's own spend gate, not a
        # CLI-availability fact — a probe must never silently clear it.
        # Release it only through the explicit `provider_cost_clear` command.
        if provider.configured and provider.state != STATE_COST_BLOCKED:
            provider.reason = reason
            # Demotes as readily as it promotes: a provider previously
            # AVAILABLE whose auth session has since expired must not stay
            # routable just because an earlier probe once found it usable
            # (Grok Build review, issue #97).
            provider.state = state_for_reason(reason)
        ctx.state.upsert_provider_state(provider)
    return CommandResult(
        ok=available,
        message=f"{name}: {reason} ({worker.cli_bin})",
        data={
            "cli_bin": worker.cli_bin,
            "cli_available": cli_available,
            "available": available,
            "reason": reason,
        },
    )


def cmd_set_max_writers(ctx: CommandContext, *, count: int) -> CommandResult:
    if count < 1:
        raise CommandError("max writers must be at least 1")
    ctx.scheduler.policy = ConcurrencyPolicy(
        max_global_workers=ctx.scheduler.policy.max_global_workers,
        max_write_workers=count,
        max_extra_read_workers=ctx.scheduler.policy.max_extra_read_workers,
        max_heavy_jobs=ctx.scheduler.policy.max_heavy_jobs,
        max_per_provider=ctx.scheduler.policy.max_per_provider,
        provider_limits=ctx.scheduler.policy.provider_limits,
    )
    ctx.state.set_control_setting("max_write_workers", str(count))
    ctx.state.record_event(category="command", message=f"max write workers set to {count}")
    return CommandResult(ok=True, message=f"max write workers set to {count}")


def cmd_set_concurrency(
    ctx: CommandContext,
    *,
    global_count: int | None = None,
    write_count: int | None = None,
    read_count: int | None = None,
    heavy_count: int | None = None,
    provider_count: int | None = None,
) -> CommandResult:
    values = {
        "global": global_count,
        "write": write_count,
        "read": read_count,
        "heavy": heavy_count,
        "provider": provider_count,
    }
    if not any(value is not None for value in values.values()):
        raise CommandError("at least one concurrency cap is required")
    if any(value is not None and value < 1 for value in values.values()):
        raise CommandError("concurrency caps must be at least 1")
    current = ctx.scheduler.policy
    ctx.scheduler.policy = ConcurrencyPolicy(
        max_global_workers=global_count or current.max_global_workers,
        max_write_workers=write_count or current.max_write_workers,
        max_extra_read_workers=read_count or current.max_extra_read_workers,
        max_heavy_jobs=heavy_count or current.max_heavy_jobs,
        max_per_provider=provider_count or current.max_per_provider,
        provider_limits=current.provider_limits,
    )
    for key, value in values.items():
        if value is not None:
            ctx.state.set_control_setting(f"max_{key}_workers", str(value))
    ctx.state.record_event(category="command", message=f"concurrency caps updated: {values}")
    return CommandResult(ok=True, message="concurrency caps updated", data=ctx.scheduler.slot_usage(ctx.state.list_tasks()).as_dict())


def cmd_prioritize(ctx: CommandContext, *, task_id: str, priority: int) -> CommandResult:
    task = _require_task(ctx, task_id)
    task.priority = priority
    ctx.state.upsert_task(task)
    ctx.state.record_event(category="command", task_id=task_id, message=f"priority set to {priority}")
    return CommandResult(ok=True, message=f"{task_id} priority set to {priority}")


def cmd_defer(ctx: CommandContext, *, task_id: str) -> CommandResult:
    return cmd_prioritize(ctx, task_id=task_id, priority=-1)


def cmd_dry_run(ctx: CommandContext, *, task_id: str) -> CommandResult:
    return cmd_start(ctx, task_id=task_id, dry_run=True)


def cmd_usage_override(ctx: CommandContext, *, runbook_id: str, reason: str) -> CommandResult:
    """Explicit, audited operator authorization for premium Codex routing."""

    runbook = ctx.state.get_runbook(runbook_id)
    if runbook is None:
        raise CommandError(f"unknown runbook {runbook_id!r}")
    clean_reason = str(reason or "").strip()
    if not clean_reason:
        raise CommandError("premium override requires a reason")
    usage = ctx.state.get_usage_governance(runbook_id)
    current_invocations = int(usage.get("codex_invocations", 0)) if usage else 0
    runbook.codex_policy = "unrestricted"
    runbook.codex_auto_eligible = True
    # A premium override authorizes exactly one additional invocation even
    # when the prior runbook budget is exhausted; it is not an unbounded flag.
    runbook.max_codex_invocations = max(runbook.max_codex_invocations, current_invocations + 1)
    ctx.state.upsert_runbook(runbook)
    if usage is None:
        usage = new_usage_record(
            runbook_id=runbook.id,
            task_id=runbook.task_id,
            classification=classify_task(role="primary-implementation"),
            codex_policy="unrestricted",
            codex_auto_eligible=True,
            max_codex_invocations=runbook.max_codex_invocations,
        )
    if usage:
        usage["codex_policy"] = "unrestricted"
        usage["codex_auto_eligible"] = True
        usage["max_codex_invocations"] = runbook.max_codex_invocations
        usage["escalation_state"] = "operator-override"
        usage["escalation_reason"] = clean_reason[:500]
        usage["escalation_history"] = [*usage.get("escalation_history", []), {
            "kind": "premium-override", "reason": clean_reason[:500], "at": utc_now_iso(),
        }]
        ctx.state.upsert_usage_governance(usage)
    ctx.state.record_event(
        category="usage_governance", task_id=runbook.task_id, level="warning",
        message=f"premium Codex override enabled for {runbook_id}: {clean_reason[:500]}",
    )
    return CommandResult(ok=True, message=f"premium override enabled for {runbook_id}")


def _runbook_to_result(runbook: Runbook, message: str) -> CommandResult:
    return CommandResult(ok=True, message=message, data={"runbook_id": runbook.id, "status": runbook.status})


def cmd_runbook_create(ctx: CommandContext, **fields: Any) -> CommandResult:
    try:
        runbook = runbooks_module.create_runbook(
            state=ctx.state,
            registry=ctx.registry,
            repo_root=ctx.repo_root,
            project_id=ctx.selected_project_id,
            **fields,
        )
    except RunbookError as exc:
        raise CommandError(str(exc)) from None
    return _runbook_to_result(runbook, f"runbook {runbook.id} created (DRAFT)")


def cmd_runbook_update(ctx: CommandContext, *, runbook_id: str, **fields: Any) -> CommandResult:
    try:
        runbook = runbooks_module.update_runbook(
            state=ctx.state, registry=ctx.registry, runbook_id=runbook_id, **fields
        )
    except RunbookError as exc:
        raise CommandError(str(exc)) from None
    return _runbook_to_result(runbook, f"runbook {runbook.id} updated")


def cmd_runbook_start(ctx: CommandContext, *, runbook_id: str, dry_run: bool = False) -> CommandResult:
    try:
        runbook = runbooks_module.start_runbook(
            state=ctx.state,
            registry=ctx.registry,
            supervisor=ctx.supervisor,
            repo_root=ctx.repo_root,
            runbook_id=runbook_id,
            dry_run=dry_run,
            scheduler=ctx.scheduler,
        )
    except RunbookError as exc:
        raise CommandError(str(exc)) from None
    return _runbook_to_result(runbook, f"runbook {runbook.id} started")


def cmd_quickstart_start(ctx: CommandContext, *, key: str, dry_run: bool = False) -> CommandResult:
    """Re-resolve ``key`` fresh (never trusting a client-supplied branch/worktree/objective),

    provision a worktree if the resolved option needs one, then create and
    start the Runbook. See ``quickstart.start_quickstart_option``.
    """

    try:
        runbook = start_quickstart_option(
            state=ctx.state,
            registry=ctx.registry,
            supervisor=ctx.supervisor,
            repo_root=ctx.project_root,
            key=key,
            dry_run=dry_run,
            scheduler=ctx.scheduler,
            project_id=ctx.selected_project_id,
            project=ctx.selected_project,
        )
    except QuickStartError as exc:
        raise CommandError(str(exc)) from None
    return _runbook_to_result(runbook, f"quickstart {key!r} started as runbook {runbook.id}")


def cmd_runbook_pause(ctx: CommandContext, *, runbook_id: str) -> CommandResult:
    try:
        runbook = runbooks_module.pause_runbook(state=ctx.state, runbook_id=runbook_id)
    except RunbookError as exc:
        raise CommandError(str(exc)) from None
    return _runbook_to_result(runbook, f"runbook {runbook.id} paused")


def cmd_runbook_resume(ctx: CommandContext, *, runbook_id: str) -> CommandResult:
    try:
        runbook = runbooks_module.resume_runbook(state=ctx.state, runbook_id=runbook_id)
    except RunbookError as exc:
        raise CommandError(str(exc)) from None
    return _runbook_to_result(runbook, f"runbook {runbook.id} resumed")


def cmd_runbook_stop(ctx: CommandContext, *, runbook_id: str) -> CommandResult:
    try:
        current = ctx.state.get_runbook(runbook_id)
        if current is not None and current.task_id:
            ctx.supervisor.terminate_task(current.task_id)
        runbook = runbooks_module.stop_runbook(state=ctx.state, runbook_id=runbook_id)
    except RunbookError as exc:
        raise CommandError(str(exc)) from None
    return _runbook_to_result(runbook, f"runbook {runbook.id} stop requested")


def cmd_runbook_stop_after_current(ctx: CommandContext, *, runbook_id: str) -> CommandResult:
    try:
        runbook = runbooks_module.stop_after_current_runbook(state=ctx.state, runbook_id=runbook_id)
    except RunbookError as exc:
        raise CommandError(str(exc)) from None
    return _runbook_to_result(runbook, f"runbook {runbook.id}: stop-after-current recorded")


def cmd_runbook_retry(ctx: CommandContext, *, runbook_id: str, worker: str) -> CommandResult:
    try:
        runbook = runbooks_module.retry_runbook(
            state=ctx.state,
            registry=ctx.registry,
            supervisor=ctx.supervisor,
            repo_root=ctx.repo_root,
            runbook_id=runbook_id,
            worker_name=worker,
            scheduler=ctx.scheduler,
        )
    except RunbookError as exc:
        raise CommandError(str(exc)) from None
    return _runbook_to_result(runbook, f"runbook {runbook.id} retried in place with {worker}")


def cmd_runbook_retry_acceptance(ctx: CommandContext, *, runbook_id: str) -> CommandResult:
    """ENG-AGENT-13 (issue #138): resume a halted acceptance stage.

    Unlike ``cmd_runbook_retry`` this never relaunches the implementation
    worker -- valid only from ``OWNER_ACTION_REQUIRED``/``BLOCKED``, once the
    operator has resolved whatever the halted stage's evidence reported.
    """

    try:
        runbook = runbooks_module.retry_acceptance(state=ctx.state, runbook_id=runbook_id)
    except (RunbookError, ValueError) as exc:
        raise CommandError(str(exc)) from None
    return _runbook_to_result(runbook, f"runbook {runbook.id} acceptance stage {runbook.acceptance_stage!r} resumed")


def cmd_worktree_adopt(ctx: CommandContext, *, path: str) -> CommandResult:
    from .operations import OperationError, adopt_worktree

    try:
        data = adopt_worktree(ctx, path=path)
    except OperationError as exc:
        raise CommandError(str(exc)) from None
    return CommandResult(ok=True, message="worktree adopted", data=data)


def cmd_worktree_cleanup(ctx: CommandContext, *, confirm: bool = False) -> CommandResult:
    from .operations import cleanup_worktrees

    data = cleanup_worktrees(ctx, confirm=confirm)
    message = (
        f"removed {len(data['removed'])} eligible finished worktree(s)"
        if confirm else f"preview: {len(data['eligible'])} eligible; {len(data['protected'])} protected"
    )
    return CommandResult(ok=True, message=message, data=data)


def cmd_git_operation(
    ctx: CommandContext,
    *,
    action: str,
    path: str,
    confirm: bool = False,
    prepare_id: str | None = None,
) -> CommandResult:
    from .operations import OperationError, git_operation

    try:
        data = git_operation(ctx, action=action, path=path, confirm=confirm, prepare_id=prepare_id)
    except OperationError as exc:
        raise CommandError(str(exc)) from None
    return CommandResult(ok=data.get("state") == "SUCCEEDED", message=str(data.get("message", data.get("state"))), data=data)


def _overnight_command(ctx: CommandContext, fn: Callable[..., dict[str, Any]], session_id: str | None, message: str, **kw: Any) -> CommandResult:
    """Run a session-control function. These only mutate the durable record; the daemon advances it."""

    from .overnight import OvernightError, get_session, session_view

    try:
        target = get_session(ctx.state, session_id, ctx.selected_project_id)["session_id"]
        session = fn(ctx.state, target, **kw)
    except OvernightError as exc:
        raise CommandError(str(exc)) from None
    return CommandResult(ok=True, message=f"overnight session {session['session_id']}: {message}", data=session_view(ctx.state, session))


def cmd_overnight_start(
    ctx: CommandContext,
    *,
    duration: Any,
    project_id: str | None = None,
    max_tasks: int | None = None,
    authorize_merge: bool = False,
) -> CommandResult:
    from .overnight import OvernightError, create_session, session_view

    project_id = project_id or ctx.selected_project_id
    if not project_id:
        raise CommandError("no project selected; pass project_id")
    try:
        session = create_session(
            ctx.state, project_id=project_id, duration=duration, max_tasks=max_tasks,
            merge_authorized=bool(authorize_merge),
        )
    except OvernightError as exc:
        raise CommandError(str(exc)) from None
    return CommandResult(
        ok=True,
        message=(
            f"overnight session {session['session_id']} started for {project_id} until {session['deadline_at']}; "
            "the Octarel daemon (octarel run) performs the advancement"
        ),
        data=session_view(ctx.state, session),
    )


def cmd_overnight_pause(ctx: CommandContext, *, session_id: str | None = None) -> CommandResult:
    from .overnight import pause_session

    return _overnight_command(ctx, pause_session, session_id, "paused")


def cmd_overnight_resume(ctx: CommandContext, *, session_id: str | None = None) -> CommandResult:
    from .overnight import resume_session

    return _overnight_command(ctx, resume_session, session_id, "resumed")


def cmd_overnight_stop_after_current(ctx: CommandContext, *, session_id: str | None = None) -> CommandResult:
    from .overnight import stop_after_current

    return _overnight_command(ctx, stop_after_current, session_id, "will stop after the current task")


def cmd_overnight_stop(ctx: CommandContext, *, session_id: str | None = None) -> CommandResult:
    from .overnight import stop_now

    return _overnight_command(ctx, stop_now, session_id, "stopped", supervisor=ctx.supervisor)


def cmd_terminal_history_clear(ctx: CommandContext) -> CommandResult:
    ctx.state.clear_terminal_commands(project_id=ctx.selected_project_id)
    ctx.state.record_event(category="terminal", message="terminal command metadata cleared by operator")
    return CommandResult(ok=True, message="terminal command metadata cleared")


_DISPATCH: dict[str, Callable[..., CommandResult]] = {
    COMMAND_ENQUEUE: cmd_enqueue,
    COMMAND_START: cmd_start,
    COMMAND_PAUSE: cmd_pause,
    COMMAND_RESUME: cmd_resume,
    COMMAND_STOP: cmd_stop,
    COMMAND_STOP_AFTER_CURRENT: cmd_stop_after_current,
    COMMAND_PROVIDER_ENABLE: cmd_provider_enable,
    COMMAND_PROVIDER_DISABLE: cmd_provider_disable,
    COMMAND_PROVIDER_DRAIN: cmd_provider_drain,
    COMMAND_PROVIDER_COST_BLOCK: cmd_provider_cost_block,
    COMMAND_PROVIDER_COST_CLEAR: cmd_provider_cost_clear,
    COMMAND_PROVIDER_FAILURE_CLEAR: cmd_provider_failure_clear,
    COMMAND_PROBE: cmd_probe,
    COMMAND_SET_MAX_WRITERS: cmd_set_max_writers,
    COMMAND_PRIORITIZE: cmd_prioritize,
    COMMAND_DEFER: cmd_defer,
    COMMAND_DRY_RUN: cmd_dry_run,
    COMMAND_RUNBOOK_CREATE: cmd_runbook_create,
    COMMAND_RUNBOOK_UPDATE: cmd_runbook_update,
    COMMAND_RUNBOOK_START: cmd_runbook_start,
    COMMAND_RUNBOOK_PAUSE: cmd_runbook_pause,
    COMMAND_RUNBOOK_RESUME: cmd_runbook_resume,
    COMMAND_RUNBOOK_STOP: cmd_runbook_stop,
    COMMAND_RUNBOOK_STOP_AFTER_CURRENT: cmd_runbook_stop_after_current,
    COMMAND_RUNBOOK_RETRY: cmd_runbook_retry,
    COMMAND_RUNBOOK_RETRY_ACCEPTANCE: cmd_runbook_retry_acceptance,
    COMMAND_QUICKSTART_START: cmd_quickstart_start,
    COMMAND_WORKTREE_ADOPT: cmd_worktree_adopt,
    COMMAND_WORKTREE_CLEANUP: cmd_worktree_cleanup,
    COMMAND_GIT_OPERATION: cmd_git_operation,
    COMMAND_TERMINAL_HISTORY_CLEAR: cmd_terminal_history_clear,
    COMMAND_USAGE_OVERRIDE: cmd_usage_override,
    COMMAND_MANAGED_DISPATCH: cmd_managed_dispatch,
    COMMAND_SET_CONCURRENCY: cmd_set_concurrency,
    COMMAND_OVERNIGHT_START: cmd_overnight_start,
    COMMAND_OVERNIGHT_PAUSE: cmd_overnight_pause,
    COMMAND_OVERNIGHT_RESUME: cmd_overnight_resume,
    COMMAND_OVERNIGHT_STOP_AFTER_CURRENT: cmd_overnight_stop_after_current,
    COMMAND_OVERNIGHT_STOP: cmd_overnight_stop,
}


def apply_command(ctx: CommandContext, verb: str, **kwargs: Any) -> CommandResult:
    """Dispatch ``verb`` to its handler. The single entry point both surfaces call."""

    handler = _DISPATCH.get(verb)
    if handler is None:
        raise CommandError(f"unknown command {verb!r}; expected one of {ALL_COMMANDS}")
    return handler(ctx, **kwargs)


__all__ = [
    "CommandContext",
    "CommandError",
    "CommandResult",
    "apply_command",
    "ALL_COMMANDS",
    "KIND_WRITE",
]
