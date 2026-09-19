"""ENG-AGENT-13 (issue #138): the runbook acceptance pipeline.

A primary-implementation worker exiting 0 means the *implementation attempt*
succeeded, not the runbook. This module extends the existing Task DAG (the
same ``dependencies``/``runbook_id``/``managed_admit`` mechanism
``runbooks.start_runbook`` already uses) so a runbook continues
deterministically through Test -> Review -> Checkpoint -> PR readiness before
``reconcile_runbooks`` may report it terminal ``SUCCEEDED``. It never invents
a second scheduler, task store, review engine, test runner, or PR workflow:

- Test is the existing authoritative exact-tree local gate
  (``scripts/ci/local_gate.run_gate``), which already selects risk-based
  T0-T3 evidence and reuses still-valid exact-tree results.
- Review dispatches a bounded, read-only, exact-tree scoped ``diff-review``
  Task through the existing ``managed_admit`` dispatch path -- the same
  eligibility/cost/fairness/Codex-quota gates every other task already goes
  through -- and feeds the resulting manifest back into ``run_gate`` as its
  own independent-review evidence.
- Checkpoint and PR readiness are small deterministic Git/``gh`` operations,
  never a second merge/PR engine; automatic merge is explicitly out of scope
  (the issue this module implements says so) and is never performed here.

Every stage records truthful PASS/FAIL/NOT_APPLICABLE evidence with a reason
on ``Runbook.acceptance_evidence`` -- ``NOT_REPORTED`` is never silently
treated as passing.
"""

from __future__ import annotations

import re
import subprocess
import uuid
from pathlib import Path
from typing import Any

from scripts.ci.change_risk import classify
from scripts.ci.local_gate import candidate as gate_candidate
from scripts.ci.local_gate import review_response_verdict, run_gate
from scripts.ci.runtime_paths import has_non_runtime_changes, pathspec_excludes

from ..registry import PERMISSION_STANDARD, Registry
from ..validation import validate_task_id
from .dispatch import managed_admit
from .models import (
    ADMISSION_PENDING,
    KIND_READ,
    LAUNCH_DELEGATED,
    RUNBOOK_ACCEPTANCE_PENDING,
    RUNBOOK_BLOCKED,
    RUNBOOK_IMPLEMENTATION_COMPLETE,
    RUNBOOK_OWNER_ACTION_REQUIRED,
    RUNBOOK_SUCCEEDED,
    TASK_BLOCKED,
    TASK_CANCELLED,
    TASK_FAILED,
    TASK_PENDING,
    TASK_SUCCEEDED,
    Runbook,
    Task,
    utc_now_iso,
)
from .octascene_project import OCTASCENE_GITHUB_REMOTE
from .scheduler import Scheduler
from .state import State

# Stage order (issue #138: "Bootstrap -> Implement -> Test -> Review ->
# Checkpoint -> PR readiness -> Report"; Bootstrap/Implement/Report are not
# separate stages here -- Implement is the existing session Task, and Report
# is ``generate_report``, called once by ``reconcile_runbooks`` after DONE).
STAGE_TEST = "test"
STAGE_REVIEW = "review"
STAGE_CHECKPOINT = "checkpoint"
STAGE_PR_READINESS = "pr_readiness"
STAGE_DONE = "DONE"
STAGE_ORDER: tuple[str, ...] = (STAGE_TEST, STAGE_REVIEW, STAGE_CHECKPOINT, STAGE_PR_READINESS)

EVIDENCE_PASS = "PASS"
EVIDENCE_FAIL = "FAIL"
EVIDENCE_NOT_APPLICABLE = "NOT_APPLICABLE"

ACCEPTANCE_REVIEW_ROLE = "diff-review"
DEFAULT_BASE_REF = "origin/main"
# Legacy unscoped default for callers/tests that have no selected ProjectContract.
# Production merge/PR paths with a selected project must pass ``github_remote``.
GITHUB_REPO = OCTASCENE_GITHUB_REMOTE

# ENG-AGENT-15 (issue #142): a real V1-05 overnight-development run finalized
# SUCCEEDED immediately after implementation exit with zero Test/Review/
# Checkpoint/PR evidence -- not because this module's logic was wrong, but
# because the long-running Control Plane daemon serving that run had been
# started (2026-09-15 15:34 UTC) *before* ENG-AGENT-13 merged this module into
# main (2026-09-16 00:56 UTC) and was never restarted. A live Python process
# never reloads modified/merged source; only a restart re-imports it, so that
# daemon kept executing pre-acceptance-pipeline bytecode for hours after main
# (and the checked-out files on disk) already contained the fix. Bump this
# string whenever a change here alters what PASS/FAIL/NOT_APPLICABLE means for
# an existing stage, or adds/removes a required stage -- ``check_runtime_freshness``
# below compares it against the same constant read straight off disk so a
# stale-but-still-running process can detect that it must not be trusted to
# finalize a runbook, instead of silently repeating this incident.
ACCEPTANCE_PIPELINE_CONTRACT_VERSION = "ENG-AGENT-15"

