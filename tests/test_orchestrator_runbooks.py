"""ENG-AGENT-02-S5 (issue #93): durable Runbooks — model, presets, lifecycle, safety."""

from __future__ import annotations

import dataclasses
import os
import subprocess
from pathlib import Path

import pytest

from scripts.agents.control_plane import acceptance, runbooks
from scripts.agents.control_plane.models import (
    LAUNCH_SESSION,
    PERMISSION_REPO_CONFIGURED_AUTO,
    PERMISSION_STANDARD,
    RUNBOOK_ACCEPTANCE_PENDING,
    RUNBOOK_BLOCKED,
    RUNBOOK_DEADLINE_REACHED,
    RUNBOOK_DRAFT,
    RUNBOOK_FAILED,
    RUNBOOK_IMPLEMENTATION_COMPLETE,
    RUNBOOK_OWNER_ACTION_REQUIRED,
    RUNBOOK_PAUSED,
    RUNBOOK_RUNNING,
    RUNBOOK_STOPPING,
    RUNBOOK_SUCCEEDED,
    TASK_CANCELLED,
    TASK_FAILED,
    TASK_RUNNING,
    TASK_SUCCEEDED,
    Task,
    utc_now_iso,
)
from scripts.agents.control_plane.state import State
from scripts.agents.control_plane.supervisor import Supervisor
from scripts.agents.registry import Registry, load_registry


def _registry_with(**overrides) -> Registry:
    """Clone the real registry, replacing one worker's fields for a test.

    Used to exercise branches (e.g. a read-only Claude Code worker) that no
    real ``workers.json`` entry currently reaches, per Grok Build's
    independent review (issue #94): the plain ``create_runbook`` read-only
    guard was previously untested because every real Claude Code worker is
    write-capable.
    """

    base = load_registry()
    name = overrides.pop("name")
    worker = dataclasses.replace(base.get(name), **overrides)
    workers = dict(base.workers)
    workers[name] = worker
    return Registry(workers=workers, routes=base.routes, intensities=base.intensities)


def _git_worktree(tmp_path: Path, *, branch: str = "eng/test-runbook") -> Path:
    subprocess.run(["git", "init", "-q", "-b", branch, str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    (tmp_path / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", "seed"], check=True)
    return tmp_path


# --------------------------------------------------------------------------- presets


def test_five_required_presets_are_registered():
    keys = {p["key"] for p in runbooks.list_presets()}
    assert keys == {"overnight-development", "finish-pr", "test-fix", "ui-polish", "review-only"}


def test_preset_catalog_never_carries_credential_shaped_values():
    for preset in runbooks.list_presets():
        assert "api_key" not in preset and "token" not in preset and "password" not in preset
        assert "credential" not in preset and "secret_value" not in preset


def test_review_only_preset_is_read_only_and_has_no_checkpoint_policy():
    preset = runbooks.get_preset("review-only")
    assert preset.writes_code is False
    assert preset.checkpoint_policy == "none"
    assert preset.role == "diff-review"


def test_overnight_preset_defaults_to_repo_configured_auto_profile():
    preset = runbooks.get_preset("overnight-development")
    assert preset.permission_profile == PERMISSION_REPO_CONFIGURED_AUTO
    assert preset.default_duration_minutes == 480


# --------------------------------------------------------------------------- create/update


def test_create_runbook_from_preset_defaults_everything():
    state = State(":memory:")
    registry = load_registry()
    rb = runbooks.create_runbook(
        state=state,
        registry=registry,
        name="",
        preset="finish-pr",
        source_ref="PR #92",
        branch="eng/ENG-AGENT-02-S4-ui-redesign",
        worktree="/tmp/does-not-need-to-exist-for-create",
    )
    assert rb.status == RUNBOOK_DRAFT
    assert rb.name == "Finish PR"
    assert "PR #92" in rb.objective
    assert rb.max_duration_minutes == 240
    assert rb.parent_worker == "claude-code"
    assert all(rb.safety_profile.values()), "every safety prohibition must default to True"
    assert state.get_runbook(rb.id) is not None


def test_create_runbook_auto_provisions_a_review_checkout_from_a_pr_reference(tmp_path, monkeypatch):
    """ENG-AGENT-14 (issue #140): "Review Only" + a PR reference (not an

    existing worktree path) must auto-provision an isolated, exact-head
    review checkout rather than requiring the operator to have already
    created and registered one by hand.
    """

    from scripts.agents.control_plane import provisioning

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo_root, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo_root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_root, check=True)
    (repo_root / "README.md").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo_root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo_root, check=True)
    head_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_root, capture_output=True, text=True, check=True,
    ).stdout.strip()
    monkeypatch.setattr(
        provisioning, "resolve_pr_reference", lambda **kwargs: (139, "eng/reviewed-branch", head_sha),
    )

    state = State(":memory:")
    rb = runbooks.create_runbook(
        state=state, registry=load_registry(), name="x", preset="review-only", source_ref="PR #139",
        branch="unused", worktree="pr:139", repo_root=repo_root,
    )

    assert rb.worktree != "pr:139"
    assert Path(rb.worktree).is_dir()
    assert rb.branch == "eng/reviewed-branch"
    checked_out = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=rb.worktree, capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert checked_out == head_sha

    registered = next(w for w in state.list_worktrees() if w.path == rb.worktree)
    assert registered.managed is True
    assert registered.review_pr == 139
    assert registered.review_head_sha == head_sha
    assert registered.branch is None  # detached


def test_create_runbook_does_not_treat_a_real_worktree_path_as_a_pr_reference():
    """Even for the diff-review role, a genuine absolute worktree path (which

    may happen to end in digits, e.g. "...-139") must never be misidentified
    as a PR reference -- validate_target already requires every real
    worktree path to be absolute, and _looks_like_pr_reference only matches
    bare numbers/"pr:<n>"/PR URLs, none of which are ever absolute paths.
    """

    state = State(":memory:")
    rb = runbooks.create_runbook(
        state=state, registry=load_registry(), name="x", preset="review-only", source_ref="x",
        branch="b", worktree="/tmp/a-real-worktree-path-139",
    )
    assert rb.worktree == "/tmp/a-real-worktree-path-139"


def test_create_runbook_rejects_relaxed_safety_profile():
    state = State(":memory:")
    registry = load_registry()
    with pytest.raises(runbooks.RunbookError, match="cannot relax"):
        runbooks.create_runbook(
            state=state,
            registry=registry,
            name="x",
            preset="test-fix",
            source_ref="ENG-X",
            branch="b",
            worktree="/tmp/x",
            safety_profile={"forbid_force_push": False},
        )


