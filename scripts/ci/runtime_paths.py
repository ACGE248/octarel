"""Issue #146: the single canonical list of Control Plane runtime/evidence

directories that are never candidate/product content.

``.agent-output/`` (task audit evidence), ``.orchestrator-state/`` (the
scheduler/runbook database), and ``.local-gate/`` (exact-tree gate evidence)
are written by the Control Plane itself while it *validates* a candidate --
Test, Review, Checkpoint, and PR-readiness all read and write under these
directories as the acceptance pipeline runs. A given branch's own tracked
``.gitignore`` is product content with its own history; an old branch's
``.gitignore`` predating one of these directories must never be able to let
that directory's runtime writes leak into candidate-tree identity, changed-
path calculation, checkpoint contents, or exact-tree review scope. Every
place that stages, diffs, or snapshots a candidate tree must exclude these
directories by consulting this module directly, never by relying on
``--exclude-standard``/``.gitignore`` alone.
"""

from __future__ import annotations

import fcntl
import subprocess
from pathlib import Path
from typing import Iterable

# Bare directory names, matching scripts/ci/environment.py's prepare_worktree
# (the other canonical enumeration of these directories -- kept in sync by
# hand since that module provisions them and this module excludes them).
CP_RUNTIME_DIRNAMES: tuple[str, ...] = (".agent-output", ".orchestrator-state", ".local-gate")

# Same set, as "<name>/" prefixes for matching repo-relative path strings
# (git output is always "/"-separated, even on Windows).
_RUNTIME_PREFIXES: tuple[str, ...] = tuple(f"{name}/" for name in CP_RUNTIME_DIRNAMES)


def normalize(path: str) -> str:
    normalized = path.strip().replace("\\", "/")
    return normalized[2:] if normalized.startswith("./") else normalized


def is_runtime_path(path: str) -> bool:
    """True when ``path`` (repo-relative, as ``git`` prints it) is CP runtime state."""

    normalized = normalize(path)
    return normalized in CP_RUNTIME_DIRNAMES or normalized.startswith(_RUNTIME_PREFIXES)


def filter_runtime_paths(paths: Iterable[str]) -> list[str]:
    """Drop every CP runtime path from ``paths``, preserving order."""

    return [path for path in paths if path and not is_runtime_path(path)]


def pathspec_excludes() -> list[str]:
    """``git`` pathspec-magic tokens that exclude every runtime directory.

    MUST be passed alongside an explicit positive pathspec (e.g. ``.``), never
    alone: a pathspec list made entirely of ``:(exclude)`` entries has no
    positive component for the excludes to narrow, so e.g. ``git reset --
    :(exclude)a/`` matches (and resets) *everything*, not nothing. The correct
    shape is ``git add -A -- . :(exclude)a/ :(exclude)b/`` -- stage everything
    under ``.`` except ``a/`` and ``b/``.
    """

    return [f":(exclude){name}/" for name in CP_RUNTIME_DIRNAMES]


def runtime_dir_pathspecs() -> list[str]:
    """Plain (non-magic) pathspecs naming only the runtime directories.

    Unlike ``pathspec_excludes()``, this is a positive pathspec list on its
    own -- e.g. ``git reset -- .agent-output/ .orchestrator-state/
    .local-gate/`` unstages only these directories (self-healing an index a
    caller already staged them into), leaving every other staged path intact.
    """

    return [f"{name}/" for name in CP_RUNTIME_DIRNAMES]


def git_status_porcelain_lines(worktree: Path, *, untracked: str = "all", timeout: float = 15) -> list[str]:
    """Raw ``git status --porcelain`` lines, read directly rather than through

    a caller's own convenience git wrapper. Every existing per-module
    ``_git``/``_run_git`` helper in this codebase calls ``.strip()`` on the
    *entire* multi-line porcelain blob before splitting it into lines -- fine
    for the single-line output every other git subcommand they wrap returns,
    but ``.strip()`` on a multi-line blob only trims the very start and end
    of the whole string. When the first porcelain line is an unstaged-only
    change (status column ``" M"``, a leading space), that strip silently
    eats the line's leading space, shifting its fixed-width ``"XY "`` status
    prefix left by one column -- ``" M .agent-output/x.json"`` becomes
    ``"M .agent-output/x.json"``, and the fixed ``line[3:]`` slice every
    caller here uses to recover the path then returns ``"agent-output/x.json"``,
    silently dropping the leading ``"."`` and defeating ``is_runtime_path``.
    A failed/unavailable ``git`` returns an empty list (fail-open, matching
    every caller's pre-existing "no output means not dirty" behavior).
    """

    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", f"--untracked-files={untracked}"],
            cwd=worktree, capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    return result.stdout.splitlines()


def has_non_runtime_changes(worktree: Path, *, untracked: str = "all") -> bool:
    """True when ``git status --porcelain`` reports anything outside CP runtime dirs."""

    return any(
        not is_runtime_path(line[3:])
        for line in git_status_porcelain_lines(worktree, untracked=untracked)
        if line
    )


def append_missing_local_excludes(worktree: Path, patterns: Iterable[str], *, comment: str) -> None:
    """Idempotently add ``patterns`` to this worktree's local (never-committed) excludes.

    Writes to ``$GIT_COMMON_DIR/info/exclude`` (resolved with
    ``git rev-parse --git-common-dir`` so linked worktrees share their main
    checkout's excludes, matching how Git itself scopes that file) rather
    than the tracked ``.gitignore`` -- this file is never committed, so it
    applies uniformly to old and new worktrees alike without depending on
    branch content or requiring any historical branch to be edited. Best-
    effort and never raises; a failure here is not itself evidence of
    contamination since every identity-relevant call site excludes its own
    patterns directly too.

    The whole read-modify-append is guarded by an advisory ``flock`` on the
    exclude file itself: the Control Plane runs multiple concurrently-ticking
    worktrees against the same repository, and this file is shared by every
    linked worktree (``$GIT_COMMON_DIR`` is one directory for all of them),
    so two unsynchronized callers racing this function could each read it
    before either had appended, and both then append the same "missing"
    lines -- accumulating duplicate entries over a long-running daemon's
    life instead of staying a one-time idempotent self-heal.
    """

    try:
        common_dir = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=worktree, capture_output=True, text=True, timeout=15, check=False,
        )
        if common_dir.returncode != 0:
            return
        exclude_path = (worktree / common_dir.stdout.strip() / "info" / "exclude").resolve()
        exclude_path.parent.mkdir(parents=True, exist_ok=True)
        exclude_path.touch(exist_ok=True)
        with exclude_path.open("r+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                existing = handle.read()
                existing_lines = set(existing.splitlines())
                missing = [pattern for pattern in patterns if pattern not in existing_lines]
                if not missing:
                    return
                header = "" if not existing or existing.endswith("\n") else "\n"
                addition = "\n".join([f"# {comment}", *missing, ""])
                handle.seek(0, 2)  # os.SEEK_END -- the lock guarantees nothing else grew the file since our read
                handle.write(header + addition)
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except (OSError, subprocess.SubprocessError):
        return


def ensure_git_excludes(worktree: Path) -> None:
    """Idempotently add every CP runtime directory to this worktree's local excludes.

    See :func:`append_missing_local_excludes` for the underlying mechanism.
    This is defense-in-depth alongside the explicit ``pathspec_excludes()``
    used at every call site that matters for candidate-tree identity.
    """

    append_missing_local_excludes(
        worktree,
        (f"{name}/" for name in CP_RUNTIME_DIRNAMES),
        comment="ENG-AGENT-16 (issue #146): Control Plane runtime/evidence state, never candidate identity",
    )
