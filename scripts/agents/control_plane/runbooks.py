"""ENG-AGENT-02-S5 (issue #93): durable Runbooks — bounded unattended development sessions.

A Runbook never invents a second scheduler or provider registry. Starting one
creates exactly one ``Task`` (``launch_mode=LAUNCH_SESSION``, see
``supervisor.py``) that the existing ``Scheduler``/``Supervisor`` own exactly
like every other task: same concurrency slots, same write-lock enforcement,
same recovery/reconciliation, same audit trail under ``.agent-output/``. This
module only adds the durable Runbook record, its five saved presets, the
constructed session prompt, pre-flight branch/worktree validation, and
deadline-aware status reconciliation on top of that unchanged foundation.
"""

from __future__ import annotations

import datetime as _dt
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..policy import compose_policy_bundle, validate_policy_preservation
from ..redaction import redact_text
from ..registry import Registry, RegistryError
from ..validation import TASK_ID_RE, validate_task_id
from .acceptance import (
    advance_acceptance_pipeline,
    check_runtime_freshness,
    is_acceptance_applicable,
    start_acceptance,
)
from .advancement import advance_after_success
from .dispatch import managed_admit
from .models import (
    DEFAULT_SAFETY_PROFILE,
    DEFAULT_STOP_CONDITIONS,
    KIND_WRITE,
    LAUNCH_SESSION,
    PERMISSION_REPO_CONFIGURED_AUTO,
    PERMISSION_STANDARD,
    RUNBOOK_ACCEPTANCE_IN_PROGRESS_STATES,
    RUNBOOK_ACCEPTANCE_PENDING,
    RUNBOOK_BLOCKED,
    RUNBOOK_CANCELLED,
    RUNBOOK_DEADLINE_REACHED,
    RUNBOOK_DRAFT,
    RUNBOOK_FAILED,
    RUNBOOK_IMPLEMENTATION_COMPLETE,
    RUNBOOK_LAUNCHED_STATES,
    RUNBOOK_OWNER_ACTION_REQUIRED,
    RUNBOOK_PAUSED,
    RUNBOOK_RUNNING,
    RUNBOOK_STOPPING,
    RUNBOOK_SUCCEEDED,
    RUNBOOK_TERMINAL_STATES,
    TASK_CANCELLED,
    TASK_FAILED,
    TASK_PAUSED,
    TASK_PENDING,
    TASK_QUEUED,
    TASK_RUNNING,
    TASK_SUCCEEDED,
    Runbook,
    Task,
    WorktreeRecord,
    utc_now_iso,
)
from .provider_state import failure_attribution
from .recovery import discover_git_worktrees, pid_is_alive
from .state import State
from .usage_policy import (
    CODEX_POLICIES,
    build_context_manifest,
    classify_failure,
    classify_task,
    codex_allowed,
    new_usage_record,
    should_escalate,
)

REPORTS_DIRNAME = "reports"
ALLOWED_DURATION_MINUTES = (120, 240, 360, 480)
MIN_DURATION_MINUTES = 30
MAX_DURATION_MINUTES = 720


class RunbookError(ValueError):
    pass


@dataclass(frozen=True)
class RunbookPreset:
    key: str
    name: str
    description: str
    objective_template: str
    role: str
    default_parent_worker: str
    default_duration_minutes: int
    phases: tuple[str, ...]
    permission_profile: str = PERMISSION_STANDARD
    checkpoint_policy: str = "after_each_green_slice"
    writes_code: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "description": self.description,
            "objective_template": self.objective_template,
            "role": self.role,
            "default_parent_worker": self.default_parent_worker,
            "default_duration_minutes": self.default_duration_minutes,
            "phases": list(self.phases),
            "permission_profile": self.permission_profile,
            "checkpoint_policy": self.checkpoint_policy,
            "writes_code": self.writes_code,
        }


PRESETS: dict[str, RunbookPreset] = {
    p.key: p
    for p in (
        RunbookPreset(
            key="overnight-development",
            name="Overnight Development",
            description=(
                "Bounded, unattended multi-hour development run: bootstrap from repository truth, "
                "finish the referenced work end to end, verify locally, checkpoint continuously, and "
                "leave a morning report."
            ),
            objective_template=(
                "Continue {source_ref} from current repository truth and the explicitly composed role/workflow/"
                "provider policy bundle. Inspect git status and relevant maintained task/program contracts before "
                "assuming anything is stale. Implement the "
                "referenced scope completely and professionally, following existing architecture and "
                "conventions. Work autonomously; use the safest reversible repository-compliant default "
                "instead of stopping for routine questions."
            ),
            role="primary-implementation",
            default_parent_worker="claude-code",
            default_duration_minutes=480,
            phases=("Bootstrap", "Implement", "Test", "Review", "Checkpoint", "Report"),
            permission_profile=PERMISSION_REPO_CONFIGURED_AUTO,
        ),
        RunbookPreset(
            key="finish-pr",
            name="Finish PR",
            description="Bring an already-open, mostly-done pull request to locally review-ready quality.",
            objective_template=(
                "Bring {source_ref} to locally review-ready quality. Verify current PR state and diff "
                "before assuming what remains. Close every real gap against its stated scope/acceptance "
                "criteria, run focused verification after each fix, and checkpoint continuously."
            ),
            role="primary-implementation",
            default_parent_worker="claude-code",
            default_duration_minutes=240,
            phases=("Verify current state", "Close gaps", "Test", "Checkpoint", "Report"),
        ),
        RunbookPreset(
            key="test-fix",
            name="Test & Fix",
            description="Run the relevant test suite(s), diagnose real failures, and fix root causes only.",
            objective_template=(
                "Run the test suite(s) relevant to {source_ref}, diagnose any failures, and fix root "
                "causes. Do not weaken, skip, delete, or xfail a legitimate test to obtain a green run. "
                "Do not add unrelated features or refactors while fixing failures."
            ),
            role="primary-implementation",
            default_parent_worker="claude-code",
            default_duration_minutes=120,
            phases=("Run tests", "Diagnose failures", "Fix", "Re-verify", "Checkpoint", "Report"),
        ),
        RunbookPreset(
            key="ui-polish",
            name="UI Polish",
            description="Visual-fidelity, responsive, and accessibility polish pass against an existing target.",
            objective_template=(
                "Perform a visual-fidelity, responsive (desktop + mobile), and accessibility polish pass "
                "on {source_ref} against its existing approved reference/spec. Fix real discrepancies and "
                "defects only; do not redesign arbitrarily. Capture before/after screenshot evidence."
            ),
            role="primary-implementation",
            default_parent_worker="claude-code",
            default_duration_minutes=240,
            phases=(
                "Audit",
                "Fix",
                "Responsive pass",
                "Accessibility pass",
                "Screenshot evidence",
                "Checkpoint",
                "Report",
            ),
        ),
        RunbookPreset(
            key="review-only",
            name="Review Only",
            description="Independent, bounded, read-only review. Never writes, commits, or pushes.",
            objective_template=(
                "Independently review {source_ref} (diff/branch/PR) for correctness regressions, "
                "accessibility, responsive/layout defects, data-honesty violations, security/secret "
                "exposure, spend/provider safety, test gaps, and documentation drift. Read-only: never "
                "edit, commit, or push."
            ),
            role="diff-review",
            default_parent_worker="grok-build-review",
            default_duration_minutes=120,
            phases=("Read diff", "Identify findings", "Report"),
            checkpoint_policy="none",
            writes_code=False,
        ),
    )
}


def list_presets() -> list[dict[str, Any]]:
    """Zero-AI, static preset catalog for the Control Center's Runs UI."""

    return [PRESETS[key].as_dict() for key in sorted(PRESETS)]


def get_preset(key: str) -> RunbookPreset:
    try:
        return PRESETS[key]
    except KeyError:
        raise RunbookError(f"unknown preset {key!r}; known presets: {', '.join(sorted(PRESETS))}") from None


def _new_id(prefix: str = "RB") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def validate_duration_minutes(minutes: int) -> int:
    if not isinstance(minutes, int) or isinstance(minutes, bool):
        raise RunbookError("duration_minutes must be an integer number of minutes")
    if minutes < MIN_DURATION_MINUTES or minutes > MAX_DURATION_MINUTES:
        raise RunbookError(
            f"duration_minutes must be between {MIN_DURATION_MINUTES} and {MAX_DURATION_MINUTES} "
            f"(canonical choices: {', '.join(str(m) for m in ALLOWED_DURATION_MINUTES)})"
        )
    return minutes


