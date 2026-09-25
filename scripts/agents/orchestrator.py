#!/usr/bin/env python3
"""ENG-AGENT-02: durable local orchestrator daemon + control-center CLI entry.

Extends ENG-AGENT-01's single-shot ``scripts.agents.orchestrate`` with a
persistent process that schedules dependency-safe, concurrency-capped
delegated-worker tasks and can additionally serve a developer-only web
dashboard. Both surfaces share one command-application layer
(``control_plane/commands.py``) so behavior never diverges between the CLI and
the dashboard's control endpoints.

Usage (run as a module, like ``scripts.agents.orchestrate``, so its relative
imports resolve — a bare ``python scripts/agents/orchestrator.py`` fails with
``ImportError: attempted relative import with no known parent package``)::

    python -m scripts.agents.orchestrator run [--dry-run] [--poll-interval 2.0]
    python -m scripts.agents.orchestrator dashboard [--port 8877] [--host 127.0.0.1]
    python -m scripts.agents.orchestrator status
    python -m scripts.agents.orchestrator enqueue --id T1 --task-ref ENG-AGENT-02 \\
        --role focused-tests --worker opencode2-gemini-flash-lite -- "run tests"
    python -m scripts.agents.orchestrator start --id T1
    python -m scripts.agents.orchestrator provider-disable --name grok-build
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import signal
import sys
import time
from pathlib import Path

from .control_plane.advancement_lease import (
    claim_daemon_authority,
    release_daemon_authority,
)
from .control_plane.commands import CommandContext, CommandError, apply_command
from .control_plane.dispatch import managed_admit
from .control_plane.overnight import recover_on_restart as recover_overnight_on_restart
from .control_plane.overnight import tick as overnight_tick
from .control_plane.provider_state import reconcile_provider_states
from .control_plane.recovery import run_recovery
from .control_plane.remote_access import RemoteAccessConfig, RemoteAccessState
from .control_plane.runbooks import reconcile_runbooks, recover_runbooks_on_restart
from .control_plane.scheduler import ConcurrencyPolicy, Scheduler
from .control_plane.state import (
    DB_FILENAME,
    STATE_DIRNAME,
    State,
    default_db_path,
    resolve_standalone_db_path,
)
from .control_plane.supervisor import Supervisor
from .registry import load_registry
from .runner import ENV_CANONICAL_REPO_ROOT, repo_root

DEFAULT_DASHBOARD_HOST = "127.0.0.1"
DEFAULT_DASHBOARD_PORT = 8877
DEFAULT_POLL_INTERVAL_SECONDS = 2.0

# ENG-AGENT-02-S6 (issue #95): the dashboard must never bind to a non-loopback
# interface, even accidentally via a hand-typed --host. Remote access is meant
# to arrive exclusively through a local Cloudflare Tunnel process connecting
# to loopback, never a direct public bind (an explicit repository Non-goal).
ENV_ALLOW_NONLOOPBACK_BIND = "OCTAGES_ORCH_ALLOW_NONLOOPBACK_BIND"
_LOOPBACK_HOSTNAMES = {"localhost"}


def _is_loopback_host(host: str) -> bool:
    if host.strip().lower() in _LOOPBACK_HOSTNAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


EXIT_OK = 0
EXIT_USAGE = 2
EXIT_COMMAND_FAILED = 3


def _publish_runtime_identity(ctx: CommandContext, *, port: int) -> None:
    """Record that this process is the canonical Octarel dashboard (CPX-06)."""

    if not os.environ.get("OCTAREL_CODE_ROOT") and not os.environ.get("OCTAREL_STATE_DIR"):
        return
    try:
        import atexit

        from .control_plane.cutover import (
            clear_runtime_identity,
            write_runtime_identity,
        )
        from .control_plane.project import cp_code_root

        state_root = (
            Path(ctx.state.db_path).parent if str(ctx.state.db_path) != ":memory:" else cp_code_root()
        )
        selected = ctx.selected_project
        write_runtime_identity(
            state_dir=state_root,
            code_root=cp_code_root(),
            pid=os.getpid(),
            port=port,
            selected_project_id=selected.project_id if selected is not None else None,
            selected_project_root=str(selected.local_repo_root) if selected is not None else None,
        )
        atexit.register(clear_runtime_identity, state_root)
    except Exception:  # noqa: BLE001 - identity is diagnostic, never a start blocker
        return


def _standalone_state_root() -> Path | None:
    """Octarel production state lives with the Octarel checkout, not a managed project.

    Tests and the still-embedded Octages daemon omit these env vars and keep
    state under the context ``root``.
    """

    env_dir = os.environ.get("OCTAREL_STATE_DIR")
    if env_dir:
        return Path(env_dir)
    env_code = os.environ.get("OCTAREL_CODE_ROOT")
    if env_code:
        return Path(env_code)
    return None


def _build_context(root: Path, *, in_memory: bool = False, state_root: Path | None = None) -> CommandContext:
    registry = load_registry()
    if in_memory:
        db_path: Path | str = ":memory:"
    elif state_root is not None:
        db_path = state_root / DB_FILENAME if (state_root / DB_FILENAME).exists() or state_root.name == STATE_DIRNAME else default_db_path(state_root)
    else:
        standalone = resolve_standalone_db_path()
        db_path = standalone if standalone is not None else default_db_path(root)
    state = State(db_path)
    reconcile_provider_states(state, registry)
    defaults = ConcurrencyPolicy()
    def cap(key: str, default: int) -> int:
        raw = state.get_control_setting(key, str(default))
        try:
            return max(1, int(raw or ""))
        except ValueError:
            return default
    scheduler = Scheduler(
        ConcurrencyPolicy(
            max_global_workers=cap("max_global_workers", defaults.max_global_workers),
            max_write_workers=cap("max_write_workers", defaults.max_write_workers),
            max_extra_read_workers=cap("max_read_workers", defaults.max_extra_read_workers),
            max_heavy_jobs=cap("max_heavy_workers", defaults.max_heavy_jobs),
            max_per_provider=cap("max_provider_workers", defaults.max_per_provider),
        )
    )
    supervisor = Supervisor(registry=registry, repo_root=root, state=state)
    # ENG-CP-03 (issue #165): the one deterministic, idempotent registry
    # startup path -- auto-migrates an existing single-project installation
    # into the managed-project registry and selects a project. Safe to run on
    # every start; a failure here must never stop the Control Plane from
    # starting, since the operator needs the UI precisely in order to fix a
    # misconfigured or missing project root.
    #
    # ENG-CP-06: never treat the Octarel code checkout as OctaScene. Bootstrap
    # against OCTAREL_OCTASCENE_ROOT / a checkout that actually looks like
    # OctaScene, then rebind scheduling ``root`` to the selected project.
    try:
        from .control_plane.octascene_project import (
            ENV_OCTASCENE_ROOT,
            _looks_like_octascene,
        )
        from .control_plane.project_registry import bootstrap_registry, selected_project

        env_octascene = os.environ.get(ENV_OCTASCENE_ROOT) or os.environ.get(ENV_CANONICAL_REPO_ROOT)
        if env_octascene:
            bootstrap_root: Path | None = Path(env_octascene)
        elif _looks_like_octascene(root):
            bootstrap_root = root
        else:
            bootstrap_root = None
        bootstrap_registry(state, bootstrap_root)
        selected = selected_project(state)
        if selected is not None:
            root = selected.local_repo_root
            supervisor = Supervisor(registry=registry, repo_root=root, state=state)
    except Exception as exc:  # noqa: BLE001 - startup must not hard-fail on registry state
        state.record_event(
            category="project_registry", level="warning", message=f"registry bootstrap failed: {exc}"
        )
    return CommandContext(state=state, registry=registry, scheduler=scheduler, supervisor=supervisor, repo_root=root)


def _run_startup_recovery(ctx: CommandContext, root: Path) -> None:
    """Reconcile worktrees/tasks once per registered project, each in its own root.

    ENG-CP-03 (issue #165): recovery observes exactly one repository per call,
    so it must run once per enabled managed project against *that project's*
    checkout, with the project's id threaded through. Running it only against
    the Control Plane's own ``root`` (the pre-ENG-CP-03 behavior) both left
    other projects' worktrees unreconciled and — because the unscoped path
    replaces the whole table — destroyed their persisted rows on every start.

    A project whose checkout has gone missing is skipped with a recorded
    warning rather than aborting startup for every other project.
    """

    from .control_plane.project_registry import list_projects

    try:
        projects = list_projects(ctx.state, enabled_only=True)
    except Exception as exc:  # noqa: BLE001 - startup must not hard-fail on registry state
        ctx.state.record_event(
            category="recovery", level="warning", message=f"could not list projects for recovery: {exc}"
        )
        projects = []

    if not projects:
        # No registry yet (a database that predates ENG-CP-03 whose bootstrap
        # could not run): behave exactly as before this slice.
        run_recovery(ctx.state, root)
        return

    for project in projects:
        project_root = Path(project["local_repo_root"])
        if not project_root.is_dir():
            ctx.state.record_event(
                category="recovery",
                level="warning",
                message=f"skipped recovery for project {project['project_id']!r}: {project_root} is not a directory",
                project_id=project["project_id"],
            )
            continue
        run_recovery(ctx.state, project_root, project_id=project["project_id"])


def _resolved_canonical_repo_root(args: argparse.Namespace) -> Path:
    """Resolve canonical repository truth for this CLI invocation.

    Precedence matches ``runner.canonical_repo_root``: an explicit
    ``--canonical-repo-root`` flag, then the ``OCTAGES_ORCH_CANONICAL_REPO_ROOT``
    environment variable, then the ordinary cwd-derived lookup. Implemented
    directly against the module-level ``repo_root`` symbol (rather than
    delegating to ``runner.canonical_repo_root``'s own internal cwd fallback)
    so existing tests that monkeypatch ``orchestrator.repo_root`` to point at
    a fixture checkout keep working unchanged.
    """

    configured = getattr(args, "canonical_repo_root", None) or os.environ.get(ENV_CANONICAL_REPO_ROOT)
    if configured:
        return repo_root(Path(configured))
    return repo_root()


def _cmd_run(args: argparse.Namespace) -> int:
    root = _resolved_canonical_repo_root(args)

    if args.dry_run:
        # Zero writes, zero subprocess/model invocations: an ephemeral
        # in-memory store, never launched via the real supervisor.
        ctx = _build_context(root, in_memory=True, state_root=_standalone_state_root())
        tasks = ctx.state.list_tasks()
        runnable = ctx.scheduler.next_runnable(tasks)
        print("[dry run] no tasks are persisted yet in a fresh in-memory store" if not tasks else "")
        print(f"[dry run] would consider {len(runnable)} runnable task(s); no subprocess was started")
        for task in runnable:
            print(f"  - would start {task.id} ({task.task_ref}/{task.role} via {task.worker})")
        return EXIT_OK

    # ENG-AO-06: a daemon launched from (or left attached to) a terminal must never be suspended by
    # that terminal's job control. Reads/writes on the tty fail with EIO instead of stopping us.
    for name in ("SIGTTIN", "SIGTTOU"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), signal.SIG_IGN)
    ctx = _build_context(root, state_root=_standalone_state_root())
    # ENG-AO-07: the daemon is the authoritative advancement owner; dashboards observe while it lives.
    if not args.once and not claim_daemon_authority(ctx.state):
        print("another live Octarel daemon already holds advancement authority for this state; exiting")
        return EXIT_COMMAND_FAILED
    _run_startup_recovery(ctx, root)
    recover_runbooks_on_restart(state=ctx.state)
    recover_overnight_on_restart(ctx.state)
    print(f"orchestrator daemon started (state: {ctx.state.db_path})")
    try:
        while True:
            ctx.supervisor.poll_once()
            ctx.supervisor.reconcile_recovered_once()
            reconcile_runbooks(
                state=ctx.state,
                repo_root=root,
                registry=ctx.registry,
                supervisor=ctx.supervisor,
                scheduler=ctx.scheduler,
            )
            # ENG-AO-05: the daemon (never the dashboard) advances durable overnight sessions.
            overnight_tick(
                state=ctx.state, registry=ctx.registry, supervisor=ctx.supervisor, scheduler=ctx.scheduler,
                allow_launch=not ctx.stop_after_current,
            )
            if ctx.stop_after_current:
                still_running = [t for t in ctx.state.list_tasks() if t.state == "RUNNING"]
                if not still_running:
                    # Consume the durable one-shot request so a future daemon
                    # start does not immediately exit again.
                    ctx.state.set_control_setting("stop_after_current", "0")
                    print("stop-after-current requested and no tasks are running; exiting")
                    return EXIT_OK
            else:
                tasks = sorted(
                    (task for task in ctx.state.list_tasks() if task.state in {"PENDING", "QUEUED"}),
                    key=lambda task: (-task.priority, task.created_at, task.id),
                )
                for task in tasks:
                    if task.id in ctx.held_task_ids:
                        continue
                    managed_admit(
                        state=ctx.state,
                        registry=ctx.registry,
                        scheduler=ctx.scheduler,
                        supervisor=ctx.supervisor,
                        repo_root=root,
                        task=task,
                    )
            if args.once:
                return EXIT_OK
            time.sleep(args.poll_interval)
    except KeyboardInterrupt:
        print("\norchestrator daemon stopping (KeyboardInterrupt)")
        return EXIT_OK
    finally:
        release_daemon_authority(ctx.state)
        ctx.supervisor.shutdown_all()


def _cmd_dashboard(args: argparse.Namespace) -> int:
    import uvicorn

    from .control_plane.dashboard_api import create_app

    if not _is_loopback_host(args.host) and os.environ.get(ENV_ALLOW_NONLOOPBACK_BIND, "").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        print(
            f"error: refusing to bind the dashboard to non-loopback host {args.host!r}. Remote access is meant "
            f"to arrive through a local Cloudflare Tunnel connecting to 127.0.0.1, never a direct public bind. "
            f"Set {ENV_ALLOW_NONLOOPBACK_BIND}=1 to override if you have a specific, deliberate reason.",
            file=sys.stderr,
        )
        return EXIT_USAGE

    remote_config = RemoteAccessConfig.from_env()
    problems = remote_config.startup_problems()
    hard_stop = False
    for problem in problems:
        print(f"warning: remote access config: {problem}", file=sys.stderr)
        if "required when remote access is enabled" in problem:
            hard_stop = True
    if hard_stop:
        return EXIT_USAGE

    root = _resolved_canonical_repo_root(args)
    ctx = _build_context(root, state_root=_standalone_state_root())
    remote = RemoteAccessState.from_config(remote_config)
    app = create_app(ctx, remote=remote)
    if remote_config.enabled:
        print(
            f"remote access ENABLED: hostname={remote_config.hostname!r} team_domain={remote_config.team_domain!r} "
            f"allowlist={sorted(remote_config.allowed_emails) or '(any Access-authenticated identity)'}"
        )
    _publish_runtime_identity(ctx, port=args.port)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return EXIT_OK


def _cmd_status(args: argparse.Namespace) -> int:
    root = _resolved_canonical_repo_root(args)
    ctx = _build_context(root, state_root=_standalone_state_root())
    tasks = ctx.state.list_tasks()
    providers = ctx.state.list_provider_states()
    print(f"tasks: {len(tasks)}")
    for task in tasks:
        print(f"  - {task.id}: {task.state} ({task.task_ref}/{task.role} via {task.worker})")
    print(f"providers: {len(providers)}")
    for provider in providers:
        print(f"  - {provider.name}: {provider.state} (configured={provider.configured})")
    return EXIT_OK


def _cmd_generic(verb: str, args: argparse.Namespace) -> int:
    root = _resolved_canonical_repo_root(args)
    ctx = _build_context(root, state_root=_standalone_state_root())
    kwargs = {
        key: value
        for key, value in vars(args).items()
        if key not in ("command", "func", "canonical_repo_root") and value is not None
    }
    try:
        result = apply_command(ctx, verb, **kwargs)
    except CommandError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    print(result.message)
    return EXIT_OK if result.ok else EXIT_COMMAND_FAILED


def _cmd_overnight(args: argparse.Namespace) -> int:
    """ENG-AO-05: create/control a durable overnight session. The daemon (``run``) advances it."""

    import json

    from .control_plane.overnight import list_views

    root = _resolved_canonical_repo_root(args)
    ctx = _build_context(root, state_root=_standalone_state_root())
    action = args.overnight_cmd
    if action == "status":
        views = list_views(ctx.state, getattr(args, "project", None) or ctx.selected_project_id)
        print(json.dumps(views[0] if views else None, indent=2, sort_keys=True, default=str))
        return EXIT_OK
    verb = {
        "start": "overnight_start",
        "pause": "overnight_pause",
        "resume": "overnight_resume",
        "stop-after-current": "overnight_stop_after_current",
        "stop": "overnight_stop",
    }[action]
    if action == "start":
        kwargs = {
            "project_id": args.project, "duration": args.duration,
            "max_tasks": args.max_tasks, "authorize_merge": args.authorize_merge,
        }
    else:
        kwargs = {"session_id": args.session}
    kwargs = {k: v for k, v in kwargs.items() if v is not None}
    try:
        result = apply_command(ctx, verb, **kwargs)
    except CommandError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    print(result.message)
    return EXIT_OK if result.ok else EXIT_COMMAND_FAILED


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scripts.agents.orchestrator",
        description="ENG-AGENT-02 durable local orchestrator daemon + control-center CLI.",
    )
    parser.add_argument(
        "--canonical-repo-root",
        default=None,
        help=(
            "Path to the canonical OctaScene repository checkout to use for scheduling-relevant "
            "reads (Quick Start ledger, roadmap, git status, worktree discovery, task resolution). "
            "Overrides the OCTAGES_ORCH_CANONICAL_REPO_ROOT environment variable, which itself "
            "overrides the daemon process's own cwd. The Control Plane code checkout serving this "
            "process is never required to be this same path (ENG-AGENT-12, issue #136)."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="run the scheduling loop in the foreground")
    p_run.add_argument("--dry-run", action="store_true", help="plan one iteration; zero writes, zero subprocesses")
    p_run.add_argument("--once", action="store_true", help="run a single scheduling iteration then exit")
    p_run.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL_SECONDS)
    p_run.set_defaults(func=_cmd_run)

    p_dash = sub.add_parser("dashboard", help="serve the control-center dashboard")
    p_dash.add_argument("--host", default=DEFAULT_DASHBOARD_HOST)
    p_dash.add_argument("--port", type=int, default=DEFAULT_DASHBOARD_PORT)
    p_dash.set_defaults(func=_cmd_dashboard)

    p_status = sub.add_parser("status", help="print tasks and providers")
    p_status.set_defaults(func=_cmd_status)

    p_enqueue = sub.add_parser("enqueue", help="create a new durable task")
    p_enqueue.add_argument("--id", dest="task_id", required=True)
    p_enqueue.add_argument("--task-ref", required=True)
    p_enqueue.add_argument("--role", required=True)
    p_enqueue.add_argument("--worker", required=True)
    p_enqueue.add_argument("--kind", default=None, choices=["write", "read", "heavy"])
    p_enqueue.add_argument("--priority", type=int, default=0)
    p_enqueue.add_argument("--dep", dest="dependencies", action="append", default=None)
    p_enqueue.add_argument("--scope", dest="scopes", action="append", default=None)
    p_enqueue.add_argument("prompt", nargs=argparse.REMAINDER)
    p_enqueue.set_defaults(func=lambda a: _cmd_generic("enqueue", a))

    p_dispatch = sub.add_parser("dispatch", help="create and launch through managed ENG-AGENT-10 admission")
    p_dispatch.add_argument("--id", dest="task_id", required=True)
    p_dispatch.add_argument("--task-ref", required=True)
    p_dispatch.add_argument("--owner-ref", required=True)
    p_dispatch.add_argument("--role", required=True)
    p_dispatch.add_argument("--worker", default="claude-code", help="preferred worker; managed balancing selects")
    p_dispatch.add_argument("--kind", default="write", choices=["write", "read", "heavy"])
    p_dispatch.add_argument("--priority", type=int, default=0)
    p_dispatch.add_argument("--dep", dest="dependencies", action="append", default=None)
    p_dispatch.add_argument("--scope", dest="scopes", action="append", default=None)
    p_dispatch.add_argument("--changed-path", dest="changed_paths", action="append", default=None)
    p_dispatch.add_argument("--integration-base", default=None)
    p_dispatch.add_argument("--avoid-provider", default=None)
    p_dispatch.add_argument("--required-capability", default=None)
    p_dispatch.add_argument("--dry-run", action="store_true")
    p_dispatch.add_argument("prompt", nargs=argparse.REMAINDER)
    p_dispatch.set_defaults(func=lambda a: _cmd_generic("managed_dispatch", a))

    p_start = sub.add_parser("start", help="launch a pending/queued task now")
    p_start.add_argument("--id", dest="task_id", required=True)
    p_start.add_argument("--dry-run", dest="dry_run", action="store_true")
    p_start.set_defaults(func=lambda a: _cmd_generic("start", a))

    for name, verb in (("pause", "pause"), ("resume", "resume"), ("stop", "stop"), ("dry-run", "dry_run")):
        p = sub.add_parser(name, help=f"{verb} a task")
        p.add_argument("--id", dest="task_id", required=True)
        p.set_defaults(func=lambda a, verb=verb: _cmd_generic(verb, a))

    p_stop_after = sub.add_parser("stop-after-current", help="stop scheduling new tasks once current work finishes")
    p_stop_after.set_defaults(func=lambda a: _cmd_generic("stop_after_current", a))

    for name, verb in (
        ("provider-enable", "provider_enable"),
        ("provider-disable", "provider_disable"),
        ("provider-drain", "provider_drain"),
        ("provider-cost-clear", "provider_cost_clear"),
        ("provider-failure-clear", "provider_failure_clear"),
        ("probe", "probe"),
    ):
        p = sub.add_parser(name, help=f"{verb} a provider")
        p.add_argument("--name", required=True)
        p.set_defaults(func=lambda a, verb=verb: _cmd_generic(verb, a))

    p_cost_block = sub.add_parser("provider-cost-block", help="hard-block a provider after a budget check trips")
    p_cost_block.add_argument("--name", required=True)
    p_cost_block.add_argument("--reason", default="")
    p_cost_block.set_defaults(func=lambda a: _cmd_generic("provider_cost_block", a))

    p_writers = sub.add_parser("set-max-writers", help="change the write-worker concurrency cap")
    p_writers.add_argument("--count", type=int, required=True)
    p_writers.set_defaults(func=lambda a: _cmd_generic("set_max_writers", a))

    p_caps = sub.add_parser("set-concurrency", help="configure managed global/kind/provider caps")
    p_caps.add_argument("--global", dest="global_count", type=int)
    p_caps.add_argument("--write", dest="write_count", type=int)
    p_caps.add_argument("--read", dest="read_count", type=int)
    p_caps.add_argument("--heavy", dest="heavy_count", type=int)
    p_caps.add_argument("--provider", dest="provider_count", type=int)
    p_caps.set_defaults(func=lambda a: _cmd_generic("set_concurrency", a))

    p_prioritize = sub.add_parser("prioritize", help="set a task's priority")
    p_prioritize.add_argument("--id", dest="task_id", required=True)
    p_prioritize.add_argument("--priority", type=int, required=True)
    p_prioritize.set_defaults(func=lambda a: _cmd_generic("prioritize", a))

    p_defer = sub.add_parser("defer", help="lower a task's priority so it yields to others")
    p_defer.add_argument("--id", dest="task_id", required=True)
    p_defer.set_defaults(func=lambda a: _cmd_generic("defer", a))

    p_ovn = sub.add_parser(
        "overnight",
        help="continuous overnight advancement of a managed project (the daemon advances it)",
    )
    ovn = p_ovn.add_subparsers(dest="overnight_cmd", required=True)
    p_ovn_start = ovn.add_parser("start", help="continue <project> overnight within explicit bounds")
    p_ovn_start.add_argument("project", nargs="?", default=None, help="project id (default: the selected project)")
    p_ovn_start.add_argument("--duration", required=True, help="maximum duration, e.g. 6h, 10h, 90m, 2h30m")
    p_ovn_start.add_argument("--max-tasks", dest="max_tasks", type=int, default=None, help="stop after N accepted tasks")
    p_ovn_start.add_argument(
        "--authorize-merge", dest="authorize_merge", action="store_true",
        help="explicitly authorise gated merges for THIS session (project must set capability overnight_merge)",
    )
    p_ovn_start.set_defaults(func=_cmd_overnight)
    for name, help_text in (
        ("status", "print the session state as JSON"),
        ("pause", "hold advancement (the in-flight task keeps running)"),
        ("resume", "resume a paused session or an owner-action stop"),
        ("stop-after-current", "finish the current task, then stop"),
        ("stop", "safe stop: cancel the current runbook through owned-process termination"),
    ):
        p_ovn_x = ovn.add_parser(name, help=help_text)
        p_ovn_x.add_argument("--session", default=None, help="session id (default: the selected project's session)")
        if name == "status":
            p_ovn_x.add_argument("--project", default=None)
        p_ovn_x.set_defaults(func=_cmd_overnight)

    p_rb_create = sub.add_parser("runbook-create", help="create a DRAFT runbook from a saved preset")
    p_rb_create.add_argument("--name", default="")
    p_rb_create.add_argument("--preset", required=True)
    p_rb_create.add_argument("--source-ref", dest="source_ref", required=True)
    p_rb_create.add_argument("--branch", required=True)
    p_rb_create.add_argument("--worktree", required=True)
    p_rb_create.add_argument("--objective", default=None)
    p_rb_create.add_argument("--duration-minutes", dest="duration_minutes", type=int, default=None)
    p_rb_create.set_defaults(func=lambda a: _cmd_generic("runbook_create", a))

    p_rb_list = sub.add_parser("runbook-list", help="list durable runbooks")
    p_rb_list.set_defaults(func=_cmd_runbook_list)

    for name, verb in (
        ("runbook-start", "runbook_start"),
        ("runbook-pause", "runbook_pause"),
        ("runbook-resume", "runbook_resume"),
        ("runbook-stop", "runbook_stop"),
        ("runbook-stop-after-current", "runbook_stop_after_current"),
    ):
        p = sub.add_parser(name, help=f"{verb} a runbook")
        p.add_argument("--id", dest="runbook_id", required=True)
        p.set_defaults(func=lambda a, verb=verb: _cmd_generic(verb, a))

    return parser


def _cmd_runbook_list(args: argparse.Namespace) -> int:
    root = _resolved_canonical_repo_root(args)
    ctx = _build_context(root, state_root=_standalone_state_root())
    for runbook in ctx.state.list_runbooks():
        print(
            f"  - {runbook.id}: {runbook.status} ({runbook.name!r}, preset={runbook.preset}, source={runbook.source_ref})"
        )
    return EXIT_OK


_TOP_LEVEL_ONLY_FLAGS = ("-h", "--help", "--canonical-repo-root")


def _normalize_argv(argv: list[str]) -> list[str]:
    """Allow a bare top-level ``--dry-run``/``--once`` to default to ``run ...``.

    ``--canonical-repo-root`` is a real top-level parser flag shared by every
    subcommand (ENG-AGENT-12, issue #136), so a leading
    ``--canonical-repo-root <path> <subcommand> ...`` invocation must reach
    ``argparse`` unchanged rather than being misread as an omitted ``run``.
    """

    if argv and argv[0].startswith("-") and argv[0] not in _TOP_LEVEL_ONLY_FLAGS:
        return ["run", *argv]
    return argv


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    argv = _normalize_argv(list(argv if argv is not None else sys.argv[1:]))
    args = parser.parse_args(argv)
    if getattr(args, "prompt", None) and args.prompt and args.prompt[0] == "--":
        args.prompt = args.prompt[1:]
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
