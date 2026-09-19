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
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator

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
        handle.write(f"{worker.name} pid={os.getpid()} at={int(time.time())}")
    try:
        yield
    finally:
        try:
            lock_path.unlink()
        except FileNotFoundError:  # pragma: no cover - best effort cleanup
            pass


def run_worker_process(command: list[str], root: Path, *, timeout: float | None) -> tuple[int, str]:
    """Run ``command`` from ``root`` and return ``(exit_code, combined_output)``.

    Never raises for a non-zero exit; a timeout returns exit code ``124`` and the
    partial output captured so far. The subprocess inherits a minimal environment
    allowlist so unrelated credentials cannot cross the worker boundary.
    """

    allowed_names = {"HOME", "PATH", "SHELL", "TMPDIR", "USER", "LOGNAME", "LANG", "TERM", "COLORTERM", "NO_COLOR"}
    env = {
        key: value
        for key, value in os.environ.items()
        if key in allowed_names or key.startswith("LC_") or key.startswith("XDG_")
    }

    try:
        completed = subprocess.run(
            command,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        partial = (exc.stdout or "") + (exc.stderr or "")
        return 124, partial + f"\n[timed out after {timeout}s]\n"
    return completed.returncode, (completed.stdout or "") + (completed.stderr or "")


def _first_json_object(output: str) -> dict[str, object] | None:
    """Decode a leading CLI JSON object even when stderr follows it."""

    try:
        payload, _ = json.JSONDecoder().raw_decode(output.lstrip())
    except (json.JSONDecodeError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def structured_failure(output: str) -> str | None:
    """Return a failure reason exposed by a structured CLI result, if any."""

    payload = _first_json_object(output)
    lowered = output.lower()
    if "tool permission requests are auto-denied" in lowered or "was denied" in lowered:
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


def structured_actual_model(output: str, requested_model: str) -> str:
    """Prefer a structured CLI's reported model identifier when available."""

    payload = _first_json_object(output)
    if payload is None:
        return requested_model
    usage = payload.get("modelUsage")
    if not isinstance(usage, dict) or not usage:
        return requested_model
    reported = list(usage)
    matches = [name for name in reported if requested_model and requested_model in name]
    if len(matches) == 1:
        return matches[0]
    return reported[0] if len(reported) == 1 else requested_model
