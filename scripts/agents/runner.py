"""Git-worktree safety checks and subprocess execution for delegated workers.

Write-capable workers must never share a checkout. This module refuses to launch
one on ``main``/``master`` or while another write worker holds the per-checkout
lock, and it captures a write lock for the duration of the run.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterable, Iterator

from .registry import Worker

PROTECTED_BRANCHES = frozenset({"main", "master"})
_LOCK_NAME = ".write-lock"


class WriteSafetyError(RuntimeError):
    """Raised when a write-capable worker would be unsafe to launch here."""


def repo_root(start: Path | None = None) -> Path:
    start = start or Path.cwd()
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=start,
        capture_output=True,
        text=True,
        check=True,
    )
    return Path(result.stdout.strip())


# ENG-AGENT-12 (issue #136): the orchestrator daemon's own code checkout is
# allowed to live at a separate, independently-versioned development
# location (e.g. a dedicated ``DevCP`` checkout used to iterate on Control
# Plane code without disturbing a product worktree). But every
# scheduling-relevant read this process performs -- the Quick Start ledger,
# roadmap/status documents, git status, worktree discovery, and task/branch
# resolution -- must resolve against the maintainer's configured canonical
# OctaScene repository checkout, never silently fall back to whatever commit
# the running dashboard process's own ``cwd`` happens to be checked out to.
# Before this existed, the daemon always used ``repo_root()`` (cwd-derived),
# so a daemon launched with its working directory pointed at a stale,
# detached-HEAD secondary checkout served stale ledger/roadmap/task truth
# with no way to tell it apart from the intended canonical checkout.
ENV_CANONICAL_REPO_ROOT = "OCTAGES_ORCH_CANONICAL_REPO_ROOT"


def canonical_repo_root(start: Path | None = None, *, override: str | Path | None = None) -> Path:
    """Resolve the canonical OctaScene repository truth root.

    Precedence: an explicit ``override`` (e.g. a ``--canonical-repo-root`` CLI
    flag) wins first, then the ``OCTAGES_ORCH_CANONICAL_REPO_ROOT`` environment
    variable, then the ordinary cwd-derived :func:`repo_root`. Both the
    override and the environment variable are resolved through
    ``git rev-parse --show-toplevel`` run *inside* the named directory, so a
    misconfigured non-repository path fails loudly instead of silently
    degrading to the CP code checkout.
    """

    configured = override if override is not None else os.environ.get(ENV_CANONICAL_REPO_ROOT)
    if configured:
        return repo_root(Path(configured))
    return repo_root(start)


# ENG-CP-02 (issue #165): variables meaningful only to *this* Control Plane
# process's own scheduling truth -- never to a subprocess this process spawns
# to act on a selected project/target worktree. Today that is exactly
# ``ENV_CANONICAL_REPO_ROOT`` (ENG-AGENT-12/issue #136 already fixed this
# leaking into the exact-tree local gate's own phase subprocesses,
# ``scripts/ci/local_gate.py``'s ``_ORCHESTRATION_ONLY_ENV_VARS``); this is
# the same variable name, exposed here as the one shared, reusable home so a
# second orchestration-only variable never needs a second ad hoc exclusion
# list wherever a target-project subprocess is spawned.
ORCHESTRATION_ONLY_ENV_VARS: tuple[str, ...] = (ENV_CANONICAL_REPO_ROOT,)


def sanitized_subprocess_env(
    base_env: dict[str, str] | None = None, *, extra_exclude: Iterable[str] = ()
) -> dict[str, str]:
    """A copy of ``base_env`` (default: this process's own environment) with
    Control-Plane-only orchestration variables removed.

    Use this whenever spawning a subprocess that will act on a selected
    project/target worktree rather than on this Control Plane process's own
    canonical scheduling truth -- e.g. the daemon's ``Supervisor`` launching
    ``orchestrate.py`` in a task's worktree. A variable like
    ``OCTAGES_ORCH_CANONICAL_REPO_ROOT`` is correct for *this* process's own
    ``canonical_repo_root()`` resolution and would be wrong to hand to a
    subprocess whose job is to operate on a different, explicitly-named
    worktree -- inheriting it unfiltered risks exactly the class of
    contamination ENG-AGENT-12/issue #161 already fixed once for the local
    gate's own phase subprocesses.
    """

    source = dict(os.environ if base_env is None else base_env)
    exclude = set(ORCHESTRATION_ONLY_ENV_VARS) | set(extra_exclude)
    return {key: value for key, value in source.items() if key not in exclude}


def current_branch(root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def worktree_snapshot(root: Path) -> dict[str, str]:
    """Fingerprint tracked and non-ignored untracked files.

    Unlike a set of porcelain paths, this detects a worker changing a file that
    was already dirty before the run. Ignored ``.agent-output`` artifacts and
    dependency/media trees are excluded by Git.
    """

    result = subprocess.run(
        ["git", "ls-files", "-co", "--exclude-standard", "-z"],
        cwd=root,
        capture_output=True,
        check=True,
    )
    snapshot: dict[str, str] = {}
    for raw in result.stdout.split(b"\0"):
        if not raw:
            continue
        relative = os.fsdecode(raw)
        path = root / relative
        if path.is_symlink():
            payload = os.readlink(path).encode("utf-8", errors="surrogateescape")
        elif path.is_file():
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            snapshot[relative] = digest.hexdigest()
            continue
        else:
            payload = b"<missing>"
        snapshot[relative] = hashlib.sha256(payload).hexdigest()
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
    ).stdout.strip()
    staged = subprocess.run(["git", "diff", "--cached", "--binary"], cwd=root, capture_output=True, check=True).stdout
    snapshot["@git/HEAD"] = head
    snapshot["@git/index"] = hashlib.sha256(staged).hexdigest()
    return snapshot


def _active_lock_holder(lock_path: Path) -> str | None:
    """Return a live/unverifiable holder, removing a verifiably stale lock."""

    if not lock_path.exists():
        return None
    holder = lock_path.read_text(encoding="utf-8").strip()
    match = re.search(r"\bpid=(\d+)\b", holder)
    if not match:
        return holder or "unknown holder"
    try:
        os.kill(int(match.group(1)), 0)
    except ProcessLookupError:
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass
        return None
    except PermissionError:
        return holder
    return holder


def assert_write_safety(worker: Worker, root: Path, *, allow_write: bool, lock_dir: Path) -> None:
    """Raise :class:`WriteSafetyError` if launching ``worker`` here is unsafe."""

    if not worker.is_write_capable:
        return

    reasons: list[str] = []
    if not allow_write:
        reasons.append(
            f"{worker.name} is write-capable; pass --allow-write and confirm this is its own dedicated worktree"
        )
    branch = current_branch(root)
    if branch == "HEAD":
        reasons.append("refusing to run a write-capable worker from detached HEAD")
    elif branch in PROTECTED_BRANCHES:
        reasons.append(f"refusing to run a write-capable worker on protected branch {branch!r}")

    lock_path = lock_dir / _LOCK_NAME
    holder = _active_lock_holder(lock_path)
    if holder:
        reasons.append(
            f"another write-capable worker holds the checkout lock ({holder}); "
            "concurrent write workers must use separate worktrees"
        )
    if reasons:
        raise WriteSafetyError("; ".join(reasons))


@contextmanager
def write_lock(worker: Worker, lock_dir: Path) -> Iterator[None]:
    """Hold a per-checkout write lock for the duration of a write-worker run."""

    if not worker.is_write_capable:
        yield
        return
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / _LOCK_NAME
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        holder = _active_lock_holder(lock_path)
        if holder:
            raise WriteSafetyError(f"another write-capable worker holds the checkout lock ({holder})") from None
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            holder = _active_lock_holder(lock_path) or "new holder"
            raise WriteSafetyError(f"another write-capable worker holds the checkout lock ({holder})") from None
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        # ENG-PC-07: ``at`` is the lock acquisition boundary used to detect a
        # recycled live PID. ``created`` is the stronger identity stamp when
        # the platform exposes it; recovery truthfully records which proof it
        # could use and never falls back to executable-name matching.
        from .control_plane.recovery import process_create_time

        created = process_create_time(os.getpid())
        created_field = f" created={created:.6f}" if created is not None else ""
        handle.write(f"{worker.name} pid={os.getpid()} at={int(time.time())}{created_field}")
    try:
        yield
    finally:
        try:
            lock_path.unlink()
        except FileNotFoundError:  # pragma: no cover - best effort cleanup
            pass


_ALLOWED_ENV_NAMES = {"HOME", "PATH", "SHELL", "TMPDIR", "USER", "LOGNAME", "LANG", "TERM", "COLORTERM", "NO_COLOR"}


def _worker_environment() -> dict[str, str]:
    """Minimal environment allowlist so unrelated credentials cannot cross the worker boundary."""

    return {
        key: value
        for key, value in os.environ.items()
        if key in _ALLOWED_ENV_NAMES or key.startswith("LC_") or key.startswith("XDG_")
    }


worker_environment = _worker_environment


def _environment_for_command(command: list[str], env: dict[str, str] | None = None) -> dict[str, str]:
    """Return the minimized worker environment plus command-specific safety compatibility.

    Grok's built-in ``strict`` sandbox intentionally cannot read the user's global
    Git configuration. Git treats an unreadable ``~/.gitconfig`` as fatal even for
    read-only commands such as ``git status``. Pointing Git at the OS null device
    preserves the strict filesystem boundary while leaving repository-local and
    system Git configuration available. It does not grant a new path or credential.
    """

    result = dict(_worker_environment() if env is None else env)
    if command and Path(command[0]).name == "grok" and any(
        left == "--sandbox" and right == "strict" for left, right in zip(command, command[1:])
    ):
        result["GIT_CONFIG_GLOBAL"] = os.devnull
    return result


def run_worker_process(
    command: list[str], root: Path, *, timeout: float | None, env: dict[str, str] | None = None
) -> tuple[int, str]:
    """Run ``command`` from ``root`` and return ``(exit_code, combined_output)``.

    Never raises for a non-zero exit; a timeout returns exit code ``124`` and the
    partial output captured so far. The subprocess inherits a minimal environment
    allowlist so unrelated credentials cannot cross the worker boundary. A caller
    that must control the interpreter environment (ENG-AO-09) passes an explicit
    ``env`` derived from :func:`worker_environment`.
    """

    try:
        completed = subprocess.run(
            command,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_environment_for_command(command, env),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        partial = (exc.stdout or "") + (exc.stderr or "")
        return 124, partial + f"\n[timed out after {timeout}s]\n"
    return completed.returncode, (completed.stdout or "") + (completed.stderr or "")


def run_worker_process_group(
    command: list[str], root: Path, *, timeout: float | None, on_group: Callable[[int | None], None] | None = None
) -> tuple[int, str]:
    """Like :func:`run_worker_process`, but the worker leads its own process group that is reaped on timeout.

    ENG-AO-02 read-only bots use this so a bot that spawned children can neither outlive its bound nor keep
    the output pipes open: on timeout the whole group is killed. ``on_group`` receives the group id when it
    starts and ``None`` when the process has finished, so a caller can reap stragglers.
    """

    process = subprocess.Popen(
        command,
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_worker_environment(),
        start_new_session=True,
    )
    if on_group is not None:
        on_group(process.pid)
    try:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_process_group(process.pid)
            stdout, stderr = process.communicate()
            return 124, (stdout or "") + (stderr or "") + f"\n[timed out after {timeout}s]\n"
        return process.returncode, (stdout or "") + (stderr or "")
    finally:
        # Any leftover member of the group (a child that outlived a clean exit) is reaped too.
        kill_process_group(process.pid)
        if on_group is not None:
            on_group(None)


def kill_process_group(pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


BOUNDARY_EVIDENCE_MEASURED = "MEASURED"
BOUNDARY_EVIDENCE_UNKNOWN = "UNKNOWN"


def _structured_denial(payload: dict[str, object] | None) -> bool | None:
    """Return a typed denial value, or ``None`` when none is exposed.

    Claude Code reports ``permission_denials`` at the result root. Antigravity
    reports ``denied_actions`` inside ``usage`` (and older/newer adapters may
    expose either field at the result root), so only those known structured
    locations are inspected. Model-authored prose is deliberately not treated
    as evidence that a tool action occurred. Only list-valued fields match the
    known transport schemas; a missing or malformed field cannot prove either
    a clean boundary or a violation.
    """

    if payload is None:
        return None
    containers = [payload]
    usage = payload.get("usage")
    if isinstance(usage, dict):
        containers.append(usage)
    values = [
        container[field]
        for container in containers
        for field in ("permission_denials", "denied_actions")
        if field in container and isinstance(container[field], list)
    ]
    if not values:
        return None
    return any(bool(value) for value in values)


def _structured_values(output: str) -> list[object]:
    """Decode line-oriented CLI JSON values.

    Headless CLIs may emit more than one JSON value, with diagnostic prose
    before, between, or after them.  Only values beginning a logical output
    line (or immediately following another decoded value) are treated as CLI
    records, so a JSON example embedded in model-authored prose is not
    mistaken for transport metadata.
    """

    decoder = json.JSONDecoder()
    values: list[object] = []
    position = 0
    while position < len(output):
        line_end = output.find("\n", position)
        if line_end == -1:
            line_end = len(output)
        candidate = position
        while candidate < line_end and output[candidate] in " \t\r":
            candidate += 1
        try:
            value, end = decoder.raw_decode(output, candidate)
        except (json.JSONDecodeError, TypeError):
            position = line_end + 1
            continue
        values.append(value)
        position = end
    return values


_STRUCTURED_RESULT_KEYS = frozenset(
    {"is_error", "isError", "result", "response", "text", "stopReason", "subtype"}
)


def first_structured_result(output: str) -> dict[str, object] | None:
    """Select one terminal CLI result from line-anchored structured values.

    The last dictionary carrying a recognized terminal-result field wins,
    because transports may emit progress records before their final outcome.
    If no dictionary carries such a field, the last decoded dictionary is the
    best available structured record (for example, a usage-only result). JSON
    embedded later on a prose line is never decoded by :func:`_structured_values`.

    This is the single result-record selector used by failure, actual-model,
    and adapter usage readers. Boundary-denial evidence deliberately differs:
    it scans every decoded record because a denial may live in a separate
    usage event.
    """

    records = [value for value in _structured_values(output) if isinstance(value, dict)]
    terminal = [record for record in records if _STRUCTURED_RESULT_KEYS.intersection(record)]
    if terminal:
        return terminal[-1]
    return records[-1] if records else None


def worker_boundary_evidence(output: str) -> dict[str, object]:
    """Classify read-only boundary evidence without inferring from prose.

    A transport proves the boundary only by emitting a recognized, well-typed
    field.  Text is untrusted reviewer content: it may quote source, a diff, or
    a diagnostic banner, so prose-only output is explicitly ``UNKNOWN``.
    """

    values = _structured_values(output)
    typed = [_structured_denial(value) for value in values if isinstance(value, dict)]
    measured = [value for value in typed if value is not None]
    if any(measured):
        return {
            "class": BOUNDARY_EVIDENCE_MEASURED,
            "worker_action_denied": True,
            "reason": "transport emitted a non-empty typed denial field",
        }
    if measured:
        return {
            "class": BOUNDARY_EVIDENCE_MEASURED,
            "worker_action_denied": False,
            "reason": "transport emitted an empty typed denial field",
        }
    reason = (
        "transport emitted no decoded JSON object with a recognized typed denial field"
        if values
        else "transport emitted no decoded JSON records and cannot prove the boundary state"
    )
    return {
        "class": BOUNDARY_EVIDENCE_UNKNOWN,
        "worker_action_denied": None,
        "reason": reason,
    }


def worker_action_denied(output: str) -> bool:
    """Return whether CLI evidence proves that a worker tool action was denied."""

    return worker_boundary_evidence(output)["worker_action_denied"] is True


def structured_failure(output: str) -> str | None:
    """Return a failure reason exposed by a structured CLI result, if any."""

    payload = first_structured_result(output)
    if worker_action_denied(output):
        return "worker tool action was denied by the configured read-only permission boundary"
    if payload is None:
        return None
    result = str(payload.get("result", "")).strip()
    if (payload.get("is_error") is True or payload.get("isError") is True) and result:
        from .redaction import redact_text

        return redact_text(result)[:800]
    if "response" in payload and not str(payload.get("response", "")).strip():
        return "structured worker result contained an empty response"
    stop_reason = str(payload.get("stopReason", "")).lower()
    if stop_reason in {"cancelled", "canceled", "error", "failed", "timeout"}:
        return f"structured worker result reported stopReason={stop_reason}"
    if payload.get("is_error") is True or payload.get("isError") is True:
        return "structured worker result reported an error"
    if payload.get("subtype") in {"error", "failed"}:
        return f"structured worker result reported subtype={payload['subtype']}"
    return None


def structured_actual_model_report(output: str, requested_model: str) -> tuple[str, bool]:
    """Prefer a structured CLI's reported model identifier when available.

    Returns ``(model_id, measured)``. ``measured`` is ``True`` only when ``model_id`` was
    genuinely read from the CLI's own ``modelUsage`` block -- never for one of the three
    fallback paths (output not JSON, no ``modelUsage`` block, or an ambiguous set of reported
    names) that fall back to ``requested_model`` instead. Callers that need to tell a real
    provider report apart from a fallback (e.g. ``adapter_contract.run_result_from_record``'s
    MEASURED/DERIVED distinction) must use this return value rather than re-deriving it from
    ``model_id`` alone, since a fallback can coincidentally equal a genuinely reported id.
    """

    payload = first_structured_result(output)
    if payload is None:
        return requested_model, False
    usage = payload.get("modelUsage")
    if not isinstance(usage, dict) or not usage:
        return requested_model, False
    reported = list(usage)
    matches = [name for name in reported if requested_model and requested_model in name]
    if len(matches) == 1:
        return matches[0], True
    if len(reported) == 1:
        return reported[0], True
    return requested_model, False


def structured_actual_model(output: str, requested_model: str) -> str:
    """Prefer a structured CLI's reported model identifier when available."""

    return structured_actual_model_report(output, requested_model)[0]
