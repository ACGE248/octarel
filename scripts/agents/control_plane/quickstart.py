"""ENG-AGENT-02-S7 (issue #97): Quick Start — one-click, fully specified runs.

The Control Center's Runbook form used to require an operator to already
know which task is next, its branch, and its worktree, and to type all three
by hand. This module re-derives that from current repository truth instead,
so the ordinary "Continue Video Editor" flow never requires knowing a task
ID, branch name, or worktree path.

The Video Editor's canonical ledger
(``docs/video-editor/IMPLEMENTATION_STATUS_V2.md``) is parsed fresh on every
call — nothing here is allowed to hard-code a specific task ID as "the" next
one, because the ledger changes as work merges. If the ledger currently
reports no eligible task, or a task is eligible but has no existing worktree
yet, this reports that truthfully rather than fabricating a ready-to-launch
option.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..registry import Registry
from .models import DEFAULT_STOP_CONDITIONS, PERMISSION_REPO_CONFIGURED_AUTO, Runbook
from .provisioning import ProvisioningError, provision_worktree
from .recovery import discover_git_worktrees
from .state import State

if TYPE_CHECKING:
    from .supervisor import Supervisor

VIDEO_EDITOR_LEDGER_RELATIVE = Path("docs/video-editor/IMPLEMENTATION_STATUS_V2.md")
VIDEO_EDITOR_PRACTICAL_PLAN_RELATIVE = "docs/OCTASCENE_STANDALONE_VIDEO_EDITOR_PRACTICAL_PLAN.md"
VIDEO_EDITOR_COMMANDS_RELATIVE = "docs/OCTASCENE_VIDEO_EDITOR_IMPLEMENTATION_COMMANDS_V2.md"

# From the ledger's own "## Status values" line. Only "pending" is ever
# eligible to become the proposed next task — every other status either
# already resolved the task or requires a human decision this module must
# never make silently (e.g. "blocked", "in-progress").
ELIGIBLE_STATUS = "pending"

_TABLE_ROW_RE = re.compile(r"^\|\s*([A-Za-z0-9_.-]+)\s*\|\s*([a-z-]+)\s*\|\s*(.*?)\s*\|$")


@dataclass(frozen=True)
class LedgerTask:
    task_id: str
    status: str
    notes: str


def parse_video_editor_ledger(text: str) -> list[LedgerTask]:
    """Parse every ``| ID | status | notes |`` row, in document order.

    Deliberately tolerant of the surrounding prose/headings/evidence-log
    sections: any line that is not a well-formed 3-column table row —
    including the header row and the ``|---|---|---|`` separator — is
    silently skipped, so this never needs to track which section a row
    belongs to.
    """

    tasks: list[LedgerTask] = []
    for line in text.splitlines():
        match = _TABLE_ROW_RE.match(line.strip())
        if not match:
            continue
        task_id, status, notes = match.groups()
        if not re.search(r"[A-Za-z]", task_id):  # excludes "---" separator cells
            continue
        if task_id == "ID":  # header row
            continue
        tasks.append(LedgerTask(task_id=task_id, status=status, notes=notes))
    return tasks


def next_eligible_video_editor_task(repo_root: Path) -> LedgerTask | None:
    """The first ``pending`` row in ledger document order, or ``None``.

    ``None`` means the ledger currently reports no eligible task (e.g.
    everything remaining is blocked/in-progress) — callers must show this
    truthfully rather than falling back to a guess.
    """

    ledger_path = repo_root / VIDEO_EDITOR_LEDGER_RELATIVE
    try:
        text = ledger_path.read_text(encoding="utf-8")
    except OSError:
        return None
    for task in parse_video_editor_ledger(text):
        if task.status == ELIGIBLE_STATUS:
            return task
    return None


def _slugify(notes: str, *, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", notes.lower()).strip("-")
    return slug[:max_len].rstrip("-") or "task"


def find_existing_worktree_for_task(repo_root: Path, task_id: str):
    """The first discovered *dedicated* worktree whose branch names ``task_id``, if any.

    Never matches the primary checkout (``repo_root`` itself): a real task
    worktree is always a dedicated sibling directory in this repository's
    own convention, and a primary checkout that merely happens to be on a
    similarly-named branch is not a substitute for one (Grok Build review,
    issue #97) — treating it as one would launch an unattended write-capable
    session directly against the maintainer's main checkout. The match
    itself uses word-boundary-style anchoring (``V1-01`` cannot match inside
    ``v1-010``) rather than a bare substring test.
    """

    needle = re.escape(task_id.lower())
    pattern = re.compile(rf"(?<![a-z0-9]){needle}(?![a-z0-9])")
    repo_root_resolved = repo_root.resolve()
    for record in discover_git_worktrees(repo_root):
        if Path(record.path).resolve() == repo_root_resolved:
            continue
        branch = (record.branch or "").removeprefix("refs/heads/").lower()
        if pattern.search(branch):
            return record
    return None


@dataclass(frozen=True)
class QuickStartOption:
    key: str
    title: str
    objective: str
    source_ref: str
    preset: str
    parent_worker: str
    duration_minutes: int
    permission_profile: str
    stop_conditions: tuple[str, ...]
    program: str = "OctaScene"
    task_id: str | None = None
    task_title: str | None = None
    issue_reference: str | None = None
    why_next: str = "Selected from current repository state."
    dependency_state: str = "Ready"
    proposed_tester: str = "opencode2-gemini-flash-lite"
    proposed_reviewer: str = "codex-review"
    proposed_tester_unavailable_reason: str | None = None
    proposed_reviewer_unavailable_reason: str | None = None
    branch_worktree_mode: str = "automatic"
    expected_checks: tuple[str, ...] = ()
    checkpoint_pr_behavior: str = "Checkpoint on the task branch; never auto-merge."
    action: str = "prepare"
    branch: str | None = None
    worktree: str | None = None
    continues_existing_worktree: bool = False
    codex_policy: str = "conserve"
    codex_auto_eligible: bool = False
    max_codex_invocations: int = 1
    # None means ready to start; otherwise the exact reason (and, where
    # applicable, the exact operator command) this option cannot be started
    # yet — the Control Center must show this truthfully rather than
    # letting "Start Development" fail confusingly later.
    unavailable_reason: str | None = None
    # ENG-AGENT-12 (issue #136): deterministic evidence proving *which*
    # repository truth this exact resolution used -- distinct from the
    # human-facing ``why_next``/``dependency_state`` prose above. Present
    # only for ledger-derived options (``continue-video-editor``); ``None``
    # for options that derive from the live checkout's current branch
    # instead of a ledger file. This is what lets an operator tell a stale
    # Control Plane process (reading the wrong checkout, or a checkout whose
    # HEAD is behind current canonical truth) apart from a genuinely stale
    # ledger, straight from the API response.
    resolution_evidence: dict[str, str | None] | None = None

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "title": self.title,
            "objective": self.objective,
            "source_ref": self.source_ref,
            "preset": self.preset,
            "parent_worker": self.parent_worker,
            "duration_minutes": self.duration_minutes,
            "permission_profile": self.permission_profile,
            "stop_conditions": list(self.stop_conditions),
            "program": self.program,
            "task_id": self.task_id,
            "task_title": self.task_title,
            "issue_reference": self.issue_reference,
            "why_next": self.why_next,
            "dependency_state": self.dependency_state,
            "proposed_tester": self.proposed_tester,
            "proposed_reviewer": self.proposed_reviewer,
            "proposed_tester_unavailable_reason": self.proposed_tester_unavailable_reason,
            "proposed_reviewer_unavailable_reason": self.proposed_reviewer_unavailable_reason,
            "branch_worktree_mode": self.branch_worktree_mode,
            "expected_checks": list(self.expected_checks),
            "checkpoint_pr_behavior": self.checkpoint_pr_behavior,
            "action": self.action,
            "branch": self.branch,
            "worktree": self.worktree,
            "continues_existing_worktree": self.continues_existing_worktree,
            "codex_policy": self.codex_policy,
            "codex_auto_eligible": self.codex_auto_eligible,
            "max_codex_invocations": self.max_codex_invocations,
            "unavailable_reason": self.unavailable_reason,
            "ready": self.unavailable_reason is None,
            "resolution_evidence": self.resolution_evidence,
        }


def _repo_head_sha(repo_root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    sha = (result.stdout or "").strip()
    return sha if result.returncode == 0 and sha else None


def _ledger_resolution_evidence(repo_root: Path, task: "LedgerTask | None") -> dict[str, str | None]:
    """Deterministic proof of exactly which checkout/commit/ledger produced this resolution.

    ENG-AGENT-12 (issue #136): a stale Quick Start recommendation can come
    from the resolver reading a stale checkout just as easily as from a
    stale ledger. This function is the sole place that assembles the facts
    an operator needs to tell those apart, so every ledger-derived option
    carries it rather than only the ones that happened to resolve a task.
    """

    ledger_path = repo_root / VIDEO_EDITOR_LEDGER_RELATIVE
    mtime_iso: str | None = None
    try:
        mtime_iso = datetime.fromtimestamp(ledger_path.stat().st_mtime, tz=timezone.utc).isoformat()
    except OSError:
        mtime_iso = None
    return {
        "repo_root": str(repo_root.resolve()) if repo_root.exists() else str(repo_root),
        "head_sha": _repo_head_sha(repo_root),
        "ledger_path": str(VIDEO_EDITOR_LEDGER_RELATIVE),
        "ledger_mtime": mtime_iso,
        "resolved_task_id": task.task_id if task else None,
        "resolved_at": datetime.now(timezone.utc).isoformat(),
    }


def continue_video_editor_option(repo_root: Path) -> QuickStartOption:
    """Build the "Continue Video Editor" Quick Start option from current ledger truth.

    Never hard-codes a task ID: the returned ``source_ref``/``branch`` are
    whatever the ledger and the repository's actual worktrees say right now.
    """

    task = next_eligible_video_editor_task(repo_root)
    if task is None:
        return QuickStartOption(
            key="continue-video-editor",
            title="Continue Video Editor",
            objective="",
            source_ref="",
            preset="overnight-development",
            parent_worker="claude-code",
            duration_minutes=480,
            permission_profile=PERMISSION_REPO_CONFIGURED_AUTO,
            stop_conditions=DEFAULT_STOP_CONDITIONS,
            program="Standalone Video Editor",
            why_next="The canonical ledger currently has no pending row eligible to start.",
            dependency_state="Blocked — maintainer decision required",
            proposed_tester="opencode2-gemini-flash-lite",
            proposed_reviewer="codex-review",
            branch_worktree_mode="automatic once an eligible task exists",
            expected_checks=("Defined by the next eligible maintained implementation command",),
            checkpoint_pr_behavior="No branch or PR is created until the ledger exposes an eligible task.",
            unavailable_reason=(
                f"{VIDEO_EDITOR_LEDGER_RELATIVE} reports no task with status "
                f"{ELIGIBLE_STATUS!r} right now; the ledger needs a maintainer decision."
            ),
            resolution_evidence=_ledger_resolution_evidence(repo_root, None),
        )

    existing = find_existing_worktree_for_task(repo_root, task.task_id)
    slug = _slugify(task.notes)
    proposed_branch = f"video-editor/{task.task_id.lower()}-{slug}"
    branch = (existing.branch or "").removeprefix("refs/heads/") if existing else proposed_branch
    worktree = existing.path if existing else str(repo_root.parent / f"{repo_root.name}-{task.task_id.lower()}")

    # `unavailable_reason` is reserved for "there is truly nothing to start"
    # (the ledger has no eligible task at all, handled above). A missing
    # worktree is *not* one of those cases: `start_quickstart_option` (and
    # the `quickstart_start` command) provisions one automatically, so this
    # option must stay startable — never disable "Start Development" for the
    # exact case the auto-provisioning feature exists to handle.

    objective = (
        f"Continue the Standalone Video Editor from current repository truth. Read "
        f"{VIDEO_EDITOR_PRACTICAL_PLAN_RELATIVE} and {VIDEO_EDITOR_COMMANDS_RELATIVE} for the implementation "
        f"contract, then inspect {VIDEO_EDITOR_LEDGER_RELATIVE}, git status, and recent commits in this "
        f"worktree before assuming anything is stale. The ledger reports {task.task_id} ({task.notes}) as the "
        f"next eligible task. Implement it completely, verify locally, checkpoint continuously, and update the "
        f"ledger's status/evidence for {task.task_id} once real, tested work exists."
    )

    return QuickStartOption(
        key="continue-video-editor",
        title=f"Continue Video Editor — {task.task_id}",
        objective=objective,
        source_ref=f"{task.task_id} ({task.notes})",
        preset="overnight-development",
        parent_worker="claude-code",
        duration_minutes=480,
        permission_profile=PERMISSION_REPO_CONFIGURED_AUTO,
        stop_conditions=DEFAULT_STOP_CONDITIONS,
        program="Standalone Video Editor",
        task_id=task.task_id,
        task_title=task.notes.rstrip("."),
        issue_reference=task.task_id,
        why_next=(
            f"{task.task_id} is the first pending row in the canonical Video Editor ledger; "
            "all earlier dependencies are recorded complete or superseded."
        ),
        dependency_state="Ready — earlier ledger tasks are complete or superseded",
        proposed_tester="opencode2-gemini-flash-lite",
        proposed_reviewer="codex-review",
        branch_worktree_mode="automatic",
        expected_checks=(
            "Task-focused Python/frontend tests from the maintained implementation command",
            "Changed-file lint/type/build checks",
            "Risk-appropriate deterministic repository gate before review",
        ),
        checkpoint_pr_behavior=(
            "Commit only green slices on the dedicated task branch; open/update its scoped PR; never auto-merge."
        ),
        branch=branch,
        worktree=worktree,
        continues_existing_worktree=existing is not None,
        # Pressing Start Development is an explicit selection of this
        # repository-configured role route. Permit one subscription Codex
        # replacement for ordinary implementation work if the preferred
        # worker is unavailable and every canonical fallback gate passes.
        # The selector still chooses by registry order at launch time; this
        # does not pin a provider or enable API billing/paid overflow.
        codex_policy="balanced",
        codex_auto_eligible=True,
        max_codex_invocations=1,
        unavailable_reason=None,
        resolution_evidence=_ledger_resolution_evidence(repo_root, task),
    )


def _current_branch(repo_root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    branch = (result.stdout or "").strip()
    return branch if result.returncode == 0 and branch and branch != "HEAD" else None


def _current_worktree_option(
    repo_root: Path,
    *,
    key: str,
    title: str,
    preset: str,
    objective: str,
    parent_worker: str,
    duration_minutes: int,
    checks: tuple[str, ...],
) -> QuickStartOption:
    branch = _current_branch(repo_root)
    unavailable = None
    if not branch or branch in {"main", "master"}:
        unavailable = "The Control Center checkout is not on a dedicated task/PR branch."
    return QuickStartOption(
        key=key,
        title=title,
        objective=objective.format(branch=branch or "UNKNOWN"),
        source_ref=branch or "No dedicated branch",
        preset=preset,
        parent_worker=parent_worker,
        duration_minutes=duration_minutes,
        permission_profile="standard",
        stop_conditions=DEFAULT_STOP_CONDITIONS,
        program="Current OctaScene branch",
        task_id=branch,
        task_title=title,
        issue_reference=branch,
        why_next="Uses the branch and worktree currently serving this Control Center; no PR metadata is guessed.",
        dependency_state="Ready" if unavailable is None else "Blocked",
        expected_checks=checks,
        checkpoint_pr_behavior="Checkpoint on the current branch and update its existing PR if one exists; never auto-merge.",
        branch=branch,
        worktree=str(repo_root),
        continues_existing_worktree=True,
        unavailable_reason=unavailable,
    )


def _uses_video_editor_quickstart(project: Any | None) -> bool:
    """OctaScene compatibility: the Video Editor ledger Quick Start catalog.

    Legacy callers (no project) keep the existing catalog. A selected project
    uses that catalog only when it declared the video-editor-ledger adapter.
    """

    if project is None:
        return True
    from .task_sources import (
        ADAPTER_VIDEO_EDITOR_LEDGER,
        TaskSourceError,
        task_source_adapter_name,
    )

    try:
        return task_source_adapter_name(project) == ADAPTER_VIDEO_EDITOR_LEDGER
    except TaskSourceError:
        return False


def continue_next_task_option(project: Any, repo_root: Path) -> QuickStartOption:
    """Generic Quick Start option from the selected project's task-source adapter."""

    from .task_sources import TaskSourceError, next_eligible_task

    program = getattr(project, "display_name", None) or "selected project"
    try:
        task = next_eligible_task(project)
    except TaskSourceError as exc:
        return QuickStartOption(
            key="continue-next-task",
            title=f"Continue {program} next eligible task",
            objective="",
            source_ref="",
            preset="overnight-development",
            parent_worker="claude-code",
            duration_minutes=480,
            permission_profile=PERMISSION_REPO_CONFIGURED_AUTO,
            stop_conditions=DEFAULT_STOP_CONDITIONS,
            program=program,
            why_next="The selected project's task source could not be read.",
            dependency_state="Blocked",
            unavailable_reason=str(exc),
        )
    if task is None:
        return QuickStartOption(
            key="continue-next-task",
            title=f"Continue {program} next eligible task",
            objective="",
            source_ref="",
            preset="overnight-development",
            parent_worker="claude-code",
            duration_minutes=480,
            permission_profile=PERMISSION_REPO_CONFIGURED_AUTO,
            stop_conditions=DEFAULT_STOP_CONDITIONS,
            program=program,
            why_next="The selected project's task source currently has no eligible task.",
            dependency_state="Blocked — no eligible task",
            unavailable_reason=(
                f"project {project.project_id!r} currently has no eligible task in its declared task source"
            ),
        )

    existing = find_existing_worktree_for_task(repo_root, task.task_id)
    slug = _slugify(task.title)
    proposed_branch = f"{task.task_id.lower()}-{slug}"
    branch = (existing.branch or "").removeprefix("refs/heads/") if existing else proposed_branch
    worktree = existing.path if existing else str(repo_root.parent / f"{repo_root.name}-{task.task_id.lower().lstrip('#')}")
    objective = (
        f"Continue {program} from current repository truth. The selected project's task source "
        f"reports {task.task_id} ({task.title}) as the next eligible task. Implement it completely, "
        f"verify with the project's declared validation command, and do not copy another project's "
        f"roadmap or ledger into Control Plane state."
    )
    return QuickStartOption(
        key="continue-next-task",
        title=f"Continue {program} — {task.task_id}",
        objective=objective,
        source_ref=task.source_ref or f"{task.task_id} ({task.title})",
        preset="overnight-development",
        parent_worker="claude-code",
        duration_minutes=480,
        permission_profile=PERMISSION_REPO_CONFIGURED_AUTO,
        stop_conditions=DEFAULT_STOP_CONDITIONS,
        program=program,
        task_id=task.task_id,
        task_title=task.title,
        issue_reference=task.task_id,
        why_next=f"{task.task_id} is the first eligible task in the selected project's task source.",
        dependency_state="Ready",
        branch=branch,
        worktree=worktree,
        continues_existing_worktree=existing is not None,
        expected_checks=("Repository-owned validation command from the exact candidate/worktree",),
        checkpoint_pr_behavior=(
            "Commit only green slices on the dedicated task branch; open/update its scoped PR; never auto-merge."
        ),
        unavailable_reason=None,
    )


def _routable_worker(
    state: State,
    registry: Registry,
    role: str,
    fallback: str,
    *,
    permission_profile: str = "standard",
) -> tuple[str, str | None]:
    """First runnable worker plus a truthful reason when the route is empty."""

    from .provider_state import ROUTABLE_STATES

    try:
        route = registry.route(role)
    except Exception:  # noqa: BLE001 - an unknown role keeps the declared default
        return fallback, f"role {role!r} has no configured route"
    known = {p.name: p for p in state.list_provider_states()}
    blocked: list[str] = []
    for name in route:
        worker = registry.workers.get(name)
        if worker is None:
            blocked.append(f"{name}: unknown worker")
            continue
        if not worker.enabled:
            blocked.append(f"{name}: disabled")
            continue
        if worker.allow_api_billing or worker.cost_class == "optional-overflow":
            blocked.append(f"{name}: paid/overflow route")
            continue
        if not registry.repository_data_reuse_allowed(name):
            blocked.append(f"{name}: unauthorized for repository data")
            continue
        if permission_profile != "standard" and not worker.supports_permission_profile(permission_profile):
            blocked.append(f"{name}: permission profile {permission_profile} unsupported")
            continue
        provider = known.get(name)
        if known and (provider is None or not provider.configured or provider.state not in ROUTABLE_STATES):
            blocked.append(f"{name}: {getattr(provider, 'state', 'UNKNOWN')}")
            continue
        return name, None
    return "(no routable worker)", "; ".join(blocked) or f"no eligible worker for {role}"


def _default_branch_ref(repo_root: Path, project: Any | None) -> str | None:
    """The ref a task branch must contain to be current (``origin/<default>``, else the local default)."""

    default = getattr(project, "default_branch", None) or "main"
    for ref in (f"origin/{default}", default):
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", ref], cwd=str(repo_root), capture_output=True, text=True, check=False
        )
        if result.returncode == 0:
            return ref
    return None


def _stale_unowned_draft(option: QuickStartOption, *, repo_root: Path, project: Any | None, state: State) -> bool:
    """True when the task's existing worktree is a draft nobody at Octarel owns, on obsolete ancestry.

    Adopting it would silently continue stale work (for example a stacked branch forked from a
    pre-reconciliation head, hundreds of commits behind). A branch that some Octarel run created
    or worked on is a continuation, not a stale draft, and is never treated this way.
    """

    if not (option.continues_existing_worktree and option.branch and option.worktree):
        return False
    project_id = getattr(project, "project_id", None)
    for run in state.list_runbooks(project_id=project_id):
        if run.branch == option.branch or run.worktree == option.worktree:
            return False
    ref = _default_branch_ref(repo_root, project)
    if ref is None:
        return False
    contains = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ref, "HEAD"], cwd=option.worktree, capture_output=True, text=True, check=False
    )
    return contains.returncode == 1  # 0 = current, 1 = does not contain the default branch tip, other = unknown


