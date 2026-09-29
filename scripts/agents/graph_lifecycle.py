"""First-class, local-only Graphify lifecycle (ENG-AO-10 / issue #41).

Generation remains implemented by :mod:`graph_context`; this module adds
capability probing, explicit refresh coordination, cached health, and refresh
evidence.  Dashboard reads call :func:`cached_status` only and can never start
Graphify.
"""

from __future__ import annotations

import atexit
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import graph_context

SUPPORTED_VERSION = "0.9.71"
UPSTREAM_REPOSITORY = "https://github.com/Graphify-Labs/graphify"
UPSTREAM_RELEASE_COMMIT = "d6eaa8aae8df155874ebb1044302c055c286342a"
UPSTREAM_REVIEWED_HEAD = "9fd5aadfd8ff7c2de95c78ef90f9b9f2721cbd98"
INSTALL_COMMANDS = ("uv tool install graphifyy", "pipx install graphifyy")
STATUS_FILENAME = "status.json"
PROBE_TIMEOUT_SECONDS = 5.0

TRIGGERS = frozenset(
    {
        "manual",
        "project-selected",
        "implementation-checkpoint",
        "pre-review",
        "post-merge",
        "overnight-next-task",
        "context-on-demand",
    }
)

EventRecorder = Callable[[Mapping[str, Any]], None]


def _iso(timestamp: float | None = None) -> str:
    import datetime as dt

    return dt.datetime.fromtimestamp(timestamp or time.time(), tz=dt.UTC).isoformat().replace("+00:00", "Z")


def state_event_recorder(state: Any, project_id: str | None) -> EventRecorder:
    """Adapt refresh evidence to the existing Control Plane event ledger."""

    def record(evidence: Mapping[str, Any]) -> None:
        status = str(evidence.get("status") or "FAILED_SAFE")
        state.record_event(
            category="graphify",
            level="error" if status == "FAILED_SAFE" else "info",
            project_id=project_id,
            message=(
                f"Graphify refresh {status}; trigger={evidence.get('trigger')}; "
                f"worktree={evidence.get('worktree_id')}; tree={evidence.get('tree_id')}; "
                f"duration={evidence.get('duration_seconds', 0)}s; reason={evidence.get('reason')}"
            ),
        )

    return record


def _binary() -> str | None:
    configured = os.environ.get(graph_context.ENV_BIN)
    if configured:
        return configured if Path(configured).is_file() or shutil.which(configured) else None
    return shutil.which("graphify")


def _run_probe(binary: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [binary, *args],
        capture_output=True,
        text=True,
        timeout=PROBE_TIMEOUT_SECONDS,
        check=False,
        stdin=subprocess.DEVNULL,
        env=graph_context._scrubbed_env(os.environ),
    )


def capability_status(*, probe: bool = True) -> dict[str, Any]:
    """Return truthful CLI capability without installing or mutating a repository."""

    checked = _iso() if probe else None
    binary = _binary()
    base: dict[str, Any] = {
        "installed": bool(binary),
        "path": binary,
        "version": None,
        "supported_version": SUPPORTED_VERSION,
        "supported_command": "graphify extract <filtered-snapshot> --code-only",
        "ast_only": True,
        "llm_enrichment": False,
        "api_billing": False,
        "last_health_probe": checked,
        "install_commands": list(INSTALL_COMMANDS),
        "upstream_repository": UPSTREAM_REPOSITORY,
        "upstream_release_commit": UPSTREAM_RELEASE_COMMIT,
        "upstream_reviewed_head": UPSTREAM_REVIEWED_HEAD,
    }
    if not binary:
        return {**base, "status": "MISSING", "reason": f"Graphify is not installed; run `{INSTALL_COMMANDS[0]}` (or `{INSTALL_COMMANDS[1]}`)"}
    if not probe:
        return {**base, "status": "UNKNOWN", "reason": "installation has not been probed"}
    try:
        version_run = _run_probe(binary, "--version")
        version_text = (version_run.stdout + "\n" + version_run.stderr).strip()
        match = re.search(r"(?<!\d)(\d+\.\d+\.\d+)(?!\d)", version_text)
        version = match.group(1) if match else None
        help_run = _run_probe(binary, "--help")
        help_text = help_run.stdout + "\n" + help_run.stderr
    except (OSError, subprocess.SubprocessError) as exc:
        return {**base, "status": "FAILED_SAFE", "reason": f"Graphify health probe failed safely: {type(exc).__name__}"}
    base["version"] = version
    if version_run.returncode != 0 or not version:
        return {**base, "status": "OUTDATED", "reason": "Graphify version could not be verified; no refresh was attempted"}
    if version != SUPPORTED_VERSION:
        return {**base, "status": "OUTDATED", "reason": f"Graphify {version} is incompatible with pinned {SUPPORTED_VERSION}; no refresh was attempted"}
    if help_run.returncode != 0 or "--code-only" not in help_text:
        return {**base, "status": "OUTDATED", "reason": "installed Graphify does not advertise extract --code-only; no refresh was attempted"}
    return {**base, "status": "READY", "reason": "pinned local code-only Graphify capability is ready"}