def validate_target(
    *, repo_root: Path, state: State, branch: str, worktree: str, exclude_runbook_id: str | None = None
) -> None:
    """Pre-flight branch/worktree validation. Raises :class:`RunbookError` on any problem.

    This is a *pre-flight* check for the UI; the authoritative enforcement is
    still ``runner.assert_write_safety``/``write_lock`` at actual launch time
    (defense in depth, same pattern ``supervisor.launch_task`` already uses
    for ordinary tasks).
    """

    worktree_path = Path(worktree)
    if not worktree_path.is_absolute():
        raise RunbookError(f"worktree {worktree!r} must be an absolute path")
    if not worktree_path.exists():
        raise RunbookError(f"worktree {worktree!r} does not exist; create it with 'git worktree add' first")

    discovered = {Path(w.path).resolve(): w for w in discover_git_worktrees(repo_root)}
    match = discovered.get(worktree_path.resolve())
    if match is None:
        raise RunbookError(f"{worktree!r} is not a registered git worktree of this repository")
    if match.branch and branch and match.branch not in (branch, f"refs/heads/{branch}"):
        raise RunbookError(f"worktree {worktree!r} is checked out to {match.branch!r}, not the requested {branch!r}")

    # One write-capable agent per worktree: refuse a second concurrently-launched
    # runbook/task targeting the same worktree.
    for other in state.list_runbooks():
        if other.id == exclude_runbook_id:
            continue
        # ENG-AGENT-13 independent-review finding (round 3): the acceptance
        # pipeline (git add -A, run_gate, commit, push) actively mutates this
        # worktree while the runbook sits at IMPLEMENTATION_COMPLETE/
        # ACCEPTANCE_PENDING -- the one-writer-per-checkout rule must cover
        # that whole window, not just RUNNING/PAUSED/STOPPING.
        if other.status in (
            RUNBOOK_RUNNING, RUNBOOK_PAUSED, RUNBOOK_STOPPING, *RUNBOOK_ACCEPTANCE_IN_PROGRESS_STATES,
        ) and Path(
            other.worktree
        ).resolve() == worktree_path.resolve():
            raise RunbookError(
                f"worktree {worktree!r} already has an active runbook ({other.id}, status={other.status})"
            )
    for task in state.list_tasks(state=TASK_RUNNING):
        if task.worktree and Path(task.worktree).resolve() == worktree_path.resolve():
            raise RunbookError(f"worktree {worktree!r} already has a running task ({task.id})")


def _validate_permission_profile_for_worker(permission_profile: str, worker, *, worker_name: str) -> None:
    """Shared gate: the unattended profile is only ever valid for a write-capable

    write-capable worker that actually declares an invocation for it. Called at
    every point a Runbook's ``(permission_profile, parent_worker)`` pair can
    change — ``create_runbook``, ``update_runbook`` (a later edit can retarget
    either field independently), and ``start_runbook`` (defense in depth
    immediately before a Task/subprocess is created, in case persisted state
    was ever written by a path that skipped the first two) — so no single
    missed call site can leave an incompatible pair on a launchable Runbook.
    """

    from .models import RUNBOOK_PERMISSION_PROFILES

    if permission_profile not in RUNBOOK_PERMISSION_PROFILES:
        raise RunbookError(f"unknown permission_profile {permission_profile!r}")
    if permission_profile != PERMISSION_REPO_CONFIGURED_AUTO:
        return
    if worker.is_read_only:
        raise RunbookError("the repo_configured_auto unattended profile cannot be used with a read-only worker")
    if not worker.supports_permission_profile(permission_profile):
        raise RunbookError(f"worker {worker_name!r} does not declare a {permission_profile!r} invocation")


def stable_session_task_ref(runbook: Runbook) -> str:
    """Return a validator-safe evidence key without losing the display source."""

    candidate = (runbook.source_ref or "").split(maxsplit=1)[0].rstrip(":;,.()")
    if TASK_ID_RE.fullmatch(candidate) and len(candidate) <= 64:
        return validate_task_id(candidate)
    return validate_task_id(runbook.id.upper())


def eligible_retry_workers(*, state: State, registry: Registry, runbook: Runbook) -> list[dict[str, str]]:
    """List configured/routable workers that can safely resume this runbook."""

    preset = PRESETS.get(runbook.preset)
    role = preset.role if preset else "primary-implementation"
    failed_worker = None
    if runbook.task_id:
        failed_task = state.get_task(runbook.task_id)
        failed_worker = failed_task.failed_worker_id if failed_task else None
    candidates: list[dict[str, str]] = []
    route = runbook.worker_routes.get(role, list(registry.routes.get(role, ())))
    for name in route:
        if name == failed_worker:
            continue
        worker = registry.get(name)
        provider = state.get_provider_state(name)
        if role not in worker.roles or ((preset is None or preset.writes_code) and not worker.is_write_capable):
            continue
        if runbook.permission_profile != PERMISSION_STANDARD and not worker.supports_permission_profile(
            runbook.permission_profile
        ):
            continue
        if not provider or not provider.configured or provider.state not in {"AVAILABLE", "BUSY"}:
            continue
        if worker.allow_api_billing:
            continue
        if worker.provider == "OpenAI" or name.startswith("codex"):
            usage = state.get_usage_governance(runbook.id) or {}
            allowed, _ = codex_allowed(
                policy=runbook.codex_policy,
                classification=str(usage.get("classification") or classify_task(role=role)),
                auto_eligible=runbook.codex_auto_eligible,
                invocations=int(usage.get("codex_invocations", 0)),
                max_invocations=runbook.max_codex_invocations,
            )
            if not allowed:
                continue
        candidates.append(
            {
                "name": name,
                "display_name": str(worker.raw.get("display_name", name)),
                "execution_system": worker.execution_system,
                "provider": worker.provider,
                "model": worker.default_model,
                "route": worker.cost_class,
            }
        )
    return candidates


def _attempted_workers(usage: dict[str, Any], task: Task) -> set[str]:
    """Return every route already launched or reserved for this durable run."""

    attempted = {
        str(item.get("worker"))
        for item in usage.get("route_history", [])
        if isinstance(item, dict) and item.get("worker")
    }
    if task.worker:
        attempted.add(task.worker)
    if task.failed_worker_id:
        attempted.add(task.failed_worker_id)
    return attempted


_PR_REFERENCE_RE = re.compile(r"^(?:pr:)?\d+$|^https://github\.com/[^/]+/[^/]+/pull/\d+/?$", re.IGNORECASE)


def _looks_like_pr_reference(worktree: str) -> bool:
    """Distinguish a PR reference ("pr:139", "139", a PR URL) from a real path.

    A real worktree is always an absolute filesystem path (``validate_target``
    already requires this); none of those ever match this pattern, so this
    can never misfire on a genuine worktree path.
    """

    return bool(_PR_REFERENCE_RE.match(worktree.strip()))


def _provision_and_register_review_worktree(
    *, state: State, repo_root: Path, pr_reference: str, github_remote: str | None = None,
) -> tuple[str, str]:
    """Resolve/provision an exact-head review checkout for ``pr_reference``, register

    it in durable state, and return ``(worktree_path, branch)`` for the caller
    to store on the new Runbook -- see ``provisioning.provision_review_worktree``
    for the actual git-level reuse/creation logic and its safety guarantees
    (never main/DevCP/a product worktree/another task's checkout as an
    implicit substitute).
    """

    from .acceptance import GITHUB_REPO
    from .provisioning import ProvisioningError, provision_review_worktree

    existing = [
        (record.path, record.review_head_sha)
        for record in state.list_worktrees()
        if record.review_head_sha
    ]
    try:
        provisioned = provision_review_worktree(
            repo_root=repo_root,
            repository=github_remote or GITHUB_REPO,
            pr_reference=pr_reference,
            existing=existing,
        )
    except ProvisioningError as exc:
        raise RunbookError(f"could not provision a review checkout for {pr_reference!r}: {exc}") from None

    record = WorktreeRecord(
        path=provisioned.path,
        branch=None,  # detached -- discover_git_worktrees() correctly reports no branch for this checkout
        managed=True,
        review_repository=provisioned.repository,
        review_pr=provisioned.pr_number,
        review_head_sha=provisioned.head_sha,
    )
    state.upsert_worktree(record)
    # branch is informational only here (validate_target skips the branch
    # match check entirely for a worktree whose discovered branch is None,
    # i.e. detached) -- store the PR's real branch name for display.
    return provisioned.path, provisioned.branch