def test_create_runbook_rejects_unknown_preset():
    state = State(":memory:")
    registry = load_registry()
    with pytest.raises(runbooks.RunbookError, match="unknown preset"):
        runbooks.create_runbook(
            state=state, registry=registry, name="x", preset="nope", source_ref="x", branch="b", worktree="/tmp/x"
        )


def test_create_runbook_rejects_out_of_range_duration():
    state = State(":memory:")
    registry = load_registry()
    with pytest.raises(runbooks.RunbookError, match="duration_minutes"):
        runbooks.create_runbook(
            state=state,
            registry=registry,
            name="x",
            preset="test-fix",
            source_ref="x",
            branch="b",
            worktree="/tmp/x",
            duration_minutes=5000,
        )


def test_repo_configured_auto_profile_is_rejected_for_a_non_claude_worker():
    state = State(":memory:")
    registry = load_registry()
    with pytest.raises(runbooks.RunbookError, match="repo_configured_auto"):
        runbooks.create_runbook(
            state=state,
            registry=registry,
            name="x",
            preset="review-only",
            source_ref="x",
            branch="b",
            worktree="/tmp/x",
            parent_worker="grok-build-review",
            permission_profile=PERMISSION_REPO_CONFIGURED_AUTO,
        )


def test_update_runbook_edits_objective_before_launch():
    state = State(":memory:")
    registry = load_registry()
    rb = runbooks.create_runbook(
        state=state, registry=registry, name="x", preset="test-fix", source_ref="x", branch="b", worktree="/tmp/x"
    )
    updated = runbooks.update_runbook(state=state, registry=registry, runbook_id=rb.id, objective="new objective text")
    assert updated.objective == "new objective text"


def test_update_runbook_is_rejected_once_running():
    state = State(":memory:")
    registry = load_registry()
    rb = runbooks.create_runbook(
        state=state, registry=registry, name="x", preset="test-fix", source_ref="x", branch="b", worktree="/tmp/x"
    )
    rb.status = RUNBOOK_RUNNING
    state.upsert_runbook(rb)
    with pytest.raises(runbooks.RunbookError, match="only a DRAFT runbook"):
        runbooks.update_runbook(state=state, registry=registry, runbook_id=rb.id, objective="too late")


# --------------------------------------------------------------------------- validation


def test_validate_target_rejects_a_worktree_that_is_not_a_registered_git_worktree(tmp_path):
    state = State(":memory:")
    stray = tmp_path / "not-a-worktree"
    stray.mkdir()
    with pytest.raises(runbooks.RunbookError, match="not a registered git worktree"):
        runbooks.validate_target(repo_root=tmp_path, state=state, branch="main", worktree=str(stray))


def test_validate_target_rejects_a_second_concurrent_runbook_on_the_same_worktree(tmp_path):
    repo = _git_worktree(tmp_path)
    state = State(":memory:")
    registry = load_registry()
    rb = runbooks.create_runbook(
        state=state,
        registry=registry,
        name="x",
        preset="test-fix",
        source_ref="x",
        branch="eng/test-runbook",
        worktree=str(repo),
    )
    rb.status = RUNBOOK_RUNNING
    state.upsert_runbook(rb)
    with pytest.raises(runbooks.RunbookError, match="already has an active runbook"):
        runbooks.validate_target(repo_root=repo, state=state, branch="eng/test-runbook", worktree=str(repo))


def test_validate_target_rejects_a_worktree_still_in_the_acceptance_pipeline(tmp_path):
    """ENG-AGENT-13 independent-review finding (round 3): the acceptance

    pipeline (git add -A, run_gate, commit, push) actively mutates this
    worktree while a runbook sits at IMPLEMENTATION_COMPLETE/
    ACCEPTANCE_PENDING -- one-writer-per-checkout must cover that window,
    not only RUNNING/PAUSED/STOPPING.
    """

    repo = _git_worktree(tmp_path)
    state = State(":memory:")
    registry = load_registry()
    rb = runbooks.create_runbook(
        state=state, registry=registry, name="x", preset="test-fix", source_ref="x",
        branch="eng/test-runbook", worktree=str(repo),
    )
    rb.status = RUNBOOK_IMPLEMENTATION_COMPLETE
    state.upsert_runbook(rb)
    with pytest.raises(runbooks.RunbookError, match="already has an active runbook"):
        runbooks.validate_target(repo_root=repo, state=state, branch="eng/test-runbook", worktree=str(repo))

    rb.status = RUNBOOK_ACCEPTANCE_PENDING
    state.upsert_runbook(rb)
    with pytest.raises(runbooks.RunbookError, match="already has an active runbook"):
        runbooks.validate_target(repo_root=repo, state=state, branch="eng/test-runbook", worktree=str(repo))


def test_validate_target_passes_for_a_clean_registered_worktree(tmp_path):
    repo = _git_worktree(tmp_path)
    state = State(":memory:")
    runbooks.validate_target(repo_root=repo, state=state, branch="eng/test-runbook", worktree=str(repo))


# --------------------------------------------------------------------------- prompt


def test_session_prompt_embeds_objective_safety_and_deadline():
    state = State(":memory:")
    registry = load_registry()
    rb = runbooks.create_runbook(
        state=state,
        registry=registry,
        name="Overnight run",
        preset="overnight-development",
        source_ref="PR #92",
        branch="b",
        worktree="/tmp/x",
        duration_minutes=120,
    )
    prompt = runbooks.build_session_prompt(rb, registry=registry)
    assert rb.objective in prompt
    assert "120 minutes" in prompt
    assert "force push" in prompt.lower() or "force-push" in prompt.lower()
    assert rb.id in prompt
    assert "Never merge or enable auto-merge" in prompt


# --------------------------------------------------------------------------- start/pause/resume/stop


def _fake_running_supervisor(monkeypatch, *, pid: int = 4242):
    class FakeProcess:
        def __init__(self):
            self.pid = pid

    def _spawn(self, argv, cwd):  # noqa: ARG001
        return FakeProcess()

    monkeypatch.setattr(Supervisor, "_spawn", _spawn)
    monkeypatch.setattr("scripts.agents.control_plane.supervisor.assert_write_safety", lambda *a, **k: None)


