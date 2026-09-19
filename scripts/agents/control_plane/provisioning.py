"""ENG-AGENT-02-S7 (issue #97): safe, explicit git worktree provisioning.

Before this module, an operator who wanted to start work on a task with no
existing worktree had to run ``git worktree add`` by hand — the Control
Center could detect a *missing* worktree but never create one, so "Start
Development" could not actually work end-to-end for a freshly-eligible task.

This is the one write-shaped filesystem/git operation the Control Center
triggers on the operator's behalf. It is deliberately narrow and paranoid:

- Only ever called synchronously inside an explicit, operator-confirmed
  action (:func:`quickstart.start_quickstart_option`) — never automatically,
  never on a read path, never from a background loop.
- Only ever provisions a path/branch this process itself derived from
  repository truth (see ``quickstart.py``); it never accepts an arbitrary
  client-supplied path or branch string.
- Refuses to run unless the target path is a sibling directory of the
  repository root (this repository's own established worktree convention —
  see ``git worktree list`` for every existing example) that does not
  already exist, and the branch does not already exist either — so this can
  never overwrite a path or reuse a branch that already means something.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from scripts.ci.environment import prepare_worktree as inspect_worktree

# Conservative allow-list: matches every real branch/worktree-name convention
# already in use in this repository (see ``git worktree list``), while
# rejecting anything shell- or path-traversal-shaped.
_SAFE_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_SAFE_DIR_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class ProvisioningError(ValueError):
    pass


@dataclass(frozen=True)
class ProvisionResult:
    path: str
    branch: str
    base_ref: str
    readiness: dict[str, object] | None = None


def _run_git(args: list[str], *, cwd: Path, timeout: float = 30.0) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProvisioningError(f"git {' '.join(args)} could not be run: {exc}") from None


def _existing_branches(repo_root: Path) -> set[str]:
    result = _run_git(["for-each-ref", "--format=%(refname:short)", "refs/heads/"], cwd=repo_root)
    if result.returncode != 0:
        return set()
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


@dataclass(frozen=True)
class ReviewProvisionResult:
    path: str
    repository: str
    pr_number: int
    head_sha: str
    branch: str
    reused_existing: bool
    bootstrap: dict | None = None


def resolve_pr_reference(*, repo_root: Path, repository: str, pr_reference: str) -> tuple[int, str, str]:
    """Resolve a PR number/URL to ``(number, branch, head_sha)`` via ``gh``.

    Raises :class:`ProvisioningError` if ``gh`` is unavailable, the reference
    does not resolve, or the PR does not belong to ``repository`` (never
    silently reviews the wrong repository's code).
    """

    import json as _json

    ref = pr_reference.strip()
    if ref.lower().startswith("pr:"):
        ref = ref[3:].strip()
    if not re.fullmatch(r"\d+", ref) and not ref.startswith("http"):
        raise ProvisioningError(f"PR reference {pr_reference!r} is not a PR number, 'pr:<number>', or URL")
    try:
        result = subprocess.run(
            ["gh", "pr", "view", ref, "--repo", repository, "--json", "number,headRefName,headRefOid,state"],
            cwd=str(repo_root), capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProvisioningError(f"gh pr view could not be run: {exc}") from None
    if result.returncode != 0:
        raise ProvisioningError(f"gh pr view {ref!r} failed: {(result.stderr or result.stdout).strip()[:400]}")
    try:
        data = _json.loads(result.stdout)
    except _json.JSONDecodeError:
        raise ProvisioningError(f"gh pr view {ref!r} returned unparseable output") from None
    number, branch, head_sha = data.get("number"), data.get("headRefName"), data.get("headRefOid")
    if not isinstance(number, int) or not branch or not head_sha:
        raise ProvisioningError(f"gh pr view {ref!r} did not resolve number/headRefName/headRefOid")
    return number, branch, head_sha


def provision_review_worktree(
    *, repo_root: Path, repository: str, pr_reference: str, existing: list[tuple[str, str | None]] = (),
    bootstrap_ui: bool = False,
) -> ReviewProvisionResult:
    """Resolve ``pr_reference`` and provision (or reuse) an isolated, detached,

    exact-head review checkout for it (ENG-AGENT-14, issue #140).

    ``existing`` is the caller's list of ``(path, review_head_sha)`` for
    already-registered review worktrees; if one already matches the resolved
    head SHA, its path is returned unchanged (``reused_existing=True``) and no
    new worktree is created -- this is the only reuse this function performs:
    it never treats an unrelated worktree (main, DevCP, a product
    implementation checkout, or a review checkout for a different PR/head) as
    an implicit substitute, because those never appear in ``existing`` with a
    matching ``review_head_sha`` in the first place.
    """

    repo_root = repo_root.resolve()
    number, branch, head_sha = resolve_pr_reference(repo_root=repo_root, repository=repository, pr_reference=pr_reference)

    for path, recorded_head_sha in existing:
        if recorded_head_sha == head_sha and Path(path).is_dir():
            return ReviewProvisionResult(
                path=path, repository=repository, pr_number=number, head_sha=head_sha, branch=branch,
                reused_existing=True,
            )

    worktree_path = repo_root.parent / f"{repo_root.name}-review-pr-{number}-{head_sha[:8]}"
    if not worktree_path.exists():
        result = _run_git(
            ["worktree", "add", "--detach", str(worktree_path), head_sha], cwd=repo_root, timeout=60.0,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise ProvisioningError(f"git worktree add --detach failed: {detail}")

    bootstrap_result = None
    if bootstrap_ui and (worktree_path / "package-lock.json").is_file():
        # ENG-AGENT-14 Part C (issue #140): only bootstrap when the review
        # checkout will actually be used to run/audit the app (e.g. the
        # Control Center UI audit against a reviewed PR) -- an ordinary
        # code-only diff-review never needs Node/Playwright at all. Reuses
        # scripts.ci.environment.bootstrap_dependencies unmodified: fresh
        # lockfile fingerprint / already-cached browser are both reused, not
        # reinstalled/redownloaded.
        from scripts.ci.environment import bootstrap_dependencies

        bootstrap_result = bootstrap_dependencies(worktree_path, require_frontend=True, require_browser=True)

    return ReviewProvisionResult(
        path=str(worktree_path), repository=repository, pr_number=number, head_sha=head_sha, branch=branch,
        reused_existing=False, bootstrap=bootstrap_result,
    )


def provision_worktree(*, repo_root: Path, worktree: str, branch: str, base_ref: str = "main") -> ProvisionResult:
    """Create ``worktree`` on a new ``branch`` off ``base_ref``.

    Raises :class:`ProvisioningError` (never a raw ``subprocess`` failure or
    an unvalidated filesystem write) for any of: an unsafe/absolute-path
    violation, a path that already exists, a branch name that already
    exists, or the underlying ``git worktree add`` failing.
    """

    repo_root = repo_root.resolve()
    worktree_path = Path(worktree)
    if not worktree_path.is_absolute():
        raise ProvisioningError(f"worktree {worktree!r} must be an absolute path")
    worktree_path = worktree_path.resolve() if worktree_path.parent.exists() else worktree_path
    if worktree_path.parent != repo_root.parent:
        raise ProvisioningError(
            f"worktree {worktree!r} must be a sibling directory of the repository root ({repo_root.parent}), "
            "matching this repository's existing worktree convention"
        )
    if not _SAFE_DIR_NAME_RE.match(worktree_path.name):
        raise ProvisioningError(f"worktree directory name {worktree_path.name!r} contains unsafe characters")
    if worktree_path.exists():
        raise ProvisioningError(f"{worktree_path} already exists; refusing to overwrite it")

    if not _SAFE_BRANCH_RE.match(branch) or ".." in branch:
        raise ProvisioningError(f"branch name {branch!r} contains unsafe characters")
    if branch in _existing_branches(repo_root):
        raise ProvisioningError(f"branch {branch!r} already exists; refusing to reuse it for a new worktree")

    result = _run_git(["worktree", "add", str(worktree_path), "-b", branch, base_ref], cwd=repo_root, timeout=60.0)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise ProvisioningError(f"git worktree add failed: {detail}")

    has_frontend = (worktree_path / "package-lock.json").is_file()
    readiness = inspect_worktree(
        worktree_path, require_frontend=has_frontend, require_browser=has_frontend,
        create_local_dirs=True, hydrate_offline=True,
    )
    return ProvisionResult(path=str(worktree_path), branch=branch, base_ref=base_ref, readiness=readiness)