def create_runbook(
    *,
    state: State,
    registry: Registry,
    name: str,
    preset: str,
    source_ref: str,
    branch: str,
    worktree: str,
    objective: str | None = None,
    parent_worker: str | None = None,
    duration_minutes: int | None = None,
    worker_routes: dict[str, list[str]] | None = None,
    concurrency: dict[str, int] | None = None,
    safety_profile: dict[str, bool] | None = None,
    checkpoint_policy: str | None = None,
    stop_conditions: list[str] | None = None,
    permission_profile: str | None = None,
    codex_policy: str = "conserve",
    codex_auto_eligible: bool = False,
    max_codex_invocations: int = 1,
    repo_root: Path | None = None,
    project_id: str | None = None,
) -> Runbook:
    """Create a new ``DRAFT`` runbook from a preset. Populates but never launches it."""

    preset_def = get_preset(preset)
    if preset_def.role == "diff-review" and repo_root is not None and _looks_like_pr_reference(worktree):
        # ENG-AGENT-14 (issue #140): "Review Only" + a PR reference (not an
        # existing worktree path) auto-provisions an isolated, exact-head,
        # detached review checkout instead of requiring the operator to have
        # already created and registered one by hand.
        github_remote = None
        if project_id:
            row = state.get_project(project_id)
            if row:
                github_remote = row.get("github_remote")
        worktree, branch = _provision_and_register_review_worktree(
            state=state, repo_root=repo_root, pr_reference=worktree, github_remote=github_remote,
        )
    pinned_implementation = (worker_routes or {}).get(preset_def.role)
    worker_name = parent_worker or (pinned_implementation[0] if pinned_implementation else preset_def.default_parent_worker)
    if pinned_implementation and worker_name not in pinned_implementation:
        raise RunbookError(
            f"parent worker {worker_name!r} is outside the pinned {preset_def.role!r} route"
        )
    try:
        worker = registry.get(worker_name)
    except RegistryError as exc:
        raise RunbookError(str(exc)) from None
    if preset_def.role not in worker.roles:
        raise RunbookError(f"worker {worker_name!r} does not declare role {preset_def.role!r}")
    if preset_def.writes_code and not worker.is_write_capable:
        raise RunbookError(f"preset {preset!r} requires a write-capable worker; {worker_name!r} is read-only")

    minutes = validate_duration_minutes(duration_minutes or preset_def.default_duration_minutes)
    resolved_objective = (objective or preset_def.objective_template.format(source_ref=source_ref)).strip()
    if not resolved_objective:
        raise RunbookError("objective must not be empty")

    profile = dict(DEFAULT_SAFETY_PROFILE)
    if safety_profile:
        unknown = set(safety_profile) - set(DEFAULT_SAFETY_PROFILE)
        if unknown:
            raise RunbookError(f"unknown safety_profile key(s): {', '.join(sorted(unknown))}")
        profile.update({k: bool(v) for k, v in safety_profile.items()})
        if any(not v for v in profile.values()):
            disabled = ", ".join(k for k, v in profile.items() if not v)
            raise RunbookError(
                f"safety profile cannot relax {disabled}: every prohibition is mandatory for a runbook"
            )

    resolved_stop_conditions = tuple(stop_conditions) if stop_conditions else DEFAULT_STOP_CONDITIONS
    from .models import RUNBOOK_STOP_CONDITIONS as _VALID_STOPS

    unknown_stops = set(resolved_stop_conditions) - _VALID_STOPS
    if unknown_stops:
        raise RunbookError(f"unknown stop condition(s): {', '.join(sorted(unknown_stops))}")

    resolved_permission = permission_profile or preset_def.permission_profile
    _validate_permission_profile_for_worker(resolved_permission, worker, worker_name=worker_name)
    if codex_policy not in CODEX_POLICIES:
        raise RunbookError(f"unknown codex_policy {codex_policy!r}")
    if not isinstance(max_codex_invocations, int) or isinstance(max_codex_invocations, bool) or max_codex_invocations < 0:
        raise RunbookError("max_codex_invocations must be a non-negative integer")

    resolved_routes: dict[str, list[str]] = {}
    for role, names in (worker_routes or {}).items():
        try:
            allowed = set(registry.route(role))
        except RegistryError as exc:
            raise RunbookError(str(exc)) from None
        invalid = set(names) - allowed
        if invalid:
            raise RunbookError(f"role {role!r} does not permit worker(s): {', '.join(sorted(invalid))}")
        resolved_routes[role] = list(names)

    runbook = Runbook(
        id=_new_id(),
        name=name.strip() or preset_def.name,
        preset=preset,
        objective=resolved_objective,
        source_ref=source_ref,
        branch=branch,
        worktree=worktree,
        parent_worker=worker_name,
        max_duration_minutes=minutes,
        worker_routes=resolved_routes,
        concurrency=dict(concurrency or {"max_concurrent_write_workers": 1}),
        safety_profile=profile,
        checkpoint_policy=checkpoint_policy or preset_def.checkpoint_policy,
        stop_conditions=resolved_stop_conditions,
        permission_profile=resolved_permission,
        codex_policy=codex_policy,
        codex_auto_eligible=bool(codex_auto_eligible),
        max_codex_invocations=max_codex_invocations,
        phases=preset_def.phases,
        status=RUNBOOK_DRAFT,
        # ENG-CP-03 (issue #165): the managed project this runbook (and its
        # acceptance state) belongs to, stamped at creation so it stays visible
        # under the project it was created for.
        project_id=project_id,
    )
    state.upsert_runbook(runbook)
    state.record_event(
        category="runbook",
        message=f"runbook {runbook.id} created from preset {preset!r} (DRAFT)",
        project_id=project_id,
    )
    return runbook


def update_runbook(*, state: State, registry: Registry, runbook_id: str, **fields: Any) -> Runbook:
    runbook = state.get_runbook(runbook_id)
    if runbook is None:
        raise RunbookError(f"unknown runbook {runbook_id!r}")
    if runbook.status in RUNBOOK_LAUNCHED_STATES:
        raise RunbookError(f"runbook {runbook_id!r} is {runbook.status}; only a DRAFT runbook can be edited")

    if "name" in fields and fields["name"]:
        runbook.name = str(fields["name"]).strip()
    if "objective" in fields and fields["objective"]:
        runbook.objective = str(fields["objective"]).strip()
    if "duration_minutes" in fields and fields["duration_minutes"] is not None:
        runbook.max_duration_minutes = validate_duration_minutes(int(fields["duration_minutes"]))
    if "branch" in fields and fields["branch"]:
        runbook.branch = str(fields["branch"])
    if "worktree" in fields and fields["worktree"]:
        runbook.worktree = str(fields["worktree"])
    if "parent_worker" in fields and fields["parent_worker"]:
        try:
            registry.get(str(fields["parent_worker"]))
        except RegistryError as exc:
            raise RunbookError(str(exc)) from None
        runbook.parent_worker = str(fields["parent_worker"])
    if "checkpoint_policy" in fields and fields["checkpoint_policy"]:
        runbook.checkpoint_policy = str(fields["checkpoint_policy"])
    if "permission_profile" in fields and fields["permission_profile"]:
        from .models import RUNBOOK_PERMISSION_PROFILES

        value = str(fields["permission_profile"])
        if value not in RUNBOOK_PERMISSION_PROFILES:
            raise RunbookError(f"unknown permission_profile {value!r}")
        runbook.permission_profile = value
    if "codex_policy" in fields and fields["codex_policy"]:
        value = str(fields["codex_policy"])
        if value not in CODEX_POLICIES:
            raise RunbookError(f"unknown codex_policy {value!r}")
        runbook.codex_policy = value
    if "codex_auto_eligible" in fields:
        runbook.codex_auto_eligible = bool(fields["codex_auto_eligible"])
    if "max_codex_invocations" in fields and fields["max_codex_invocations"] is not None:
        value = fields["max_codex_invocations"]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise RunbookError("max_codex_invocations must be a non-negative integer")
        runbook.max_codex_invocations = value
    if "stop_conditions" in fields and fields["stop_conditions"] is not None:
        from .models import RUNBOOK_STOP_CONDITIONS as _VALID_STOPS

        values = tuple(fields["stop_conditions"])
        unknown = set(values) - _VALID_STOPS
        if unknown:
            raise RunbookError(f"unknown stop condition(s): {', '.join(sorted(unknown))}")
        runbook.stop_conditions = values

    # Re-validate the (permission_profile, parent_worker) pair against every
    # edit, not only the field that happened to change this call: either an
    # unrelated `parent_worker` swap or an unrelated `permission_profile` edit
    # can leave the other one incompatible (Grok Build review, issue #94).
    try:
        current_worker = registry.get(runbook.parent_worker)
    except RegistryError as exc:
        raise RunbookError(str(exc)) from None
    _validate_permission_profile_for_worker(runbook.permission_profile, current_worker, worker_name=runbook.parent_worker)

    state.upsert_runbook(runbook)
    usage = state.get_usage_governance(runbook.id)
    if usage:
        usage["codex_policy"] = runbook.codex_policy
        usage["codex_auto_eligible"] = runbook.codex_auto_eligible
        usage["max_codex_invocations"] = runbook.max_codex_invocations
        state.upsert_usage_governance(usage)
    state.record_event(category="runbook", task_id=runbook.task_id, message=f"runbook {runbook.id} updated")
    return runbook