def test_start_runbook_launches_exactly_one_session_task(tmp_path, monkeypatch):
    repo = _git_worktree(tmp_path)
    state = State(":memory:")
    registry = load_registry()
    _fake_running_supervisor(monkeypatch)
    supervisor = Supervisor(registry=registry, repo_root=repo, state=state)

    rb = runbooks.create_runbook(
        state=state,
        registry=registry,
        name="x",
        preset="test-fix",
        source_ref="x",
        branch="eng/test-runbook",
        worktree=str(repo),
        duration_minutes=120,
    )
    started = runbooks.start_runbook(state=state, registry=registry, supervisor=supervisor, repo_root=repo, runbook_id=rb.id)

    assert started.status == RUNBOOK_RUNNING
    assert started.task_id is not None
    task = state.get_task(started.task_id)
    assert task.launch_mode == LAUNCH_SESSION
    assert task.timeout_seconds == 120 * 60
    assert task.state == TASK_RUNNING
    assert len(state.list_tasks()) == 1, "a runbook must create exactly one underlying task"


def test_start_runbook_twice_is_rejected():
    state = State(":memory:")
    registry = load_registry()
    rb = runbooks.create_runbook(
        state=state, registry=registry, name="x", preset="test-fix", source_ref="x", branch="b", worktree="/tmp/x"
    )
    rb.status = RUNBOOK_RUNNING
    state.upsert_runbook(rb)
    with pytest.raises(runbooks.RunbookError, match="only a DRAFT runbook can be started"):
        runbooks.start_runbook(state=state, registry=registry, supervisor=None, repo_root=Path("/tmp"), runbook_id=rb.id)


def test_stop_runbook_cancels_the_underlying_task_without_force_killing(tmp_path, monkeypatch):
    repo = _git_worktree(tmp_path)
    state = State(":memory:")
    registry = load_registry()
    _fake_running_supervisor(monkeypatch)
    supervisor = Supervisor(registry=registry, repo_root=repo, state=state)
    rb = runbooks.create_runbook(
        state=state, registry=registry, name="x", preset="test-fix", source_ref="x", branch="eng/test-runbook", worktree=str(repo)
    )
    runbooks.start_runbook(state=state, registry=registry, supervisor=supervisor, repo_root=repo, runbook_id=rb.id)

    stopped = runbooks.stop_runbook(state=state, runbook_id=rb.id)
    assert stopped.status == RUNBOOK_STOPPING
    task = state.get_task(stopped.task_id)
    assert task.state == TASK_CANCELLED


def test_pause_then_resume_a_not_yet_launched_task(tmp_path, monkeypatch):
    repo = _git_worktree(tmp_path)
    state = State(":memory:")
    registry = load_registry()

    # Never actually launched: simulate a runbook whose task is still PENDING.
    rb = runbooks.create_runbook(
        state=state, registry=registry, name="x", preset="test-fix", source_ref="x", branch="eng/test-runbook", worktree=str(repo)
    )
    task = Task(id="t-manual", task_ref="x", role="primary-implementation", worker="claude-code")
    state.upsert_task(task)
    rb.task_id = task.id
    state.upsert_runbook(rb)

    paused = runbooks.pause_runbook(state=state, runbook_id=rb.id)
    assert paused.status == RUNBOOK_PAUSED
    resumed = runbooks.resume_runbook(state=state, runbook_id=rb.id)
    assert resumed.status == RUNBOOK_RUNNING


# --------------------------------------------------------------------------- reconciliation


def test_reconcile_runbooks_does_not_finalize_succeeded_until_acceptance_passes(tmp_path):
    """ENG-AGENT-13 (issue #138): worker exit 0 is the implementation attempt

    succeeding, not the runbook. With no exact-tree candidate available (this
    worktree is not even a Git repository), the acceptance pipeline's Test
    stage truthfully fails and the runbook halts BLOCKED -- it must never be
    reported terminal SUCCEEDED on worker exit alone.
    """

    state = State(":memory:")
    task = Task(id="t1", task_ref="x", role="primary-implementation", worker="claude-code", state=TASK_SUCCEEDED, result="PASS")
    state.upsert_task(task)
    rb = runbooks.create_runbook(
        state=state, registry=load_registry(), name="x", preset="test-fix", source_ref="x", branch="b", worktree=str(tmp_path)
    )
    rb.task_id = task.id
    rb.status = RUNBOOK_RUNNING
    rb.started_at = utc_now_iso()
    state.upsert_runbook(rb)

    result = runbooks.reconcile_runbooks(state=state, repo_root=tmp_path)
    assert result["finalized"] == 1
    finalized = state.get_runbook(rb.id)
    assert finalized.status != RUNBOOK_SUCCEEDED
    assert finalized.status == RUNBOOK_BLOCKED
    assert finalized.acceptance_evidence["implementation"]["status"] == "PASS"
    assert finalized.acceptance_evidence["test"]["status"] == "FAIL"
    assert finalized.report_markdown is not None
    assert "test" in finalized.report_markdown
    assert (tmp_path / ".orchestrator-state" / "reports" / f"{rb.id}.md").exists()


def test_reconcile_runbooks_holds_the_acceptance_pipeline_while_paused(tmp_path):
    """ENG-AGENT-13 final-review finding: pause_runbook's own docstring says

    pausing "only prevents the daemon from scheduling further work for this
    runbook" -- the acceptance pipeline (git add -A, run_gate, commit, push,
    gh pr create) is exactly that. If the implementation worker exits 0 while
    the runbook is PAUSED, reconcile must not start or advance acceptance at
    all until the operator resumes it.
    """

    state = State(":memory:")
    task = Task(id="t1", task_ref="x", role="primary-implementation", worker="claude-code", state=TASK_SUCCEEDED, result="PASS")
    state.upsert_task(task)
    rb = runbooks.create_runbook(
        state=state, registry=load_registry(), name="x", preset="test-fix", source_ref="x", branch="b", worktree=str(tmp_path)
    )
    rb.task_id = task.id
    rb.status = RUNBOOK_PAUSED
    rb.started_at = utc_now_iso()
    state.upsert_runbook(rb)

    result = runbooks.reconcile_runbooks(state=state, repo_root=tmp_path)

    assert result["finalized"] == 0
    held = state.get_runbook(rb.id)
    assert held.status == RUNBOOK_PAUSED
    assert held.acceptance_evidence == {}
    assert held.report_markdown is None


