"""Developer-only control-center dashboard: a second, fully independent FastAPI app.

This module never imports anything from ``app/`` or ``frontend/``. It binds to
``127.0.0.1`` only on its own port (default ``8877``) and stays available
whether or not the product app is running. ENG-AGENT-02-S9 supersedes the old
probe-only decision with an explicitly owned, fixed-command ``make run``
development subprocess; it never manages an externally started process or
production runtime.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as _dt
import fcntl
import os
import pty
import re
import shutil
import socket
import struct
import subprocess
import termios
import threading
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import model_catalog, native_models
from ..redaction import redact_text
from .agent_activity import latest_subagents, list_attempts, read_attempt
from .commands import CommandContext, CommandError, apply_command
from .operations import (
    AppLifecycleManager,
    OperationError,
    delegation_evidence,
    derived_roadmap,
    list_worktree_statuses,
    refresh_repository_health,
    repository_health,
    sanitize_terminal_command,
)
from .provider_state import (
    NON_ROUTABLE_STATES,
    provider_state_age_seconds,
    provider_state_freshness,
)
from .quickstart import list_quickstart_options, resolve_quickstart_option
from .remote_access import (
    AccessIdentityError,
    RemoteAccessConfig,
    RemoteAccessState,
    extract_access_token,
    origin_matches_hostname,
    request_carries_access_assertion,
)
from .routing import MODE_SINGLE_PRIMARY, compute_routing
from .runbooks import list_presets
from . import manager_chat as _manager_chat
from .steering import (
    DESTRUCTIVE_VERBS,
    nl_ai_route_available,
    parse_steering_text,
)
from .telemetry import (
    SOURCE_LOCAL_ACCOUNTING,
    account_facts_for_worker,
    checkpoint_status,
    claude_quota_windows,
    execution_route_for_cost_class,
)

# Runbook lifecycle commands are destructive/state-changing exactly like their
# task-level counterparts; the same client-tamper-proof confirm gate applies.
RUNBOOK_DESTRUCTIVE_COMMANDS = frozenset({"runbook_stop", "runbook_stop_after_current"})
CONFIRM_COMMANDS = frozenset({"terminal_history_clear", "usage_override", "overnight_stop"})

DASHBOARD_DIR = Path(__file__).with_name("dashboard")
DEFAULT_OCTASCENE_HOST = "127.0.0.1"
DEFAULT_OCTASCENE_PORT = 8765
_PROBE_TIMEOUT_SECONDS = 0.35
CHECKPOINT_CACHE_TTL_SECONDS = 20.0
WORKTREE_STATUS_CACHE_TTL_SECONDS = 10.0
RECONCILE_INTERVAL_SECONDS = 0.5
# /api/usage spawns a real (non-billable, local-status-only) CLI subprocess
# per worker that has one — deliberately not part of the 2-second dashboard
# refresh poll. This TTL bounds how often it can actually run even if a
# client polls it directly, so opening/switching views repeatedly can never
# turn into "no giant provider API fan-out" (see AGENTS.md perf guidance).
USAGE_CACHE_TTL_SECONDS = 60.0

# ENG-AGENT-02-S6 (issue #95): security headers appropriate for a same-origin,
# no-external-resource local dashboard. Applied unconditionally (remote mode
# on or off) since they are harmless for local use and cost nothing. The CSP
# has no external hosts or inline scripts. xterm.js performs measured terminal
# layout through element style attributes, so CSP3 permits style attributes
# only; inline style elements/scripts remain blocked.
SECURITY_HEADERS: dict[str, str] = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; style-src-attr 'unsafe-inline'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
}
STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
TERMINAL_MAX_INPUT_BYTES = 16_384


def _terminal_info(repo_root: Path) -> dict[str, Any]:
    root = repo_root.resolve()
    venv = root / ".venv"
    venv_active = (venv / "bin" / "python").is_file()
    shell = shutil.which("zsh") or "/bin/zsh"
    return {
        "title": "OctaScene Control Center Terminal",
        "repository": str(root),
        "branch": _git_current_branch(root) or "UNKNOWN",
        "virtual_environment": "Active" if venv_active else "Unavailable",
        "shell": Path(shell).name,
        "venv_path": str(venv) if venv_active else None,
    }


def _terminal_websocket_identity(websocket: WebSocket, remote: RemoteAccessState) -> str | None:
    """Authorize a shell handshake without trusting a loopback proxy peer.

    A direct local browser must be loopback-to-loopback. A request whose Origin
    is the configured remote hostname must carry and pass the existing
    Cloudflare Access assertion/allowlist. The tunnel itself connects from
    loopback, so peer address alone can never authorize remote shell access.
    """

    origin = websocket.headers.get("origin") or ""
    parsed = urlparse(origin)
    client_host = websocket.client.host if websocket.client else ""
    local_hosts = {"127.0.0.1", "::1", "localhost"}
    if client_host in local_hosts and parsed.hostname in local_hosts:
        return "local"
    if not remote.config.enabled or not origin_matches_hostname(origin, remote.config.hostname):
        return None
    if not request_carries_access_assertion(websocket.headers, websocket.cookies):
        return None
    try:
        identity = remote.verifier.verify(extract_access_token(websocket.headers, websocket.cookies))
    except AccessIdentityError:
        return None
    return identity.email if remote.verifier.is_allowed(identity) else None


def _terminal_child_env(repo_root: Path) -> dict[str, str]:
    """Build a minimal non-secret environment; never inherit tokens/credentials."""

    allowed = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "USER", "LOGNAME", "TMPDIR")
    env = {key: os.environ[key] for key in allowed if os.environ.get(key)}
    env.update({"TERM": "xterm-256color", "SHELL": shutil.which("zsh") or "/bin/zsh"})
    # ENG-AO-09: the shell opens in the selected project's checkout; Octarel's own active virtual environment
    # must not leak onto its PATH (only the project's own .venv, below, may lead it).
    from .managed_environment import isolated_environment

    env = isolated_environment(env, None)
    venv = repo_root.resolve() / ".venv"
    if (venv / "bin" / "python").is_file():
        env["VIRTUAL_ENV"] = str(venv)
        env["PATH"] = f"{venv / 'bin'}:{env.get('PATH', '/usr/bin:/bin')}"
    return env


def probe_octascene_app(host: str = DEFAULT_OCTASCENE_HOST, port: int = DEFAULT_OCTASCENE_PORT) -> str:
    """Return ``RUNNING`` / ``STOPPED`` / ``UNKNOWN`` from a short local TCP probe.

    Purely informational: never starts, stops, or otherwise touches the
    OctaScene app process. A refused/timed-out connection is a normal
    "not running" outcome, not an error worth surfacing as UNKNOWN.
    """

    try:
        with socket.create_connection((host, port), timeout=_PROBE_TIMEOUT_SECONDS):
            return "RUNNING"
    except (ConnectionRefusedError, TimeoutError, OSError):
        return "STOPPED"
    except Exception:  # noqa: BLE001 - never let a probe error break the dashboard
        return "UNKNOWN"


def parse_roadmap_program_index(roadmap_path: Path) -> list[dict[str, str]]:
    """Parse the existing ``## Program index`` Markdown table. Status labels only."""

    if not roadmap_path.exists():
        return []
    text = roadmap_path.read_text(encoding="utf-8")
    match = re.search(r"^## Program index\s*$(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
    if not match:
        return []
    lines = [line for line in match.group(1).splitlines() if line.strip().startswith("|")]
    if len(lines) < 2:
        return []
    headers = [cell.strip() for cell in lines[0].strip("|").split("|")]
    rows: list[dict[str, str]] = []
    for line in lines[2:]:  # skip header + separator row
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) != len(headers):
            continue
        rows.append(dict(zip(headers, cells)))
    return rows