def build_session_prompt(runbook: Runbook, *, registry: Registry) -> str:
    """Construct the full unattended-session prompt embedded in the launched worker call.

    Mirrors the structure of a maintainer-authored overnight orchestration
    prompt: bootstrap instructions, the editable objective, explicit worker
    routing, the mandatory safety profile, stop conditions, duration/deadline,
    checkpoint policy, and the final-report requirement. The launched worker
    (a parent orchestrator, exactly like a manually-run overnight Claude
    session) remains responsible for repository policy regardless of any
    local permission profile.
    """

    preset_def = PRESETS.get(runbook.preset)
    forbidden_lines = "\n".join(f"- {key.replace('_', ' ')}" for key, v in runbook.safety_profile.items() if v)
    routes_lines = (
        "\n".join(f"- {role}: {', '.join(names)}" for role, names in runbook.worker_routes.items())
        or "- use the existing scripts/agents/workers.json routing (no runbook-specific override)"
    )
    phases_lines = "\n".join(f"{i + 1}. {phase}" for i, phase in enumerate(runbook.phases)) or "(single continuous phase)"

    return f"""You are the parent orchestrator for durable runbook {runbook.id} ("{runbook.name}"),
preset "{preset_def.name if preset_def else runbook.preset}", running under OctAges' local
orchestrator daemon (ENG-AGENT-02-S5, issue #93). This is an UNATTENDED, BOUNDED session.

BOOTSTRAP FROM CURRENT REPOSITORY TRUTH
The invocation prepends the deterministic canonical AGENTS/core/role/workflow/provider policy bundle.
Read only relevant maintained task/program/ADR documents for {runbook.source_ref}; the roadmap is required
only when product placement/status matters. Inspect git status, branch, and recent commits. Repository code
and maintained canonical documentation override this prompt.

WORKTREE
Branch: {runbook.branch}
Worktree: {runbook.worktree}
This is your dedicated worktree. Do not modify any other worktree. One write-capable agent per
worktree; if a second write-capable worker is required, use a separate dedicated worktree.

OBJECTIVE
{runbook.objective}

FALLBACK / RECOVERY
{runbook.recovery_note or "No fallback; initial selected provider."}

PHASES (informational checklist; adapt order to real repository state)
{phases_lines}

WORKER ROUTING
{routes_lines}
Prefer the least expensive capable worker/model for each role, escalating only when evidence shows
a cheaper tier is insufficient, per AGENTS.md's usage-aware model-escalation policy.
Codex policy: {runbook.codex_policy}. Automatic Codex eligibility: {runbook.codex_auto_eligible}.
Maximum Codex invocations for this run: {runbook.max_codex_invocations}. Quota, rate-limit, and
context-window failures must be handled by wait/compact/reroute and never by a stronger-model retry.

DURATION AND STOP CONDITIONS
Maximum duration: {runbook.max_duration_minutes} minutes from launch.
Stop when any of the following is true: {", ".join(runbook.stop_conditions)}.
When the deadline approaches, finish and checkpoint the current coherent action rather than
abruptly leaving work in a corrupted or half-finished state.

CHECKPOINT POLICY: {runbook.checkpoint_policy}
After every meaningful focused-test-green slice: run `git diff --check`, run the relevant focused
tests, reconcile any affected documentation, commit, and push to the normal feature branch above.
Never force-push. Never merge or enable auto-merge.

MANDATORY SAFETY PROFILE (non-negotiable; the launch's local permission mode never overrides this)
{forbidden_lines}
Provider enablement, CC-required, spend-limit, accounting, and production-readiness gates remain
authoritative regardless of this session's permission mode.

FINAL REPORT
Before finishing, write a concise final report (summary, files changed, tests/checks run, exact
commits/pushes, delegation actually performed, blockers/background waits, remaining defects, and
the exact next recommended action). Never claim a delegated worker ran unless existing audit
evidence (`.agent-output/`) proves it. This runbook's own record is {runbook.id}.

Proceed autonomously now.
"""


def start_runbook(
    *, state: State, registry: Registry, supervisor, repo_root: Path, runbook_id: str, dry_run: bool = False,
    scheduler=None,
) -> Runbook:
    runbook = state.get_runbook(runbook_id)
    if runbook is None:
        raise RunbookError(f"unknown runbook {runbook_id!r}")
    if runbook.status != RUNBOOK_DRAFT:
        raise RunbookError(f"runbook {runbook_id!r} is {runbook.status}; only a DRAFT runbook can be started")

    # Re-validate immediately before launch (defense in depth, Grok Build review issue #94):
    # catches any persisted (permission_profile, parent_worker) pair that reached this
    # point without going through create_runbook/update_runbook's own checks, e.g. a
    # direct state write. Never rely on the spawned subprocess to be the only backstop.
    try:
        launch_worker = registry.get(runbook.parent_worker)
    except RegistryError as exc:
        raise RunbookError(str(exc)) from None
    _validate_permission_profile_for_worker(runbook.permission_profile, launch_worker, worker_name=runbook.parent_worker)
    validate_target(repo_root=repo_root, state=state, branch=runbook.branch, worktree=runbook.worktree)
    if scheduler is None:
        from .scheduler import Scheduler

        scheduler = Scheduler()

    preset_def = PRESETS.get(runbook.preset)
    role = preset_def.role if preset_def else "primary-implementation"
    prompt_text = build_session_prompt(runbook, registry=registry)
    policy_bundle = compose_policy_bundle(
        root=Path(runbook.worktree),
        registry=registry,
        worker_name=runbook.parent_worker,
        route_role=role,
        acceptance_criteria=runbook.objective,
    )

    stable_ref = stable_session_task_ref(runbook)
    existing_claim = next(
        (item for item in state.list_task_identity_claims() if item["stable_task_id"] == stable_ref),
        None,
    )
    task = Task(
        id=f"{runbook.id}-session",
        task_ref=stable_ref,
        role=role,
        worker=runbook.parent_worker,
        kind=KIND_WRITE if (preset_def is None or preset_def.writes_code) else "read",
        state=TASK_PENDING,
        priority=10,
        worktree=runbook.worktree,
        command=(prompt_text,),
        launch_mode=LAUNCH_SESSION,
        timeout_seconds=runbook.max_duration_minutes * 60,
        runbook_id=runbook.id,
        permission_profile=runbook.permission_profile,
        owner_ref=str(existing_claim["owner_ref"]) if existing_claim else f"task:{stable_ref}",
        codex_policy=runbook.codex_policy,
        codex_auto_eligible=runbook.codex_auto_eligible,
        max_codex_invocations=runbook.max_codex_invocations,
        # ENG-CP-03: a runbook's session task always belongs to the same
        # managed project as the runbook that launched it.
        project_id=runbook.project_id,
    )
    classification = classify_task(role=role, changed_paths=())
    usage = new_usage_record(
        runbook_id=runbook.id,
        task_id=task.id,
        classification=classification,
        codex_policy=runbook.codex_policy,
        codex_auto_eligible=runbook.codex_auto_eligible,
        max_codex_invocations=runbook.max_codex_invocations,
        context_manifest={
            **build_context_manifest(
                paths=(
                    "AGENTS.md",
                    *policy_bundle.manifest["core_policies"],
                    policy_bundle.manifest["role_policy"],
                    policy_bundle.manifest["workflow_policy"],
                    policy_bundle.manifest["provider_policy"],
                ),
            ),
            "policy_manifest": policy_bundle.manifest,
        },
    )

    if launch_worker.provider == "OpenAI" or launch_worker.name.startswith("codex"):
        allowed, why = codex_allowed(
            policy=runbook.codex_policy,
            classification=str(usage["classification"]),
            auto_eligible=runbook.codex_auto_eligible,
            invocations=int(usage["codex_invocations"]),
            max_invocations=runbook.max_codex_invocations,
        )
        if not allowed:
            raise RunbookError(f"Codex launch blocked: {why}")
    else:
        why = f"explicit role-compatible {launch_worker.provider} route under {runbook.codex_policy} Codex policy"
    state.upsert_task(task)
    state.upsert_usage_governance(usage)

    provider = state.get_provider_state(runbook.parent_worker)
    if provider is not None and (not provider.configured or provider.state not in {"AVAILABLE", "BUSY"}):
        provider_detail = str(provider.reason or "").strip()
        reason = f"worker {runbook.parent_worker!r} is unavailable before launch: {provider.state}"
        if provider_detail and provider_detail != "AVAILABLE":
            reason += f"; {provider_detail}"
        reason = redact_text(reason)[:800]
        attribution = failure_attribution(
            worker=launch_worker,
            reason=reason,
            provider_state=provider.state,
        )
        task.state = TASK_FAILED
        task.result = "UNAVAILABLE"
        task.last_error = reason
        task.failed_worker_id = attribution["worker_id"]
        task.failure_execution_system = attribution["execution_system"]
        task.failure_provider = attribution["provider"]
        task.failure_model = attribution["model"]
        task.failure_category = attribution["category"]
        task.failure_reason_sanitized = attribution["reason"]
        task.failure_reset = attribution["reset"]
        task.failure_reset_source = attribution["reset_source"]
        task.fallback_automatic = None
        state.upsert_task(task)
        usage["route_history"] = [{
            "role": role,
            "worker": launch_worker.name,
            "provider": launch_worker.provider,
            "model": launch_worker.default_model,
            "intensity": launch_worker.default_intensity,
            "reason": reason,
            "alternatives": [name for name in registry.route(role) if name != launch_worker.name],
            "status": "UNAVAILABLE",
            "automatic": False,
            "failure_category": attribution["category"],
            "failure_reason": attribution["reason"],
            "failure_provider": attribution["provider"],
            "failure_model": attribution["model"],
        }]
        state.upsert_usage_governance(usage)
        runbook.task_id = task.id
        runbook.status = RUNBOOK_FAILED
        runbook.started_at = utc_now_iso()
        runbook.ended_at = utc_now_iso()
        state.upsert_runbook(runbook)
        state.record_event(
            category="runbook",
            task_id=task.id,
            level="warning",
            message=f"runbook {runbook.id} preferred worker unavailable before launch: {reason}",
        )
        retried = automatic_fallback_runbook(
            state=state,
            registry=registry,
            supervisor=supervisor,
            repo_root=repo_root,
            runbook_id=runbook.id,
            dry_run=dry_run,
            scheduler=scheduler,
        )
        if retried is not None:
            return retried
        blocked = state.get_runbook(runbook.id) or runbook
        raise RunbookError(blocked.recovery_note or reason)

    admission = managed_admit(
        state=state,
        registry=registry,
        scheduler=scheduler,
        supervisor=supervisor,
        repo_root=repo_root,
        task=task,
        dry_run=dry_run,
    )
    launched = admission.task
    launch_worker = registry.get(launched.worker)
    why = launched.selected_worker_reason or why
    runbook.task_id = launched.id
    if launched.state == TASK_RUNNING:
        usage["route_history"] = [{
            "role": role,
            "worker": launch_worker.name,
            "provider": launch_worker.provider,
            "model": launch_worker.default_model,
            "intensity": launch_worker.default_intensity,
            "reason": why,
            "alternatives": [name for name in registry.route(role) if name != launch_worker.name],
            "status": "RUNNING",
            "automatic": False,
            "pid": launched.pid,
        }]
        if launch_worker.provider == "OpenAI" or launch_worker.name.startswith("codex"):
            usage["codex_invocations"] = int(usage["codex_invocations"]) + 1
        state.upsert_usage_governance(usage)
        runbook.status = RUNBOOK_RUNNING
        runbook.started_at = utc_now_iso()
        deadline = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(minutes=runbook.max_duration_minutes)
        runbook.deadline_at = deadline.isoformat(timespec="seconds")
        state.upsert_runbook(runbook)
        state.record_event(
            category="runbook",
            task_id=task.id,
            message=f"runbook {runbook.id} started (pid={launched.pid}, deadline={runbook.deadline_at})",
        )
    else:
        state.upsert_runbook(runbook)
        state.record_event(
            category="runbook",
            level="warning",
            task_id=task.id,
            message=f"runbook {runbook.id} failed to start: {launched.last_error or launched.state}",
        )
        raise RunbookError(f"could not start runbook: {launched.last_error or launched.state}")
    return runbook