def test_reconcile_runbooks_stop_after_implementation_success_halts_owner_action_required(tmp_path):
    """ENG-AGENT-13 final-review finding: stop_runbook's docstring assumed

    nothing further would run once the task reached a terminal state -- true
    before the acceptance pipeline existed, false now (it performs new
    commit/push/PR work after a successful implementation exit). A stop
    requested during/after implementation must halt the runbook truthfully
    rather than silently letting the pipeline run to completion.
    """

    state = State(":memory:")
    task = Task(id="t1", task_ref="x", role="primary-implementation", worker="claude-code", state=TASK_SUCCEEDED, result="PASS")
    state.upsert_task(task)
    rb = runbooks.create_runbook(
        state=state, registry=load_registry(), name="x", preset="test-fix", source_ref="x", branch="b", worktree=str(tmp_path)
    )
    rb.task_id = task.id
    rb.status = RUNBOOK_STOPPING
    rb.started_at = utc_now_iso()
    state.upsert_runbook(rb)

    result = runbooks.reconcile_runbooks(state=state, repo_root=tmp_path)

    assert result["finalized"] == 1
    halted = state.get_runbook(rb.id)
    assert halted.status == RUNBOOK_OWNER_ACTION_REQUIRED
    assert halted.status != RUNBOOK_SUCCEEDED
    # Round-2 final-review finding: implementation evidence IS recorded and
    # acceptance_stage IS set to a real, resumable stage here -- halting
    # without doing so left the documented recovery path (Resume acceptance)
    # unable to ever start the pipeline (acceptance_stage stayed at the bare
    # default "PENDING", which advance_acceptance_pipeline's own defensive
    # unknown-stage branch rejects as BLOCKED).
    assert halted.acceptance_evidence["implementation"]["status"] == "PASS"
    assert halted.acceptance_stage == acceptance.STAGE_TEST
    assert "stop" in halted.recovery_note.lower()
    assert halted.report_markdown is not None


def test_resuming_a_stop_before_acceptance_actually_completes_the_pipeline(tmp_path, monkeypatch):
    """Round-2 final-review finding: the documented recovery from a stop-before-

    acceptance halt (Resume acceptance / runbook_retry_acceptance) must
    actually be able to run the pipeline to completion, not dead-end on an
    unknown acceptance_stage.
    """

    repo = _git_worktree(tmp_path)
    state = State(":memory:")
    task = Task(
        id="t1", task_ref="x", role="primary-implementation", worker="claude-code",
        state=TASK_SUCCEEDED, result="PASS", worktree=str(repo),
    )
    state.upsert_task(task)
    rb = runbooks.create_runbook(
        state=state, registry=load_registry(), name="x", preset="test-fix", source_ref="x",
        branch="eng/test-runbook", worktree=str(repo),
    )
    rb.task_id = task.id
    rb.status = RUNBOOK_STOPPING
    rb.started_at = utc_now_iso()
    state.upsert_runbook(rb)

    result = runbooks.reconcile_runbooks(state=state, repo_root=repo)
    assert result["finalized"] == 1
    halted = state.get_runbook(rb.id)
    assert halted.status == RUNBOOK_OWNER_ACTION_REQUIRED

    resumed = runbooks.retry_acceptance(state=state, runbook_id=rb.id)
    assert resumed.status == RUNBOOK_ACCEPTANCE_PENDING
    assert resumed.acceptance_stage == acceptance.STAGE_TEST

    from scripts.agents.control_plane import acceptance as acceptance_module

    monkeypatch.setattr(acceptance_module, "classify", lambda paths: type("R", (), {"review_level": "low"})())
    monkeypatch.setattr(
        acceptance_module, "run_gate", lambda **kwargs: {"result": "pass", "evidence_path": ".local-gate/fake.json"},
    )
    task = state.get_task(rb.task_id)
    acceptance_module.advance_acceptance_pipeline(
        state=state, repo_root=repo, registry=None, scheduler=None, supervisor=None,
        runbook=resumed, task=task, base_ref="HEAD",
    )

    assert resumed.acceptance_evidence["test"]["status"] == "PASS"
    assert resumed.acceptance_stage == acceptance.STAGE_CHECKPOINT
    assert resumed.status != RUNBOOK_BLOCKED


def test_stop_after_current_halts_before_acceptance_once_the_worker_finishes(tmp_path):
    """Round-2 final-review finding: stop_after_current_runbook used to be

    purely informational (true before the acceptance pipeline existed).
    Requesting it while a runbook is RUNNING must now actually withhold
    Test/Review/Checkpoint/PR once the current worker finishes, exactly like
    an explicit Stop.
    """

    state = State(":memory:")
    task = Task(id="t1", task_ref="x", role="primary-implementation", worker="claude-code", state=TASK_SUCCEEDED, result="PASS")
    state.upsert_task(task)
    rb = runbooks.create_runbook(
        state=state, registry=load_registry(), name="x", preset="test-fix", source_ref="x", branch="b", worktree=str(tmp_path)
    )
    rb.task_id = task.id
    rb.status = RUNBOOK_RUNNING  # still RUNNING -- unlike an explicit Stop
    rb.started_at = utc_now_iso()
    state.upsert_runbook(rb)

    runbooks.stop_after_current_runbook(state=state, runbook_id=rb.id)
    assert state.get_runbook(rb.id).stop_after_current_requested is True

    result = runbooks.reconcile_runbooks(state=state, repo_root=tmp_path)

    assert result["finalized"] == 1
    halted = state.get_runbook(rb.id)
    assert halted.status == RUNBOOK_OWNER_ACTION_REQUIRED
    assert halted.status != RUNBOOK_SUCCEEDED
    assert halted.stop_after_current_requested is False  # consumed
    assert halted.acceptance_evidence["implementation"]["status"] == "PASS"
    assert halted.acceptance_stage == acceptance.STAGE_TEST


def test_reconcile_runbooks_finalizes_a_failed_task_as_failed(tmp_path):
    state = State(":memory:")
    task = Task(id="t1", task_ref="x", role="primary-implementation", worker="claude-code", state=TASK_FAILED, last_error="boom")
    state.upsert_task(task)
    rb = runbooks.create_runbook(
        state=state, registry=load_registry(), name="x", preset="test-fix", source_ref="x", branch="b", worktree=str(tmp_path)
    )
    rb.task_id = task.id
    rb.status = RUNBOOK_RUNNING
    state.upsert_runbook(rb)

    runbooks.reconcile_runbooks(state=state, repo_root=tmp_path)
    assert state.get_runbook(rb.id).status == RUNBOOK_FAILED