def _coordinates(root: Path, project_id: str | None) -> tuple[dict[str, str], dict[str, str], int, str, Path]:
    identity = graph_context._identity(root, project_id)
    files, excluded = graph_context._tracked_tree(root)
    tree_id = graph_context._sha(*(f"{path}:{digest}" for path, digest in sorted(files.items())))
    worktree_dir = graph_context.graph_state_root() / identity["project_key"] / identity["worktree_id"][:16]
    return identity, files, excluded, tree_id, worktree_dir


def _status_path(worktree_dir: Path) -> Path:
    return worktree_dir / STATUS_FILENAME


def _write_status(worktree_dir: Path, evidence: Mapping[str, Any]) -> None:
    worktree_dir.mkdir(parents=True, exist_ok=True)
    target = _status_path(worktree_dir)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=worktree_dir, prefix=".status-", suffix=".tmp", delete=False
        ) as handle:
            handle.write(json.dumps(dict(evidence), indent=2, sort_keys=True) + "\n")
            temporary = Path(handle.name)
        temporary.replace(target)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _read_status(worktree_dir: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(_status_path(worktree_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def record_context_cache(worktree_dir: Path, cache_dir: Path) -> None:
    """Project a validated on-demand context build into lifecycle current health."""

    meta = json.loads((cache_dir / "meta.json").read_text(encoding="utf-8"))
    if not isinstance(meta, dict) or meta.get("status") != "READY":
        return
    tree_id = str(meta.get("tree_id") or "")[:24]
    worktree_id = str(meta.get("worktree_id") or "")[:16]
    evidence = {
        **meta,
        "tree_id": tree_id,
        "worktree_id": worktree_id,
        "cache_key": f"{meta.get('project_key')}/{worktree_id}/{tree_id}",
        "reason": "tree-matched code-only graph is current",
        "authoritative": False,
    }
    _write_status(worktree_dir, evidence)


def _branch(root: Path) -> str | None:
    try:
        return graph_context._git(root, "branch", "--show-current").decode().strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def cached_status(root: Path, *, project_id: str | None = None, verify_tree: bool = True) -> dict[str, Any]:
    """Read cached health for one checkout. Never invokes Graphify."""

    root = Path(root).resolve()
    capability = capability_status(probe=False)
    if capability["status"] == "MISSING":
        return {**capability, "project_id": project_id, "worktree": str(root), "branch": _branch(root)}
    if not verify_tree:
        try:
            identity = graph_context._identity(root, project_id)
            worktree_dir = graph_context.graph_state_root() / identity["project_key"] / identity["worktree_id"][:16]
        except (OSError, subprocess.SubprocessError):
            return {**capability, "status": "FAILED_SAFE", "reason": "selected checkout identity could not be inspected", "project_id": project_id, "worktree": str(root)}
        saved = _read_status(worktree_dir)
        common = {
            **capability,
            "project_id": project_id,
            "project_key": identity["project_key"],
            "worktree": str(root),
            "worktree_id": identity["worktree_id"][:16],
            "branch": _branch(root),
        }
        return {**common, **saved} if saved is not None else {
            **common, "status": "STALE", "reason": "no cached Graphify lifecycle refresh exists for this worktree"
        }
    try:
        identity, files, excluded, tree_id, worktree_dir = _coordinates(root, project_id)
    except (OSError, subprocess.SubprocessError, OverflowError):
        return {**capability, "status": "FAILED_SAFE", "reason": "selected checkout could not be inspected", "project_id": project_id, "worktree": str(root)}
    saved = _read_status(worktree_dir)
    common = {
        **capability,
        "project_id": project_id,
        "project_key": identity["project_key"],
        "worktree": str(root),
        "worktree_id": identity["worktree_id"][:16],
        "branch": _branch(root),
        "current_tree_id": tree_id[:24],
        "files_current": len(files),
        "files_excluded": excluded,
    }
    if saved is None:
        return {**common, "status": "STALE", "reason": "no cached Graphify lifecycle refresh exists for this worktree"}
    status = {**common, **saved}
    status.update(
        project_id=project_id,
        project_key=identity["project_key"],
        worktree=str(root),
        worktree_id=identity["worktree_id"][:16],
        branch=_branch(root),
        current_tree_id=tree_id[:24],
        files_current=len(files),
        files_excluded=excluded,
    )
    indexed = saved.get("tree_id")
    if saved.get("status") == "REFRESHING":
        return status
    if saved.get("status") == "FAILED_SAFE":
        return status
    if indexed != tree_id[:24]:
        status.update(status="STALE", reason="indexed tree does not match the selected checkout")
    elif saved.get("status") == "READY":
        finished = saved.get("finished_epoch")
        status["graph_age_seconds"] = max(0.0, time.time() - float(finished)) if finished else None
    return status


def _refresh_graph(
    root: Path,
    *,
    project_id: str | None = None,
    trigger: str,
    event_recorder: EventRecorder | None = None,
) -> dict[str, Any]:
    """Implementation for :func:`refresh_graph`; callers use the contained wrapper."""

    started = time.time()
    root = Path(root).resolve()
    if trigger not in TRIGGERS:
        return {"status": "FAILED_SAFE", "reason": f"unsupported Graphify refresh trigger: {trigger}", "trigger": trigger}
    try:
        identity, files, excluded, tree_id, worktree_dir = _coordinates(root, project_id)
    except Exception as exc:  # noqa: BLE001 - advisory refresh must not break orchestration
        return {"status": "FAILED_SAFE", "reason": f"checkout inspection failed safely: {type(exc).__name__}", "trigger": trigger}
    short_tree = tree_id[:24]
    base = {
        "project_id": project_id,
        "project_key": identity["project_key"],
        "worktree": str(root),
        "worktree_id": identity["worktree_id"][:16],
        "branch": _branch(root),
        "tree_id": short_tree,
        "cache_key": f"{identity['project_key']}/{identity['worktree_id'][:16]}/{short_tree}",
        "trigger": trigger,
        "started_at": _iso(started),
        "started_epoch": started,
        "files": len(files),
        "files_excluded": excluded,
        "api_llm_disabled": True,
        "authoritative": False,
    }
    capability = capability_status(probe=True)
    binary = capability.get("path")
    base.update(
        last_health_probe=capability.get("last_health_probe"),
        installed_path=binary,
        supported_command=capability.get("supported_command"),
    )
    if capability["status"] != "READY" or not binary:
        finished = time.time()
        evidence = {
            **base,
            "status": "FAILED_SAFE",
            "reason": capability["reason"],
            "graphify_version": capability.get("version"),
            "finished_at": _iso(finished),
            "finished_epoch": finished,
            "duration_seconds": round(finished - started, 6),
        }
        # MISSING is derivable without leaving per-fixture cache debris. An
        # installed-but-incompatible/broken CLI is durable health evidence.
        if binary:
            _write_status(worktree_dir, evidence)
        if event_recorder is not None:
            try:
                event_recorder(evidence)
            except Exception:  # noqa: BLE001, S110 - evidence recording cannot break orchestration
                pass
        return evidence
    _write_status(worktree_dir, {**base, "status": "REFRESHING", "reason": "bounded local refresh in progress"})
    cache_dir = worktree_dir / short_tree
    expected = {"schema": graph_context.CACHE_SCHEMA, **identity, "tree_id": tree_id, "llm_enrichment": False}
    failure = ""
    graph, _why = graph_context._load_valid_cache(cache_dir, expected)
    if graph is None:
        failure = graph_context._build_graph(
            str(binary), root, files, cache_dir,
            {**expected, "files": len(files), "files_excluded": excluded},
            trigger=trigger, graphify_version=capability.get("version"),
            incremental_from=graph_context._previous_cache(worktree_dir, cache_dir),
        )
        graph, validate_reason = graph_context._load_valid_cache(cache_dir, expected)
        if not failure and graph is None:
            failure = f"refreshed graph did not validate: {validate_reason}"
    finished = time.time()
    if failure:
        evidence = {
            **base,
            "status": "FAILED_SAFE",
            "reason": failure,
            "graphify_version": capability.get("version"),
            "finished_at": _iso(finished),
            "finished_epoch": finished,
            "duration_seconds": round(finished - started, 6),
        }
    else:
        summary = graph_context._summarize(graph or {}, [], set(files))
        evidence = {
            **base,
            "status": "READY",
            "reason": "tree-matched code-only graph is current",
            "graphify_version": capability.get("version"),
            "nodes": summary["graph_nodes"],
            "edges": summary["graph_edges"],
            "finished_at": _iso(finished),
            "finished_epoch": finished,
            "duration_seconds": round(finished - started, 6),
        }
        graph_context._prune(worktree_dir, cache_dir)
    _write_status(worktree_dir, evidence)
    if event_recorder is not None:
        try:
            event_recorder(evidence)
        except Exception:  # noqa: BLE001, S110 - evidence recording cannot break an advisory refresh
            pass
    return evidence


def refresh_graph(
    root: Path,
    *,
    project_id: str | None = None,
    trigger: str,
    event_recorder: EventRecorder | None = None,
) -> dict[str, Any]:
    """Refresh one exact checkout/tree into isolated Octarel state; never raises."""

    try:
        return _refresh_graph(root, project_id=project_id, trigger=trigger, event_recorder=event_recorder)
    except Exception as exc:  # noqa: BLE001 - advisory lifecycle must never break orchestration
        evidence = {
            "status": "FAILED_SAFE",
            "reason": f"Graphify refresh failed safely: {type(exc).__name__}",
            "trigger": trigger,
            "project_id": project_id,
            "api_llm_disabled": True,
            "authoritative": False,
        }
        if event_recorder is not None:
            try:
                event_recorder(evidence)
            except Exception:  # noqa: BLE001, S110 - the contained result is still returned
                pass
        return evidence


@dataclass
class _Request:
    key: tuple[str, str, str]
    root: Path
    project_id: str | None
    trigger: str
    recorder: EventRecorder | None
    future: Future[dict[str, Any]]


class RefreshCoordinator:
    """One bounded refresh worker with duplicate coalescing and supersession."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._queued: dict[tuple[str, str], _Request] = {}
        self._active: _Request | None = None
        self._stopping = False
        self._thread = threading.Thread(target=self._run, name="octarel-graphify-refresh", daemon=True)
        self._thread.start()

    def request(
        self, root: Path, *, project_id: str | None, trigger: str, event_recorder: EventRecorder | None = None
    ) -> Future[dict[str, Any]]:
        root = Path(root).resolve()
        try:
            identity, _files, _excluded, tree_id, worktree_dir = _coordinates(root, project_id)
            key = (identity["project_key"], identity["worktree_id"][:16], tree_id[:24])
        except Exception as exc:  # noqa: BLE001
            future: Future[dict[str, Any]] = Future()
            future.set_result({"status": "FAILED_SAFE", "reason": f"checkout inspection failed safely: {type(exc).__name__}", "trigger": trigger})
            return future
        if trigger != "manual":
            cached = _read_status(worktree_dir)
            if cached is not None and cached.get("status") == "READY" and cached.get("tree_id") == key[2]:
                future = Future()
                future.set_result(dict(cached))
                return future
        lane = key[:2]
        with self._condition:
            if self._stopping:
                future = Future()
                future.set_result({"status": "FAILED_SAFE", "reason": "Graphify refresh coordinator is shutting down", "trigger": trigger})
                return future
            if self._active is not None and self._active.key == key:
                return self._active.future
            queued = self._queued.get(lane)
            if queued is not None and queued.key == key:
                return queued.future
            if queued is not None and not queued.future.done():
                queued.future.set_result({"status": "STALE", "reason": "superseded by a newer tree refresh", "trigger": queued.trigger, "tree_id": queued.key[2]})
            future = Future()
            self._queued[lane] = _Request(key, root, project_id, trigger, event_recorder, future)
            self._condition.notify()
            return future

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._queued and not self._stopping:
                    self._condition.wait()
                if self._stopping and not self._queued:
                    return
                lane = next(iter(self._queued))
                request = self._queued.pop(lane)
                self._active = request
            try:
                result = refresh_graph(
                    request.root, project_id=request.project_id, trigger=request.trigger, event_recorder=request.recorder
                )
            except Exception as exc:  # noqa: BLE001 - defense in depth for injected/test implementations
                result = {
                    "status": "FAILED_SAFE",
                    "reason": f"Graphify coordinator contained {type(exc).__name__}",
                    "trigger": request.trigger,
                }
            if not request.future.done():
                request.future.set_result(result)
            with self._condition:
                self._active = None
                self._condition.notify_all()

    def shutdown(self, *, wait: bool = True) -> None:
        with self._condition:
            self._stopping = True
            for request in self._queued.values():
                if not request.future.done():
                    request.future.set_result({"status": "FAILED_SAFE", "reason": "Graphify refresh cancelled during shutdown", "trigger": request.trigger})
            self._queued.clear()
            self._condition.notify_all()
        if wait:
            self._thread.join(timeout=graph_context.BUILD_TIMEOUT_SECONDS + 5)


_coordinator: RefreshCoordinator | None = None
_coordinator_lock = threading.Lock()


def coordinator() -> RefreshCoordinator:
    global _coordinator
    with _coordinator_lock:
        if _coordinator is None:
            _coordinator = RefreshCoordinator()
        return _coordinator


def request_refresh(
    root: Path,
    *,
    project_id: str | None,
    trigger: str,
    event_recorder: EventRecorder | None = None,
    wait: bool = False,
) -> Future[dict[str, Any]] | dict[str, Any]:
    future = coordinator().request(root, project_id=project_id, trigger=trigger, event_recorder=event_recorder)
    return future.result(timeout=graph_context.BUILD_TIMEOUT_SECONDS + 15) if wait else future


def _shutdown_global() -> None:
    if _coordinator is not None:
        _coordinator.shutdown(wait=False)


atexit.register(_shutdown_global)