def retry_acceptance(*, state: State, runbook_id: str) -> Runbook:
    """Resume a runbook halted at ``OWNER_ACTION_REQUIRED``/``BLOCKED`` (issue #138).

    Unlike ``retry_runbook`` this never relaunches the implementation worker:
    the underlying task is already ``TASK_SUCCEEDED`` and stays untouched.
    Only the acceptance stage that halted is re-attempted; earlier PASS/
    NOT_APPLICABLE stage evidence is reused, not recomputed.
    """

    from .acceptance import resume_acceptance

    runbook, task = _require_launched(state, runbook_id)
    resume_acceptance(runbook=runbook)
    state.upsert_runbook(runbook)
    state.record_event(
        category="runbook", task_id=task.id,
        message=f"runbook {runbook.id} acceptance stage {runbook.acceptance_stage!r} resumed by operator",
    )
    return runbook


def retry_runbook(
    *, state: State, registry: Registry, supervisor, repo_root: Path, runbook_id: str, worker_name: str,
    automatic: bool = False, dry_run: bool = False, scheduler=None,
) -> Runbook:
    """Retry one failed runbook in place with an explicitly selected worker.

    The Runbook row, underlying Task id, objective, branch and worktree are
    preserved. The prior sanitized reason is retained in ``recovery_note`` and
    the event log before the same Task row is reset and relaunched.
    """

    runbook, task = _require_launched(state, runbook_id)
    if runbook.status not in {RUNBOOK_FAILED, RUNBOOK_CANCELLED} or task.state not in {
        TASK_FAILED,
        TASK_CANCELLED,
    }:
        raise RunbookError(f"runbook {runbook_id!r} is not a terminal failed/cancelled run")
    if task.pid and pid_is_alive(task.pid):
        raise RunbookError(f"runbook {runbook_id!r} still has a live worker PID; refusing duplicate launch")

    preset = PRESETS.get(runbook.preset)
    role = preset.role if preset else task.role
    permitted = runbook.worker_routes.get(role)
    if permitted is not None and worker_name not in permitted:
        raise RunbookError(
            f"worker {worker_name!r} is outside this runbook's pinned {role!r} route; "
            "change the runbook explicitly before retrying"
        )
    try:
        worker = registry.get(worker_name)
    except RegistryError as exc:
        raise RunbookError(str(exc)) from None
    if role not in worker.roles:
        raise RunbookError(f"worker {worker_name!r} does not declare role {role!r}")
    if (preset is None or preset.writes_code) and not worker.is_write_capable:
        raise RunbookError(f"runbook {runbook_id!r} requires a write-capable implementation worker")
    _validate_permission_profile_for_worker(runbook.permission_profile, worker, worker_name=worker_name)
    reason = worker.availability_reason()
    if reason != "AVAILABLE":
        raise RunbookError(f"worker {worker_name!r} is not eligible: {reason}")
    provider = state.get_provider_state(worker_name)
    if provider is not None and (not provider.configured or provider.state not in {"AVAILABLE", "BUSY"}):
        raise RunbookError(f"worker {worker_name!r} is not routable: {provider.state}")

    validate_target(
        repo_root=repo_root,
        state=state,
        branch=runbook.branch,
        worktree=runbook.worktree,
        exclude_runbook_id=runbook.id,
    )
    previous_worker = task.worker
    previous_reason = redact_text(
        task.failure_reason_sanitized or task.last_error or "worker exited without a recorded reason"
    )[:800]
    failure_kind = classify_failure(previous_reason)
    if failure_kind == "safety-policy":
        raise RunbookError("safety/policy failure blocks retry; provider hopping is forbidden")
    if failure_kind == "context-limit":
        raise RunbookError("context limit requires context minimization before provider retry")
    usage = state.get_usage_governance(runbook.id) or new_usage_record(
        runbook_id=runbook.id, task_id=task.id, classification=classify_task(role=role),
        codex_policy=runbook.codex_policy, codex_auto_eligible=runbook.codex_auto_eligible,
        max_codex_invocations=runbook.max_codex_invocations,
    )
    stronger_tier = (worker.provider == "OpenAI" or worker_name.startswith("codex")) and not (
        registry.get(previous_worker).provider == "OpenAI" or previous_worker.startswith("codex")
    )
    recoverable = {"quota", "rate-limit", "auth-cli", "provider-outage"}
    if stronger_tier and failure_kind not in recoverable and usage.get("escalation_state") != "operator-override":
        escalate, escalation_reason = should_escalate(
            failure=failure_kind, attempts_at_tier=1, stronger_tier_available=True
        )
        if not escalate:
            raise RunbookError(
                f"stronger-model retry blocked: {escalation_reason}; "
                "use an audited premium override if intentional"
            )
    if worker.provider == "OpenAI" or worker_name.startswith("codex"):
        allowed, why = codex_allowed(
            policy=runbook.codex_policy,
            classification=str(usage["classification"]),
            auto_eligible=runbook.codex_auto_eligible,
            invocations=int(usage["codex_invocations"]),
            max_invocations=runbook.max_codex_invocations,
        )
        if not allowed:
            raise RunbookError(f"Codex retry blocked: {why}; use an audited premium override if intentional")
    previous_policy = (usage.get("context_manifest") or {}).get("policy_manifest")
    if not previous_policy:
        previous_policy = compose_policy_bundle(
            root=Path(runbook.worktree),
            registry=registry,
            worker_name=previous_worker,
            route_role=role,
            acceptance_criteria=runbook.objective,
        ).manifest
    replacement_policy = compose_policy_bundle(
        root=Path(runbook.worktree),
        registry=registry,
        worker_name=worker_name,
        route_role=role,
        fallback_reason=f"{failure_kind}: {previous_reason}",
        acceptance_criteria=runbook.objective,
    ).manifest
    validate_policy_preservation(previous_policy, replacement_policy)
    runbook.parent_worker = worker_name
    runbook.recovery_note = f"Previous attempt with {previous_worker} failed: {previous_reason}. Retrying with {worker_name}."
    runbook.status = RUNBOOK_RUNNING
    runbook.ended_at = None
    state.upsert_runbook(runbook)
    task.task_ref = stable_session_task_ref(runbook)
    task.role = role
    task.worker = worker_name
    task.kind = KIND_WRITE if (preset is None or preset.writes_code) else "read"
    task.state = TASK_PENDING
    task.pid = None
    task.result = None
    task.last_error = None
    task.fallback_reason = f"{failure_kind}: {previous_reason}"
    task.fallback_selected_worker = worker_name
    task.fallback_automatic = automatic
    task.command = (build_session_prompt(runbook, registry=registry),)
    state.upsert_task(task)
    state.record_event(
        category="runbook",
        task_id=task.id,
        level="warning",
        message=(
            f"runbook {runbook.id} retry requested: {previous_worker} -> {worker_name}; "
            f"previous sanitized reason: {previous_reason}"
        ),
    )
    usage["escalation_state"] = "rerouted"
    usage["escalation_reason"] = previous_reason
    usage["escalation_history"] = [*usage.get("escalation_history", []), {
        "from": previous_worker, "to": worker_name, "failure": failure_kind, "reason": previous_reason,
    }]
    usage["route_history"] = [*usage.get("route_history", []), {
        "role": role,
        "worker": worker_name,
        "provider": worker.provider,
        "model": worker.default_model,
        "intensity": worker.default_intensity,
        "reason": f"{'automatic' if automatic else 'operator'} retry after {failure_kind}",
        "alternatives": [name for name in registry.route(role) if name != worker_name],
        "status": "STARTING",
        "automatic": automatic,
        "from_worker": previous_worker,
        "failure_category": failure_kind.upper().replace("-", "_"),
    }]
    context_manifest = dict(usage.get("context_manifest") or {})
    context_manifest["policy_manifest"] = replacement_policy
    usage["context_manifest"] = context_manifest
    if worker.provider == "OpenAI" or worker_name.startswith("codex"):
        # Reserve the bounded invocation before process launch. A crash may
        # conservatively consume a slot, but can never duplicate an uncounted
        # Codex attempt after restart.
        usage["codex_invocations"] = int(usage["codex_invocations"]) + 1
    state.upsert_usage_governance(usage)

    if scheduler is None:
        from .scheduler import Scheduler

        scheduler = Scheduler()
    admission = managed_admit(
        state=state,
        registry=registry,
        scheduler=scheduler,
        supervisor=supervisor,
        repo_root=repo_root,
        task=task,
        dry_run=dry_run,
    )
    launched = admission.task
    if launched.state != TASK_RUNNING:
        route_history = list(usage.get("route_history", []))
        if route_history:
            route_history[-1] = {**route_history[-1], "status": "BLOCKED", "reason": launched.last_error or launched.state}
            usage["route_history"] = route_history
            state.upsert_usage_governance(usage)
        runbook.status = RUNBOOK_FAILED
        runbook.recovery_note += f" Relaunch blocked: {launched.last_error or launched.state}."
        state.upsert_runbook(runbook)
        raise RunbookError(f"could not retry runbook: {launched.last_error or launched.state}")

    route_history = list(usage.get("route_history", []))
    route_history[-1] = {**route_history[-1], "status": "RUNNING", "pid": launched.pid}
    usage["route_history"] = route_history
    state.upsert_usage_governance(usage)

    runbook.status = RUNBOOK_RUNNING
    runbook.started_at = utc_now_iso()
    runbook.ended_at = None
    deadline = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(minutes=runbook.max_duration_minutes)
    runbook.deadline_at = deadline.isoformat(timespec="seconds")
    state.upsert_runbook(runbook)
    state.record_event(
        category="runbook",
        task_id=task.id,
        message=f"runbook {runbook.id} retried in place with worker={worker_name} pid={launched.pid}",
    )
    return runbook