def test_reconcile_runbooks_flags_deadline_without_touching_a_live_task(tmp_path):
    state = State(":memory:")
    task = Task(id="t1", task_ref="x", role="primary-implementation", worker="claude-code", state=TASK_RUNNING)
    state.upsert_task(task)
    rb = runbooks.create_runbook(
        state=state, registry=load_registry(), name="x", preset="test-fix", source_ref="x", branch="b", worktree=str(tmp_path)
    )
    rb.task_id = task.id
    rb.status = RUNBOOK_RUNNING
    rb.deadline_at = "2000-01-01T00:00:00+00:00"  # long past
    state.upsert_runbook(rb)

    result = runbooks.reconcile_runbooks(state=state, repo_root=tmp_path)
    assert result["deadline_flagged"] == 1
    flagged = state.get_runbook(rb.id)
    assert flagged.status == RUNBOOK_DEADLINE_REACHED
    # The live task itself is never force-killed/mutated by reconciliation.
    assert state.get_task(task.id).state == TASK_RUNNING


def test_reconcile_runbooks_never_raises_on_a_bad_state_write(tmp_path, monkeypatch):
    state = State(":memory:")
    task = Task(id="t1", task_ref="x", role="primary-implementation", worker="claude-code", state=TASK_SUCCEEDED)
    state.upsert_task(task)
    rb = runbooks.create_runbook(
        state=state, registry=load_registry(), name="x", preset="test-fix", source_ref="x", branch="b", worktree=str(tmp_path)
    )
    rb.task_id = task.id
    rb.status = RUNBOOK_RUNNING
    state.upsert_runbook(rb)

    def _boom(*_a, **_k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(state, "upsert_runbook", _boom)
    result = runbooks.reconcile_runbooks(state=state, repo_root=tmp_path)  # must not raise
    assert result["finalized"] == 0


# --------------------------------------------------------------------------- ENG-AGENT-15 runtime staleness


def test_reconcile_runbooks_halts_instead_of_succeeding_when_runtime_is_stale(tmp_path, monkeypatch):
    """ENG-AGENT-15 (issue #142): a real V1-05 run finalized SUCCEEDED with

    zero acceptance evidence because the long-running daemon serving it had
    started hours before ENG-AGENT-13 merged this pipeline into main and was
    never restarted -- it was still executing pre-acceptance bytecode. A
    process that detects it is running acceptance logic older than what is
    checked out on disk must halt on operator authority instead of finalizing
    SUCCEEDED (or advancing any stage) using logic it knows is stale.
    """

    state = State(":memory:")
    task = Task(id="t1", task_ref="x", role="primary-implementation", worker="claude-code", state=TASK_SUCCEEDED, result="PASS")
    state.upsert_task(task)
    rb = runbooks.create_runbook(
        state=state, registry=load_registry(), name="x", preset="test-fix", source_ref="x", branch="b", worktree=str(tmp_path)
    )
    rb.task_id = task.id
    rb.status = RUNBOOK_RUNNING
    rb.started_at = utc_now_iso()
    state.upsert_runbook(rb)

    monkeypatch.setattr(
        runbooks, "check_runtime_freshness",
        lambda **kwargs: "Control Plane runtime staleness detected: fixture contract mismatch",
    )

    result = runbooks.reconcile_runbooks(state=state, repo_root=tmp_path)

    assert result["finalized"] == 1
    halted = state.get_runbook(rb.id)
    assert halted.status == RUNBOOK_OWNER_ACTION_REQUIRED
    assert halted.status != RUNBOOK_SUCCEEDED
    assert "staleness" in halted.recovery_note
    # Implementation genuinely did pass -- that truthful evidence is still
    # recorded even though no further stage was attempted.
    assert halted.acceptance_evidence["implementation"]["status"] == "PASS"
    assert "test" not in halted.acceptance_evidence
    assert halted.acceptance_stage == acceptance.STAGE_TEST
    assert halted.report_markdown is not None


def test_stale_runtime_halt_is_idempotent_across_repeated_reconciliation(tmp_path, monkeypatch):
    """A stale process keeps polling every tick; it must halt exactly once and

    never re-dispatch or re-finalize the same runbook on every subsequent
    tick while nobody has restarted it.
    """

    state = State(":memory:")
    task = Task(id="t1", task_ref="x", role="primary-implementation", worker="claude-code", state=TASK_SUCCEEDED, result="PASS")
    state.upsert_task(task)
    rb = runbooks.create_runbook(
        state=state, registry=load_registry(), name="x", preset="test-fix", source_ref="x", branch="b", worktree=str(tmp_path)
    )
    rb.task_id = task.id
    rb.status = RUNBOOK_RUNNING
    rb.started_at = utc_now_iso()
    state.upsert_runbook(rb)

    monkeypatch.setattr(runbooks, "check_runtime_freshness", lambda **kwargs: "stale fixture reason")

    first = runbooks.reconcile_runbooks(state=state, repo_root=tmp_path)
    assert first["finalized"] == 1
    events_after_first = len(state.list_events())

    second = runbooks.reconcile_runbooks(state=state, repo_root=tmp_path)
    third = runbooks.reconcile_runbooks(state=state, repo_root=tmp_path)

    assert second["finalized"] == 0
    assert third["finalized"] == 0
    # No duplicate halt event, no duplicate report regeneration event, on
    # every later tick -- the top-of-loop terminal-state skip does the work.
    assert len(state.list_events()) == events_after_first
    assert state.get_runbook(rb.id).status == RUNBOOK_OWNER_ACTION_REQUIRED


def test_restart_after_stale_halt_resumes_and_completes_acceptance(tmp_path, monkeypatch):
    """Once an operator restarts the Control Plane (simulated here by the

    freshness check going back to ``None``, as a real restart re-importing
    current code would produce) and resumes, the preserved implementation
    candidate must proceed through the full pipeline to genuine SUCCEEDED --
    never re-running implementation.
    """

    repo = _git_worktree(tmp_path)
    state = State(":memory:")
    task = Task(
        id="t1", task_ref="x", role="primary-implementation", worker="claude-code",
        state=TASK_SUCCEEDED, result="PASS", worktree=str(repo),
    )
    state.upsert_task(task)
    rb = runbooks.create_runbook(
        state=state, registry=load_registry(), name="x", preset="test-fix", source_ref="x",
        branch="eng/test-runbook", worktree=str(repo),
    )
    rb.task_id = task.id
    rb.status = RUNBOOK_RUNNING
    rb.started_at = utc_now_iso()
    state.upsert_runbook(rb)

    monkeypatch.setattr(runbooks, "check_runtime_freshness", lambda **kwargs: "stale fixture reason")
    result = runbooks.reconcile_runbooks(state=state, repo_root=repo)
    assert result["finalized"] == 1
    halted = state.get_runbook(rb.id)
    assert halted.status == RUNBOOK_OWNER_ACTION_REQUIRED
    original_task_id = halted.task_id

    # "Restart": a freshly-imported process reports fresh, and the operator
    # resumes the halted stage.
    monkeypatch.setattr(runbooks, "check_runtime_freshness", lambda **kwargs: None)
    resumed = runbooks.retry_acceptance(state=state, runbook_id=rb.id)
    assert resumed.status == RUNBOOK_ACCEPTANCE_PENDING
    assert resumed.task_id == original_task_id  # implementation never rerun

    from scripts.agents.control_plane import acceptance as acceptance_module

    monkeypatch.setattr(acceptance_module, "classify", lambda paths: type("R", (), {"review_level": "low"})())
    monkeypatch.setattr(
        acceptance_module, "run_gate", lambda **kwargs: {"result": "pass", "evidence_path": ".local-gate/fake.json"},
    )
    task = state.get_task(rb.task_id)
    acceptance_module.advance_acceptance_pipeline(
        state=state, repo_root=repo, registry=None, scheduler=None, supervisor=None,
        runbook=resumed, task=task, base_ref="HEAD",
    )

    assert resumed.task_id == original_task_id
    assert resumed.acceptance_evidence["test"]["status"] == "PASS"
    assert resumed.acceptance_stage == acceptance.STAGE_CHECKPOINT
    assert resumed.status != RUNBOOK_BLOCKED


# --------------------------------------------------------------------------- supervisor wiring


def test_supervisor_builds_session_argv_with_timeout_for_launch_session_tasks(tmp_path):
    registry = load_registry()
    state = State(":memory:")
    supervisor = Supervisor(registry=registry, repo_root=tmp_path, state=state)
    task = Task(
        id="RB-abc-session",
        task_ref="ENG-AGENT-02-S5",
        role="primary-implementation",
        worker="claude-code",
        launch_mode=LAUNCH_SESSION,
        timeout_seconds=28800,
        command=("the full session prompt",),
    )
    argv = supervisor._build_argv(task, dry_run=True)
    assert "session" in argv
    assert "run" not in argv or argv[argv.index("run") - 1] != "orchestrate"
    assert "--timeout" in argv
    assert argv[argv.index("--timeout") + 1] == "28800"
    assert argv[-1] == "the full session prompt"
    assert "--dry-run" in argv


def test_real_dry_run_session_end_to_end_spawns_no_worker_cli(tmp_path):
    """One real, cheap subprocess call through the new `session` CLI verb, always dry-run."""

    import json as _json
    import os
    import shutil
    import sys

    repo = tmp_path / "session-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "checkout", "-b", "eng/session-test"], cwd=repo, check=True)
    (repo / "README.md").write_text("session\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    task_ref = "ENG-AGENT-02-S5-SESSION-TEST"
    evidence_dir = repo / ".agent-output" / task_ref
    registry = load_registry()
    state = State(":memory:")
    supervisor = Supervisor(registry=registry, repo_root=repo, state=state)
    task = Task(
        id="dryrun-session",
        task_ref=task_ref,
        role="primary-implementation",
        worker="claude-code",
        worktree=str(repo),
        launch_mode=LAUNCH_SESSION,
        timeout_seconds=60,
        command=("describe the registry",),
    )
    try:
        result = supervisor.launch_task(task, dry_run=True)
        assert result.state == TASK_RUNNING
        assert result.pid != os.getpid()
        assert sys.executable

        process = supervisor._processes[task.id]
        return_code = process.wait(timeout=30)
        assert return_code == 0
        manifests = list((evidence_dir / "claude-code").glob("*/manifest.json"))
        assert len(manifests) == 1
        payload = _json.loads(manifests[0].read_text(encoding="utf-8"))
        assert payload["result"] == "DRY_RUN"
    finally:
        shutil.rmtree(evidence_dir, ignore_errors=True)


# ------------------------------------------- unattended permission-profile plumbing (issue #94)


def test_starting_an_overnight_runbook_carries_repo_configured_auto_onto_the_task(tmp_path, monkeypatch):
    """Runbook.permission_profile -> Task.permission_profile -> session argv, end to end.

    Confirms the gap reported in issue #94: previously the Task created by
    ``start_runbook`` never carried the Runbook's ``permission_profile`` at
    all, so the launched Claude session always used the default
    ``--permission-mode manual`` invocation regardless of the preset.
    """

    repo = _git_worktree(tmp_path)
    state = State(":memory:")
    registry = load_registry()
    _fake_running_supervisor(monkeypatch)
    supervisor = Supervisor(registry=registry, repo_root=repo, state=state)

    rb = runbooks.create_runbook(
        state=state,
        registry=registry,
        name="Overnight",
        preset="overnight-development",
        source_ref="PR #94",
        branch="eng/test-runbook",
        worktree=str(repo),
        duration_minutes=120,
    )
    assert rb.permission_profile == PERMISSION_REPO_CONFIGURED_AUTO

    started = runbooks.start_runbook(state=state, registry=registry, supervisor=supervisor, repo_root=repo, runbook_id=rb.id)
    task = state.get_task(started.task_id)
    assert task.permission_profile == PERMISSION_REPO_CONFIGURED_AUTO

    argv = supervisor._build_argv(task, dry_run=True)
    assert "--permission-profile" in argv
    assert argv[argv.index("--permission-profile") + 1] == PERMISSION_REPO_CONFIGURED_AUTO


def test_starting_a_standard_profile_runbook_omits_the_permission_profile_flag(tmp_path, monkeypatch):
    """A standard-profile (default) runbook must produce byte-identical argv to before

    this feature existed: no ``--permission-profile`` flag at all, since the
    worker's own default (manual) invocation already applies.
    """

    repo = _git_worktree(tmp_path)
    state = State(":memory:")
    registry = load_registry()
    _fake_running_supervisor(monkeypatch)
    supervisor = Supervisor(registry=registry, repo_root=repo, state=state)

    rb = runbooks.create_runbook(
        state=state,
        registry=registry,
        name="Finish it",
        preset="finish-pr",
        source_ref="PR #92",
        branch="eng/test-runbook",
        worktree=str(repo),
        duration_minutes=120,
    )
    assert rb.permission_profile == PERMISSION_STANDARD

    started = runbooks.start_runbook(state=state, registry=registry, supervisor=supervisor, repo_root=repo, runbook_id=rb.id)
    task = state.get_task(started.task_id)
    assert task.permission_profile == PERMISSION_STANDARD

    argv = supervisor._build_argv(task, dry_run=True)
    assert "--permission-profile" not in argv


def test_real_dry_run_overnight_session_builds_the_unattended_claude_invocation(tmp_path):
    """A genuine (dry-run, non-billable) subprocess round trip through `orchestrate session`

    proving the actual argv Claude would be launched with for the unattended
    profile: ``--dangerously-skip-permissions`` and no ``--permission-mode``.
    """

    import json as _json
    import shutil

    repo = tmp_path / "unattended-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "checkout", "-b", "eng/unattended-test"], cwd=repo, check=True)
    (repo / "README.md").write_text("unattended\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    task_ref = "ENG-AGENT-02-S5-UNATTENDED-TEST"
    evidence_dir = repo / ".agent-output" / task_ref
    registry = load_registry()
    state = State(":memory:")
    supervisor = Supervisor(registry=registry, repo_root=repo, state=state)
    task = Task(
        id="dryrun-unattended-session",
        task_ref=task_ref,
        role="primary-implementation",
        worker="claude-code",
        worktree=str(repo),
        launch_mode=LAUNCH_SESSION,
        timeout_seconds=60,
        permission_profile=PERMISSION_REPO_CONFIGURED_AUTO,
        command=("finish the overnight runbook",),
    )
    try:
        result = supervisor.launch_task(task, dry_run=True)
        assert result.state == TASK_RUNNING
        process = supervisor._processes[task.id]
        return_code = process.wait(timeout=30)
        assert return_code == 0

        manifests = list((evidence_dir / "claude-code").glob("*/manifest.json"))
        assert len(manifests) == 1
        payload = _json.loads(manifests[0].read_text(encoding="utf-8"))
        assert payload["result"] == "DRY_RUN"
        requested_command = payload["requested_command"]
        assert "--dangerously-skip-permissions" in requested_command
        assert "--permission-mode" not in requested_command
        assert any("permission_profile=repo_configured_auto" in note for note in payload["notes"])
    finally:
        shutil.rmtree(evidence_dir, ignore_errors=True)


def test_repo_configured_auto_is_rejected_for_review_only_even_if_explicitly_requested():
    """review-only stays read-only: it can never gain the unattended write profile,

    even when the caller explicitly asks for it (not just via the preset default).
    """

    state = State(":memory:")
    registry = load_registry()
    with pytest.raises(runbooks.RunbookError, match="read-only|repo_configured_auto"):
        runbooks.create_runbook(
            state=state,
            registry=registry,
            name="x",
            preset="review-only",
            source_ref="x",
            branch="b",
            worktree="/tmp/x",
            permission_profile=PERMISSION_REPO_CONFIGURED_AUTO,
        )


def test_permission_profile_survives_task_persistence_reload(tmp_path):
    state = State(":memory:")
    task = Task(
        id="persist-1",
        task_ref="ENG-AGENT-02-S5",
        role="primary-implementation",
        worker="claude-code",
        permission_profile=PERMISSION_REPO_CONFIGURED_AUTO,
    )
    state.upsert_task(task)
    reloaded = state.get_task(task.id)
    assert reloaded.permission_profile == PERMISSION_REPO_CONFIGURED_AUTO


def test_permission_profile_survives_runbook_persistence_reload(tmp_path):
    state = State(":memory:")
    registry = load_registry()
    rb = runbooks.create_runbook(
        state=state,
        registry=registry,
        name="Overnight",
        preset="overnight-development",
        source_ref="x",
        branch="b",
        worktree=str(tmp_path),
    )
    reloaded = state.get_runbook(rb.id)
    assert reloaded.permission_profile == PERMISSION_REPO_CONFIGURED_AUTO


def test_repo_configured_auto_still_blocked_by_a_real_write_lock_held_by_another_worker(tmp_path, monkeypatch):
    """The unattended permission profile only skips Claude's own tool-approval prompts;

    it must never bypass this framework's own one-write-capable-agent-per-worktree
    write-lock enforcement (real integration, same lock-file mechanism runner.py uses).
    """

    repo = _git_worktree(tmp_path)
    state = State(":memory:")
    registry = load_registry()
    supervisor = Supervisor(registry=registry, repo_root=repo, state=state)

    lock_dir = repo / ".agent-output"
    lock_dir.mkdir()
    (lock_dir / ".write-lock").write_text(f"other-worker pid={os.getpid()} at=0", encoding="utf-8")

    spawned = []
    monkeypatch.setattr(supervisor, "_spawn", lambda argv, cwd: spawned.append(argv) or object())

    rb = runbooks.create_runbook(
        state=state,
        registry=registry,
        name="Overnight",
        preset="overnight-development",
        source_ref="x",
        branch="eng/test-runbook",
        worktree=str(repo),
        duration_minutes=120,
    )
    with pytest.raises(runbooks.RunbookError, match="could not start runbook"):
        runbooks.start_runbook(state=state, registry=registry, supervisor=supervisor, repo_root=repo, runbook_id=rb.id)

    assert spawned == [], "the write-lock must block spawn even for the unattended permission profile"


# ------------------------- re-validation gaps found by Grok Build review (issue #94) ------------------------


def test_create_runbook_rejects_repo_configured_auto_for_a_read_only_claude_code_worker():
    """No real ``claude-code`` registry entry is read-only today, so ``create_runbook``'s

    ``is_read_only`` branch was previously unreachable/untested (Grok Build review):
    a synthetic read-only "Claude Code" worker exercises it directly. Uses the
    read-only ``review-only`` preset (``writes_code=False``) so the earlier,
    unrelated write-capability check does not fire first and mask this branch.
    """

    registry = _registry_with(name="claude-code", capability="read-only", roles=("diff-review",))
    state = State(":memory:")
    with pytest.raises(runbooks.RunbookError, match="cannot be used with a read-only worker"):
        runbooks.create_runbook(
            state=state,
            registry=registry,
            name="x",
            preset="review-only",
            source_ref="x",
            branch="b",
            worktree="/tmp/x",
            parent_worker="claude-code",
            permission_profile=PERMISSION_REPO_CONFIGURED_AUTO,
        )


def test_update_runbook_rejects_retargeting_permission_profile_on_a_read_only_worker():
    """``update_runbook`` must re-apply the same worker gate ``create_runbook`` does:

    a review-only draft (parent_worker=grok-build-review, read-only, non-Claude)
    must not be patchable into ``repo_configured_auto`` after creation.
    """

    state = State(":memory:")
    registry = load_registry()
    rb = runbooks.create_runbook(
        state=state,
        registry=registry,
        name="x",
        preset="review-only",
        source_ref="x",
        branch="b",
        worktree="/tmp/x",
    )
    assert rb.permission_profile == PERMISSION_STANDARD
    with pytest.raises(runbooks.RunbookError, match="claude-code|read-only"):
        runbooks.update_runbook(
            state=state,
            registry=registry,
            runbook_id=rb.id,
            permission_profile=PERMISSION_REPO_CONFIGURED_AUTO,
        )
    assert state.get_runbook(rb.id).permission_profile == PERMISSION_STANDARD, "the rejected edit must not persist"


def test_update_runbook_rejects_a_parent_worker_swap_that_breaks_an_existing_auto_profile():
    """The inverse gap: an overnight (repo_configured_auto) draft's `parent_worker`

    must not be swapped to a worker that cannot support the profile already on it.
    """

    state = State(":memory:")
    registry = load_registry()
    rb = runbooks.create_runbook(
        state=state,
        registry=registry,
        name="x",
        preset="overnight-development",
        source_ref="x",
        branch="b",
        worktree="/tmp/x",
    )
    assert rb.permission_profile == PERMISSION_REPO_CONFIGURED_AUTO
    with pytest.raises(runbooks.RunbookError, match="repo_configured_auto|read-only"):
        runbooks.update_runbook(state=state, registry=registry, runbook_id=rb.id, parent_worker="grok-build")
    assert state.get_runbook(rb.id).parent_worker == "claude-code", "the rejected edit must not persist"


def test_run_session_rejects_repo_configured_auto_for_a_write_capable_non_claude_worker():
    """``grok-build`` is write-capable but declares no ``repo_configured_auto`` template:

    the profile must still be rejected, not just for read-only workers.
    """

    from scripts.agents import orchestrate
    from scripts.agents.validation import ValidationError

    registry = load_registry()
    grok = registry.get("grok-build")
    assert grok.is_write_capable and not grok.is_read_only
    with pytest.raises(ValidationError, match="permission_profile"):
        orchestrate.run_session(
            registry=registry,
            root=Path(__file__).resolve().parents[1],
            task="ENG-AGENT-02-S5",
            worker_name="grok-build",
            role="secondary-implementation",
            model=None,
            intensity="low",
            why="test",
            prompt="do it",
            dry_run=True,
            timeout=30.0,
            permission_profile=PERMISSION_REPO_CONFIGURED_AUTO,
        )


def test_orchestrate_run_cli_no_longer_accepts_a_permission_profile_flag():
    """The general ``run`` verb (ENG-AGENT-01's narrow-scope delegation path) must never

    be a second way to reach the unattended profile; only ``session`` may (Grok Build
    review, issue #94).
    """

    from scripts.agents.orchestrate import build_parser

    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["run", "--task", "ENG-X", "--worker", "claude-code", "--role", "primary-implementation",
             "--why", "test", "--permission-profile", "repo_configured_auto", "--", "do it"]
        )
    # session still accepts it (that is the intended, sole, unattended-capable verb).
    parsed = parser.parse_args(
        ["session", "--task", "ENG-X", "--worker", "claude-code", "--role", "primary-implementation",
         "--why", "test", "--permission-profile", "repo_configured_auto", "--", "do it"]
    )
    assert parsed.permission_profile == PERMISSION_REPO_CONFIGURED_AUTO


def test_build_command_standard_profile_is_byte_identical_to_omitting_the_argument():
    """Locks the "byte-for-byte unchanged" claim as an actual equality assertion

    rather than only spot-checking individual flags (Grok Build review, issue #94).
    """

    registry = load_registry()
    claude = registry.get("claude-code")
    explicit_standard = claude.build_command(model=None, intensity="medium", prompt="x", permission_profile=PERMISSION_STANDARD)
    omitted = claude.build_command(model=None, intensity="medium", prompt="x")
    assert explicit_standard == omitted


def test_tasks_table_migration_adds_permission_profile_with_standard_default(tmp_path):
    """An on-disk database created before this column existed must keep opening,

    and reading back an old row must default to ``PERMISSION_STANDARD`` (mirrors
    the same forward-compatible-migration contract already relied on for
    ``launch_mode``/``timeout_seconds``/``runbook_id``).
    """

    import sqlite3

    db_path = tmp_path / "orchestrator.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            task_ref TEXT NOT NULL,
            role TEXT NOT NULL,
            worker TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'write',
            state TEXT NOT NULL,
            priority INTEGER NOT NULL DEFAULT 0,
            dependencies TEXT NOT NULL DEFAULT '[]',
            pid INTEGER,
            worktree TEXT,
            command TEXT NOT NULL DEFAULT '[]',
            result TEXT,
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO tasks (id, task_ref, role, worker, state, created_at, updated_at) "
        "VALUES ('t1', 'x', 'primary-implementation', 'claude-code', 'PENDING', '2026-01-01T00:00:00+00:00', "
        "'2026-01-01T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()

    state = State(db_path)
    task = state.get_task("t1")
    assert task.permission_profile == PERMISSION_STANDARD


# --------------------------------------------------------------------------- recovery


def test_recover_runbooks_on_restart_annotates_a_reclaimed_task(tmp_path):
    from scripts.agents.control_plane.models import TASK_QUEUED

    state = State(":memory:")
    task = Task(id="t1", task_ref="x", role="primary-implementation", worker="claude-code", state=TASK_QUEUED)
    state.upsert_task(task)
    rb = runbooks.create_runbook(
        state=state, registry=load_registry(), name="x", preset="test-fix", source_ref="x", branch="b", worktree=str(tmp_path)
    )
    rb.task_id = task.id
    rb.status = RUNBOOK_RUNNING
    state.upsert_runbook(rb)

    result = runbooks.recover_runbooks_on_restart(state=state)
    assert result["annotated"] == 1
    assert state.get_runbook(rb.id).recovery_note is not None


def test_recover_runbooks_on_restart_leaves_a_live_task_untouched(tmp_path):
    state = State(":memory:")
    task = Task(id="t1", task_ref="x", role="primary-implementation", worker="claude-code", state=TASK_RUNNING)
    state.upsert_task(task)
    rb = runbooks.create_runbook(
        state=state, registry=load_registry(), name="x", preset="test-fix", source_ref="x", branch="b", worktree=str(tmp_path)
    )
    rb.task_id = task.id
    rb.status = RUNBOOK_RUNNING
    state.upsert_runbook(rb)

    result = runbooks.recover_runbooks_on_restart(state=state)
    assert result["annotated"] == 0
    assert state.get_runbook(rb.id).recovery_note is None