def _existing_reconciled(option: QuickStartOption, repo_root: Path) -> tuple[str, str] | None:
    """A ``<branch>-reconciled*`` worktree a previous Quick Start already provisioned for this task."""

    prefix = f"{option.branch}-reconciled"
    for record in discover_git_worktrees(repo_root):
        branch = (record.branch or "").removeprefix("refs/heads/")
        if branch == prefix or branch.startswith(f"{prefix}-"):
            return branch, record.path
    return None


def _fresh_reconciled_names(option: QuickStartOption, repo_root: Path) -> tuple[str, str]:
    """A new branch/worktree pair for the task (repository precedent: ``<name>-reconciled``)."""

    taken = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname:short)", "refs/heads/"], cwd=str(repo_root), capture_output=True, text=True, check=False
    ).stdout.split()
    for index in range(1, 10):
        suffix = "-reconciled" if index == 1 else f"-reconciled-{index}"
        branch, worktree = f"{option.branch}{suffix}", f"{option.worktree}{suffix}"
        if branch not in taken and not Path(worktree).exists():
            return branch, worktree
    return f"{option.branch}-reconciled-x", f"{option.worktree}-reconciled-x"


def _apply_run_truth(
    option: QuickStartOption, *, repo_root: Path, project: Any | None, state: State | None, registry: Registry | None
) -> QuickStartOption:
    """Overlay what Octarel itself knows about this project's runs and routes.

    Two truths the task source cannot supply: (1) the task may already have an
    accepted implementation run that is only waiting on its PR/ledger, and (2) the
    tester/reviewer shown must be workers that can actually run right now.
    """

    if state is None:
        return option
    from dataclasses import replace

    changes: dict[str, Any] = {}
    if option.action == "prepare" and option.task_id and option.unavailable_reason is None:
        from .advancement import accepted_run_for_task

        accepted = accepted_run_for_task(state, getattr(project, "project_id", None), option.task_id)
        if accepted is not None:
            changes.update(
                unavailable_reason=(
                    f"{option.task_id} was already implemented and accepted by run {accepted.id} (every acceptance "
                    "stage passed). Its task source still lists it as eligible until its PR merges and the ledger is "
                    "reconciled, so Octarel will not start it again. Merge or reconcile the existing branch/PR first."
                ),
                dependency_state="Blocked — already accepted; awaiting merge/reconciliation",
                why_next=f"{option.task_id} is first in the ledger but already has accepted run {accepted.id}.",
                checkpoint_pr_behavior="No new run is started for an accepted task; finish its existing PR instead.",
            )
    if option.action == "prepare" and "unavailable_reason" not in changes and _stale_unowned_draft(
        option, repo_root=repo_root, project=project, state=state
    ):
        adopted = _existing_reconciled(option, repo_root)
        branch, worktree = adopted or _fresh_reconciled_names(option, repo_root)
        changes.update(
            branch=branch,
            worktree=worktree,
            continues_existing_worktree=adopted is not None,
            checkpoint_pr_behavior=(
                f"An earlier draft branch {option.branch} exists on stale ancestry (it does not contain the current "
                f"default branch) and is left untouched. This run starts a fresh branch from current main; reconcile "
                f"the draft's commits explicitly. " + option.checkpoint_pr_behavior
            ),
        )
    if registry is not None and option.action == "prepare":
        from .provider_state import ROUTABLE_STATES

        tester, tester_reason = _routable_worker(state, registry, "focused-tests", option.proposed_tester)
        reviewer, reviewer_reason = _routable_worker(state, registry, "diff-review", option.proposed_reviewer)
        implementer, implementer_reason = _routable_worker(
            state,
            registry,
            "primary-implementation",
            option.parent_worker,
            permission_profile=option.permission_profile,
        )
        changes.update(
            proposed_tester=tester,
            proposed_reviewer=reviewer,
            proposed_tester_unavailable_reason=tester_reason,
            proposed_reviewer_unavailable_reason=reviewer_reason,
            parent_worker=implementer,
        )
        provider_rows = {row.name: row for row in state.list_provider_states()}
        codex_routable = any(
            name.startswith("codex")
            and (worker := registry.workers.get(name)) is not None
            and worker.enabled
            and not worker.allow_api_billing
            and worker.cost_class != "optional-overflow"
            and registry.repository_data_reuse_allowed(name)
            and (provider := provider_rows.get(name)) is not None
            and provider.configured
            and provider.state in ROUTABLE_STATES
            for name in registry.route("primary-implementation")
        )
        if not codex_routable:
            changes.update(codex_policy="conserve", codex_auto_eligible=False)
        if implementer_reason and "unavailable_reason" not in changes:
            changes.update(
                unavailable_reason=f"No eligible implementation worker: {implementer_reason}",
                dependency_state="Blocked — no eligible implementation route",
            )
    return replace(option, **changes) if changes else option