def automatic_fallback_runbook(
    *, state: State, registry: Registry, supervisor, repo_root: Path, runbook_id: str, dry_run: bool = False,
    scheduler=None,
) -> Runbook | None:
    """Retry a recoverable failed run through the existing equivalent-role policy."""

    runbook, task = _require_launched(state, runbook_id)
    failure = classify_failure(task.failure_reason_sanitized or task.last_error)
    if failure not in {"quota", "rate-limit", "auth-cli", "provider-outage"}:
        return None
    preset = PRESETS.get(runbook.preset)
    role = preset.role if preset else task.role
    failed_worker = registry.workers.get(task.failed_worker_id or task.worker)
    if not failed_worker or not failed_worker.is_write_capable or role not in {
        "primary-implementation",
        "secondary-implementation",
    }:
        return None
    if not state.claim_pending_fallback(task.id):
        # The explicit start request and the dashboard's reconciliation loop
        # can observe the same newly failed task on adjacent threads.  Only the
        # durable compare-and-set winner may select/launch a replacement.  The
        # loser waits briefly for the winner's persisted outcome so the UI can
        # report that single launch as success rather than overwriting it with
        # a spurious self-ownership failure.
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            current_task = state.get_task(task.id)
            current_runbook = state.get_runbook(runbook.id)
            if current_task is None or current_runbook is None:
                return None
            if (
                current_task.fallback_automatic is True
                and current_task.fallback_selected_worker == current_task.worker
                and current_task.state == TASK_RUNNING
                and current_runbook.status == RUNBOOK_RUNNING
            ):
                return current_runbook
            if (
                current_task.fallback_automatic is False
                and current_runbook.recovery_note
                and current_runbook.recovery_note.startswith("Automatic fallback blocked")
            ):
                return None
            time.sleep(0.01)
        return None
    usage = state.get_usage_governance(runbook.id) or new_usage_record(
        runbook_id=runbook.id, task_id=task.id, classification=classify_task(role=role),
        codex_policy=runbook.codex_policy, codex_auto_eligible=runbook.codex_auto_eligible,
        max_codex_invocations=runbook.max_codex_invocations,
    )
    original_policy = (usage.get("context_manifest") or {}).get("policy_manifest")
    if not original_policy:
        original_policy = compose_policy_bundle(
            root=Path(runbook.worktree), registry=registry, worker_name=task.failed_worker_id or task.worker,
            route_role=role, acceptance_criteria=runbook.objective,
        ).manifest
    from .usage_policy import fallback_for_failure

    try:
        validate_target(
            repo_root=repo_root,
            state=state,
            branch=runbook.branch,
            worktree=runbook.worktree,
            exclude_runbook_id=runbook.id,
        )
    except RunbookError as exc:
        task.fallback_automatic = False
        task.fallback_selected_worker = None
        state.upsert_task(task)
        runbook.recovery_note = f"Automatic fallback blocked by worktree safety: {exc}. Manual recovery is required."
        state.upsert_runbook(runbook)
        state.record_event(category="runbook", task_id=task.id, level="warning", message=runbook.recovery_note)
        return None

    attempted = _attempted_workers(usage, task)
    permitted_route = runbook.worker_routes.get(role, list(registry.route(role)))
    availability = {
        name: registry.get(name).availability_reason()
        for name in permitted_route
        if name not in attempted
    }

    decision = fallback_for_failure(
        root=Path(runbook.worktree), registry=registry,
        provider_states={item.name: item for item in state.list_provider_states()},
        route_role=role, failed_worker=task.failed_worker_id or task.worker,
        failure_message=task.failure_reason_sanitized or task.last_error or "unknown failure",
        original_policy_manifest=original_policy, classification=str(usage["classification"]),
        codex_policy=runbook.codex_policy, codex_auto_eligible=runbook.codex_auto_eligible,
        codex_invocations=int(usage["codex_invocations"]), max_codex_invocations=runbook.max_codex_invocations,
        excluded_workers=attempted,
        permission_profile=runbook.permission_profile,
        worker_availability=availability,
        allowed_workers=permitted_route,
    )
    task.fallback_alternatives = decision.alternatives_considered
    task.fallback_selected_worker = decision.worker
    task.fallback_automatic = decision.worker is not None
    state.upsert_task(task)
    if decision.worker is None:
        runbook.recovery_note = f"Automatic fallback blocked: {decision.reason}. Manual retry is required."
        state.upsert_runbook(runbook)
        state.record_event(category="runbook", task_id=task.id, level="warning", message=runbook.recovery_note)
        return None
    state.record_event(
        category="runbook",
        task_id=task.id,
        level="warning",
        message=f"automatically falling back from {task.failed_worker_id or task.worker} to {decision.worker}",
    )
    try:
        return retry_runbook(
            state=state, registry=registry, supervisor=supervisor, repo_root=repo_root,
            runbook_id=runbook_id, worker_name=decision.worker, automatic=True, dry_run=dry_run,
            scheduler=scheduler,
        )
    except RunbookError as exc:
        failed_task = state.get_task(task.id) or task
        failed_task.fallback_automatic = False
        state.upsert_task(failed_task)
        runbook = state.get_runbook(runbook.id) or runbook
        runbook.status = RUNBOOK_FAILED
        runbook.recovery_note = f"Automatic fallback blocked before replacement launch: {exc}. Manual recovery is required."
        state.upsert_runbook(runbook)
        state.record_event(category="runbook", task_id=task.id, level="error", message=runbook.recovery_note)
        return None


def _require_launched(state: State, runbook_id: str) -> tuple[Runbook, Task]:
    runbook = state.get_runbook(runbook_id)
    if runbook is None:
        raise RunbookError(f"unknown runbook {runbook_id!r}")
    if not runbook.task_id:
        raise RunbookError(f"runbook {runbook_id!r} has not been started")
    task = state.get_task(runbook.task_id)
    if task is None:
        raise RunbookError(f"runbook {runbook_id!r}'s underlying task {runbook.task_id!r} is missing")
    return runbook, task


def pause_runbook(*, state: State, runbook_id: str) -> Runbook:
    """Pause a queued/not-yet-launched successor task. A live subprocess keeps running

    (there is no safe way to suspend an in-flight worker subprocess); pause
    only prevents the daemon from scheduling further work for this runbook.
    """

    runbook, task = _require_launched(state, runbook_id)
    if task.state in (TASK_PENDING, TASK_QUEUED):
        task.state = TASK_PAUSED
        state.upsert_task(task)
    runbook.status = RUNBOOK_PAUSED
    state.upsert_runbook(runbook)
    state.record_event(category="runbook", task_id=task.id, message=f"runbook {runbook.id} paused by operator")
    return runbook


def resume_runbook(*, state: State, runbook_id: str) -> Runbook:
    runbook, task = _require_launched(state, runbook_id)
    if task.state == TASK_PAUSED:
        task.state = TASK_PENDING
        state.upsert_task(task)
    runbook.status = RUNBOOK_RUNNING
    state.upsert_runbook(runbook)
    state.record_event(category="runbook", task_id=task.id, message=f"runbook {runbook.id} resumed by operator")
    return runbook