def _git_config_value(repo_root: Path, key: str) -> str | None:
    """Read one local ``git config`` value. Never invents a name if git is silent."""

    try:
        result = subprocess.run(
            ["git", "config", "--get", key],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = (result.stdout or "").strip()
    return value or None


def _git_current_branch(repo_root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = (result.stdout or "").strip()
    if not value or value == "HEAD":
        return None
    return value


def local_developer_identity(repo_root: Path) -> dict[str, str]:
    """Honest local operator identity for the dashboard chrome.

    Prefers ``git config user.name``, then the OS username, then a generic
    placeholder. Never fabricates a specific person's name.
    """

    git_name = _git_config_value(repo_root, "user.name")
    os_name = os.environ.get("USER") or os.environ.get("USERNAME") or os.environ.get("LOGNAME")
    if git_name:
        name, source = git_name, "git"
    elif os_name:
        name, source = os_name, "os"
    else:
        name, source = "Local Developer", "fallback"
    branch = _git_current_branch(repo_root) or "UNKNOWN"
    return {
        "name": name,
        "role": "Developer",
        "branch": branch,
        "source": source,
    }


def _git_head_sha(root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    sha = (result.stdout or "").strip()
    return sha if result.returncode == 0 and sha else None


def _repo_root_of(path: Path) -> Path | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(path),
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    out = (result.stdout or "").strip()
    return Path(out) if out else None


def cp_process_status(ctx: CommandContext) -> dict[str, Any]:
    """Deterministic evidence for diagnosing a stale/never-restarted daemon process.

    ENG-AGENT-12 (issue #136): the Control Plane's own code checkout is
    allowed to differ from ``ctx.repo_root`` (the configured canonical
    repository truth this process schedules against -- see
    ``runner.canonical_repo_root``). Both locations, both their current HEAD
    SHAs, and this process's PID/start time are reported explicitly here so
    an operator (or a future automated check) can tell "the running process
    was never restarted after a merge" apart from "the canonical repo root
    is misconfigured" instead of guessing from dashboard symptoms alone.
    """

    cp_code_root = _repo_root_of(Path(__file__).resolve().parent)
    started_at: str | None = None
    pid = os.getpid()
    try:
        import psutil

        started_at = datetime_fromtimestamp_iso(psutil.Process(pid).create_time())
    except Exception:  # pragma: no cover - psutil optional / platform-dependent
        started_at = None
    canonical_head = _git_head_sha(ctx.repo_root)
    cp_code_head = _git_head_sha(cp_code_root) if cp_code_root else None

    # ENG-CP-01 (issue #165): the generic managed-project contract this
    # process is currently scheduling against -- never this process's own cwd
    # or ``cp_code_root``. Failure to resolve is reported truthfully (``None``)
    # rather than raised, since this endpoint's other fields must keep working
    # even if the selected project root became invalid.
    #
    # ENG-CP-03: this now reports the *registry's* selected project rather than
    # unconditionally rebuilding the OctaScene contract, so the diagnostic
    # matches what the operator actually has selected. It falls back to the
    # OctaScene adapter only when no registry row is selectable yet (a
    # Control Plane started against a database that predates the registry and
    # whose bootstrap has not run), preserving the CPX-01 behavior exactly.
    selected_project: dict[str, Any] | None = None
    try:
        from .project_registry import selected_project as registry_selected_project

        contract = registry_selected_project(ctx.state)
        if contract is None:
            from .octascene_project import octascene_project

            contract = octascene_project(ctx.repo_root)
        selected_project = contract.as_dict()
    except Exception:  # noqa: BLE001 - diagnostic endpoint must not 500 on a bad root
        selected_project = None

    return {
        "pid": pid,
        "process_started_at": started_at,
        "runtime": "octarel" if os.environ.get("OCTAREL_CODE_ROOT") or os.environ.get("OCTAREL_STATE_DIR") else "embedded",
        "process_cwd": os.getcwd(),
        "state_db": None if str(getattr(ctx.state, "db_path", "")) == ":memory:" else str(getattr(ctx.state, "db_path", "")),
        "canonical_repo_root": str(ctx.repo_root.resolve()) if ctx.repo_root.exists() else str(ctx.repo_root),
        "canonical_repo_head": canonical_head,
        "cp_code_root": str(cp_code_root.resolve()) if cp_code_root else None,
        "cp_code_head": cp_code_head,
        # True only when both HEADs resolved and differ -- never a false
        # positive just because one side could not be determined.
        "cp_code_differs_from_canonical": bool(
            canonical_head and cp_code_head and canonical_head != cp_code_head
        ),
        "selected_project": selected_project,
    }


def datetime_fromtimestamp_iso(ts: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _resources_snapshot(repo_root: Path) -> dict[str, Any]:
    try:
        import psutil
    except ImportError:  # pragma: no cover - psutil is a requirements-dev.txt pin
        return {"available": False, "note": "psutil not installed"}

    disk = psutil.disk_usage(str(repo_root))
    try:
        load1, load5, load15 = os.getloadavg()
    except (OSError, AttributeError):  # pragma: no cover - not available on all platforms
        load1 = load5 = load15 = None
    return {
        "available": True,
        "cpu_percent": psutil.cpu_percent(interval=None),
        "memory_percent": psutil.virtual_memory().percent,
        "disk_percent": disk.percent,
        "load_average": {"1m": load1, "5m": load5, "15m": load15},
    }


def _task_to_dict(task: Any, *, superseded: bool = False) -> dict[str, Any]:
    from .models import task_projection

    return {
        "superseded": superseded,
        "id": task.id,
        "task_ref": task.task_ref,
        "role": task.role,
        "worker": task.worker,
        "kind": task.kind,
        "state": task.state,
        "priority": task.priority,
        "dependencies": list(task.dependencies),
        "pid": task.pid,
        "worktree": task.worktree,
        "result": task.result,
        "last_error": task.last_error,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "projection": "HISTORICAL" if superseded else task_projection(task.state),
        "failed_worker_id": task.failed_worker_id,
        "failure_execution_system": task.failure_execution_system,
        "failure_provider": task.failure_provider,
        "failure_model": task.failure_model,
        "failure_category": task.failure_category,
        "failure_reason_sanitized": task.failure_reason_sanitized,
        "failure_reset": task.failure_reset,
        "failure_reset_source": task.failure_reset_source,
        "fallback_alternatives": list(task.fallback_alternatives),
        "fallback_selected_worker": task.fallback_selected_worker,
        "fallback_automatic": task.fallback_automatic,
        "stale_recovered": task.stale_recovered,
        "owner_ref": task.owner_ref,
        "required_capability": task.required_capability,
        "changed_paths": list(task.changed_paths),
        "integration_base": task.integration_base,
        "avoid_provider": task.avoid_provider,
        "admission_state": task.admission_state,
        "admission_reason": task.admission_reason,
        "dependency_wave": task.dependency_wave,
        "selected_worker_reason": task.selected_worker_reason,
        "selected_provider": task.selected_provider,
        "selection_alternatives": list(task.selection_alternatives),
    }


def _runbook_to_dict(runbook: Any, *, state: Any = None, registry: Any = None) -> dict[str, Any]:
    import datetime as _dt

    remaining_seconds: int | None = None
    if runbook.deadline_at:
        try:
            deadline = _dt.datetime.fromisoformat(runbook.deadline_at)
            remaining_seconds = max(0, int((deadline - _dt.datetime.now(_dt.timezone.utc)).total_seconds()))
        except ValueError:  # pragma: no cover - deadline_at is always our own isoformat
            remaining_seconds = None
    body = {
        "id": runbook.id,
        "name": runbook.name,
        "preset": runbook.preset,
        "objective": runbook.objective,
        "source_ref": runbook.source_ref,
        "branch": runbook.branch,
        "worktree": runbook.worktree,
        "parent_worker": runbook.parent_worker,
        "max_duration_minutes": runbook.max_duration_minutes,
        "worker_routes": runbook.worker_routes,
        "concurrency": runbook.concurrency,
        "safety_profile": runbook.safety_profile,
        "checkpoint_policy": runbook.checkpoint_policy,
        "stop_conditions": list(runbook.stop_conditions),
        "permission_profile": runbook.permission_profile,
        "codex_policy": runbook.codex_policy,
        "codex_auto_eligible": runbook.codex_auto_eligible,
        "max_codex_invocations": runbook.max_codex_invocations,
        "phases": list(runbook.phases),
        "status": runbook.status,
        "task_id": runbook.task_id,
        "created_at": runbook.created_at,
        "updated_at": runbook.updated_at,
        "started_at": runbook.started_at,
        "deadline_at": runbook.deadline_at,
        "ended_at": runbook.ended_at,
        "remaining_seconds": remaining_seconds,
        "recovery_note": runbook.recovery_note,
        "has_report": runbook.report_markdown is not None,
        # ENG-AGENT-13 (issue #138): distinguish "implementation attempt
        # succeeded" from "acceptance pipeline succeeded" -- SUCCEEDED here
        # means every required stage below is PASS/NOT_APPLICABLE.
        "acceptance_stage": runbook.acceptance_stage,
        "acceptance_evidence": runbook.acceptance_evidence,
    }
    if state is not None:
        # OCTAREL-OPS-02: persisted advancement decision (never recomputed on read).
        body["advancement"] = state.get_advancement(runbook.id)
    if state is not None and registry is not None:
        from .runbooks import eligible_retry_workers

        task = state.get_task(runbook.task_id) if runbook.task_id else None
        reason = None
        if task:
            reason = task.failure_reason_sanitized or (
                redact_text(task.last_error).strip()[:800] if task.last_error else None
            )
        body["failure_reason"] = reason
        body["failure"] = ({
            "worker_id": task.failed_worker_id or task.worker,
            "execution_system": task.failure_execution_system or "UNKNOWN",
            "provider": task.failure_provider or "UNKNOWN",
            "model": task.failure_model or "UNKNOWN",
            "category": task.failure_category or "UNKNOWN",
            "reason": reason,
            "reset": task.failure_reset,
            "reset_source": task.failure_reset_source,
        } if task and reason else None)
        body["eligible_retry_workers"] = (
            eligible_retry_workers(state=state, registry=registry, runbook=runbook)
            if runbook.status in {"FAILED", "CANCELLED"}
            else []
        )
        body["recommended_retry_worker"] = body["eligible_retry_workers"][0] if body["eligible_retry_workers"] else None
        usage = state.get_usage_governance(runbook.id) or {}
        attempts = []
        for index, attempt in enumerate(usage.get("route_history", []), start=1):
            if not isinstance(attempt, dict):
                continue
            worker_id = str(attempt.get("worker") or "UNKNOWN")
            worker = registry.workers.get(worker_id)
            attempts.append(
                {
                    "attempt": index,
                    "worker_id": worker_id,
                    "worker_name": str(worker.raw.get("display_name", worker_id)) if worker else worker_id,
                    "execution_system": worker.execution_system if worker else "UNKNOWN",
                    "provider": worker.provider if worker else str(attempt.get("provider") or "UNKNOWN"),
                    "model": str(attempt.get("model") or (worker.effective_model if worker else "UNKNOWN")),
                    "status": str(attempt.get("status") or "UNKNOWN"),
                    "automatic": bool(attempt.get("automatic", False)),
                    "from_worker": attempt.get("from_worker"),
                    "failure_category": attempt.get("failure_category"),
                    "failure_reason": attempt.get("failure_reason"),
                }
            )
        body["attempt_history"] = attempts
        body["fallback_transition"] = None
        if task and task.fallback_automatic is True and task.failed_worker_id and task.worker != task.failed_worker_id:
            replacement = registry.workers.get(task.worker)
            body["fallback_transition"] = {
                "status": task.state,
                "from_worker": task.failed_worker_id,
                "to_worker": task.worker,
                "to_worker_name": (
                    str(replacement.raw.get("display_name", task.worker)) if replacement else task.worker
                ),
                "automatic": True,
            }
        elif task and task.fallback_automatic is False and reason:
            body["fallback_transition"] = {
                "status": "BLOCKED",
                "from_worker": task.failed_worker_id or task.worker,
                "to_worker": task.fallback_selected_worker,
                "to_worker_name": task.fallback_selected_worker,
                "automatic": False,
                "reason": runbook.recovery_note,
            }
        recovery = []
        if reason:
            recovery.append("Review the sanitized failure reason and retained run evidence.")
        if body["eligible_retry_workers"] and not body["fallback_transition"]:
            recovery.append("Select an eligible implementation worker and retry this same runbook/task in place.")
        if "quota" in (reason or "").lower() or "limit" in (reason or "").lower():
            recovery.append("Wait for the subscription quota reset or choose another subscription-authenticated worker.")
        body["recovery_actions"] = recovery
    return body


def _provider_to_dict(provider: Any, *, registry: Any = None, tasks: list[Any] | None = None) -> dict[str, Any]:
    worker = registry.workers.get(provider.name) if registry is not None else None
    raw = worker.raw if worker else {}
    active_tasks = [task for task in (tasks or []) if task.worker == provider.name and task.state == "RUNNING"]
    running_count = sum(1 for task in (tasks or []) if task.worker == provider.name and task.state == "RUNNING")
    route_type = {
        "premium-subscription": "Subscription",
        "free-verified": "Free",
        "supplemental-configured": "Free",
        "free-dynamic": "Free",
        "metered-configured": "API",
        "optional-overflow": "Disabled",
        "catalog-only": "Not Configured",
    }.get(provider.cost_class, "Unknown")
    capability = worker.capability if worker else "not configured"
    effective_state = "DISABLED" if worker is not None and not worker.enabled else provider.state
    display_state = "DRAINING" if effective_state == "COOLING_DOWN" else effective_state
    effective_reason = "DISABLED_BY_REGISTRY" if worker is not None and not worker.enabled else provider.reason
    return {
        "name": provider.name,
        "display_name": raw.get("display_name", provider.name.replace("-", " ").title()),
        "execution_system": provider.execution_system,
        "provider": provider.provider,
        "cost_class": provider.cost_class,
        "state": effective_state,
        "display_state": display_state,
        "configured": provider.configured,
        "consecutive_failures": provider.consecutive_failures,
        "last_probe_at": provider.last_probe_at,
        "last_error": provider.last_error,
        # ENG-AGENT-02-S7 (issue #97): the truthful why behind `state` -- e.g.
        # NOT_AUTHENTICATED vs CLI_MISSING vs CATALOG_ONLY -- so the Control
        # Center never has to collapse every unavailability cause into a bare
        # "NOT_CONFIGURED" badge.
        "reason": effective_reason,
        # ENG-AGENT-12 (issue #136): explicit freshness facts for the state
        # actually used at route selection -- "source" names where `state`
        # last came from (a real CLI probe vs the initial, never-probed seed
        # row), "timestamp" is `last_probe_at` unchanged, "freshness" is
        # FRESH/STALE/UNKNOWN against the same threshold the pre-launch
        # refresh uses, and "reason" duplicates the field above under this
        # freshness envelope so a caller reading only this object still gets
        # the full source/timestamp/freshness/reason quartet in one place.
        "freshness": {
            "source": "probe" if provider.last_probe_at else "seed",
            "timestamp": provider.last_probe_at,
            "age_seconds": provider_state_age_seconds(provider),
            "freshness": provider_state_freshness(provider),
            "reason": effective_reason,
        },
        "model": worker.effective_model if worker else None,
        "intensity": worker.default_intensity if worker else None,
        "roles": list(worker.roles) if worker else [],
        "execution_route": execution_route_for_cost_class(provider.cost_class),
        "running_task_count": running_count,
        "route_type": route_type,
        "capability": capability,
        "description": raw.get("description") or f"{provider.provider} is visible for development routing but has no configured executable adapter.",
        "best_for": list(raw.get("best_for", [])),
        "agents": [raw.get("display_name", provider.name.replace("-", " ").title())] if worker else [],
        "models": [worker.effective_model] if worker and worker.effective_model else [],
        "quota_visibility": "Provider/session facts only when locally exposed; CLI installation is not quota proof.",
        "spend_safety": "No automatic paid fallback; explicit authorization and configured limits remain required.",
        "active_tasks": [task.id for task in active_tasks],
    }


def _make_lifespan(ctx: CommandContext):
    """Build the dashboard's own reconciliation loop, scoped to ``ctx.supervisor``.

    The dashboard can launch subprocesses through ``/api/commands`` and
    ``/api/steering/execute`` (both ultimately call ``ctx.supervisor.launch_task``),
    but unlike ``orchestrator.py``'s ``run`` loop, nothing was ever calling
    ``ctx.supervisor.poll_once()`` for the dashboard's own process table — a task
    launched this way stayed ``RUNNING`` forever even after its subprocess exited.
    This loop only reconciles processes *this* ``Supervisor`` instance launched; it
    never schedules queued/pending work itself (that stays the orchestrator daemon's
    job) and never touches any other Supervisor's process table.
    """

    async def _reconcile_loop() -> None:
        from .advancement_lease import daemon_authority_active
        from .runbooks import reconcile_runbooks

        while True:
            await asyncio.sleep(RECONCILE_INTERVAL_SECONDS)
            ctx.supervisor.poll_once()
            # ENG-AO-07: while a live daemon holds advancement authority the dashboard only observes
            # (it still polls its own subprocesses); the per-runbook lease guards the no-daemon case.
            try:
                if daemon_authority_active(ctx.state):
                    continue
            except Exception:  # noqa: BLE001 - the monitoring loop must never die on a probe error
                continue
            reconcile_runbooks(
                state=ctx.state,
                repo_root=ctx.repo_root,
                registry=ctx.registry,
                supervisor=ctx.supervisor,
                scheduler=ctx.scheduler,
            )

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        task = asyncio.create_task(_reconcile_loop())
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            shutdown_all = getattr(ctx.supervisor, "shutdown_all", None)
            if callable(shutdown_all):
                shutdown_all()
            manager = getattr(_app.state, "app_lifecycle", None)
            if manager is not None:
                with contextlib.suppress(OperationError):
                    manager.shutdown_owned()

    return lifespan


def _json_error(status_code: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": detail})


def _remote_identity_of(request: Request) -> Any | None:
    """The verified :class:`RemoteIdentity` the middleware attached, if any."""

    return getattr(request.state, "remote_identity", None)


def _record_remote_audit(ctx: CommandContext, request: Request, *, verb: str, target: str | None, result: str) -> None:
    """Audit a remote state-changing action: timestamp (via ``record_event``'s own

    ``ts``), identity, verb, target and result — never secrets (only the
    already-public verb name/target id/result string ever appear here; no
    request body, header, or token is logged). A no-op for local requests, so
    the existing local audit trail (already produced by the command layer
    itself) is completely unaffected (issue #95 acceptance criterion 8).
    """

    identity = _remote_identity_of(request)
    if identity is None:
        return
    target_repr = target if target else "-"
    ctx.state.record_event(
        category="remote_audit",
        message=f"[remote:{identity.email}] {verb}({target_repr}) -> {result}",
    )


def _install_remote_access_middleware(app: FastAPI, ctx: CommandContext, remote: RemoteAccessState) -> None:
    """Gate a request's identity/authorization when it carries a Cloudflare

    Access assertion and remote mode is enabled. Precisely what "disabled by
    default" / "preserve localhost-only behavior" (issue #95 criteria 2/12)
    guarantee: an ordinary request's *authentication, authorization, rate
    limiting, and audit* behavior is completely unaffected by this function
    either way — when disabled, or when the request carries no assertion at
    all, none of the verification/allowlist/CSRF/rate-limit logic below ever
    runs. What this function *always* does, local or remote, enabled or not
    (an independent review, issue #95, correctly flagged the module
    docstring for previously overclaiming "byte-for-byte" here) — is attach
    a handful of harmless, always-on security headers to the response and
    let ``/api/identity`` report an honest ``access_mode`` — additions with
    no bearing on what any request is authorized to do.
    """

    @app.middleware("http")
    async def _remote_access_and_security_headers(request: Request, call_next):  # noqa: ANN001 - Starlette signature
        request.state.access_mode = "local"
        request.state.remote_identity = None

        if remote.config.enabled and request_carries_access_assertion(request.headers, request.cookies):
            request.state.access_mode = "remote"
            token = extract_access_token(request.headers, request.cookies)
            try:
                identity = remote.verifier.verify(token)
            except AccessIdentityError as exc:
                if not remote.auth_failure_limiter.allow("remote"):
                    ctx.state.record_event(
                        category="remote_access",
                        level="error",
                        message="rate-limited: too many failed Access verifications",
                    )
                    return _json_error(429, "too many failed authentication attempts; try again shortly")
                ctx.state.record_event(
                    category="remote_access", level="warning", message=f"rejected unverifiable Access token: {exc}"
                )
                return _json_error(401, "authentication required")
            if not remote.verifier.is_allowed(identity):
                # Shares the failed-auth budget (not a separate one): an
                # authenticated-but-denied identity is still an attempt worth
                # slowing down, e.g. against enumerating which emails an
                # allowlist accepts (independent review, issue #95).
                if not remote.auth_failure_limiter.allow("remote"):
                    ctx.state.record_event(
                        category="remote_access",
                        level="error",
                        message="rate-limited: too many failed Access verifications",
                    )
                    return _json_error(429, "too many failed authentication attempts; try again shortly")
                ctx.state.record_event(
                    category="remote_access",
                    level="warning",
                    message=f"rejected non-allowlisted identity {identity.email!r}",
                )
                return _json_error(403, "identity is not authorized for remote access")

            request.state.remote_identity = identity

            if request.method in STATE_CHANGING_METHODS:
                origin = request.headers.get("origin") or request.headers.get("referer")
                if not origin_matches_hostname(origin, remote.config.hostname):
                    ctx.state.record_event(
                        category="remote_access",
                        level="warning",
                        message=f"[remote:{identity.email}] rejected state-changing request: origin/referer "
                        f"{origin!r} does not match {remote.config.hostname!r}",
                    )
                    return _json_error(403, "origin/referer check failed for a state-changing request")
                if not remote.state_change_limiter.allow(identity.email):
                    ctx.state.record_event(
                        category="remote_access",
                        level="warning",
                        message=f"[remote:{identity.email}] rate-limited a state-changing request",
                    )
                    return _json_error(429, "too many requests; slow down")

        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers[name] = value
        # ENG-AGENT-02-S7: since S6, this dashboard is also reachable through a real
        # Cloudflare edge (Tunnel + Access), which applies its own default caching
        # rules to static-shaped paths unless told not to -- a stale cached
        # `/static/app.js`/`/static/styles.css` (or even a cached `/` shell) can make
        # a just-shipped UI fix invisible to a remote browser indefinitely. This is a
        # fast-iterating dev tool, never a CDN-cacheable static site: no response here
        # is ever safe to cache at any layer (browser or edge).
        if request.url.path == "/" or request.url.path.startswith(("/api/", "/static/")):
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        return response


def create_app(
    ctx: CommandContext,
    *,
    roadmap_path: Path | None = None,
    remote: RemoteAccessState | None = None,
    manager_invoker: _manager_chat.Invoker = _manager_chat.subprocess_invoker,
) -> FastAPI:
    """Build the dashboard FastAPI app for a given command context.

    ``ctx`` is the same :class:`CommandContext` the CLI uses, so every control
    endpoint here calls exactly the same code path as ``orchestrator.py``.
    ``remote`` defaults to a disabled :class:`RemoteAccessState` (matching
    :class:`RemoteAccessConfig`'s own disabled-by-default), so every existing
    caller (the CLI, the Playwright fixture, existing tests) that never passes
    it keeps today's pure-local behavior exactly as before ENG-AGENT-02-S6.

    ``manager_invoker`` is the seam Manager Chat (OCTAREL-UI-05, issue #24)
    uses to run one bounded interpretation command. It is injected so tests and
    the Playwright fixture drive natural-language routing with a deterministic
    fake and never make a live or billable provider call.
    """

    if remote is None:
        remote = RemoteAccessState.from_config(RemoteAccessConfig())

    app = FastAPI(
        title="OctAges Orchestrator Control Center",
        docs_url=None,
        redoc_url=None,
        lifespan=_make_lifespan(ctx),
    )
    _install_remote_access_middleware(app, ctx, remote)
    roadmap_file = roadmap_path or (ctx.repo_root / "docs" / "PRODUCT_ROADMAP.md")
    _usage_cache: dict[str, Any] = {"at": 0.0, "body": None}
    app_lifecycle = AppLifecycleManager(ctx)
    app.state.app_lifecycle = app_lifecycle
    worktree_status_cache: dict[str, Any] = {"key": object(), "at": 0.0, "rows": []}
    worktree_status_lock = threading.Lock()

    def cached_worktree_statuses() -> list[dict[str, Any]]:
        """Cache expensive Git-derived facts while task/run state stays live elsewhere."""

        key = ctx.selected_project_id
        with worktree_status_lock:
            fresh = (
                worktree_status_cache["key"] == key
                and time.monotonic() - worktree_status_cache["at"] < WORKTREE_STATUS_CACHE_TTL_SECONDS
            )
            if not fresh:
                worktree_status_cache.update(
                    key=key,
                    at=time.monotonic(),
                    rows=list_worktree_statuses(ctx),
                )
            return [dict(row) for row in worktree_status_cache["rows"]]

    # ENG-CP-03 (issue #165): every project-dependent read in this app goes
    # through one of these three helpers, so "which project am I looking at" is
    # resolved in exactly one place per record type rather than at ~25 call
    # sites. The selected id is read fresh on each call (never captured at app
    # construction) because the operator can switch projects at runtime.
    #
    # A ``None`` id means no project is registered/selected at all -- a fresh
    # database, or a context built directly by a test -- in which case these
    # behave exactly as they did before this slice and return every row.
    def scoped_tasks(state: str | None = None) -> list[Any]:
        return ctx.state.list_tasks(state=state, project_id=ctx.selected_project_id)

    def scoped_runbooks() -> list[Any]:
        return ctx.state.list_runbooks(project_id=ctx.selected_project_id)

    def superseded() -> tuple[set[str], set[str]]:
        """Run/task ids that are history (already accepted, or taken over by a later accepted run)."""

        from .advancement import completed_work_refs, superseded_ids

        return superseded_ids(scoped_runbooks(), scoped_tasks(), completed_work_refs(ctx.selected_project))

    def current_tasks() -> list[Any]:
        _runs, old_tasks = superseded()
        return [t for t in scoped_tasks() if t.id not in old_tasks]

    def scoped_events(limit: int) -> list[Any]:
        return ctx.state.list_events(limit=limit, project_id=ctx.selected_project_id)

    refresh_repository_health(ctx)

    def projected_gate() -> dict[str, Any]:
        from scripts.ci.local_gate import read_latest_gate

        from .models import TASK_ACTIVE_STATES, TASK_PAUSED_STATES, TASK_QUEUED_STATES
        from .validation_adapter import (
            ADAPTER_EXACT_TREE_LOCAL_GATE,
            validation_adapter_name,
        )

        # ENG-CP-03: the gate belongs to the selected project's checkout, not to
        # whichever repository this Control Plane process was started from.
        gate_root = ctx.project_root
        project = ctx.selected_project
        if project is not None and validation_adapter_name(project) != ADAPTER_EXACT_TREE_LOCAL_GATE:
            return {
                "ready": False,
                "status": "NOT_READY",
                "reason": "selected project uses its declared validation_command; OctaScene local-gate evidence is not the authority",
                "adapter": validation_adapter_name(project),
                "validation_command": list(project.validation_command),
            }
        latest = read_latest_gate(gate_root)
        row = next((item for item in cached_worktree_statuses() if Path(item["path"]).resolve() == gate_root.resolve()), {})
        branch = row.get("branch")
        candidate_states = TASK_ACTIVE_STATES | TASK_QUEUED_STATES | TASK_PAUSED_STATES
        has_candidate = any(task.state in candidate_states for task in scoped_tasks())
        if branch in {"main", "master"} and row.get("dirty") is False and not has_candidate:
            return {**latest, "ready": False, "status": "IDLE", "reason": "no active candidate"}
        return {**latest, "status": "READY" if latest.get("ready") else "NOT_READY"}

    @app.get("/api/overview")
    def overview() -> dict[str, Any]:
        from .models import task_projection

        _runs, old_tasks = superseded()
        tasks = scoped_tasks()
        providers = ctx.state.list_provider_states()
        counts = {key: 0 for key in ("ACTIVE", "QUEUED", "PAUSED", "NEEDS_ATTENTION", "HISTORICAL")}
        for task in tasks:
            counts["HISTORICAL" if task.id in old_tasks else task_projection(task.state)] += 1
        return {
            "task_count": counts["ACTIVE"],
            "task_counts": {key.lower(): value for key, value in counts.items()},
            "provider_count": len(providers),
            "configured_provider_count": sum(1 for p in providers if p.configured),
            "stop_after_current": ctx.stop_after_current,
            "octascene_app_status": probe_octascene_app(),
        }

    @app.get("/api/cp-status")
    def cp_status() -> dict[str, Any]:
        """Read-only, zero-AI: canonical-truth/CP-code location and HEAD evidence.

        ENG-AGENT-12 (issue #136): lets an operator (or an automated check)
        tell a stale-daemon-process deployment mismatch apart from a
        genuinely stale ledger straight from the API, without needing shell
        access to the host running the daemon.
        """

        return cp_process_status(ctx)

    # ------------------------------------------------------------ ENG-CP-03
    # Managed-project registry and selection (issue #165). Every route here
    # reads/writes only the Control Plane's own registry; none of them touches,
    # checks out, or mutates any managed repository.

    def _projects_payload() -> dict[str, Any]:
        from .project_registry import list_projects, selected_project_id

        return {
            "projects": list_projects(ctx.state),
            "selected_project_id": selected_project_id(ctx.state),
            # Reported so the Control Center can always distinguish "the
            # repository being managed" from "the checkout this Control Plane's
            # own code runs from" (ENG-AGENT-12 / CPX-01). They are allowed to
            # be the same path today; they are never inferred from each other.
            "cp_code_root": str(_cp_code_root_path()),
        }

    def _cp_code_root_path() -> Path:
        from .project import cp_code_root

        return cp_code_root()

    def _require_filesystem_authority(request: Request, action: str) -> None:
        """Remote identities may not supply arbitrary local filesystem paths.

        ENG-CP-03 / issue #95 boundary: a Cloudflare Access session is
        authenticated and allowlisted, but registering a project means naming an
        arbitrary directory on the host running the daemon and having the
        Control Plane read it. That authority is deliberately not part of what
        remote access currently grants, so these operations are refused for a
        remote identity with an explicit, non-leaking message rather than being
        silently downgraded. Local requests are completely unaffected.
        """

        identity = _remote_identity_of(request)
        if identity is None:
            return
        ctx.state.record_event(
            category="remote_access",
            level="warning",
            message=f"[remote:{identity.email}] refused {action}: filesystem paths are local-only",
        )
        raise HTTPException(
            status_code=403,
            detail=(
                f"{action} requires providing a local filesystem path, which remote sessions are not "
                "authorized to do; perform this action from the machine running the Control Plane"
            ),
        )

    @app.get("/api/projects")
    def projects_list() -> dict[str, Any]:
        return _projects_payload()

    @app.post("/api/projects/detect")
    def projects_detect(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
        """Report what can be detected about a candidate folder, before saving."""

        from .project import ProjectRootError
        from .project_registry import detect_project

        _require_filesystem_authority(request, "project detection")
        path = payload.get("path")
        if not isinstance(path, str) or not path.strip():
            raise HTTPException(status_code=400, detail="path is required")
        try:
            return detect_project(path.strip())
        except ProjectRootError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None

    @app.post("/api/projects/select")
    def projects_select(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
        from .project_registry import ProjectRegistryError, select_project

        project_id = payload.get("project_id")
        if not isinstance(project_id, str) or not project_id:
            raise HTTPException(status_code=400, detail="project_id is required")
        try:
            select_project(ctx.state, project_id)
        except ProjectRegistryError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        _record_remote_audit(ctx, request, verb="project_select", target=project_id, result="ok")
        # Re-observe the newly selected repository immediately so the first
        # post-switch render shows that project's own worktrees/health rather
        # than an empty panel until the next refresh tick.
        with contextlib.suppress(Exception):
            refresh_repository_health(ctx)
        return _projects_payload()

    @app.post("/api/projects")
    def projects_create(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
        from .project import ProjectRootError
        from .project_registry import ProjectRegistryError, register_project

        _require_filesystem_authority(request, "project registration")
        try:
            contract = register_project(ctx.state, payload)
        except (ProjectRegistryError, ProjectRootError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        _record_remote_audit(ctx, request, verb="project_create", target=contract.project_id, result="ok")
        return _projects_payload()

    @app.patch("/api/projects/{project_id}")
    def projects_update(project_id: str, request: Request, payload: dict[str, Any]) -> dict[str, Any]:
        from .project import ProjectRootError
        from .project_registry import ProjectRegistryError, update_project

        if "local_repo_root" in payload:
            _require_filesystem_authority(request, "changing a project's repository path")
        try:
            update_project(ctx.state, project_id, payload)
        except (ProjectRegistryError, ProjectRootError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        _record_remote_audit(ctx, request, verb="project_update", target=project_id, result="ok")
        return _projects_payload()

    @app.delete("/api/projects/{project_id}")
    def projects_delete(project_id: str, request: Request, confirm: bool = False) -> dict[str, Any]:
        """Remove a project from the registry. Requires explicit confirmation.

        Never deletes the repository, its branches, its worktrees, or any
        product file, and never deletes Control Plane history -- the response
        reports exactly how many historical records were preserved.
        """

        from .project_registry import ProjectRegistryError, remove_project

        try:
            result = remove_project(ctx.state, project_id, confirm=confirm)
        except ProjectRegistryError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        _record_remote_audit(ctx, request, verb="project_delete", target=project_id, result="ok")
        return {**_projects_payload(), "removal": result}

    @app.get("/api/projects/{project_id}/validate")
    def projects_validate(project_id: str) -> dict[str, Any]:
        from .project_registry import UnknownProjectError, get_project, validate_project

        try:
            contract = get_project(ctx.state, project_id)
        except UnknownProjectError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        return validate_project(contract)

    @app.post("/api/projects/{project_id}/refresh")
    def projects_refresh(project_id: str) -> dict[str, Any]:
        """Re-derive repository truth for a project, without copying it into CP state.

        Returns what the repository says *right now* -- which declared policy/
        roadmap/task-source paths actually exist on the current branch, the
        checkout's observed Git remote and default branch. Nothing here is
        written into the registry: it is resolved fresh on every call, exactly
        like CPX-01's ``present_relative_paths`` already does.
        """

        from .project import present_relative_paths
        from .project_registry import (
            UnknownProjectError,
            detect_default_branch,
            detect_git_remote,
            get_project,
            validate_project,
        )

        try:
            contract = get_project(ctx.state, project_id)
        except UnknownProjectError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        root = contract.local_repo_root
        return {
            "project_id": project_id,
            "present_policy_entrypoints": list(present_relative_paths(root, contract.policy_entrypoints)),
            "present_roadmap_paths": list(present_relative_paths(root, contract.roadmap_paths)),
            "present_task_sources": list(present_relative_paths(root, contract.task_sources)),
            "observed_github_remote": detect_git_remote(root),
            "observed_default_branch": detect_default_branch(root),
            "validation": validate_project(contract),
        }

    @app.get("/api/tasks")
    def tasks() -> list[dict[str, Any]]:
        _runs, old_tasks = superseded()
        return [_task_to_dict(t, superseded=t.id in old_tasks) for t in scoped_tasks()]

    @app.get("/api/processes")
    def processes() -> list[dict[str, Any]]:
        return [
            {"task_id": t.id, "pid": t.pid, "worker": t.worker, "worktree": t.worktree}
            for t in scoped_tasks()
            if t.pid
        ]

    @app.get("/api/workers")
    def workers() -> dict[str, Any]:
        usage = ctx.scheduler.slot_usage(scoped_tasks())
        body = usage.as_dict()
        return {key: body[key] for key in ("write", "read", "heavy")}

    @app.get("/api/dispatch")
    def dispatch() -> dict[str, Any]:
        """Durable managed-admission truth; this read performs no provider calls."""

        tasks = scoped_tasks()
        facts = {item.task_id: item for item in ctx.scheduler.admission_facts(tasks)}
        return {
            "caps": ctx.scheduler.slot_usage(tasks).as_dict(),
            "waves": [
                {
                    "task_id": task.id,
                    "wave": task.dependency_wave if task.dependency_wave is not None else getattr(facts.get(task.id), "wave", None),
                    "admission": task.admission_state,
                    "reason": task.admission_reason or getattr(facts.get(task.id), "reason", None),
                    "worker": task.worker,
                    "worktree": task.worktree,
                    "alternatives": list(task.selection_alternatives),
                }
                for task in tasks
            ],
            "decisions": ctx.state.list_dispatch_decisions(limit=100, project_id=ctx.selected_project_id),
            "intake_claims": ctx.state.list_task_identity_claims(),
        }

    @app.get("/api/flow")
    def flow() -> dict[str, Any]:
        """Task dependency graph enriched with the full per-task pipeline chain:

        orchestrator -> task -> worker -> role -> execution system ->
        provider/model/intensity -> worktree -> tests -> review -> checkpoint
        -> PR. Every field here is read from already-persisted state/registry
        data; nothing is invented and nothing calls a provider.
        """

        all_tasks = scoped_tasks()
        provider_by_name = {p.name: p for p in ctx.state.list_provider_states()}
        nodes = []
        for t in all_tasks:
            worker = ctx.registry.workers.get(t.worker)
            provider = provider_by_name.get(t.worker)
            checkpoint = None
            if t.worktree and Path(t.worktree).exists():
                checkpoint = checkpoint_status(Path(t.worktree)).as_dict()
            if t.state == "RUNNING":
                implementation_state = "RUNNING"
            elif t.state in ("SUCCEEDED", "READY_LOCAL", "READY_BUT_UNMERGED"):
                implementation_state = "COMPLETED"
            elif t.state in ("FAILED", "BLOCKED", "CANCELLED"):
                implementation_state = t.state
            else:
                implementation_state = "PENDING"
            checkpoint_state = "NOT_REPORTED"
            if checkpoint:
                checkpoint_state = "DIRTY" if checkpoint["has_uncommitted_changes"] else "CLEAN"
            nodes.append(
                {
                    "id": t.id,
                    "label": f"{t.task_ref}/{t.role}",
                    "state": t.state,
                    "role": t.role,
                    "worker": t.worker,
                    "execution_system": worker.execution_system if worker else None,
                    "provider": worker.provider if worker else None,
                    "model": worker.effective_model if worker else None,
                    "intensity": worker.default_intensity if worker else None,
                    "provider_state": provider.state if provider else None,
                    "worktree": t.worktree,
                    "pid": t.pid,
                    "result": t.result,
                    "kind": t.kind,
                    "priority": t.priority,
                    "dependency_wave": t.dependency_wave,
                    "admission_state": t.admission_state,
                    "admission_reason": t.admission_reason,
                    "selection_alternatives": list(t.selection_alternatives),
                    "stages": [
                        {"key": "task", "label": "Task", "state": t.state, "source": "TASK_STORE"},
                        {"key": "orchestrator", "label": "Orchestrator", "state": t.state, "source": "TASK_STORE"},
                        {
                            "key": "worker",
                            "label": "Worker / provider",
                            "state": provider.reason if provider else "NOT_REPORTED",
                            "source": "PROVIDER_STORE",
                        },
                        {
                            "key": "worktree",
                            "label": "Worktree",
                            "state": "PRESENT" if checkpoint else "NOT_REPORTED",
                            "source": "LOCAL_GIT",
                        },
                        {
                            "key": "implementation",
                            "label": "Implementation",
                            "state": implementation_state,
                            "source": "TASK_STORE",
                        },
                        {"key": "tests", "label": "Tests", "state": "NOT_REPORTED", "source": "NOT_EXPOSED"},
                        {"key": "review", "label": "Review", "state": "NOT_REPORTED", "source": "NOT_EXPOSED"},
                        {
                            "key": "checkpoint",
                            "label": "Checkpoint",
                            "state": checkpoint_state,
                            "source": "LOCAL_GIT" if checkpoint else "NOT_EXPOSED",
                        },
                        {"key": "pr", "label": "PR", "state": "NOT_REPORTED", "source": "NOT_EXPOSED"},
                        {"key": "ci", "label": "CI", "state": "NOT_REPORTED", "source": "NOT_EXPOSED"},
                    ],
                }
            )
        edges = [{"from": dep, "to": t.id} for t in all_tasks for dep in t.dependencies]
        concurrent_running = [n["id"] for n in nodes if n["state"] == "RUNNING"]
        return {"nodes": nodes, "edges": edges, "concurrent_running": concurrent_running}

    @app.get("/api/workflow")
    def workflow() -> dict[str, Any]:
        """A presentation-ready, truthful snapshot for the Live Workflow card.

        Unlike ``/api/flow`` this groups one real task reference into workflow
        stages.  Missing progress, ETA, current-file and child-agent data stay
        null/empty; configured providers are never promoted to active stages.
        """

        all_tasks = current_tasks()  # history (accepted/superseded) never becomes "the current task"
        active_states = {"RUNNING", "QUEUED", "PENDING", "PAUSED", "BLOCKED"}
        groups: dict[str, list[Any]] = {}
        for item in all_tasks:
            groups.setdefault(item.task_ref, []).append(item)
        if not groups:
            return {"task": None, "orchestrator": None, "stages": [], "worktrees": [], "results": []}

        def group_rank(pair: tuple[str, list[Any]]) -> tuple[int, str]:
            _ref, items = pair
            active = any(item.state in active_states for item in items)
            newest = max((item.updated_at for item in items), default="")
            return (1 if active else 0, newest)

        task_ref, selected = max(groups.items(), key=group_rank)
        selected.sort(key=lambda item: (-item.priority, item.created_at, item.id))
        selected_ids = {item.id for item in selected}
        # A Runbook's human-facing source may include a descriptive suffix
        # (for example ``V1-01 (Local import.)``) while its durable task_ref is
        # the stable ID (``V1-01``).  The task_id foreign key is the canonical
        # join.  Text equality here previously missed the successful fallback
        # Runbook and let an older failed task determine the workflow state.
        runbooks = [rb for rb in scoped_runbooks() if rb.task_id in selected_ids]
        runbook = (
            max(runbooks, key=lambda rb: (1 if rb.status in {"RUNNING", "PAUSED", "STOPPING"} else 0, rb.updated_at))
            if runbooks
            else None
        )
        registry = ctx.registry.workers
        stage_labels = {
            "primary-implementation": "Implementation",
            "mechanical-testing": "Testing & QA",
            "focused-tests": "Testing & QA",
            "diff-review": "Review",
            "ui": "UI Implementation",
            "ui-implementation": "UI Implementation",
            "frontend": "UI Implementation",
        }
        worktrees: dict[str, dict[str, Any]] = {}
        results: list[dict[str, str]] = []
        stages = []
        for item in selected:
            worker = registry.get(item.worker)
            if item.worktree:
                branch = None
                if Path(item.worktree).exists():
                    branch = checkpoint_status(Path(item.worktree)).branch
                worktrees[item.worktree] = {"path": item.worktree, "branch": branch}
            if item.result:
                results.append({"task_id": item.id, "kind": "result", "text": redact_text(item.result)[:800]})
            if item.last_error:
                results.append({"task_id": item.id, "kind": "error", "text": redact_text(item.last_error)[:800]})
            subagents: list[dict[str, Any]] = []
            if worker and worker.subagents:  # only a bot-enabled worker (ENG-AO-02) can have bot activity
                try:
                    subagents = latest_subagents(
                        Path(item.worktree) if item.worktree else ctx.repo_root, item.task_ref, item.worker
                    )
                except (ValueError, OSError):
                    subagents = []
            attempts = []
            if runbook and runbook.task_id == item.id and runbook.recovery_note:
                attempts.append({"state": "FAILED", "summary": redact_text(runbook.recovery_note)[:800]})
            stages.append(
                {
                    "id": item.id,
                    "category": item.role,
                    "label": stage_labels.get(item.role, item.role.replace("-", " ").title()),
                    "state": item.state,
                    "worker": item.worker,
                    "execution_system": worker.execution_system if worker else None,
                    "provider": worker.provider if worker else None,
                    "model": worker.effective_model if worker else None,
                    "intensity": worker.default_intensity if worker else None,
                    "progress": None,
                    "current_action": None,
                    "current_file": None,
                    "started_at": item.created_at if item.state == "RUNNING" else None,
                    "updated_at": item.updated_at,
                    "worktree": item.worktree,
                    "subagents": subagents,
                    "attempts": attempts,
                }
            )
        states = [item.state for item in selected]
        overall = runbook.status if runbook else ("RUNNING" if "RUNNING" in states else (states[0] if states else "PENDING"))
        started = min((item.created_at for item in selected), default=None)
        return {
            "task": {
                "id": task_ref,
                "title": runbook.name if runbook else task_ref,
                "role": runbook.preset if runbook else None,
                "state": overall,
                "started_at": started,
                "updated_at": max((item.updated_at for item in selected), default=None),
                "progress": None,
                "eta": None,
            },
            "orchestrator": {
                "label": "Orchestrator",
                "execution_system": "OctaScene Control Plane",
                "state": overall,
                "current_action": f"Coordinating {len(selected)} persisted task{'s' if len(selected) != 1 else ''}",
            },
            "stages": stages,
            "worktrees": list(worktrees.values()),
            "results": results,
        }

    @app.get("/api/providers")
    def providers() -> list[dict[str, Any]]:
        tasks = scoped_tasks()
        return [_provider_to_dict(p, registry=ctx.registry, tasks=tasks) for p in ctx.state.list_provider_states()]

    @app.get("/api/routing")
    def routing(role: str = "focused-tests", mode: str = MODE_SINGLE_PRIMARY) -> dict[str, Any]:
        try:
            order = ctx.registry.route(role)
        except Exception as exc:  # noqa: BLE001 - surfaced as a 400, not a crash
            raise HTTPException(status_code=400, detail=str(exc)) from None
        provider_map = {p.name: p for p in ctx.state.list_provider_states()}
        result = compute_routing(mode, provider_map, order)
        return {
            "mode": result.mode,
            "percentages": result.percentages,
            "order": list(result.order),
            "excluded_not_configured": [
                name for name in order if name in provider_map and provider_map[name].state in NON_ROUTABLE_STATES
            ],
        }

    @app.get("/api/priority-matrix")
    def priority_matrix(mode: str = MODE_SINGLE_PRIMARY) -> dict[str, Any]:
        """Every configured role's ordered fallback chain, for the Priority & Fallback Matrix.

        OCTAREL-UI-04 (issue #23). Strictly a read model: it reuses
        ``registry.route()`` and the stored provider states that ``/api/routing``
        already uses, and computes nothing new. Routing semantics, preference
        order and eligibility rules are unchanged -- this endpoint only presents
        them for every role in one request instead of one request per role.

        Priority is the candidate's position in its role's configured route
        (P1, P2, P3, ...), which is the actual fallback order rather than a
        ranking invented for display. A candidate the router cannot currently
        use is reported with ``routable: false`` and the reason, not hidden.
        """

        provider_map = {p.name: p for p in ctx.state.list_provider_states()}
        roles: list[dict[str, Any]] = []

        for role in sorted(ctx.registry.routes):
            try:
                order = ctx.registry.route(role)
            except Exception as exc:  # noqa: BLE001 - one bad role must not break the page
                # Reported rather than skipped: a role whose route cannot be
                # resolved is a real configuration problem, and silently
                # dropping it would make the matrix look complete when it is not.
                roles.append(
                    {
                        "role": role,
                        "candidate_count": 0,
                        "routable_count": 0,
                        "candidates": [],
                        "error": f"route could not be resolved: {exc}",
                    }
                )
                continue
            result = compute_routing(mode, provider_map, order)
            shares = dict(result.percentages)
            selected = set(result.order)

            candidates: list[dict[str, Any]] = []
            for position, name in enumerate(order, start=1):
                worker = ctx.registry.workers.get(name)
                provider_state = provider_map.get(name)
                state = provider_state.state if provider_state else "UNKNOWN"
                routable = name in selected or name in shares
                candidates.append(
                    {
                        "priority": position,
                        "worker": name,
                        "display_name": (
                            worker.raw.get("display_name", name.replace("-", " ").title())
                            if worker
                            else name
                        ),
                        # A provider and an agent are distinct concepts and the
                        # design specification requires showing both.
                        "provider": worker.provider if worker else "UNKNOWN",
                        "execution_system": worker.execution_system if worker else "UNKNOWN",
                        "model": (worker.effective_model or worker.default_model) if worker else None,
                        "capability": worker.capability if worker else "UNKNOWN",
                        "cost_class": worker.cost_class if worker else "UNKNOWN",
                        "auth_mode": worker.auth_mode if worker else None,
                        "enabled": worker.enabled if worker else None,
                        "cli_available": worker.cli_available() if worker else None,
                        "provider_state": state,
                        "routable": routable,
                        "share_percent": shares.get(name),
                        "excluded_reason": (
                            None
                            if routable
                            else (
                                f"provider state {state}"
                                if state in NON_ROUTABLE_STATES
                                else "not the selected candidate for this mode"
                                if provider_state
                                else "no provider state recorded"
                            )
                        ),
                    }
                )

            roles.append(
                {
                    "role": role,
                    "candidate_count": len(candidates),
                    "routable_count": sum(1 for c in candidates if c["routable"]),
                    "candidates": candidates,
                }
            )

        return {"mode": mode, "roles": roles}

    @app.get("/api/opencode-models")
    def opencode_models() -> dict[str, Any]:
        """Cached OpenCode-discovered model inventory beside the configured OpenCode workers (ENG-AO-03).

        Read-only: it never runs OpenCode, discovers, probes, or calls a model; refresh with
        ``python -m scripts.agents.model_catalog refresh``.
        """

        body = model_catalog.inventory()
        discovered = {row["id"] for row in body["models"]}
        body["configured_workers"] = [
            {
                "worker": name,
                "role_kind": "dynamic-pool-worker" if w.model_pool else "configured-worker",
                "model": w.default_model or None,
                "model_pool": w.model_pool or None,
                "in_catalog": (w.default_model in discovered) if w.default_model else None,
                "roles": list(w.roles),
            }
            for name, w in sorted(ctx.registry.workers.items())
            if w.cli_bin == "opencode"
        ]
        return body

    @app.get("/api/native-models")
    def native_model_freshness() -> dict[str, Any]:
        """Cached native Grok/Codex model freshness: configured vs verified/candidate model (ENG-AO-04).

        Read-only: it never runs a CLI; refresh with ``python -m scripts.agents.native_models refresh``.
        """

        return native_models.inventory(ctx.registry)

    @app.get("/api/models")
    def models() -> list[dict[str, Any]]:
        tasks_by_worker: dict[str, list[Any]] = {}
        for task in scoped_tasks():
            if task.state == "RUNNING":
                tasks_by_worker.setdefault(task.worker, []).append(task)
        return [
            {
                "worker": name,
                "display_name": w.raw.get("display_name", name.replace("-", " ").title()),
                "execution_system": w.execution_system,
                "provider": w.provider,
                "default_model": w.default_model,
                "effective_model": w.effective_model,
                "native_model_family": w.native_model_family or None,
                "model_pool": w.model_pool or None,
                "default_intensity": w.default_intensity,
                "capability": w.capability,
                "cost_class": w.cost_class,
                "allowed_policy_roles": list(w.allowed_policy_roles),
                "provider_policy": w.provider_policy,
                "auth_mode": w.auth_mode,
                "repository_data_authorization": ctx.registry.effective_repository_data_authorization(name),
                "repository_data_authorization_source": (
                    "provider-level"
                    if w.provider in ctx.registry.provider_repository_data_authorizations
                    else "worker-level"
                ),
                "api_billing_enabled": w.allow_api_billing,
                "requires_isolated_worktree": w.requires_isolated_worktree,
                "enabled": w.enabled,
                "cli_available": w.cli_available(),
                "availability": next((p.state for p in ctx.state.list_provider_states() if p.name == name), "UNKNOWN"),
                "description": w.raw.get("description", w.notes),
                "best_for": list(w.raw.get("best_for", [])),
                "current_tasks": [task.id for task in tasks_by_worker.get(name, [])],
                "worktrees": [task.worktree for task in tasks_by_worker.get(name, []) if task.worktree],
            }
            for name, w in sorted(ctx.registry.workers.items())
        ]

    @app.get("/api/worktrees")
    def worktrees() -> list[dict[str, Any]]:
        return cached_worktree_statuses()

    @app.get("/api/operations")
    def operations(limit: int = 100) -> list[dict[str, Any]]:
        return ctx.state.list_operations(limit=max(1, min(limit, 100)), project_id=ctx.selected_project_id)

    @app.get("/api/repository-health")
    def repository_health_endpoint() -> dict[str, Any]:
        return repository_health(ctx)

    @app.get("/api/tests")
    def tests_summary() -> dict[str, Any]:
        gate = projected_gate()
        last_results = {t.id: t.result for t in scoped_tasks() if t.result}
        return {
            "authority": "local-deterministic-gate",
            "local_gate": gate,
            "last_task_results": last_results,
        }

    @app.get("/api/local-gate")
    def local_gate_status() -> dict[str, Any]:
        return projected_gate()

    @app.get("/api/development-throughput")
    def development_throughput(limit: int = 20) -> dict[str, Any]:
        """Truthful local evidence only; this read path makes zero provider calls."""
        from scripts.ci.local_gate import read_gate_history

        latest = projected_gate()
        evidence = latest.get("evidence") or {}
        tasks = scoped_tasks()
        task_timeline = [
            {
                "task_id": task.id,
                "task_ref": task.task_ref,
                "state": task.state,
                "worker": task.worker,
                "created_at": task.created_at,
                "updated_at": task.updated_at,
                "bootstrap_seconds": "UNKNOWN",
                "implementation_active_seconds": "UNKNOWN",
                "provider_wait_seconds": "UNKNOWN",
                "blocked_seconds": "UNKNOWN",
            }
            for task in tasks[-max(1, min(limit, 100)):]
        ]
        return {
            "authority": "local-deterministic-gate",
            "current": {
                "ready": latest.get("ready", False),
                "reason": latest.get("reason", "UNKNOWN"),
                "preflight": evidence.get("preflight", {}).get("result", "UNKNOWN"),
                "tier": evidence.get("selection", {}).get("test_tier", "UNKNOWN"),
                "test_impact": evidence.get("selection", {}).get("test_impact", {}),
                "phase_dag": evidence.get("phase_dag", []),
                "phase_results": evidence.get("checks", []),
                "ports": evidence.get("preflight", {}).get("port_allocations", {}),
                "environment": evidence.get("preflight", {}).get("environment", {}),
                "evidence_reuse": evidence.get("evidence_reuse", {}),
                "throughput": evidence.get("throughput", {}),
                "estimate_label": evidence.get("estimate_label", "UNKNOWN"),
                "estimated_phase_seconds": evidence.get("estimated_phase_seconds", {}),
            },
            "recent": read_gate_history(ctx.repo_root, limit=max(1, min(limit, 100))),
            "task_timeline": task_timeline,
            "invocations": {
                "total": len(tasks),
                "codex": sum(1 for task in tasks if task.worker.startswith("codex")),
                "by_worker": {
                    worker: sum(1 for task in tasks if task.worker == worker)
                    for worker in sorted({task.worker for task in tasks})
                },
            },
            "unknown_policy": "Unavailable timing, usage, and cost facts remain UNKNOWN.",
            "provider_calls": 0,
        }

    @app.get("/api/events")
    def events(limit: int = 200) -> list[dict[str, Any]]:
        return [
            {
                "id": e.id,
                "ts": e.ts,
                "category": e.category,
                "task_id": e.task_id,
                "provider": e.provider,
                "level": e.level,
                "message": e.message,
            }
            for e in scoped_events(limit)
        ]

    @app.get("/api/run-evidence")
    def run_evidence(limit: int = 100) -> list[dict[str, Any]]:
        return delegation_evidence(ctx.repo_root, limit=max(1, min(limit, 100)))

    def _authorized_task_id(task_id: str) -> None:
        # project/run/worker-scoped authorization only (OCTAREL-UI-01): the
        # requested task must be one THIS PROJECT's own state actually knows
        # about. An id that resolves to no task -- foreign, stale, or made up
        # -- is rejected before any .agent-output path is touched.
        #
        # ".agent-output/<x>/<worker>/<run_id>" is keyed by task_ref (see
        # scripts/agents/orchestrate.py / control_plane/supervisor.py, which
        # pass task.task_ref -- not task.id -- into run_delegation), and the
        # dashboard card that opens this viewer sends that same task_ref as
        # its "taskId" (the workflow API's "task.id" field is task_ref; see
        # workflow() above). So authorization must scope by task_ref here
        # too -- checking ctx.state.get_task(task_id) would look up the
        # wrong field and either 404 every legitimate request or (if a
        # task_ref ever collided with another task's internal id) authorize
        # the wrong task's evidence.
        if not any(item.task_ref == task_id for item in scoped_tasks()):
            raise HTTPException(status_code=404, detail=f"unknown task {task_id!r}")

    @app.get("/api/agent-activity/{task_id}/{worker}")
    def agent_activity_attempts(task_id: str, worker: str) -> dict[str, Any]:
        """Every recorded attempt (run_id) for one worker card, most recent first.

        Read-only; reuses the existing .agent-output evidence tree (see
        scripts/agents/control_plane/agent_activity.py). Never a live shell.
        """

        _authorized_task_id(task_id)
        try:
            attempts = list_attempts(ctx.repo_root, task_id, worker)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return {"task": task_id, "worker": worker, "attempts": attempts}

    @app.get("/api/agent-activity/{task_id}/{worker}/{run_id}")
    def agent_activity_attempt(task_id: str, worker: str, run_id: str) -> dict[str, Any]:
        """Details + evidence + bounded output for one attempt (Agent Activity viewer).

        Read-only, project/run/worker-scoped, redacted-on-read, bounded history --
        see agent_activity.read_attempt for the security properties this enforces.
        """

        _authorized_task_id(task_id)
        try:
            return read_attempt(ctx.repo_root, task_id, worker, run_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None

    @app.get("/api/attention")
    def attention() -> dict[str, Any]:
        old_runs, old_tasks = superseded()
        failed_or_blocked = [_task_to_dict(t) for t in scoped_tasks() if t.state in ("FAILED", "BLOCKED") and t.id not in old_tasks]
        troubled_providers = [
            _provider_to_dict(p, registry=ctx.registry, tasks=scoped_tasks())
            for p in ctx.state.list_provider_states()
            if p.state in ("QUOTA_EXHAUSTED", "RATE_LIMITED", "FAILED", "COST_BLOCKED", "LAUNCH_ENVIRONMENT_ERROR")
        ]
        troubled_runbooks = [
            _runbook_to_dict(r)
            for r in scoped_runbooks()
            if r.status in ("FAILED", "DEADLINE_REACHED", "OWNER_ACTION_REQUIRED", "BLOCKED") and r.id not in old_runs
        ]
        return {"tasks": failed_or_blocked, "providers": troubled_providers, "runbooks": troubled_runbooks}

    @app.get("/api/roadmap")
    def roadmap() -> list[dict[str, Any]]:
        """The selected project's own roadmap, parsed fresh from its repository.

        ENG-CP-03 (issue #165): the roadmap file is resolved from the selected
        project's first declared ``roadmap_paths`` entry inside its own
        checkout, so selecting a different project shows that repository's
        roadmap -- or nothing, for a project that declares none. Roadmap content
        is never copied into Control Plane state; it is read from the repository
        on every request exactly as before. An explicit ``roadmap_path``
        override (used by tests and the Playwright fixture) still wins.
        """

        target = roadmap_file
        root = ctx.project_root
        if roadmap_path is None:
            project = ctx.selected_project
            if project is not None:
                declared = next(iter(project.roadmap_paths), None)
                if declared is None:
                    return []
                target = project.local_repo_root / declared
        return derived_roadmap(root, parse_roadmap_program_index(target), include_catalog=roadmap_path is None)

    @app.get("/api/resources")
    def resources() -> dict[str, Any]:
        return _resources_snapshot(ctx.repo_root)

    @app.get("/api/app-status")
    def app_status() -> dict[str, Any]:
        body = app_lifecycle.status()
        body["port_probe"] = probe_octascene_app()
        if body["status"] == "STOPPED" and body["port_probe"] == "RUNNING":
            body["status"] = "UNKNOWN"
            body["note"] = "Port 8765 is active, but Control Center did not start that process and will not stop it."
        return body

    @app.post("/api/app-lifecycle/{action}")
    def app_lifecycle_action(action: str, request: Request, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        confirm = (payload or {}).get("confirm") is True
        if action in {"stop", "restart"} and not confirm:
            raise HTTPException(status_code=409, detail=f"{action} requires confirm=true")
        remote_identity = _remote_identity_of(request)
        actor = remote_identity.email if remote_identity else "local"
        try:
            result = app_lifecycle.action(action, actor)
        except OperationError as exc:
            _record_remote_audit(ctx, request, verb=f"app_{action}", target="local-dev-app", result=f"FAIL: {exc}")
            raise HTTPException(status_code=409, detail=str(exc)) from None
        _record_remote_audit(ctx, request, verb=f"app_{action}", target="local-dev-app", result="OK")
        return result

    @app.get("/api/terminal/history")
    def terminal_history(limit: int = 50) -> list[dict[str, Any]]:
        return ctx.state.list_terminal_commands(limit=limit, project_id=ctx.selected_project_id)

    @app.get("/api/identity")
    def identity(request: Request) -> dict[str, Any]:
        """Local git/OS operator identity, plus (ENG-AGENT-02-S6) the verified

        remote-access state for *this* request. ``access_mode``/``remote_email``
        reflect what the middleware already cryptographically verified above —
        this endpoint never re-derives or trusts anything from the request
        itself, it only reports what was already decided.
        """

        body: dict[str, Any] = dict(local_developer_identity(ctx.repo_root))
        remote_identity = _remote_identity_of(request)
        body["access_mode"] = getattr(request.state, "access_mode", "local")
        body["remote_email"] = remote_identity.email if remote_identity else None
        body["remote_access_configured"] = remote.config.enabled
        return body

    # Checkpoint status shells out to git once per worktree. A project with dozens of
    # worktrees (OctaScene has ~36) made every /api/telemetry poll cost seconds of CPU,
    # and the UI polls it every 2s from every open client, which saturated the process
    # and slowed every other endpoint. Compute it at most once per TTL, shared by all
    # callers (one computation at a time), and only for the selected project's worktrees.
    checkpoint_cache: dict[str, Any] = {"key": object(), "at": 0.0, "rows": []}
    checkpoint_lock = threading.Lock()

    def cached_checkpoints() -> list[dict[str, Any]]:
        key = ctx.selected_project_id
        with checkpoint_lock:
            fresh = checkpoint_cache["key"] == key and time.monotonic() - checkpoint_cache["at"] < CHECKPOINT_CACHE_TTL_SECONDS
            if not fresh:
                rows = [
                    checkpoint_status(Path(w.path)).as_dict()
                    for w in ctx.state.list_worktrees(project_id=key)
                    if Path(w.path).exists()
                ]
                checkpoint_cache.update(key=key, at=time.monotonic(), rows=rows)
            return list(checkpoint_cache["rows"])

    @app.get("/api/telemetry")
    def telemetry() -> dict[str, Any]:
        """Slice 3 read model: routes, quota, and checkpoint state.

        Purely local: provider ``cost_class`` -> route mapping, hand-entered
        pricing/budget config (none configured yet, so every cost stays
        ``None`` with an explicit reason), the fixed Claude quota windows
        (``UNKNOWN`` until a legitimate local source exists), and one
        ``git``-derived checkpoint status per known worktree. No network
        call, no provider/model request.
        """

        providers = [
            {
                "name": p.name,
                "cost_class": p.cost_class,
                "execution_route": execution_route_for_cost_class(p.cost_class),
                "state": p.state,
            }
            for p in ctx.state.list_provider_states()
        ]
        checkpoint_rows = cached_checkpoints()
        checkpoint_age = max(0.0, time.monotonic() - checkpoint_cache["at"])
        return {
            "providers": providers,
            "quota_windows": [w.as_dict() for w in claude_quota_windows()],
            "checkpoints": checkpoint_rows,
            "checkpoint_cache": {
                "age_seconds": round(checkpoint_age, 3),
                "ttl_seconds": CHECKPOINT_CACHE_TTL_SECONDS,
                "stale": checkpoint_age >= CHECKPOINT_CACHE_TTL_SECONDS,
            },
        }

    @app.get("/api/usage")
    def usage(request: Request) -> dict[str, Any]:
        """Per-provider account/usage facts, each labelled with its real source.

        Every fact is either CLI_REPORTED (a real local status query, e.g.
        ``claude auth status``, never billable) or NOT_EXPOSED — this never
        fabricates a number a provider does not actually report. Cached for
        ``USAGE_CACHE_TTL_SECONDS`` (server-side, shared across every caller)
        so repeated polling/view-switching can never turn into a subprocess
        spawned per worker on every request; pass ``?refresh=true`` to force
        a fresh read after a real state change (e.g. just logged in).
        """

        now = time.monotonic()
        force = request.query_params.get("refresh") == "true"
        if not force and _usage_cache["body"] is not None and (now - _usage_cache["at"]) < USAGE_CACHE_TTL_SECONDS:
            return _usage_cache["body"]

        facts = []
        tasks = scoped_tasks()
        for worker_name in ctx.registry.workers:
            facts.extend(f.as_dict() for f in account_facts_for_worker(worker_name))
            facts.append(
                {
                    "worker": worker_name,
                    "label": "local_running_tasks",
                    "value": str(sum(1 for task in tasks if task.worker == worker_name and task.state == "RUNNING")),
                    "source": SOURCE_LOCAL_ACCOUNTING,
                    "note": "Counted from the local Control Center task store",
                }
            )
        body = {
            "facts": facts,
            "cached_for_seconds": USAGE_CACHE_TTL_SECONDS,
            "refreshed_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        }
        _usage_cache["at"] = now
        _usage_cache["body"] = body
        return body

    @app.get("/api/usage-routing")
    def usage_routing() -> dict[str, Any]:
        """Durable policy, route, escalation and truthful token-quality facts."""

        return {
            "default_unattended_policy": "conserve",
            "records": ctx.state.list_usage_governance(),
            "policy_note": "Role/workflow policy is provider-independent. Quota may use an equivalent eligible provider; context is minimized first; safeguards block; none silently strengthens a model.",
        }

    @app.get("/api/runbooks/presets")
    def runbook_presets() -> list[dict[str, Any]]:
        """Zero-AI, static preset catalog (Overnight Development, Finish PR, ...)."""

        return list_presets()

    @app.get("/api/quickstart")
    def quickstart_options() -> list[dict[str, Any]]:
        """One-click, fully specified run proposals derived from current repository truth.

        Each option is re-resolved on every call (e.g. "Continue Video Editor"
        re-parses the Video Editor ledger) — nothing here is a cached or
        hard-coded task ID. An option reports `ready: false` with an explicit
        `unavailable_reason` rather than a proposal that would fail later.
        """

        return list_quickstart_options(ctx.project_root, project=ctx.selected_project, state=ctx.state, registry=ctx.registry)

    @app.get("/api/overnight")
    def overnight_sessions() -> dict[str, Any]:
        """Durable ENG-AO-05 sessions for the selected project (read-only; the daemon advances them)."""

        from .overnight import list_views

        project_id = ctx.selected_project_id
        sessions = list_views(ctx.state, project_id) if project_id else []
        live = next((s for s in sessions if s["state"] in {"ACTIVE", "PAUSED", "STOPPING"}), None)
        return {"project_id": project_id, "current": live or (sessions[0] if sessions else None), "sessions": sessions[:10]}

    @app.get("/api/runbooks")
    def runbooks_list() -> list[dict[str, Any]]:
        return [_runbook_to_dict(r, state=ctx.state, registry=ctx.registry) for r in scoped_runbooks()]

    @app.get("/api/runbooks/{runbook_id}")
    def runbook_detail(runbook_id: str) -> dict[str, Any]:
        runbook = ctx.state.get_runbook(runbook_id)
        if runbook is None:
            raise HTTPException(status_code=404, detail=f"unknown runbook {runbook_id!r}")
        body = _runbook_to_dict(runbook, state=ctx.state, registry=ctx.registry)
        if runbook.task_id:
            task = ctx.state.get_task(runbook.task_id)
            body["task"] = _task_to_dict(task) if task else None
        body["usage_governance"] = ctx.state.get_usage_governance(runbook.id)
        body["events"] = [
            {
                "id": e.id,
                "ts": e.ts,
                "category": e.category,
                "level": e.level,
                "message": e.message,
            }
            for e in scoped_events(500)
            if e.task_id == runbook.task_id
        ]
        return body

    @app.post("/api/runbooks/{runbook_id}/advance")
    def runbook_advance(runbook_id: str) -> dict[str, Any]:
        """Re-evaluate advancement from current repository truth (idempotent once a successor started)."""

        from .advancement import advance_after_success
        from .advancement_lease import advancement_lease, lease_owner

        runbook = ctx.state.get_runbook(runbook_id)
        if runbook is None:
            raise HTTPException(status_code=404, detail=f"unknown runbook {runbook_id!r}")
        # ENG-AO-07: never advance a runbook another process owns; report the owner instead.
        with advancement_lease(ctx.state, runbook_id, holder="dashboard_advance") as owner:
            if not owner:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": f"runbook {runbook_id!r} is being advanced by another Octarel process",
                        "owner": lease_owner(ctx.state, runbook_id),
                    },
                )
            return advance_after_success(
                state=ctx.state, runbook=runbook, registry=ctx.registry,
                supervisor=ctx.supervisor, scheduler=ctx.scheduler,
            )

    @app.get("/api/runbooks/{runbook_id}/report")
    def runbook_report(runbook_id: str) -> dict[str, Any]:
        runbook = ctx.state.get_runbook(runbook_id)
        if runbook is None:
            raise HTTPException(status_code=404, detail=f"unknown runbook {runbook_id!r}")
        return {"runbook_id": runbook.id, "status": runbook.status, "report_markdown": runbook.report_markdown}

    @app.post("/api/commands/{verb}")
    def run_command(verb: str, request: Request, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = dict(payload or {})
        # Only the new runbook-stop verbs gain a server-side confirm gate here
        # (same tamper-proof pattern as /api/steering/execute: derived from the
        # verb string, never a client flag). Pre-existing task/provider verbs on
        # this endpoint keep their established contract — their own UI-level
        # confirmation dialogs and Playwright coverage predate this change.
        confirm = body.pop("confirm", None) is True
        if verb in RUNBOOK_DESTRUCTIVE_COMMANDS | CONFIRM_COMMANDS and not confirm:
            raise HTTPException(
                status_code=409, detail=f"{verb} is destructive and requires confirm=true after operator review"
            )
        if verb == "git_operation":
            body["confirm"] = confirm
        if verb == "worktree_cleanup":
            body["confirm"] = confirm
        target = body.get("task_id") or body.get("runbook_id") or body.get("name") or body.get("path")
        try:
            result = apply_command(ctx, verb, **body)
        except CommandError as exc:
            _record_remote_audit(ctx, request, verb=verb, target=target, result=f"FAIL: {exc}")
            raise HTTPException(status_code=400, detail=str(exc)) from None
        _record_remote_audit(ctx, request, verb=verb, target=target, result="OK" if result.ok else "FAIL")
        return {"ok": result.ok, "message": result.message, "data": result.data}

    def _attach_quickstart_option(proposal: Any, body: dict[str, Any]) -> None:
        """Two-stage steering (ENG-AGENT-02-S7, issue #97): a PARSED proposal alone
        is not enough detail for the operator to approve, so attach the same fully
        resolved Prepared Run the Quick Start button shows, re-derived fresh from
        current repository truth. Shared by /api/steering/parse and the Manager
        endpoint so the two cannot drift apart.
        """

        if proposal.verb != "quickstart_start":
            return
        option = resolve_quickstart_option(
            ctx.project_root,
            str(proposal.args.get("key", "continue-video-editor")),
            project=ctx.selected_project,
            state=ctx.state,
            registry=ctx.registry,
        )
        body["quickstart_option"] = option.as_dict()

    @app.post("/api/steering/parse")
    def steering_parse(payload: dict[str, Any]) -> dict[str, Any]:
        """Parse steering text into a proposal. Zero AI, never executes anything.

        Slash commands and the bounded NL matcher are tried in that order
        (``parse_steering_text``). When neither recognizes the input, the
        response also reports whether an AI-assisted escalation route is even
        configured right now — informational only, never invoked here.
        """

        text = str(payload.get("text", ""))
        proposal = parse_steering_text(text)
        body = proposal.as_dict()
        if proposal.status != "PARSED":
            body["ai_escalation"] = nl_ai_route_available(ctx.registry)
        else:
            _attach_quickstart_option(proposal, body)
        ctx.state.record_event(
            category="steering",
            level="warning" if proposal.status != "PARSED" else "info",
            message=f"parsed steering input ({proposal.status}): {text!r} -> {proposal.verb or 'none'}",
        )
        return body

    @app.post("/api/steering/execute")
    def steering_execute(payload: dict[str, Any], request: Request) -> dict[str, Any]:
        """Execute a previously-parsed proposal. Destructive verbs require ``confirm: true``.

        The client must resend the exact ``verb``/``args`` a prior
        ``/api/steering/parse`` call returned — this endpoint re-derives
        destructiveness from :data:`steering.DESTRUCTIVE_VERBS` itself rather
        than trusting a client-supplied flag, so a tampered/stale client can
        never bypass the confirmation gate.
        """

        verb = payload.get("verb")
        args = payload.get("args") or {}
        # Strict identity check, not bool(): bool("false") and bool("no") are
        # both True in Python, which would let a JSON string "confirm": "false"
        # silently pass a truthy-but-wrong value through the gate. Only the
        # JSON literal `true` (Python True) counts as confirmed.
        confirm = payload.get("confirm") is True
        raw_text = str(payload.get("raw_text", ""))
        if not verb:
            raise HTTPException(status_code=400, detail="missing verb; call /api/steering/parse first")
        if not isinstance(args, dict):
            raise HTTPException(status_code=400, detail="args must be an object")
        target = args.get("task_id") or args.get("runbook_id") or args.get("name")
        if verb in DESTRUCTIVE_VERBS and not confirm:
            ctx.state.record_event(
                category="steering",
                level="warning",
                message=f"blocked unconfirmed destructive steering action: {verb}({args})",
            )
            _record_remote_audit(ctx, request, verb=verb, target=target, result="BLOCKED: unconfirmed")
            raise HTTPException(
                status_code=409,
                detail=f"{verb} is destructive and requires confirm=true after the operator reviews the preview",
            )
        try:
            result = apply_command(ctx, verb, **args)
        except CommandError as exc:
            _record_remote_audit(ctx, request, verb=verb, target=target, result=f"FAIL: {exc}")
            raise HTTPException(status_code=400, detail=str(exc)) from None
        ctx.state.record_event(
            category="steering",
            message=f"executed steering action ({raw_text!r}): {verb}({args}) -> {result.message}",
        )
        _record_remote_audit(ctx, request, verb=verb, target=target, result="OK" if result.ok else "FAIL")
        return {"ok": result.ok, "message": result.message, "data": result.data}

    @app.get("/api/manager/route")
    def manager_route() -> dict[str, Any]:
        """Which worker/provider/model would interpret a Manager Chat sentence right now.

        OCTAREL-UI-05 (issue #24). Read-only: it inspects the registry, the
        configured route order and stored provider state, and never invokes a
        worker or calls a model. The Manager surface uses it to show the route
        before the operator sends anything.
        """

        provider_map = {p.name: p for p in ctx.state.list_provider_states()}
        return _manager_chat.select_route(ctx.registry, provider_map).as_dict()

    @app.post("/api/manager/message")
    def manager_message(payload: dict[str, Any]) -> dict[str, Any]:
        """Turn one Manager Chat message into a proposal. Never executes anything.

        OCTAREL-UI-05 (issue #24). Deterministic parsing is tried first, so a
        slash command or a high-confidence bounded intent stays a zero-AI fast
        path. Only genuinely unrecognized text is routed to an eligible
        interpreter, along the role's normal configured route.

        The result is always a *proposal*. Execution still goes through
        ``/api/steering/execute``, which re-derives destructiveness itself, so
        a natural-language request for a destructive action cannot skip the
        confirmation gate.
        """

        text = str(payload.get("text", ""))
        if not text.strip():
            raise HTTPException(status_code=400, detail="message text is required")

        deterministic = parse_steering_text(text)
        if deterministic.status == "PARSED":
            body = deterministic.as_dict()
            _attach_quickstart_option(deterministic, body)
            body["route"] = {"kind": "deterministic", "eligible": True, "reason": "matched without any model call"}
            body["interpretation_status"] = "PARSED"
            ctx.state.record_event(
                category="steering",
                message=f"manager message parsed deterministically: {text!r} -> {deterministic.verb}",
            )
            return body

        provider_map = {p.name: p for p in ctx.state.list_provider_states()}
        interpretation = _manager_chat.interpret(
            text,
            registry=ctx.registry,
            provider_states=provider_map,
            invoker=manager_invoker,
        )
        body = interpretation.as_dict()
        body["route"]["kind"] = "model"

        route = interpretation.route
        # Route evidence goes into the same event log every other execution
        # decision uses, so a fallback is auditable after the fact.
        ctx.state.record_event(
            category="steering",
            level="info" if interpretation.status == "PARSED" else "warning",
            message=(
                f"manager message interpreted ({interpretation.status}) via "
                f"{route.worker or 'no-route'}/{route.provider or '-'}/{route.model or '-'}: "
                f"{text!r} -> {interpretation.proposal.verb or 'none'}"
            ),
        )
        return body

    @app.get("/api/steering/ai-route")
    def steering_ai_route() -> dict[str, Any]:
        """Whether a cheap authorized NL-escalation worker is currently available."""

        return nl_ai_route_available(ctx.registry)

    @app.get("/api/terminal/info")
    def terminal_info() -> dict[str, Any]:
        return _terminal_info(ctx.repo_root)

    @app.websocket("/api/terminal/ws")
    async def terminal_socket(websocket: WebSocket) -> None:
        identity = _terminal_websocket_identity(websocket, remote)
        if identity is None:
            await websocket.close(code=4403, reason="terminal authentication required")
            return
        if identity != "local" and not remote.state_change_limiter.allow(identity):
            await websocket.close(code=4429, reason="too many terminal connection attempts")
            return
        await websocket.accept()
        root = ctx.repo_root.resolve()
        info = _terminal_info(root)
        master_fd, slave_fd = pty.openpty()
        shell = shutil.which("zsh") or "/bin/zsh"
        process = subprocess.Popen(
            [shell, "-f"],
            cwd=root,
            env=_terminal_child_env(root),
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            start_new_session=True,
            close_fds=True,
        )
        os.close(slave_fd)
        pid = process.pid
        session_id = f"pty-{pid}-{int(time.time())}"
        command_buffer = ""
        ctx.state.record_event(category="terminal", message=f"terminal opened ({identity}); pid={pid}; session={session_id}")
        await websocket.send_json({"type": "ready", **info})

        output_queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def queue_output() -> None:
            try:
                chunk = os.read(master_fd, 4096)
            except OSError:
                chunk = b""
            output_queue.put_nowait(chunk or None)

        loop.add_reader(master_fd, queue_output)

        async def send_output() -> None:
            while True:
                chunk = await output_queue.get()
                if chunk is None:
                    return
                await websocket.send_json({"type": "output", "data": chunk.decode("utf-8", errors="replace")})

        output_task = asyncio.create_task(send_output())
        try:
            while True:
                message = await websocket.receive_json()
                kind = message.get("type")
                if kind == "input":
                    data = str(message.get("data", ""))
                    if len(data.encode("utf-8")) > TERMINAL_MAX_INPUT_BYTES:
                        await websocket.close(code=4409, reason="terminal input too large")
                        break
                    os.write(master_fd, data.encode("utf-8"))
                    command_buffer += data
                    while "\r" in command_buffer or "\n" in command_buffer:
                        positions = [p for p in (command_buffer.find("\r"), command_buffer.find("\n")) if p >= 0]
                        end = min(positions)
                        raw_command, command_buffer = command_buffer[:end], command_buffer[end + 1 :]
                        sanitized = sanitize_terminal_command(raw_command)
                        if sanitized:
                            ctx.state.record_terminal_command(actor=identity, session_id=session_id, cwd=str(root), branch=_git_current_branch(root), command=sanitized, project_id=ctx.selected_project_id)
                elif kind == "resize":
                    rows = max(2, min(200, int(message.get("rows", 24))))
                    cols = max(10, min(500, int(message.get("cols", 80))))
                    fcntl.ioctl(master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        except (WebSocketDisconnect, ValueError, OSError):
            pass
        finally:
            loop.remove_reader(master_fd)
            with contextlib.suppress(OSError):
                os.close(master_fd)
            output_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, OSError):
                await output_task
            process.terminate()
            try:
                await asyncio.to_thread(process.wait, 1.0)
            except subprocess.TimeoutExpired:
                process.kill()
                await asyncio.to_thread(process.wait)
            ctx.state.record_event(category="terminal", message=f"terminal closed ({identity}); pid={pid}; session={session_id}; exit={process.returncode}")

    if DASHBOARD_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(DASHBOARD_DIR)), name="dashboard-static")

        @app.get("/")
        def index() -> FileResponse:
            return FileResponse(str(DASHBOARD_DIR / "index.html"))

    return app
