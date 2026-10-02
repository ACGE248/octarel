"""ENG-PC-09 durable, project-declared runtime services.

This module is the single supervisor behind both the generic runtime-service
API and the legacy ``AppLifecycleManager`` compatibility adapter.  A service
can only launch the selected project's fixed argv, in that project's declared
root or a server-validated project worktree.  Destructive actions fail closed
unless pid, create time, cwd, argv, and process session all still match the
durable identity captured at launch.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import signal
import socket
import subprocess
import threading
import time
from collections.abc import Callable
from functools import wraps
from pathlib import Path
from typing import Any, TypeVar, cast

from .models import utc_now_iso

RUNTIME_ACTIONS = frozenset({"start", "stop", "restart"})
_FORBIDDEN_LAUNCHERS = frozenset(
    {
        "sh",
        "bash",
        "dash",
        "ash",
        "zsh",
        "fish",
        "ksh",
        "mksh",
        "csh",
        "tcsh",
        "pwsh",
        "powershell",
        "cmd",
        "cmd.exe",
        "env",
        "sudo",
        "doas",
        "su",
        "runuser",
    }
)
_SHELL_SYNTAX = frozenset({";", "|", "&", "<", ">", "`", "\n", "\r", "\x00"})
_SENSITIVE_ARG = re.compile(r"(?i)(password|passwd|token|api[_-]?key|authorization|private[_-]?key|secret)")
_PUBLIC_BIND_ARG = re.compile(
    r"(?ix)(?:^|[=:/])(?:0\.0\.0\.0|\[::\]|\*)(?=$|[:/])"
    r"|(?:^|[=])::(?=$|[:/])"
    r"|(?:^|=):\d+(?:$|/)"
)
_FACTS_NOT_SUPPLIED = object()
_IDENTITY_CAPTURE_TIMEOUT_SECONDS = 1.0
_IDENTITY_STABLE_SECONDS = 0.1
_IDENTITY_POLL_SECONDS = 0.01
_TERMINATION_TIMEOUT_SECONDS = 8.0
_F = TypeVar("_F", bound=Callable[..., Any])


def _supervisor_locked(method: _F) -> _F:
    """Serialize process observation and mutation through one supervisor."""

    @wraps(method)
    def wrapper(self: RuntimeServiceManager, *args: Any, **kwargs: Any) -> Any:
        with self._supervisor_lock:
            return method(self, *args, **kwargs)

    return cast(_F, wrapper)


class RuntimeServiceError(ValueError):
    """A safe, operator-actionable runtime-service refusal."""


class RuntimeServiceMissingError(RuntimeServiceError):
    """The requested service scope has no durable managed-process record."""


class RuntimeServiceManager:
    """Own project/worktree/run-scoped local services through one supervisor."""

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self._processes: dict[str, subprocess.Popen[str]] = {}
        # FastAPI executes synchronous routes in a shared threadpool. One
        # re-entrant boundary keeps a stale observation from being persisted
        # after a lifecycle mutation, while allowing restart/shutdown to call
        # the same locked helpers without deadlocking.
        self._supervisor_lock = threading.RLock()

    # -------------------------------------------------------------- declaration

    @property
    def _project(self) -> Any | None:
        return getattr(self.ctx, "selected_project", None)

    def _declaration(self) -> tuple[list[str], int, str]:
        project = self._project
        if project is None:
            raise RuntimeServiceError("no managed project is selected; runtime service is unavailable")
        capabilities = project.capabilities
        raw_argv = capabilities.get("runtime_service_argv")
        if raw_argv:
            try:
                argv = json.loads(raw_argv)
            except (TypeError, ValueError) as exc:
                raise RuntimeServiceError(
                    f"project {project.project_id!r} runtime_service_argv must be a JSON string list"
                ) from exc
        else:
            # Compatibility for already-registered projects.  shell=False and
            # the syntax checks below keep this a fixed argv, not a shell.
            legacy = (capabilities.get("app_lifecycle_command") or "").strip()
            argv = legacy.split() if legacy else []
        if not isinstance(argv, list) or not argv or not all(isinstance(part, str) and part for part in argv):
            raise RuntimeServiceError(
                f"project {project.project_id!r} does not declare a fixed runtime_service_argv"
            )
        if Path(argv[0]).name.lower() in _FORBIDDEN_LAUNCHERS:
            raise RuntimeServiceError(
                "runtime service argv may not invoke a shell, command, env, or privilege wrapper"
            )
        if any(any(token in part for token in _SHELL_SYNTAX) for part in argv):
            raise RuntimeServiceError("runtime service argv contains forbidden shell syntax")
        if any(_SENSITIVE_ARG.search(part) for part in argv):
            raise RuntimeServiceError("runtime service argv may not contain secret-bearing arguments")
        if any(_PUBLIC_BIND_ARG.search(part) for part in argv):
            raise RuntimeServiceError("runtime service argv may not request a public bind address")
        declared_host = capabilities.get("runtime_service_host")
        if raw_argv and declared_host != "127.0.0.1":
            raise RuntimeServiceError("runtime_service_host must be explicitly declared as 127.0.0.1")
        raw_port = capabilities.get("runtime_service_port") or capabilities.get("app_lifecycle_port")
        if not raw_port:
            raise RuntimeServiceError(
                f"project {project.project_id!r} does not declare runtime_service_port"
            )
        try:
            port = int(raw_port)
        except (TypeError, ValueError) as exc:
            raise RuntimeServiceError(
                f"project {project.project_id!r} runtime service port is not an integer: {raw_port!r}"
            ) from exc
        if not 1024 <= port <= 65535:
            raise RuntimeServiceError("runtime service port must be an unprivileged TCP port (1024-65535)")
        name = (capabilities.get("runtime_service_name") or f"{project.display_name} development service").strip()
        return list(argv), port, name

    def _scope(self, worktree_path: str | None, runbook_id: str | None) -> tuple[Path, str | None, str | None]:
        project = self._project
        if project is None:
            raise RuntimeServiceError("no managed project is selected")
        root = project.local_repo_root.resolve()
        known_worktrees = {
            Path(row.path).resolve()
            for row in self.ctx.state.list_worktrees(project_id=project.project_id)
        }
        known_worktrees.add(root)
        resolved_worktree: str | None = None
        resolved_runbook: str | None = None
        if worktree_path:
            candidate = Path(worktree_path).resolve()
            if candidate not in known_worktrees or not candidate.is_dir():
                raise RuntimeServiceError("runtime service cwd is not a known worktree of the selected project")
            root = candidate
            resolved_worktree = str(candidate)
        if runbook_id:
            runbook = self.ctx.state.get_runbook(runbook_id)
            if runbook is None or runbook.project_id != project.project_id:
                raise RuntimeServiceError("runtime service runbook does not belong to the selected project")
            runbook_worktree = Path(runbook.worktree).resolve()
            if runbook_worktree not in known_worktrees:
                raise RuntimeServiceError("runtime service runbook cwd is not a known worktree of the selected project")
            if resolved_worktree is not None and runbook_worktree != root:
                raise RuntimeServiceError("runtime service worktree does not match the declared runbook worktree")
            if not runbook_worktree.is_dir():
                raise RuntimeServiceError("runtime service runbook worktree no longer exists")
            root = runbook_worktree
            resolved_worktree = str(runbook_worktree)
            resolved_runbook = runbook.id
        return root, resolved_worktree, resolved_runbook

    def _service_id(self, project_id: str, worktree_path: str | None, runbook_id: str | None) -> str:
        identity = json.dumps([project_id, worktree_path or "", runbook_id or ""], separators=(",", ":"))
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
        return f"runtime-{project_id}-{digest}"

    def _log_pointer(self, service_id: str) -> str:
        return f"runtime-services/{service_id}.log"

    def _log_path(self, pointer: str) -> Path:
        if str(self.ctx.state.db_path) == ":memory:":
            raise RuntimeServiceError("runtime service launch requires durable file-backed orchestration state")
        state_root = self.ctx.state.db_path.parent.resolve()
        target = (state_root / pointer).resolve()
        if state_root not in target.parents:
            raise RuntimeServiceError("runtime service log pointer escaped the state directory")
        return target

    def _declared_row(self, worktree_path: str | None = None, runbook_id: str | None = None) -> dict[str, Any]:
        project = self._project
        if project is None:
            raise RuntimeServiceError("no managed project is selected")
        argv, port, name = self._declaration()
        cwd, worktree_path, runbook_id = self._scope(worktree_path, runbook_id)
        service_id = self._service_id(project.project_id, worktree_path, runbook_id)
        now = utc_now_iso()
        return {
            "id": service_id,
            "project_id": project.project_id,
            "name": name,
            "worktree_path": worktree_path,
            "runbook_id": runbook_id,
            "argv": argv,
            "process_argv": None,
            "cwd": str(cwd),
            "pid": None,
            "process_create_time": None,
            "process_session_id": None,
            "port": port,
            "preview_url": f"http://127.0.0.1:{port}",
            "health": "STOPPED",
            "ownership": "OCTAREL_DECLARED",
            "owner": None,
            "log_pointer": self._log_pointer(service_id),
            "status_reason": "declared fixed argv is eligible to start",
            "created_at": now,
            "updated_at": now,
            "started_at": None,
            "last_health_at": now,
            "stopped_at": None,
            "exit_code": None,
        }

    # --------------------------------------------------------------- observation

    @staticmethod
    def _port_active(port: int) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return True
        except (ConnectionRefusedError, TimeoutError, OSError):
            return False

    @staticmethod
    def _process_facts(pid: int) -> dict[str, Any] | None:
        try:
            import psutil
        except ImportError:
            return None
        try:
            process = psutil.Process(pid)
            return {
                "create_time": process.create_time(),
                "cwd": str(Path(process.cwd()).resolve()),
                "argv": process.cmdline(),
                "session_id": os.getsid(pid),
                "alive": process.is_running() and process.status() != psutil.STATUS_ZOMBIE,
            }
        except (OSError, ValueError, psutil.Error):
            return None

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            # A live process owned by another account is precisely the case
            # that must remain unverified and non-stoppable.
            return True
        except OSError:
            return False

    @staticmethod
    def _observed_argv_matches_declaration(declared: list[str], observed: list[str]) -> bool:
        """Allow only the argv[0] normalization performed by a real exec.

        macOS framework Python replaces ``python3`` with its absolute
        ``Python.app`` executable in the process cmdline. The remaining argv
        must stay byte-for-byte equal, and any changed argv[0] must become an
        absolute executable path. This keeps launch intent strict without
        later mistaking an unrelated process for the owned child.
        """

        if not declared or not observed or len(declared) != len(observed):
            return False
        if observed == declared:
            return True
        return observed[1:] == declared[1:] and Path(observed[0]).is_absolute()

    def _capture_process_identity(
        self,
        process: subprocess.Popen[str],
        row: dict[str, Any],
    ) -> dict[str, Any]:
        """Wait for post-spawn exec normalization, then capture stable facts."""

        deadline = time.monotonic() + _IDENTITY_CAPTURE_TIMEOUT_SECONDS
        stable_fingerprint: tuple[Any, ...] | None = None
        stable_since: float | None = None
        failure_reason = "launched process identity could not be captured"
        while time.monotonic() < deadline:
            facts = self._process_facts(process.pid)
            if facts is None:
                if process.poll() is not None:
                    failure_reason = "launched process exited before its identity became stable"
                    break
                stable_fingerprint = None
                stable_since = None
            elif not facts["alive"]:
                failure_reason = "launched process exited before its identity became stable"
                break
            elif facts["cwd"] != str(Path(row["cwd"]).resolve()):
                failure_reason = "launched process did not retain the declared cwd"
                stable_fingerprint = None
                stable_since = None
            elif int(facts["session_id"]) != process.pid:
                failure_reason = "launched process did not retain its dedicated process session"
                stable_fingerprint = None
                stable_since = None
            elif not self._observed_argv_matches_declaration(
                list(row["argv"]), list(facts["argv"])
            ):
                failure_reason = "launched process argv is not a permitted normalization of the declaration"
                stable_fingerprint = None
                stable_since = None
            else:
                now = time.monotonic()
                fingerprint = (
                    float(facts["create_time"]),
                    facts["cwd"],
                    tuple(facts["argv"]),
                    int(facts["session_id"]),
                )
                if fingerprint != stable_fingerprint:
                    stable_fingerprint = fingerprint
                    stable_since = now
                elif stable_since is not None and now - stable_since >= _IDENTITY_STABLE_SECONDS:
                    return facts
            time.sleep(_IDENTITY_POLL_SECONDS)
        raise RuntimeServiceError(failure_reason)

    def _cleanup_failed_launch(
        self,
        process: subprocess.Popen[str],
        row: dict[str, Any],
        failure_reason: str,
    ) -> None:
        """Terminate and reap only the exact failed start-new-session child."""

        if self._processes.get(row["id"]) is not process:
            raise RuntimeServiceError(
                f"{failure_reason}; launched child handle is no longer exact; "
                "refusing an unverified cleanup signal"
            )
        if process.poll() is None:
            try:
                session_id = os.getsid(process.pid)
            except ProcessLookupError:
                # The child exited between poll and getsid; wait below is the
                # authoritative reap/exit confirmation.
                session_id = None
            except OSError as exc:
                raise RuntimeServiceError(
                    f"{failure_reason}; launched child session could not be verified; "
                    "the exact handle remains tracked and no signal was sent"
                ) from exc
            if session_id is not None:
                if session_id != process.pid:
                    raise RuntimeServiceError(
                        f"{failure_reason}; launched child did not retain its dedicated process session; "
                        "the exact handle remains tracked and no signal was sent"
                    )
                try:
                    os.killpg(session_id, signal.SIGTERM)
                except ProcessLookupError:
                    # A natural exit raced the signal. Waiting still confirms
                    # and reaps the exact direct child.
                    pass
                except OSError as exc:
                    raise RuntimeServiceError(
                        f"{failure_reason}; SIGTERM could not be sent to the verified process group; "
                        "the exact handle remains tracked"
                    ) from exc
        try:
            process.wait(timeout=_TERMINATION_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeServiceError(
                f"{failure_reason}; launched process group did not exit after SIGTERM; "
                "the exact handle remains tracked and Octarel refuses to force-kill it"
            ) from exc
        if self._processes.get(row["id"]) is process:
            self._processes.pop(row["id"])

    def _ownership_proof(
        self,
        row: dict[str, Any],
        facts: Any = _FACTS_NOT_SUPPLIED,
    ) -> tuple[bool, str]:
        required = ("pid", "process_create_time", "process_session_id", "cwd", "argv", "process_argv")
        if any(row.get(field) in (None, "", []) for field in required):
            return False, "durable process identity is incomplete"
        if not self._observed_argv_matches_declaration(
            list(row["argv"]), list(row["process_argv"])
        ):
            return False, "durable process argv is not a permitted normalization of the declaration"
        if facts is _FACTS_NOT_SUPPLIED:
            facts = self._process_facts(int(row["pid"]))
        if facts is None and self._pid_alive(int(row["pid"])):
            return False, "live process identity cannot be inspected completely"
        if facts is None or not facts["alive"]:
            return False, "recorded process is not alive"
        if abs(float(facts["create_time"]) - float(row["process_create_time"])) > 0.05:
            return False, "PID create time changed; refusing possible PID reuse"
        if facts["cwd"] != str(Path(row["cwd"]).resolve()):
            return False, "process cwd no longer matches the declared service scope"
        if list(facts["argv"]) != list(row["process_argv"]):
            return False, "process argv no longer matches the captured process identity"
        if int(facts["session_id"]) != int(row["process_session_id"]):
            return False, "process session no longer matches the launched service"
        if int(row["process_session_id"]) != int(row["pid"]):
            return False, "launched process is not the leader of its recorded session"
        return True, "complete process identity matches"

    def _public(self, row: dict[str, Any], *, port_active: bool | None = None) -> dict[str, Any]:
        body = dict(row)
        terminal_health = (
            body.get("health")
            if body.get("health") in {"STOPPED", "EXITED", "CRASHED"}
            else None
        )
        facts = self._process_facts(int(body["pid"])) if body.get("pid") else None
        if body.get("pid"):
            proof, proof_reason = self._ownership_proof(body, facts)
        else:
            proof, proof_reason = False, "service has no live identity"
        if port_active is None:
            port_active = self._port_active(int(body["port"]))
        if body.get("pid"):
            alive = bool(facts and facts["alive"]) or (facts is None and self._pid_alive(int(body["pid"])))
            if alive and proof:
                body["health"] = "HEALTHY" if port_active else "STARTING"
                body["ownership"] = "OWNED_VERIFIED"
                body["status_reason"] = proof_reason if port_active else "owned process is alive; loopback port is not ready"
            elif alive:
                if terminal_health is not None:
                    # A terminal record retains historical PID evidence. If
                    # that number now names a different process, it remains
                    # non-stoppable but cannot permanently block a safe fresh
                    # launch on a free port.
                    body["health"] = terminal_health
                    body["ownership"] = "OWNED_EXITED"
                    body["status_reason"] = (
                        f"{proof_reason}; terminal process evidence is preserved and the live PID is not owned"
                    )
                else:
                    body["health"] = "UNVERIFIED"
                    body["ownership"] = "OWNERSHIP_UNPROVEN"
                    body["status_reason"] = proof_reason
            elif body.get("health") not in {"STOPPED", "EXITED"}:
                process = self._processes.get(body["id"])
                if process is not None:
                    body["exit_code"] = process.poll()
                body["health"] = "CRASHED"
                body["ownership"] = "OWNED_EXITED"
                body["status_reason"] = "owned process exited; evidence and append-only log were preserved"
        if body["health"] in {"STOPPED", "CRASHED", "EXITED"} and port_active:
            # Never attribute a listener merely because it uses the declared
            # port. It is informational and cannot gain a stop control.
            body["health"] = "EXTERNAL"
            body["ownership"] = "EXTERNAL_UNOWNED"
            body["status_reason"] = "declared loopback port is occupied by an external or unverified listener"
        body["last_health_at"] = utc_now_iso()
        retained_child_alive = False
        if body["health"] in {"STOPPED", "CRASHED", "EXITED"} and not port_active:
            retained_process = self._processes.get(body["id"])
            retained_child_alive = retained_process is not None and retained_process.poll() is None
        if retained_child_alive:
            body["status_reason"] = (
                "a previously launched child is still alive after failed cleanup; "
                "the exact handle remains tracked and start is refused"
            )
        body["actions"] = {
            "start": (
                body["health"] in {"STOPPED", "CRASHED", "EXITED"}
                and not port_active
                and not retained_child_alive
            ),
            "stop": body["ownership"] == "OWNED_VERIFIED",
            "restart": body["ownership"] == "OWNED_VERIFIED",
        }
        body["preview_is_validation_evidence"] = False
        return body

    @_supervisor_locked
    def _reconcile(self, row: dict[str, Any], *, port_active: bool | None = None) -> dict[str, Any]:
        observed = self._public(row, port_active=port_active)
        if observed["health"] != "EXTERNAL":
            # External-port overlays must not erase the last owned identity.
            stored = dict(observed)
            stored.pop("actions", None)
            stored.pop("preview_is_validation_evidence", None)
            return self.ctx.state.upsert_runtime_service(stored) | {
                "actions": observed["actions"],
                "preview_is_validation_evidence": False,
            }
        return observed

    @_supervisor_locked
    def list_services(self, *, include_scopes: bool = True) -> list[dict[str, Any]]:
        project = self._project
        if project is None:
            return []
        port_observations: dict[int, bool] = {}

        def observed_port(row: dict[str, Any]) -> bool:
            port = int(row["port"])
            if port not in port_observations:
                port_observations[port] = self._port_active(port)
            return port_observations[port]

        persisted: dict[str, dict[str, Any]] = {}
        for row in self.ctx.state.list_runtime_services(project_id=project.project_id):
            persisted[row["id"]] = self._reconcile(row, port_active=observed_port(row))
        declared: list[dict[str, Any]] = []
        try:
            declared.append(self._declared_row())
        except RuntimeServiceError as exc:
            for row in persisted.values():
                row["actions"]["start"] = False
                row["actions"]["restart"] = False
                row["status_reason"] = f"{exc}; {row['status_reason']}"
            return list(persisted.values())
        if include_scopes:
            seen_worktrees: set[str] = set()
            for worktree in self.ctx.state.list_worktrees(project_id=project.project_id):
                path = str(Path(worktree.path).resolve())
                if path == str(project.local_repo_root.resolve()) or path in seen_worktrees:
                    continue
                seen_worktrees.add(path)
                try:
                    declared.append(self._declared_row(path, None))
                except RuntimeServiceError:
                    # One stale/deleted worktree must not hide every healthy
                    # service belonging to the selected project.
                    continue
            for runbook in self.ctx.state.list_runbooks(project_id=project.project_id):
                try:
                    declared.append(self._declared_row(runbook.worktree, runbook.id))
                except RuntimeServiceError:
                    # Historical runbooks can legitimately outlive their
                    # worktree; they simply have no eligible launch scope.
                    continue
        for row in declared:
            if row["id"] not in persisted:
                persisted[row["id"]] = self._public(row, port_active=observed_port(row))
        return sorted(persisted.values(), key=lambda row: (row.get("runbook_id") or "", row.get("cwd") or "", row["id"]))

    @_supervisor_locked
    def status(self) -> dict[str, Any]:
        """Legacy selected-project canonical app status."""

        try:
            declared = self._declared_row()
        except RuntimeServiceError as exc:
            declaration_available = False
            declaration_error = str(exc)
            project = self._project
            stored = None
            if project is not None:
                stored = self.ctx.state.get_runtime_service(
                    self._service_id(project.project_id, None, None),
                    project_id=project.project_id,
                )
            if stored is None:
                return {
                    "status": "STOPPED", "pid": None, "port": None, "uptime_seconds": None,
                    "started_at": None, "launch_source": "undeclared", "last_exit_code": None,
                    "note": str(exc), "actions": {"start": False, "stop": False, "restart": False},
                }
            row = stored
        else:
            declaration_available = True
            declaration_error = None
            row = self.ctx.state.get_runtime_service(
                declared["id"], project_id=declared["project_id"]
            ) or declared
        observed = self._reconcile(row) if row.get("pid") else self._public(row)
        if not declaration_available:
            observed["actions"]["start"] = False
            observed["actions"]["restart"] = False
            observed["status_reason"] = f"{declaration_error}; {observed['status_reason']}"
        uptime = None
        if observed.get("started_at") and observed["health"] in {"HEALTHY", "STARTING", "UNVERIFIED"}:
            try:
                uptime = max(
                    0,
                    int((dt.datetime.now(dt.UTC) - dt.datetime.fromisoformat(observed["started_at"])).total_seconds()),
                )
            except ValueError:
                pass
        mapped = "RUNNING" if observed["health"] in {"HEALTHY", "STARTING", "UNVERIFIED"} else "STOPPED"
        if observed["health"] == "EXTERNAL":
            mapped = "UNKNOWN"
        return {
            "status": mapped,
            "health": observed["health"],
            "ownership": observed["ownership"],
            "pid": observed.get("pid") if mapped == "RUNNING" else None,
            "port": observed["port"],
            "preview_url": observed["preview_url"],
            "preview_is_validation_evidence": False,
            "uptime_seconds": uptime,
            "started_at": observed.get("started_at"),
            "launch_source": " ".join(observed["argv"]),
            "last_exit_code": observed.get("exit_code"),
            "log_pointer": observed["log_pointer"],
            "note": observed["status_reason"],
            "actions": observed["actions"],
            "service_id": observed["id"],
        }

    # ----------------------------------------------------------------- actions

    def _child_environment(self) -> dict[str, str]:
        """Use the existing credential-free worker allowlist plus project Python."""

        from ..runner import worker_environment
        from .managed_environment import (
            ManagedEnvironmentError,
            isolated_environment,
            resolve_managed_environment,
        )

        project = self._project
        environment = None
        if project is not None:
            try:
                environment = resolve_managed_environment(project)
            except ManagedEnvironmentError:
                environment = None
        child = isolated_environment(worker_environment(), environment)
        child["HOST"] = "127.0.0.1"
        return child

    @_supervisor_locked
    def action(
        self,
        action: str,
        actor: str,
        *,
        worktree_path: str | None = None,
        runbook_id: str | None = None,
        service_id: str | None = None,
    ) -> dict[str, Any]:
        if action not in RUNTIME_ACTIONS:
            raise RuntimeServiceError("unsupported runtime service action")
        if action == "stop":
            project = self._project
            if project is None:
                raise RuntimeServiceMissingError("no managed project is selected")
            _cwd, worktree_path, runbook_id = self._scope(worktree_path, runbook_id)
            expected_id = self._service_id(project.project_id, worktree_path, runbook_id)
            if service_id is not None and service_id != expected_id:
                raise RuntimeServiceError("runtime service id does not match the validated project scope")
            stored = self.ctx.state.get_runtime_service(expected_id, project_id=project.project_id)
            if stored is None:
                raise RuntimeServiceMissingError(
                    "runtime service stop refused: no durable owned service exists for this scope"
                )
            return self._stop(stored, actor)
        declared = self._declared_row(worktree_path, runbook_id)
        if service_id is not None and service_id != declared["id"]:
            raise RuntimeServiceError("runtime service id does not match the validated project scope")
        stored = self.ctx.state.get_runtime_service(declared["id"], project_id=declared["project_id"])
        row = stored or declared
        observed = self._public(row)
        if action == "start":
            if stored is not None and observed["health"] not in {"STOPPED", "CRASHED", "EXITED"}:
                raise RuntimeServiceError(f"runtime service start refused: {observed['status_reason']}")
            return self._start(self._launch_row(declared, stored), actor)
        if observed["ownership"] == "OWNED_VERIFIED":
            self._stop(row, actor)
        elif observed["health"] not in {"STOPPED", "CRASHED", "EXITED"}:
            raise RuntimeServiceError(f"restart refused: {observed['status_reason']}")
        latest = self.ctx.state.get_runtime_service(declared["id"], project_id=declared["project_id"])
        return self._start(self._launch_row(declared, latest), actor)

    @staticmethod
    def _launch_row(declared: dict[str, Any], stored: dict[str, Any] | None) -> dict[str, Any]:
        """Use current allowlisted launch facts while retaining prior evidence metadata."""

        candidate = dict(declared)
        if stored is not None:
            for field in ("created_at", "log_pointer", "stopped_at", "exit_code"):
                candidate[field] = stored.get(field)
        return candidate

    @_supervisor_locked
    def _start(self, row: dict[str, Any], actor: str) -> dict[str, Any]:
        retained_process = self._processes.get(row["id"])
        if retained_process is not None:
            if retained_process.poll() is None:
                raise RuntimeServiceError(
                    "runtime service start refused: a previously launched child is still alive; "
                    "the exact handle remains tracked and no signal was sent"
                )
            # poll() proved this exact child exited. wait() now reaps it before
            # its ownership handle can be replaced by a subsequent launch.
            retained_process.wait()
            if self._processes.get(row["id"]) is retained_process:
                self._processes.pop(row["id"])
        observed = self._public(row)
        if not observed["actions"]["start"]:
            raise RuntimeServiceError(f"runtime service start refused: {observed['status_reason']}")
        if self._port_active(int(row["port"])):
            raise RuntimeServiceError(
                f"port {row['port']} is occupied by an external or unverified listener; refusing to start or signal it"
            )
        try:
            import psutil  # noqa: F401 - ownership evidence is mandatory before launch
        except ImportError as exc:
            raise RuntimeServiceError("psutil is required to capture complete runtime-service ownership") from exc
        log_path = self._log_path(row["log_pointer"])
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as stream:
            process = subprocess.Popen(
                list(row["argv"]),
                cwd=Path(row["cwd"]),
                stdout=stream,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
                env=self._child_environment(),
            )
        self._processes[row["id"]] = process
        try:
            facts = self._capture_process_identity(process, row)
        except RuntimeServiceError as exc:
            self._cleanup_failed_launch(process, row, str(exc))
            raise RuntimeServiceError(
                f"{exc}; verified launched process group terminated and the exact child was reaped"
            ) from None
        now = utc_now_iso()
        updated = dict(row)
        updated.update(
            pid=process.pid,
            process_create_time=facts["create_time"],
            process_session_id=facts["session_id"],
            process_argv=list(facts["argv"]),
            health="STARTING",
            ownership="OWNED_VERIFIED",
            owner=actor,
            status_reason="owned process launched; waiting for loopback readiness",
            started_at=now,
            stopped_at=None,
            exit_code=None,
            last_health_at=now,
        )
        stored = self.ctx.state.upsert_runtime_service(updated)
        self.ctx.state.record_event(
            category="runtime_service",
            message=f"runtime service started; service={row['id']}; actor={actor}; pid={process.pid}",
            project_id=row["project_id"],
        )
        return self._reconcile(stored)

    @_supervisor_locked
    def _stop(self, row: dict[str, Any], actor: str) -> dict[str, Any]:
        verified, reason = self._ownership_proof(row)
        if not verified:
            raise RuntimeServiceError(f"runtime service stop refused: {reason}")
        pid = int(row["pid"])
        session_id = int(row["process_session_id"])
        try:
            os.killpg(session_id, signal.SIGTERM)
        except OSError as exc:
            detail = exc.strerror or str(exc) or type(exc).__name__
            raise RuntimeServiceError(
                "runtime service stop refused: SIGTERM could not be sent to the "
                f"verified owned process group: {detail}"
            ) from exc
        process = self._processes.get(row["id"])
        if process is not None and process.pid != pid:
            process = None
        deadline = time.monotonic() + _TERMINATION_TIMEOUT_SECONDS
        exit_code = row.get("exit_code")
        if process is not None:
            try:
                # This exact child belongs to this manager, so wait both
                # confirms termination and reaps it before STOPPED is exposed.
                exit_code = process.wait(timeout=max(0.0, deadline - time.monotonic()))
                self._processes.pop(row["id"], None)
            except subprocess.TimeoutExpired as exc:
                raise RuntimeServiceError(
                    "owned process did not stop after SIGTERM; refusing to force-kill it"
                ) from exc
        else:
            while time.monotonic() < deadline:
                facts = self._process_facts(pid)
                if facts is None:
                    # An unavailable inspection is not evidence that a
                    # durable owner exited while its PID still exists.
                    if not self._pid_alive(pid):
                        break
                elif not facts["alive"]:
                    break
                time.sleep(0.1)
            else:
                raise RuntimeServiceError(
                    "owned process did not stop after SIGTERM; refusing to force-kill it"
                )
        now = utc_now_iso()
        updated = dict(row)
        updated.update(
            health="STOPPED",
            ownership="OWNED_EXITED",
            owner=actor,
            status_reason="owned process stopped after complete identity verification",
            stopped_at=now,
            exit_code=exit_code,
            last_health_at=now,
        )
        stored = self.ctx.state.upsert_runtime_service(updated)
        self.ctx.state.record_event(
            category="runtime_service",
            message=f"runtime service stopped; service={row['id']}; actor={actor}; pid={pid}; exit={exit_code}",
            project_id=row["project_id"],
        )
        return self._public(stored)

    @_supervisor_locked
    def shutdown_owned(self) -> bool:
        stopped = False
        # Project selection is a view concern. This manager can start service A,
        # switch to project B, then start service B; process shutdown must not
        # orphan A merely because B is selected at that instant.
        for row in self.ctx.state.list_runtime_services():
            verified, _reason = self._ownership_proof(row)
            if not verified:
                continue
            try:
                self._stop(row, "control-center-shutdown")
            except RuntimeServiceError:
                # Failure to stop one service never authorizes force-kill and
                # must not prevent independently verified scopes from being
                # shut down safely.
                continue
            else:
                stopped = True
        return stopped