def stop_runbook(*, state: State, runbook_id: str) -> Runbook:
    """Mark the runbook's underlying task cancelled. Matches ``cmd_stop``: a live

    subprocess is not force-killed here (safer than corrupting in-flight work);
    the daemon's normal reconciliation records it FAILED/SUCCEEDED once the
    process actually exits, at which point ``reconcile_runbooks`` finalizes
    the runbook's own terminal status.
    """

    runbook, task = _require_launched(state, runbook_id)
    if task.state not in (TASK_SUCCEEDED, TASK_FAILED, TASK_CANCELLED):
        task.state = TASK_CANCELLED
        state.upsert_task(task)
    runbook.status = RUNBOOK_STOPPING
    state.upsert_runbook(runbook)
    state.record_event(
        category="runbook", level="warning", task_id=task.id, message=f"runbook {runbook.id} stop requested by operator"
    )
    return runbook


def stop_after_current_runbook(*, state: State, runbook_id: str) -> Runbook:
    """Let the current worker finish, then withhold any further daemon-scheduled work.

    This runbook launches exactly one session task, so before ENG-AGENT-13 a
    successful exit was terminal and there was truly no further work to
    withhold -- this was purely informational. It is not anymore: a
    successful exit now starts the acceptance pipeline (Test/Review/
    Checkpoint/PR). The durable flag set here lets reconcile_runbooks halt
    that pipeline before it starts, exactly like an explicit Stop, once the
    current worker finishes.
    """

    runbook = state.get_runbook(runbook_id)
    if runbook is None:
        raise RunbookError(f"unknown runbook {runbook_id!r}")
    runbook.stop_after_current_requested = True
    state.upsert_runbook(runbook)
    state.record_event(
        category="runbook",
        task_id=runbook.task_id,
        message=f"runbook {runbook.id}: stop-after-current requested; will halt before acceptance runs",
    )
    return runbook


def _reports_dir(repo_root: Path) -> Path:
    from .state import STATE_DIRNAME

    return repo_root / STATE_DIRNAME / REPORTS_DIRNAME


def generate_report(*, state: State, repo_root: Path, runbook: Runbook, task: Task) -> str:
    task_ids = {task.id} | {t.id for t in state.list_tasks() if t.runbook_id == runbook.id}
    events = [e for e in state.list_events(limit=1000) if e.task_id in task_ids]
    events.sort(key=lambda e: e.ts)
    lines = [
        f"# Morning report — {runbook.name} ({runbook.id})",
        "",
        f"- Source: {runbook.source_ref}",
        f"- Preset: {runbook.preset}",
        f"- Branch / worktree: {runbook.branch} / {runbook.worktree}",
        f"- Parent worker: {runbook.parent_worker}",
        f"- Duration budget: {runbook.max_duration_minutes} minutes",
        f"- Started: {runbook.started_at or 'n/a'}",
        f"- Ended: {runbook.ended_at or 'n/a'}",
        f"- Implementation attempt result: {task.result or 'n/a'} (task={task.id}, exit-related error={task.last_error or 'none'})",
        f"- Acceptance pipeline stage: {runbook.acceptance_stage}",
        f"- Final status: {runbook.status}",
        "",
        "## Acceptance evidence",
    ]
    if runbook.acceptance_evidence:
        for stage_name, record in runbook.acceptance_evidence.items():
            lines.append(f"- **{stage_name}**: {record.get('status', 'NOT_REPORTED')} — {record.get('reason', 'n/a')}")
    else:
        lines.append("- (this runbook's role does not require the acceptance pipeline)")
    lines += ["", "## Event log for this session"]
    if events:
        lines += [f"- `{e.ts}` [{e.category}] {e.message}" for e in events]
    else:
        lines.append("- (no events recorded)")
    if runbook.status == RUNBOOK_SUCCEEDED:
        next_action = (
            "Implementation and every required acceptance stage passed (or was recorded NOT_APPLICABLE with a "
            "reason). Review the PR opened for this branch; no further automatic action is taken -- merge "
            "remains governed by current repository authority."
        )
    elif runbook.status == RUNBOOK_OWNER_ACTION_REQUIRED:
        next_action = (
            "Acceptance is blocked on operator authority (e.g. commit/push/PR creation). Inspect the "
            "acceptance evidence above, resolve the authority gap, then resume this runbook's acceptance stage "
            "(the 'Resume acceptance' dashboard action / `runbook_retry_acceptance` command) -- do not use "
            "'Retry with selected worker', which relaunches the already-succeeded implementation worker."
        )
    elif runbook.status == RUNBOOK_BLOCKED:
        next_action = (
            "A required acceptance stage failed. Inspect the acceptance evidence above and the worktree's git "
            "log/diff, repair the candidate, then resume this runbook's acceptance stage (the 'Resume "
            "acceptance' dashboard action / `runbook_retry_acceptance` command) -- the implementation itself is "
            "preserved and is never rerun by this action."
        )
    else:
        next_action = "Investigate the failure/stop reason above before relaunching this runbook."
    lines += ["", "## Next action", next_action]
    report = "\n".join(lines) + "\n"

    reports_dir = _reports_dir(repo_root)
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / f"{runbook.id}.md").write_text(report, encoding="utf-8")

    runbook.report_markdown = report
    state.upsert_runbook(runbook)
    return report


_TASK_TO_RUNBOOK_TERMINAL = {
    TASK_SUCCEEDED: RUNBOOK_SUCCEEDED,
    TASK_FAILED: RUNBOOK_FAILED,
    TASK_CANCELLED: RUNBOOK_CANCELLED,
}