_CONTRACT_VERSION_RE = re.compile(r'^ACCEPTANCE_PIPELINE_CONTRACT_VERSION\s*=\s*"([^"]*)"', re.MULTILINE)
_ACCEPTANCE_MODULE_RELATIVE_PATH = Path("scripts") / "agents" / "control_plane" / "acceptance.py"


def _on_disk_contract_version(repo_root: Path) -> str | None:
    """Read the contract version straight off ``repo_root``'s own file, never

    the imported module -- the entire point is to detect when those two
    disagree because this process is stale.
    """

    try:
        text = (repo_root / _ACCEPTANCE_MODULE_RELATIVE_PATH).read_text(encoding="utf-8")
    except OSError:
        return None
    match = _CONTRACT_VERSION_RE.search(text)
    return match.group(1) if match else None


def check_runtime_freshness(*, repo_root: Path) -> str | None:
    """Return a truthful halt reason when this process's loaded acceptance

    logic has fallen behind ``repo_root``'s current checked-out source, or
    ``None`` when they agree (including when the on-disk file cannot be read
    at all, e.g. a test fixture with no such file -- that is never itself
    evidence of staleness).
    """

    on_disk = _on_disk_contract_version(repo_root)
    if on_disk is None or on_disk == ACCEPTANCE_PIPELINE_CONTRACT_VERSION:
        return None
    return (
        "Control Plane runtime staleness detected: this process has acceptance contract "
        f"{ACCEPTANCE_PIPELINE_CONTRACT_VERSION!r} loaded in memory, but {repo_root} now has "
        f"{on_disk!r} on disk. A long-running daemon never reloads merged source; it must be "
        "restarted before any runbook's acceptance pipeline can be trusted to reach terminal "
        "SUCCEEDED. No acceptance stage was advanced or finalized this tick."
    )


def _evidence(status: str, reason: str, **extra: Any) -> dict[str, Any]:
    return {"status": status, "reason": reason, "recorded_at": utc_now_iso(), **extra}


def _run(argv: list[str], cwd: Path, *, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False)