def resolve_quickstart_option(
    repo_root: Path, key: str, project: Any | None = None, *, state: State | None = None, registry: Registry | None = None
) -> QuickStartOption:
    option = _resolve_quickstart_option_base(repo_root, key, project)
    return _apply_run_truth(option, repo_root=repo_root, project=project, state=state, registry=registry)


def _resolve_quickstart_option_base(repo_root: Path, key: str, project: Any | None = None) -> QuickStartOption:
    if key == "continue-next-task":
        if project is None:
            raise QuickStartError("continue-next-task requires a selected project")
        return continue_next_task_option(project, repo_root)
    if key in {"continue-video-editor", "continue-octascene"}:
        option = continue_video_editor_option(repo_root)
        if key == "continue-octascene":
            return QuickStartOption(**{**option.__dict__, "key": key, "title": "Continue OctaScene next eligible task"})
        return option
    program = "Current OctaScene branch"
    if project is not None and not _uses_video_editor_quickstart(project):
        program = f"Current {project.display_name} branch"
    if key == "finish-current-pr":
        option = _current_worktree_option(
            repo_root,
            key=key,
            title="Finish Current PR",
            preset="finish-pr",
            objective=(
                "Finish the current branch {branch} from repository truth. Inspect its diff, issue/PR acceptance, "
                "checks, and maintained docs; close only real gaps and leave it review-ready without merging."
            ),
            parent_worker="claude-code",
            duration_minutes=240,
            checks=(
                "Focused tests for changed paths",
                "Changed-file lint/build checks",
                "PR diff and documentation-drift review",
            ),
        )
        if program != option.program:
            return QuickStartOption(**{**option.__dict__, "program": program})
        return option
    if key == "focused-test-fix":
        option = _current_worktree_option(
            repo_root,
            key=key,
            title="Focused Test & Fix",
            preset="test-fix",
            objective=(
                "On branch {branch}, select and run the smallest deterministic tests covering the current diff, "
                "fix root causes only, and re-run the same checks. Never weaken or skip legitimate tests."
            ),
            parent_worker="claude-code",
            duration_minutes=120,
            checks=("Impact-selected focused tests", "Re-run every failing focused check after the fix"),
        )
        if program != option.program:
            return QuickStartOption(**{**option.__dict__, "program": program})
        return option
    if key == "review-current-diff":
        option = _current_worktree_option(
            repo_root,
            key=key,
            title="Review Current Diff",
            preset="review-only",
            objective=(
                "Review branch {branch} read-only for correctness, security, accessibility, responsive layout, "
                "test gaps, and documentation drift. Report findings; never edit, commit, push, or merge."
            ),
            parent_worker="codex-review",
            duration_minutes=120,
            checks=("Read-only scoped diff review", "Documentation-drift audit"),
        )
        if program != option.program:
            return QuickStartOption(**{**option.__dict__, "program": program})
        return option
    if key == "custom-run":
        return QuickStartOption(
            key=key,
            title="Custom Run",
            objective="Configure a manual runbook in Advanced Settings.",
            source_ref="Manual configuration",
            preset="overnight-development",
            parent_worker="claude-code",
            duration_minutes=480,
            permission_profile="standard",
            stop_conditions=DEFAULT_STOP_CONDITIONS,
            program="Custom",
            why_next="Operator-selected manual workflow.",
            dependency_state="Configure before starting",
            branch_worktree_mode="manual in Advanced Settings",
            checkpoint_pr_behavior="Defined by the selected preset; auto-merge remains prohibited.",
            action="advanced",
        )
    raise QuickStartError(f"unknown quickstart option {key!r}")