def reconcile_runbooks(
    *, state: State, repo_root: Path, registry: Registry | None = None, supervisor=None, scheduler=None
) -> dict[str, int]:
    """Deadline-aware status reconciliation. Never force-kills a live subprocess:

    a deadline is flagged for visibility, and the launched worker itself is
    responsible for finishing/checkpointing gracefully (its prompt says so
    explicitly). Called on every daemon/dashboard reconcile tick, so it must
    never raise.
    """

    finalized = 0
    deadline_flagged = 0
    auto_fallbacks = 0
    # ENG-AGENT-15 (issue #142): computed once per tick, not per runbook -- a
    # long-running process that was never restarted after a merge must never
    # silently finalize an acceptance-applicable runbook SUCCEEDED using stale
    # in-memory acceptance logic. See ``check_runtime_freshness`` for why.
    runtime_staleness = check_runtime_freshness(repo_root=repo_root)
    now = _dt.datetime.now(_dt.timezone.utc)
    for runbook in state.list_runbooks():
        if not runbook.task_id:
            continue
        try:
            task = state.get_task(runbook.task_id)
            if task is None:
                continue
            stale_fallback_presentation = (
                runbook.status == RUNBOOK_FAILED
                and task.state in {TASK_RUNNING, TASK_SUCCEEDED}
                and bool(task.failed_worker_id)
                and task.worker != task.failed_worker_id
                and task.fallback_selected_worker == task.worker
            )
            repaired_terminal_fallback = False
            if stale_fallback_presentation:
                # Repair the bounded legacy race where one contender launched
                # the replacement and another overwrote only the Runbook/task
                # presentation as FAILED.  The replacement may still be live
                # or may have completed successfully before restart. PID
                # liveness is reconciled before this function runs; no process
                # is launched here.
                failed_provider = state.get_provider_state(task.failed_worker_id or "")
                failed_worker = registry.workers.get(task.failed_worker_id or "") if registry else None
                if failed_provider is not None and task.failure_category in {None, "UNKNOWN"}:
                    attribution = failure_attribution(
                        worker=failed_worker,
                        reason=task.failure_reason_sanitized or task.fallback_reason or "",
                        provider_state=failed_provider.state,
                    )
                    task.failure_category = attribution["category"]
                    usage = state.get_usage_governance(runbook.id)
                    if usage:
                        history = list(usage.get("route_history", []))
                        for index, attempt in enumerate(history):
                            if attempt.get("worker") == task.failed_worker_id and attempt.get("status") in {
                                "FAILED",
                                "UNAVAILABLE",
                            }:
                                history[index] = {**attempt, "failure_category": attribution["category"]}
                                break
                        usage["route_history"] = history
                        state.upsert_usage_governance(usage)
                task.fallback_automatic = True
                state.upsert_task(task)
                if task.state == TASK_RUNNING:
                    runbook.status = RUNBOOK_RUNNING
                    runbook.ended_at = None
                    outcome = f"Automatically running the same task with {task.worker}."
                elif is_acceptance_applicable(role=task.role):
                    # ENG-AGENT-13: a repaired fallback presentation must still
                    # go through acceptance, not straight to SUCCEEDED.
                    start_acceptance(runbook=runbook, task=task)
                    outcome = f"The automatic replacement {task.worker} completed successfully; continuing acceptance."
                else:
                    runbook.status = RUNBOOK_SUCCEEDED
                    runbook.ended_at = task.updated_at or utc_now_iso()
                    outcome = f"The automatic replacement {task.worker} completed successfully."
                    repaired_terminal_fallback = True
                runbook.recovery_note = f"Previous attempt with {task.failed_worker_id} was unavailable. {outcome}"
                state.upsert_runbook(runbook)
                state.record_event(
                    category="runbook",
                    task_id=task.id,
                    level="warning",
                    message=(
                        f"recovered automatic fallback presentation with worker={task.worker} "
                        f"task_state={task.state} pid={task.pid}"
                    ),
                )
            pending_fallback_decision = (
                runbook.status == RUNBOOK_FAILED
                and task.state == TASK_FAILED
                and task.fallback_automatic is None
            )
            if (
                runbook.status in RUNBOOK_TERMINAL_STATES
                and not pending_fallback_decision
                and not repaired_terminal_fallback
            ):
                continue
            if task.state == TASK_SUCCEEDED and is_acceptance_applicable(role=task.role):
                # ENG-AGENT-13 final-review finding: Pause/Stop must actually
                # hold the acceptance pipeline (git add -A, run_gate, commit,
                # push, gh pr create are all further daemon-scheduled work,
                # exactly what pause_runbook's own docstring says pausing
                # prevents) -- neither is in RUNBOOK_TERMINAL_STATES, so
                # without this check they fell straight through below.
                if runbook.status == RUNBOOK_PAUSED:
                    continue
                if runbook.status == RUNBOOK_STOPPING or runbook.stop_after_current_requested:
                    # stop_runbook's/stop_after_current_runbook's docstrings
                    # both assumed nothing further would ever run once the
                    # task reached a terminal state; the acceptance pipeline
                    # breaks that assumption, so either request must not
                    # silently let the pipeline proceed to commit/push/PR.
                    #
                    # Round-2 final-review finding: halting here without ever
                    # calling start_acceptance left acceptance_stage at its
                    # bare default "PENDING" with no implementation evidence
                    # recorded, so the documented recovery
                    # (runbook_retry_acceptance) could never actually resume
                    # -- it only pops evidence for a stage that was never
                    # entered, and "PENDING" trips advance_acceptance_pipeline's
                    # own defensive unknown-stage branch (BLOCKED). Recording
                    # implementation evidence and setting acceptance_stage to
                    # STAGE_TEST here first (a real, resumable stage) --
                    # before halting -- is what actually makes that recovery
                    # path work: resume clears no real evidence (there is
                    # none yet) and the next tick correctly starts Test.
                    if runbook.status not in (RUNBOOK_IMPLEMENTATION_COMPLETE, RUNBOOK_ACCEPTANCE_PENDING):
                        start_acceptance(runbook=runbook, task=task)
                    runbook.status = RUNBOOK_OWNER_ACTION_REQUIRED
                    runbook.stop_after_current_requested = False
                    runbook.recovery_note = (
                        "Operator requested stop before the acceptance pipeline could run. The "
                        "implementation itself is preserved and was not rerun. Resume this runbook's "
                        "acceptance stage (the 'Resume acceptance' dashboard action / "
                        "runbook_retry_acceptance command) to continue it."
                    )
                    state.upsert_runbook(runbook)
                    generate_report(state=state, repo_root=repo_root, runbook=runbook, task=task)
                    state.record_event(
                        category="runbook", task_id=task.id, level="warning",
                        message=f"runbook {runbook.id} stop requested before acceptance ran; halted OWNER_ACTION_REQUIRED",
                    )
                    finalized += 1
                    continue
                if runtime_staleness is not None:
                    # ENG-AGENT-15 (issue #142): never let a stale process
                    # start or advance the acceptance pipeline. Record the
                    # honest "implementation" evidence (it genuinely did
                    # pass) and halt on operator authority instead -- exactly
                    # the pattern ``resume_acceptance`` already knows how to
                    # recover from once the daemon is restarted. Reaching this
                    # branch at all means ``runbook.status`` is not yet in
                    # ``RUNBOOK_TERMINAL_STATES`` (the loop's own top-of-tick
                    # check above already skips terminal runbooks on every
                    # later tick, so this halt fires exactly once per runbook).
                    if runbook.status not in (RUNBOOK_IMPLEMENTATION_COMPLETE, RUNBOOK_ACCEPTANCE_PENDING):
                        start_acceptance(runbook=runbook, task=task)
                    runbook.status = RUNBOOK_OWNER_ACTION_REQUIRED
                    runbook.recovery_note = runtime_staleness
                    state.upsert_runbook(runbook)
                    generate_report(state=state, repo_root=repo_root, runbook=runbook, task=task)
                    state.record_event(
                        category="runbook", task_id=task.id, level="error",
                        message=f"runbook {runbook.id} halted: {runtime_staleness}",
                    )
                    finalized += 1
                    continue
                # ENG-AGENT-13 (issue #138): worker exit 0 means the
                # implementation attempt succeeded, not the runbook. Walk the
                # acceptance pipeline (Test -> Review -> Checkpoint -> PR
                # readiness) instead of finalizing SUCCEEDED here.
                if runbook.status not in (RUNBOOK_IMPLEMENTATION_COMPLETE, RUNBOOK_ACCEPTANCE_PENDING):
                    start_acceptance(runbook=runbook, task=task)
                    state.upsert_runbook(runbook)
                project = None
                github_remote = None
                if runbook.project_id:
                    try:
                        from .project_registry import UnknownProjectError, get_project

                        project = get_project(state, runbook.project_id)
                        github_remote = project.github_remote
                    except UnknownProjectError:
                        project = None
                advance_acceptance_pipeline(
                    state=state, repo_root=repo_root, registry=registry, scheduler=scheduler,
                    supervisor=supervisor, runbook=runbook, task=task,
                    project=project, github_remote=github_remote,
                )
                if runbook.status == RUNBOOK_SUCCEEDED:
                    generate_report(state=state, repo_root=repo_root, runbook=runbook, task=task)
                    state.record_event(
                        category="runbook", task_id=task.id,
                        message=f"runbook {runbook.id} finalized as {runbook.status}",
                    )
                    finalized += 1
                    if runbook.project_id:
                        # OCTAREL-OPS-02: accepted work continues from the project's
                        # *current* repository truth; a failure here is recorded, never fatal.
                        try:
                            advance_after_success(
                                state=state, runbook=runbook, registry=registry,
                                supervisor=supervisor, scheduler=scheduler,
                            )
                        except Exception as exc:  # noqa: BLE001
                            state.record_event(
                                category="advancement", level="error", task_id=task.id,
                                message=f"advancement error for runbook {runbook.id}: {exc}",
                            )
                elif runbook.status in (RUNBOOK_BLOCKED, RUNBOOK_OWNER_ACTION_REQUIRED):
                    generate_report(state=state, repo_root=repo_root, runbook=runbook, task=task)
                    state.record_event(
                        category="runbook", task_id=task.id, level="warning",
                        message=f"runbook {runbook.id} halted at acceptance stage {runbook.acceptance_stage!r} as {runbook.status}",
                    )
                    finalized += 1
                continue
            if task.state in _TASK_TO_RUNBOOK_TERMINAL:
                runbook.status = _TASK_TO_RUNBOOK_TERMINAL[task.state]
                runbook.ended_at = utc_now_iso()
                state.upsert_runbook(runbook)
                if task.state == TASK_FAILED and registry is not None and supervisor is not None:
                    retried = automatic_fallback_runbook(
                        state=state, registry=registry, supervisor=supervisor,
                        repo_root=repo_root, runbook_id=runbook.id, scheduler=scheduler,
                    )
                    if retried is not None:
                        auto_fallbacks += 1
                        continue
                generate_report(state=state, repo_root=repo_root, runbook=runbook, task=task)
                state.record_event(
                    category="runbook",
                    task_id=task.id,
                    message=f"runbook {runbook.id} finalized as {runbook.status}",
                )
                finalized += 1
                continue
            if runbook.deadline_at and runbook.status == RUNBOOK_RUNNING:
                try:
                    deadline = _dt.datetime.fromisoformat(runbook.deadline_at)
                except ValueError:  # pragma: no cover - deadline_at is always our own isoformat
                    deadline = None
                if deadline and now >= deadline:
                    runbook.status = RUNBOOK_DEADLINE_REACHED
                    runbook.recovery_note = (
                        "stop deadline reached while the underlying session was still running; "
                        "the worker is expected to finish/checkpoint gracefully rather than being killed"
                    )
                    state.upsert_runbook(runbook)
                    state.record_event(
                        category="runbook",
                        level="warning",
                        task_id=task.id,
                        message=f"runbook {runbook.id} reached its stop deadline; awaiting graceful finish",
                    )
                    deadline_flagged += 1
        except Exception as exc:  # noqa: BLE001 - a bad reconcile must never kill the daemon
            state.record_event(
                category="runbook", level="error", message=f"reconcile error for runbook {runbook.id}: {exc}"
            )
    return {"finalized": finalized, "deadline_flagged": deadline_flagged, "auto_fallbacks": auto_fallbacks}


def recover_runbooks_on_restart(*, state: State) -> dict[str, int]:
    """Startup recovery: a runbook whose task was RUNNING but is now reclaimed to

    QUEUED (see ``recovery.reconcile_tasks``) resumes as RUNNING again once the
    scheduler relaunches it; one still holding a genuinely live PID is left
    untouched. Never duplicates a live worker.
    """

    resumed = 0
    for runbook in state.list_runbooks():
        if runbook.status not in (RUNBOOK_RUNNING, RUNBOOK_PAUSED) or not runbook.task_id:
            continue
        task = state.get_task(runbook.task_id)
        if task is None:
            continue
        if task.state == TASK_RUNNING:
            continue  # live PID confirmed by recovery.reconcile_tasks(); nothing to do
        runbook.recovery_note = f"recovered on daemon restart; underlying task reclaimed to {task.state}"
        state.upsert_runbook(runbook)
        state.record_event(
            category="recovery",
            task_id=task.id,
            message=f"runbook {runbook.id} recovery-noted after daemon restart (task now {task.state})",
        )
        resumed += 1
    return {"annotated": resumed}