def _git(cwd: Path, *args: str, timeout: int = 30) -> str | None:
    try:
        result = _run(["git", *args], cwd, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _worktree_is_dirty(worktree: Path) -> bool:
    # ENG-AGENT-16 (issue #146): delegate to the single canonical CP-runtime-
    # path predicate (all three directories, and immune to the whole-blob
    # `.strip()` a local `_git()` call would apply to multi-line porcelain
    # output) rather than a locally hand-maintained subset -- this previously
    # omitted .orchestrator-state/, so a runbook's own scheduler-state writes
    # during Test/Review could make Checkpoint see a "dirty" worktree that
    # was never a real product change.
    return has_non_runtime_changes(worktree)


def is_acceptance_applicable(*, role: str) -> bool:
    """Only a code-writing primary-implementation session needs acceptance.

    A read-only ``review-only`` preset (role ``diff-review``) or any other
    non-writing role already does exactly what it set out to do the moment
    its worker exits 0 -- there is no implementation candidate to test,
    review, checkpoint, or open a PR for.
    """

    return role == "primary-implementation"


def resume_acceptance(*, runbook: Runbook) -> None:
    """Retry the acceptance pipeline after an operator resolved a halt.

    Only valid from ``OWNER_ACTION_REQUIRED``/``BLOCKED``. This never touches
    the (already ``TASK_SUCCEEDED``) implementation task and never reruns
    implementation -- it discards only the current stage's own evidence entry
    (which recorded the failure) so that stage is re-attempted fresh on the
    next reconcile tick; every earlier PASS/NOT_APPLICABLE stage evidence is
    left untouched and is not recomputed.
    """

    if runbook.status not in (RUNBOOK_OWNER_ACTION_REQUIRED, RUNBOOK_BLOCKED):
        raise ValueError(f"runbook {runbook.id!r} is not halted on an acceptance stage")
    stage = runbook.acceptance_stage
    evidence = dict(runbook.acceptance_evidence)
    evidence.pop(stage, None)
    runbook.acceptance_evidence = evidence
    runbook.status = RUNBOOK_ACCEPTANCE_PENDING
    runbook.recovery_note = f"{runbook.recovery_note or ''} Operator resumed the {stage!r} acceptance stage.".strip()


def start_acceptance(*, runbook: Runbook, task: Task) -> None:
    """Transition a freshly-succeeded implementation task's runbook off SUCCEEDED.

    Called exactly once, the first reconcile tick after the underlying Task
    reaches ``TASK_SUCCEEDED``.
    """

    runbook.status = RUNBOOK_IMPLEMENTATION_COMPLETE
    evidence = dict(runbook.acceptance_evidence)
    evidence.setdefault(
        "implementation",
        _evidence(EVIDENCE_PASS, "primary-implementation worker exited 0", task_id=task.id),
    )
    runbook.acceptance_evidence = evidence
    runbook.acceptance_stage = STAGE_TEST


def _github_repo(github_remote: str | None) -> str:
    return github_remote or GITHUB_REPO


def advance_acceptance_pipeline(
    *,
    state: State,
    repo_root: Path,
    registry: Registry | None,
    scheduler: Scheduler | None,
    supervisor: Any,
    runbook: Runbook,
    task: Task,
    base_ref: str = DEFAULT_BASE_REF,
    dry_run: bool = False,
    github_remote: str | None = None,
    project: Any | None = None,
) -> None:
    """Advance ``runbook`` at most one acceptance step. Mutates it in place.

    Called on every reconcile tick while ``runbook.status`` is
    ``IMPLEMENTATION_COMPLETE``/``ACCEPTANCE_PENDING``. Never raises: the
    caller (``reconcile_runbooks``) wraps every runbook in a broad
    ``try/except`` because a bad reconcile must never kill the daemon, but a
    stage failure here is still recorded as truthful evidence, not swallowed
    silently.
    """

    worktree = Path(runbook.worktree)
    evidence = dict(runbook.acceptance_evidence)
    stage = runbook.acceptance_stage or STAGE_TEST

    try:
        if stage in (STAGE_TEST, STAGE_REVIEW):
            _advance_test_and_review(
                state=state, repo_root=repo_root, registry=registry, scheduler=scheduler,
                supervisor=supervisor, runbook=runbook, task=task, worktree=worktree,
                base_ref=base_ref, evidence=evidence, dry_run=dry_run, project=project,
            )
        elif stage == STAGE_CHECKPOINT:
            _advance_checkpoint(runbook=runbook, worktree=worktree, evidence=evidence)
        elif stage == STAGE_PR_READINESS:
            _advance_pr_readiness(
                runbook=runbook, worktree=worktree, evidence=evidence, github_remote=github_remote,
            )
        else:  # pragma: no cover - defensive; stage is always one of the above
            evidence[stage] = _evidence(EVIDENCE_FAIL, f"unknown acceptance stage {stage!r}")
            runbook.status = RUNBOOK_BLOCKED
    finally:
        runbook.acceptance_evidence = evidence
        runbook.updated_at = utc_now_iso()
        state.upsert_runbook(runbook)

    if runbook.acceptance_stage == STAGE_DONE and runbook.status not in (RUNBOOK_BLOCKED, RUNBOOK_OWNER_ACTION_REQUIRED):
        required = (STAGE_TEST, STAGE_REVIEW, STAGE_CHECKPOINT, STAGE_PR_READINESS)
        if all(evidence.get(name, {}).get("status") in (EVIDENCE_PASS, EVIDENCE_NOT_APPLICABLE) for name in required):
            runbook.status = RUNBOOK_SUCCEEDED
            runbook.ended_at = utc_now_iso()
            state.upsert_runbook(runbook)


# --------------------------------------------------------------------- test + review


#: Task states in which a dispatched diff-review Task's subprocess is either
#: still going to touch the candidate worktree's Git index or has not been
#: resolved into review evidence yet. Mirrors exactly the states
#: ``_advance_review_stage`` itself still treats as "nothing to do this
#: tick" (its trailing ``else`` branch) -- the complement of the four states
#: it records terminal review evidence for (``TASK_SUCCEEDED``,
#: ``TASK_FAILED``, and the ``TASK_BLOCKED``/``TASK_CANCELLED`` pair).
_REVIEW_TASK_RESOLVED_STATES = frozenset({TASK_SUCCEEDED, TASK_FAILED, TASK_BLOCKED, TASK_CANCELLED})


def _review_task_in_flight(state: State, evidence: dict[str, Any]) -> bool:
    """True while a dispatched review Task's subprocess may still be running.

    Issue #157: that subprocess runs real Git commands (``write-tree``,
    ``diff``) against the same candidate worktree ``gate_candidate``'s own
    ``git add -A`` targets. Calling ``gate_candidate`` on a tick where the
    review Task is still ``PENDING``/``QUEUED``/``RUNNING`` races the two on
    the worktree's ``index.lock`` and fails the Test stage closed on a
    transient collision, even though the review Task itself later succeeds.
    """

    review_record = evidence.get(STAGE_REVIEW)
    if review_record is None or review_record.get("status") != "NOT_REPORTED":
        return False
    review_task_id = review_record.get("task_id")
    if not review_task_id:
        return False
    review_task = state.get_task(review_task_id)
    return review_task is not None and review_task.state not in _REVIEW_TASK_RESOLVED_STATES


def _advance_test_and_review(
    *, state: State, repo_root: Path, registry: Registry | None, scheduler: Scheduler | None,
    supervisor: Any, runbook: Runbook, task: Task, worktree: Path, base_ref: str,
    evidence: dict[str, Any], dry_run: bool, project: Any | None = None,
) -> None:
    if _review_task_in_flight(state, evidence):
        # A previously dispatched review Task has not resolved yet -- do not
        # touch the candidate worktree's Git index (gate_candidate's own
        # `git add -A`) until it does. Evidence/runbook status are left
        # exactly as the dispatch tick recorded them; the next tick after
        # the review Task reaches a resolved state re-enters normally below.
        return
    try:
        # ENG-AGENT-16 (issue #146): never stage Control Plane runtime/
        # evidence directories into the candidate index in the first place --
        # a bare `git add -A` staged them whenever the worktree's own tracked
        # .gitignore predated one or more of them, which then baked them into
        # gate_candidate's write-tree'd tree and destabilized the candidate
        # SHA between Test/Review/Checkpoint ticks. gate_candidate itself
        # also self-heals (resets any already-staged runtime paths) as a
        # second, independent layer -- this exclusion is not the only thing
        # standing between runtime writes and candidate identity.
        _run(["git", "add", "-A", "--", ".", *pathspec_excludes()], worktree, timeout=60)
        tree, _head, paths = gate_candidate(worktree, base_ref)
    except (RuntimeError, subprocess.SubprocessError) as exc:
        runbook.status = RUNBOOK_BLOCKED
        evidence[STAGE_TEST] = _evidence(EVIDENCE_FAIL, f"unable to resolve an exact-tree candidate: {exc}")
        return

    risk = classify(paths)
    review_required = risk.review_level in {"high", "critical"}
    review_record = evidence.get(STAGE_REVIEW)

    # ENG-AGENT-13 independent-review finding (round 4): a stored Review PASS
    # is only valid for the exact tree it was computed against. If an
    # operator repairs the candidate after a Test halt (a new tree), a stale
    # Review PASS must never be reused -- run_gate's own candidate_tree_sha
    # check would fail-closed on it anyway, but nothing would ever
    # re-dispatch a fresh review, deadlocking every subsequent resume.
    review_stale = (
        review_record is not None
        and review_record.get("status") == EVIDENCE_PASS
        and review_record.get("tree_sha") != tree
    )
    if review_stale:
        review_record = None

    if not review_required:
        if review_record is None or review_record.get("status") not in (EVIDENCE_PASS, EVIDENCE_NOT_APPLICABLE):
            evidence[STAGE_REVIEW] = _evidence(
                EVIDENCE_NOT_APPLICABLE, f"risk level {risk.review_level!r} does not require independent review",
            )
            review_record = evidence[STAGE_REVIEW]
    elif review_record is None or review_record.get("status") not in (EVIDENCE_PASS,):
        if review_stale:
            evidence.pop(STAGE_REVIEW, None)
        if (review_record is None or not review_record.get("task_id")) and (registry is None or scheduler is None):
            runbook.status = RUNBOOK_BLOCKED
            evidence[STAGE_REVIEW] = _evidence(EVIDENCE_FAIL, "no registry/scheduler available to dispatch independent review")
            return
        _advance_review_stage(
            state=state, repo_root=repo_root, registry=registry, scheduler=scheduler, supervisor=supervisor,
            runbook=runbook, task=task, worktree=worktree, tree=tree, changed_paths=paths, evidence=evidence,
            base_ref=base_ref,
        )
        review_record = evidence.get(STAGE_REVIEW)
        if review_record is None or review_record.get("status") != EVIDENCE_PASS:
            # Dispatched-and-pending, or freshly failed/blocked: nothing more
            # to do this tick -- never run tests while review is unresolved.
            runbook.acceptance_stage = STAGE_REVIEW
            if runbook.status not in (RUNBOOK_BLOCKED, RUNBOOK_OWNER_ACTION_REQUIRED):
                runbook.status = RUNBOOK_ACCEPTANCE_PENDING
            return
        # Review just passed this tick: fall through to run_gate below
        # immediately instead of waiting an extra idle tick.

    review_provider = None
    review_evidence_path = None
    if review_record and review_record.get("status") == EVIDENCE_PASS:
        review_provider = review_record.get("provider")
        review_evidence_path = review_record.get("manifest_path")

    gate_kwargs = {
        "base": base_ref,
        "docs_reviewed": True,
        "review_provider": review_provider,
        "review_evidence": review_evidence_path,
        "dry_run": dry_run,
        "reuse_evidence": True,
        "rerun_reason": f"ENG-AGENT-13 acceptance pipeline for runbook {runbook.id}",
        # ENG-AGENT-18 (issue #150): the worker registry
        # (scripts/agents/workers.json) is Control Plane orchestration
        # infrastructure, not part of the candidate's own product diff -- a
        # candidate branch that predates that infrastructure entirely (e.g.
        # a long-lived product branch) must still resolve it canonically
        # rather than reporting it permanently missing.
        "registry_root": repo_root,
    }
    if project is not None:
        from .validation_adapter import run_selected_project_validation

        result = run_selected_project_validation(project, worktree=worktree, gate_kwargs=gate_kwargs)
    else:
        result = run_gate(root=worktree, **gate_kwargs)
    if result.get("result") == "pass":
        evidence[STAGE_TEST] = _evidence(
            EVIDENCE_PASS, "exact-tree local gate passed", tree_sha=tree, evidence_path=result.get("evidence_path"),
        )
        runbook.acceptance_stage = STAGE_CHECKPOINT
        runbook.status = RUNBOOK_ACCEPTANCE_PENDING
    else:
        reason = "; ".join(result.get("prerequisite_failures") or []) or "the exact-tree local gate did not pass"
        evidence[STAGE_TEST] = _evidence(EVIDENCE_FAIL, reason, tree_sha=tree, evidence_path=result.get("evidence_path"))
        # ENG-AGENT-13 independent-review finding: acceptance_stage may still
        # read "review" here (set when review was dispatched/pending, then
        # left untouched on the same-tick fall-through once review passed).
        # Pin it back to "test" so retry_acceptance clears only the failing
        # Test evidence -- never the passing Review evidence -- and never
        # re-dispatches a duplicate review Task on resume.
        runbook.acceptance_stage = STAGE_TEST
        runbook.status = RUNBOOK_BLOCKED
        runbook.recovery_note = f"acceptance test stage failed: {reason}"


def _review_scope_paths(worktree: Path, changed_paths: list[str]) -> list[str]:
    """Build narrow, real ``--scope`` entries for the diff-review Task.

    Issue #144: ``_advance_review_stage`` used to hardcode a bare ``scope:.``,
    which ``validate_scope_path`` always rejects once the task's own
    ``--repo-root`` (the review worktree) and the requested scope resolve to
    the same path -- exactly the case for every acceptance-pipeline review,
    so every high/critical dispatch failed. When the candidate has specific
    changed paths (the normal case -- already computed by ``gate_candidate``
    for the Test stage, reused here rather than recomputed), each one becomes
    its own scope entry, added/deleted/renamed files included: diff-review
    scopes no longer require on-disk existence (see ``validate_scope_path``),
    since a deleted path still resolves to a valid ``git diff`` pathspec.
    When the exact-tree diff against base has zero changed paths (classify's
    own conservative "unknown/empty implies critical" default, or a genuine
    same-tree candidate), whole-candidate review semantics are preserved by
    scoping every top-level tracked entry instead of falling back to an
    unsafe repository-root scope.
    """

    scopes: list[str] = []
    seen: set[str] = set()
    for raw in changed_paths or ():
        normalized = raw.strip().replace("\\", "/").removeprefix("./")
        # A bare "." (however it arose) resolves to the repository root --
        # exactly the unsafe scope this function exists to never emit -- so
        # it must fall through to the top-level-tracked-entries fallback
        # below, never be treated as a real changed-path entry.
        if normalized and normalized != "." and normalized not in seen:
            seen.add(normalized)
            scopes.append(normalized)
    if scopes:
        return scopes
    top_level = _git(worktree, "ls-tree", "--name-only", "HEAD") or ""
    return [line for line in top_level.splitlines() if line.strip()]


def _advance_review_stage(
    *, state: State, repo_root: Path, registry: Registry, scheduler: Scheduler, supervisor: Any,
    runbook: Runbook, task: Task, worktree: Path, tree: str, changed_paths: list[str], evidence: dict[str, Any],
    base_ref: str,
) -> None:
    review_record = evidence.get(STAGE_REVIEW) or {}
    review_task_id = review_record.get("task_id")
    review_task = state.get_task(review_task_id) if review_task_id else None

    if review_task is not None:
        if review_task.state == TASK_SUCCEEDED:
            # The review Task is dispatched with worktree=str(worktree), and
            # supervisor._build_argv passes that worktree as orchestrate.py's
            # --repo-root, so its manifest.json lands under
            # <worktree>/.agent-output/... -- not under the daemon's own
            # repo_root, which is frequently a different checkout entirely.
            manifest_path, manifest, unready_reason = _find_review_manifest(
                repo_root=worktree, task_ref=review_task.task_ref, tree=tree,
            )
            if manifest is not None:
                evidence[STAGE_REVIEW] = _evidence(
                    EVIDENCE_PASS,
                    "independent read-only review passed on the exact candidate tree",
                    task_id=review_task.id,
                    provider=(manifest.get("actual") or {}).get("provider"),
                    manifest_path=manifest_path,
                    # ENG-AGENT-13 independent-review finding (round 4): record
                    # the exact tree this PASS is valid for, so a later tree
                    # change (e.g. an operator repair after a Test halt)
                    # invalidates reuse instead of feeding a stale manifest
                    # into run_gate forever.
                    tree_sha=tree,
                )
            else:
                evidence[STAGE_REVIEW] = _evidence(
                    EVIDENCE_FAIL,
                    f"review task succeeded but produced no matching exact-tree read-only manifest: {unready_reason}",
                    task_id=review_task.id,
                )
                runbook.status = RUNBOOK_BLOCKED
            return
        if review_task.state == TASK_FAILED:
            evidence[STAGE_REVIEW] = _evidence(
                EVIDENCE_FAIL, review_task.last_error or "independent review task failed", task_id=review_task.id,
            )
            runbook.status = RUNBOOK_BLOCKED
            return
        if review_task.state in (TASK_BLOCKED, TASK_CANCELLED):
            # ENG-AGENT-13 independent-review finding: a permanently blocked
            # admission (e.g. no eligible candidate) is neither SUCCEEDED nor
            # FAILED, so without this branch the review stage would sit at
            # NOT_REPORTED forever and the runbook would never reach BLOCKED
            # (or SUCCEEDED) -- a silent stall, not a truthful halt.
            evidence[STAGE_REVIEW] = _evidence(
                EVIDENCE_FAIL,
                review_task.admission_reason or review_task.last_error or f"independent review task {review_task.state}",
                task_id=review_task.id,
            )
            runbook.status = RUNBOOK_BLOCKED
            return
        # Still pending/queued/running: nothing to do this tick.
        evidence[STAGE_REVIEW] = _evidence(
            "NOT_REPORTED", "independent review is dispatched and not yet finished", task_id=review_task.id,
        )
        return

    try:
        candidates = list(registry.route(ACCEPTANCE_REVIEW_ROLE))
    except ValueError:
        candidates = []
    if not candidates:
        evidence[STAGE_REVIEW] = _evidence(EVIDENCE_FAIL, "no eligible diff-review route is configured")
        runbook.status = RUNBOOK_BLOCKED
        return

    scope_paths = _review_scope_paths(worktree, changed_paths)
    if not scope_paths:
        # Only reachable for a candidate worktree with no tracked files at
        # all (an empty repository) -- a truthful halt, never a fabricated
        # scope:. dispatch (issue #144).
        evidence[STAGE_REVIEW] = _evidence(
            EVIDENCE_FAIL, "the candidate worktree has no tracked files to scope an independent review to",
        )
        runbook.status = RUNBOOK_BLOCKED
        return

    suffix = uuid.uuid4().hex[:8].upper()
    review_task_id = f"{runbook.id}-review-{suffix.lower()}"
    review_task_ref = validate_task_id(f"{runbook.id.upper()}-REVIEW-{suffix}")
    review_prompt = (
        f"Independently review the scoped candidate diff for acceptance-pipeline runbook "
        f"{runbook.id} (task {task.task_ref}) before its Test/Checkpoint/PR-readiness stages proceed."
    )
    review_task = Task(
        id=review_task_id,
        task_ref=review_task_ref,
        role=ACCEPTANCE_REVIEW_ROLE,
        worker=candidates[0],
        kind=KIND_READ,
        state=TASK_PENDING,
        priority=10,
        worktree=str(worktree),
        # ENG-AGENT-18 (issue #155): carry the acceptance pipeline's configured
        # base ref through to the dispatched worker subprocess (supervisor's
        # ``_build_argv`` turns this into ``--diff-base``), so a clean
        # committed candidate's review diff is built against the same base
        # ``gate_candidate`` used for Test rather than failing on an empty
        # working-tree diff.
        command=tuple(f"scope:{p}" for p in scope_paths) + (f"base:{base_ref}", review_prompt),
        launch_mode=LAUNCH_DELEGATED,
        runbook_id=runbook.id,
        dependencies=(task.id,),
        # ENG-AGENT-13 independent-review finding (round 2): a client-side
        # pre-filter of `candidates` was cosmetic -- managed_admit re-derives
        # the full, unfiltered route via its own _candidate_scores and can
        # still reassign task.worker to codex-review if it is the only
        # eligible candidate. avoid_provider is the existing, real exclusion
        # mechanism dispatch.py's scoring already honors (provider diversity
        # gate), so use it: Codex/OpenAI must never satisfy the review stage,
        # matching run_gate's own explicit non-Codex requirement.
        avoid_provider="OpenAI",
        # ENG-AGENT-13 independent-review finding: a read-only reviewer must
        # never inherit the *implementation* runbook's permission_profile.
        # Worker.supports_permission_profile() rejects every read-only worker
        # for any profile other than "standard" (registry.py), so passing
        # e.g. repo_configured_auto here made managed_admit permanently
        # TASK_BLOCK every candidate on the Overnight Development preset --
        # review never reaches Test/Checkpoint/PR for exactly the preset the
        # original defect (issue #138) was filed against.
        permission_profile=PERMISSION_STANDARD,
        admission_state=ADMISSION_PENDING,
        # ENG-CP-03: the acceptance review task belongs to the same managed
        # project as the runbook whose candidate it is reviewing.
        project_id=runbook.project_id,
    )
    managed_admit(
        state=state, registry=registry, scheduler=scheduler, supervisor=supervisor,
        repo_root=repo_root, task=review_task,
    )
    evidence[STAGE_REVIEW] = _evidence(
        "NOT_REPORTED", "independent review dispatched", task_id=review_task.id,
    )


def _find_review_manifest(
    *, repo_root: Path, task_ref: str, tree: str,
) -> tuple[str | None, dict[str, Any] | None, str | None]:
    """Return ``(manifest_path, manifest, unready_reason)``.

    ``manifest_path``/``manifest`` are set only for a manifest that fully
    qualifies; otherwise both are ``None`` and ``unready_reason`` carries the
    most specific truthful reason found across every candidate manifest
    (issue #148: previously a generic "no matching manifest" message hid
    *why* -- a contradictory response, a missing field, or an agent-preset
    fallback all looked identical to an operator). "Most specific" is ranked,
    not merely first-found: a rejection based on the reviewer's actual
    response content (rank 2) is far more actionable than a structural
    mismatch like the wrong tree or role (rank 1), and a structural mismatch
    is more informative than a completely unreadable manifest (rank 0) --
    so a later (older, by mtime) manifest's more specific reason always wins
    over an earlier (newer) manifest's less specific one, never the reverse.
    """

    import json

    task_dir = repo_root / ".agent-output" / task_ref
    if not task_dir.is_dir():
        return None, None, "no review task output directory exists yet"
    manifests = sorted(task_dir.glob("*/*/manifest.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not manifests:
        return None, None, "no review manifest has been written yet"
    best_rank = -1
    best_reason: str | None = None

    def _note(rank: int, reason: str) -> None:
        nonlocal best_rank, best_reason
        if rank > best_rank:
            best_rank, best_reason = rank, reason

    for manifest_path in manifests:
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _note(0, f"manifest at {manifest_path.name!r} is unreadable/invalid JSON")
            continue
        if data.get("role") != ACCEPTANCE_REVIEW_ROLE:
            _note(1, f"manifest role {data.get('role')!r} is not {ACCEPTANCE_REVIEW_ROLE!r}")
            continue
        if data.get("result") != "PASS":
            _note(1, f"review worker result was {data.get('result')!r}, not PASS")
            continue
        if data.get("files_changed"):
            _note(1, "review worker reported files_changed; a read-only reviewer must never edit")
            continue
        if data.get("candidate_tree_sha") != tree:
            _note(1, "manifest candidate_tree_sha does not match the current candidate tree")
            continue
        # ENG-AGENT-13 independent-review finding (round 2): a read-only
        # reviewer exiting 0 with a normal (non-READY) response -- e.g.
        # one that actually found blockers -- must never be accepted as
        # review PASS here. Without this, run_gate's own stricter
        # same-tree check (local_gate.run_gate's review_evidence
        # prerequisite calls this identical function) would reject the
        # evidence on the very next line, but by then STAGE_REVIEW was
        # already durably marked PASS and never reconsidered -- Test
        # would fail forever with no way to re-dispatch review.
        verdict = review_response_verdict(manifest_path, data)
        if not verdict.ready:
            _note(2, f"review response was not READY: {verdict.reason}")
            continue
        return str(manifest_path.relative_to(repo_root)), data, None
    return None, None, best_reason or "no candidate manifest matched"


# --------------------------------------------------------------------- checkpoint


def _has_unpushed_commits(worktree: Path) -> bool:
    upstream = _git(worktree, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if not upstream:
        # No upstream configured at all -- HEAD has never been pushed.
        return True
    ahead = _git(worktree, "rev-list", "--count", f"{upstream}..HEAD")
    # ENG-AGENT-13 independent-review finding (round 3): a failed/timed-out
    # rev-list (ahead is None) must fail CLOSED (assume unpushed), not open --
    # the prior `bool(ahead)` treated None the same as "0" and could record a
    # clean-but-actually-unpushed worktree as checkpoint PASS with no push.
    if ahead is None:
        return True
    return ahead.strip() != "0"


def _advance_checkpoint(*, runbook: Runbook, worktree: Path, evidence: dict[str, Any]) -> None:
    dirty = _worktree_is_dirty(worktree)
    if not dirty and not _has_unpushed_commits(worktree):
        evidence[STAGE_CHECKPOINT] = _evidence(EVIDENCE_PASS, "worktree already clean and pushed; nothing to checkpoint")
        runbook.acceptance_stage = STAGE_PR_READINESS
        runbook.status = RUNBOOK_ACCEPTANCE_PENDING
        return

    # ENG-AGENT-13 independent-review finding (round 3): check the protected/
    # detached branch guard *before* committing -- committing to main/master/
    # a detached HEAD first (even though the later push is still refused)
    # left a local commit on a ref this stage must never touch at all.
    branch = (_git(worktree, "rev-parse", "--abbrev-ref", "HEAD") or "").strip()
    if not branch or branch in {"main", "master", "HEAD"}:
        evidence[STAGE_CHECKPOINT] = _evidence(EVIDENCE_FAIL, f"refusing to checkpoint branch {branch!r}")
        runbook.status = RUNBOOK_OWNER_ACTION_REQUIRED
        return

    if dirty:
        commit = _run(
            ["git", "commit", "-m", f"checkpoint: {runbook.id} acceptance test/review evidence green"],
            worktree, timeout=60,
        )
        if commit.returncode != 0:
            evidence[STAGE_CHECKPOINT] = _evidence(
                EVIDENCE_FAIL, f"checkpoint commit failed: {(commit.stderr or commit.stdout).strip()[:800]}",
            )
            runbook.status = RUNBOOK_OWNER_ACTION_REQUIRED
            return

    head = _git(worktree, "rev-parse", "HEAD")

    # Re-attempting the push (even when this tick made no new commit) is what
    # lets a retried checkpoint actually retry a previously failed push
    # instead of being masked by "worktree already clean" above.
    push = _run(["git", "push", "origin", f"HEAD:refs/heads/{branch}"], worktree, timeout=180)
    if push.returncode != 0:
        evidence[STAGE_CHECKPOINT] = _evidence(
            EVIDENCE_FAIL,
            f"checkpoint committed locally (head={head}) but push requires operator authority: "
            f"{(push.stderr or push.stdout).strip()[:800]}",
            head_sha=head,
        )
        runbook.status = RUNBOOK_OWNER_ACTION_REQUIRED
        return

    evidence[STAGE_CHECKPOINT] = _evidence(EVIDENCE_PASS, "committed and pushed the exact-tree candidate", head_sha=head)
    runbook.acceptance_stage = STAGE_PR_READINESS
    runbook.status = RUNBOOK_ACCEPTANCE_PENDING


# --------------------------------------------------------------------- PR readiness


def _advance_pr_readiness(*, runbook: Runbook, worktree: Path, evidence: dict[str, Any], github_remote: str | None = None) -> None:
    import json

    branch = (_git(worktree, "rev-parse", "--abbrev-ref", "HEAD") or "").strip()
    if not branch or branch in {"main", "master", "HEAD"}:
        evidence[STAGE_PR_READINESS] = _evidence(EVIDENCE_FAIL, f"no valid feature branch to open a PR from ({branch!r})")
        runbook.status = RUNBOOK_OWNER_ACTION_REQUIRED
        return

    head = _git(worktree, "rev-parse", "HEAD")

    def _view() -> dict[str, Any]:
        result = _run(
            ["gh", "pr", "view", branch, "--repo", _github_repo(github_remote), "--json", "number,state,url,headRefOid"],
            worktree, timeout=30,
        )
        if result.returncode != 0:
            return {}
        try:
            return json.loads(result.stdout)
        except ValueError:
            return {}

    pr = _view()
    # ENG-AGENT-13 independent-review finding (round 3): a MERGED/CLOSED PR
    # for a reused branch name is not a currently-open PR for this candidate
    # -- only an OPEN PR satisfies "existing"; anything else must go through
    # `gh pr create` again (a new PR can be opened for the same branch name
    # once its prior PR is no longer open).
    if pr.get("state") != "OPEN":
        create = _run(
            ["gh", "pr", "create", "--repo", _github_repo(github_remote), "--head", branch, "--fill"],
            worktree, timeout=60,
        )
        if create.returncode != 0:
            evidence[STAGE_PR_READINESS] = _evidence(
                EVIDENCE_FAIL,
                "automated PR creation is not available under current authority: "
                f"{(create.stderr or create.stdout).strip()[:800]}",
            )
            runbook.status = RUNBOOK_OWNER_ACTION_REQUIRED
            return
        pr = _view()

    if pr.get("state") != "OPEN":
        evidence[STAGE_PR_READINESS] = _evidence(EVIDENCE_FAIL, "PR metadata is not resolvable after creation attempt")
        runbook.status = RUNBOOK_OWNER_ACTION_REQUIRED
        return
    if head and pr.get("headRefOid") != head:
        # ENG-AGENT-13 independent-review finding (round 3): never accept an
        # OPEN PR whose head SHA does not match the checkpointed HEAD -- a
        # stale gh view could otherwise report SUCCEEDED for a PR that does
        # not actually contain the exact-tree candidate just pushed.
        evidence[STAGE_PR_READINESS] = _evidence(
            EVIDENCE_FAIL,
            f"open PR head {pr.get('headRefOid')!r} does not match the checkpointed HEAD {head!r}",
            pr_number=pr.get("number"), pr_url=pr.get("url"),
        )
        runbook.status = RUNBOOK_OWNER_ACTION_REQUIRED
        return

    evidence[STAGE_PR_READINESS] = _evidence(
        EVIDENCE_PASS,
        "PR is open, tracked, and matches the checkpointed HEAD; automatic merge is never performed by this pipeline",
        pr_number=pr.get("number"),
        pr_url=pr.get("url"),
        head_sha=pr.get("headRefOid"),
    )
    runbook.acceptance_stage = STAGE_DONE
    runbook.status = RUNBOOK_ACCEPTANCE_PENDING