def list_quickstart_options(
    repo_root: Path, project: Any | None = None, *, state: State | None = None, registry: Registry | None = None
) -> list[dict]:
    """All Quick Start options the dashboard's Runs UI should offer today.

    OctaScene (or a legacy caller with no selected project) keeps the Video
    Editor catalog. Any other selected project uses its declared task-source
    adapter instead of the OctaScene ledger.
    """

    if _uses_video_editor_quickstart(project):
        keys = (
            "continue-video-editor",
            "continue-octascene",
            "finish-current-pr",
            "focused-test-fix",
            "review-current-diff",
            "custom-run",
        )
    else:
        keys = (
            "continue-next-task",
            "finish-current-pr",
            "focused-test-fix",
            "review-current-diff",
            "custom-run",
        )
    return [resolve_quickstart_option(repo_root, key, project=project, state=state, registry=registry).as_dict() for key in keys]


class QuickStartError(ValueError):
    pass


def start_quickstart_option(
    *,
    state: State,
    registry: Registry,
    supervisor: "Supervisor",
    repo_root: Path,
    key: str,
    dry_run: bool = False,
    scheduler=None,
    project_id: str | None = None,
    project: Any | None = None,
) -> Runbook:
    """Re-resolve ``key`` fresh against current repository truth, then create and start it.

    The only client input trusted here is the stable ``key`` — every other
    field (objective, source_ref, branch, worktree, worker, duration,
    permission profile) is re-derived server-side on this call, so a client
    can never inject or replay a stale/tampered branch or worktree path.
    If the resolved option's proposed worktree does not exist yet, this
    provisions it (the one case Quick Start creates a worktree on the
    operator's behalf) before creating and starting the Runbook — this
    still happens even when ``dry_run=True``, since provisioning is a real,
    idempotent, already-safety-checked git operation, not the thing
    ``dry_run`` exists to avoid. ``dry_run`` passes straight through to
    ``start_runbook``, whose own guarantee is narrower than "zero writes":
    it replaces the real worker CLI with a cheap placeholder subprocess and
    still performs the same Task/Runbook state writes and ``git`` reads an
    ordinary start does (Grok Build review, issue #97, corrected this
    docstring's earlier overclaim) — used by tests/tooling, never sent by
    the production UI.
    """

    option = resolve_quickstart_option(repo_root, key, project=project, state=state, registry=registry)
    if option.action != "prepare":
        raise QuickStartError(f"quickstart option {key!r} must be configured in Advanced Settings")
    if option.unavailable_reason is not None:
        raise QuickStartError(option.unavailable_reason)
    # ENG-AGENT-10: intake ownership is checked and durably claimed before
    # the only provisioning mutation this path can perform.
    from .intake import IntakeCollision, check_and_claim

    # ``issue_reference`` here is the same internal ledger/task identity as
    # ``task_id`` (never a real GitHub issue number for this option), so it
    # must use the identical "task:<id>" owner format every other admission
    # path defaults to for that same stable id -- otherwise this legitimate
    # same-owner retry looks like a different owner and is wrongly blocked.
    stable_id = option.task_id or option.source_ref.split(maxsplit=1)[0]
    owner_ref = f"task:{option.issue_reference}" if option.issue_reference else f"task:{stable_id}"
    try:
        check_and_claim(
            state=state,
            repo_root=repo_root,
            task_ref=option.task_id or option.source_ref,
            owner_ref=owner_ref,
            source=f"quickstart:{key}",
            project_id=project_id,
        )
    except IntakeCollision as exc:
        state.record_dispatch_decision(
            task_id=f"quickstart:{key}",
            stable_task_id=stable_id,
            owner_ref=owner_ref,
            outcome="BLOCKED",
            reason=str(exc),
            project_id=project_id,
        )
        raise QuickStartError(str(exc)) from None
    # Provisioning triggers on "no existing dedicated worktree for this
    # task" (`continues_existing_worktree=False`), never on `unavailable_reason`
    # — that field is reserved for "no eligible task at all" and must stay
    # unset (Start enabled) for exactly the case being provisioned here.
    if (
        option.unavailable_reason is None
        and option.branch
        and option.worktree
        and not option.continues_existing_worktree
    ):
        try:
            provision_worktree(repo_root=repo_root, worktree=option.worktree, branch=option.branch)
        except ProvisioningError as exc:
            raise QuickStartError(f"could not provision a worktree for {key!r}: {exc}") from None
        option = resolve_quickstart_option(repo_root, key, project=project, state=state, registry=registry)

    from .runbooks import RunbookError, create_runbook, start_runbook

    try:
        runbook = create_runbook(
            state=state,
            registry=registry,
            name=option.title,
            preset=option.preset,
            source_ref=option.source_ref,
            branch=option.branch,
            worktree=option.worktree,
            objective=option.objective,
            parent_worker=option.parent_worker,
            duration_minutes=option.duration_minutes,
            project_id=project_id,
            permission_profile=option.permission_profile,
            stop_conditions=list(option.stop_conditions),
            codex_policy=option.codex_policy,
            codex_auto_eligible=option.codex_auto_eligible,
            max_codex_invocations=option.max_codex_invocations,
        )
        return start_runbook(
            state=state,
            registry=registry,
            supervisor=supervisor,
            repo_root=repo_root,
            runbook_id=runbook.id,
            dry_run=dry_run,
            scheduler=scheduler,
        )
    except RunbookError as exc:
        raise QuickStartError(str(exc)) from None
