"""Focused regressions for truthful failed-run recovery and worker fallback."""

from __future__ import annotations

import json
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from scripts.agents.control_plane.models import (
    LAUNCH_SESSION,
    PERMISSION_REPO_CONFIGURED_AUTO,
    RUNBOOK_FAILED,
    TASK_FAILED,
    TASK_RUNNING,
    TASK_SUCCEEDED,
    Runbook,
    Task,
)
from scripts.agents.control_plane.provider_state import reconcile_provider_states
from scripts.agents.control_plane.runbooks import (
    RunbookError,
    automatic_fallback_runbook,
    reconcile_runbooks,
    retry_runbook,
    stable_session_task_ref,
    start_runbook,
)
from scripts.agents.control_plane.state import State
from scripts.agents.control_plane.supervisor import Supervisor
from scripts.agents.control_plane.usage_policy import new_usage_record
from scripts.agents.policy import compose_policy_bundle
from scripts.agents.registry import REASON_AVAILABLE, load_registry
from scripts.agents.runner import structured_failure


def _git_repo(path: Path) -> Path:
    subprocess.run(["git", "init", "-q", "-b", "feature", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    (path / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "fixture"], check=True)
    return path


def test_supervisor_wrapper_runs_from_controller_when_target_predates_agents(tmp_path):
    controller = Path(__file__).resolve().parents[1]
    target = _git_repo(tmp_path / "old-feature-worktree")
    assert not (target / "scripts" / "agents").exists()
    state = State(":memory:")
    supervisor = Supervisor(registry=load_registry(), repo_root=controller, state=state)
    task = Task(
        id="rb-session",
        task_ref="V1-01",
        role="primary-implementation",
        worker="claude-code",
        worktree=str(target),
        launch_mode=LAUNCH_SESSION,
        command=("validation only",),
    )

    launched = supervisor.launch_task(task, dry_run=True)
    supervisor._processes[task.id].wait(timeout=30)
    [finished] = supervisor.poll_once()

    assert launched.state == TASK_RUNNING
    assert finished.result == "PASS"
    manifests = list((target / ".agent-output" / "V1-01" / "claude-code").glob("*/manifest.json"))
    assert len(manifests) == 1


def test_stable_session_ref_strips_display_title_and_has_safe_fallback():
    runbook = Runbook(
        id="RB-2d2efdec66",
        name="Video Editor",
        preset="overnight-development",
        objective="continue",
        source_ref="V1-01 (Local import.)",
        branch="video-editor/v1-01",
        worktree="/tmp/example",
        parent_worker="claude-code",
        max_duration_minutes=120,
    )
    assert stable_session_task_ref(runbook) == "V1-01"
    runbook.source_ref = "PR #104"
    assert stable_session_task_ref(runbook) == "RB-2D2EFDEC66"


def test_supervisor_persists_sanitized_wrapper_failure_reason(tmp_path, monkeypatch):
    state = State(":memory:")
    registry = load_registry()
    reconcile_provider_states(state, registry)
    supervisor = Supervisor(registry=registry, repo_root=tmp_path, state=state)
    monkeypatch.setattr("scripts.agents.control_plane.supervisor.assert_write_safety", lambda *a, **k: None)

    class FailedProcess:
        pid = 90408

        @staticmethod
        def poll():
            return 1

        @staticmethod
        def communicate(timeout=0):
            return "", "ModuleNotFoundError: No module named 'scripts.agents'\n"

    monkeypatch.setattr(supervisor, "_spawn", lambda argv, cwd: FailedProcess())
    task = Task(
        id="failed",
        task_ref="V1-01",
        role="primary-implementation",
        worker="claude-code",
        worktree=str(tmp_path),
    )
    supervisor.launch_task(task)
    [finished] = supervisor.poll_once()

    assert finished.last_error == "ModuleNotFoundError: No module named 'scripts.agents'"
    assert state.get_provider_state("claude-code").last_error is None
    assert state.get_provider_state("claude-code").state == "AVAILABLE"


def test_supervisor_allows_bounded_pipe_drain_after_process_exit():
    observed_timeouts = []

    class PipeStillDraining:
        @staticmethod
        def communicate(timeout=0):
            observed_timeouts.append(timeout)
            if timeout <= 0:
                raise subprocess.TimeoutExpired("wrapper", timeout)
            return "MANIFEST: .agent-output/V1-01/claude-code/attempt/manifest.json\n", ""

    stdout, stderr = Supervisor._completed_output(PipeStillDraining())

    assert observed_timeouts == [1.0]
    assert stdout.startswith("MANIFEST:")
    assert stderr == ""


def test_supervisor_prefers_sanitized_manifest_reason(tmp_path, monkeypatch):
    state = State(":memory:")
    registry = load_registry()
    reconcile_provider_states(state, registry)
    supervisor = Supervisor(registry=registry, repo_root=tmp_path, state=state)
    monkeypatch.setattr("scripts.agents.control_plane.supervisor.assert_write_safety", lambda *a, **k: None)
    manifest = tmp_path / ".agent-output" / "V1-01" / "claude-code" / "attempt" / "manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"notes": ["weekly limit reached; reset tomorrow"]}), encoding="utf-8")

    class FailedProcess:
        pid = 7

        @staticmethod
        def poll():
            return 1

        @staticmethod
        def communicate(timeout=0):
            return "MANIFEST: .agent-output/V1-01/claude-code/attempt/manifest.json\n", ""

    monkeypatch.setattr(supervisor, "_spawn", lambda argv, cwd: FailedProcess())
    task = Task(
        id="failed",
        task_ref="V1-01",
        role="primary-implementation",
        worker="claude-code",
        worktree=str(tmp_path),
    )
    supervisor.launch_task(task)
    [finished] = supervisor.poll_once()
    assert finished.last_error == "weekly limit reached; reset tomorrow"
    assert state.get_provider_state("claude-code").state == "QUOTA_EXHAUSTED"
    persisted = state.get_task("failed")
    assert persisted.failed_worker_id == "claude-code"
    assert persisted.failure_execution_system == "Claude Code"
    assert persisted.failure_provider == "Anthropic"
    assert persisted.failure_category == "QUOTA"
    assert persisted.failure_reset == "tomorrow"


def test_retry_switches_worker_without_recreating_task_or_runbook(tmp_path, monkeypatch):
    state = State(":memory:")
    registry = load_registry()
    reconcile_provider_states(state, registry)
    monkeypatch.setattr("scripts.agents.registry.Worker.availability_reason", lambda self, probe=True: REASON_AVAILABLE)
    monkeypatch.setattr("scripts.agents.control_plane.runbooks.validate_target", lambda **kwargs: None)
    monkeypatch.setattr("scripts.agents.control_plane.runbooks.pid_is_alive", lambda pid: False)

    runbook = Runbook(
        id="RB-2d2efdec66",
        name="Continue Video Editor — V1-01",
        preset="overnight-development",
        objective="preserve this exact objective",
        source_ref="V1-01 (Local import.)",
        branch="video-editor/V1-01-local-import",
        worktree=str(tmp_path),
        parent_worker="claude-code",
        max_duration_minutes=120,
        permission_profile=PERMISSION_REPO_CONFIGURED_AUTO,
        codex_policy="unrestricted",
        codex_auto_eligible=True,
        max_codex_invocations=1,
        status=RUNBOOK_FAILED,
        task_id="RB-2d2efdec66-session",
    )
    task = Task(
        id=runbook.task_id,
        task_ref=runbook.source_ref,
        role="primary-implementation",
        worker="claude-code",
        state=TASK_FAILED,
        pid=90408,
        worktree=str(tmp_path),
        last_error="worker subprocess exited 1",
        launch_mode=LAUNCH_SESSION,
        runbook_id=runbook.id,
        permission_profile=PERMISSION_REPO_CONFIGURED_AUTO,
    )
    state.upsert_runbook(runbook)
    state.upsert_task(task)

    class FakeSupervisor:
        @staticmethod
        def launch_task(relaunched):
            relaunched.state = TASK_RUNNING
            relaunched.pid = 4242
            state.upsert_task(relaunched)
            return relaunched

    retried = retry_runbook(
        state=state,
        registry=registry,
        supervisor=FakeSupervisor(),
        repo_root=tmp_path,
        runbook_id=runbook.id,
        worker_name="codex-build",
    )
    same_task = state.get_task("RB-2d2efdec66-session")

    assert retried.id == "RB-2d2efdec66"
    assert retried.objective == "preserve this exact objective"
    assert retried.worktree == str(tmp_path)
    assert retried.parent_worker == "codex-build"
    assert same_task.id == "RB-2d2efdec66-session"
    assert same_task.task_ref == "V1-01"
    assert same_task.worker == "codex-build"
    assert same_task.state == TASK_RUNNING
    assert "claude-code" in retried.recovery_note


def test_quota_failure_can_retry_to_policy_eligible_codex_without_model_escalation(tmp_path, monkeypatch):
    state = State(":memory:")
    registry = load_registry()
    reconcile_provider_states(state, registry)
    monkeypatch.setattr("scripts.agents.registry.Worker.availability_reason", lambda self, probe=True: REASON_AVAILABLE)
    monkeypatch.setattr("scripts.agents.control_plane.runbooks.validate_target", lambda **kwargs: None)
    monkeypatch.setattr("scripts.agents.control_plane.runbooks.pid_is_alive", lambda pid: False)
    runbook = Runbook(
        id="RB-quota", name="Quota", preset="overnight-development", objective="work", source_ref="ENG-1",
        branch="feature", worktree=str(tmp_path), parent_worker="claude-code", max_duration_minutes=120,
        permission_profile=PERMISSION_REPO_CONFIGURED_AUTO, codex_policy="unrestricted",
        codex_auto_eligible=True, max_codex_invocations=1, status=RUNBOOK_FAILED, task_id="RB-quota-session",
    )
    task = Task(
        id=runbook.task_id, task_ref="ENG-1", role="primary-implementation", worker="claude-code",
        state=TASK_FAILED, worktree=str(tmp_path), last_error="429 quota exhausted", launch_mode=LAUNCH_SESSION,
        runbook_id=runbook.id, permission_profile=PERMISSION_REPO_CONFIGURED_AUTO,
    )
    state.upsert_runbook(runbook)
    state.upsert_task(task)

    class FakeSupervisor:
        @staticmethod
        def launch_task(relaunched):
            relaunched.state = TASK_RUNNING
            relaunched.pid = 4242
            state.upsert_task(relaunched)
            return relaunched

    retried = retry_runbook(
        state=state, registry=registry, supervisor=FakeSupervisor(), repo_root=tmp_path,
        runbook_id=runbook.id, worker_name="codex-build",
    )
    assert retried.parent_worker == "codex-build"
    assert state.get_task(runbook.task_id).fallback_reason.startswith("quota:")


def test_codex_build_is_subscription_write_worker_separate_from_review():
    registry = load_registry()
    build = registry.get("codex-build")
    review = registry.get("codex-review")
    command = build.build_command(
        model=None,
        intensity="medium",
        prompt="implement safely",
        permission_profile=PERMISSION_REPO_CONFIGURED_AUTO,
    )

    assert build.is_write_capable
    assert review.is_read_only
    assert build.cost_class == "premium-subscription"
    assert command[:3] == ["codex", "exec", "--sandbox"]
    assert "workspace-write" in command
    assert "--ephemeral" in command
    assert all("api" not in arg.lower() or "capability" in arg.lower() for arg in command)


def test_recoverable_claude_quota_automatically_selects_same_role_codex_when_policy_allows(tmp_path, monkeypatch):
    state = State(":memory:")
    registry = load_registry()
    reconcile_provider_states(state, registry)
    state.get_provider_state("claude-code").state = "QUOTA_EXHAUSTED"
    state.upsert_provider_state(state.get_provider_state("claude-code"))
    monkeypatch.setattr("scripts.agents.registry.Worker.availability_reason", lambda self, probe=True: REASON_AVAILABLE)
    monkeypatch.setattr("scripts.agents.control_plane.runbooks.validate_target", lambda **kwargs: None)
    monkeypatch.setattr("scripts.agents.control_plane.runbooks.pid_is_alive", lambda pid: False)
    runbook = Runbook(
        id="RB-auto", name="Auto", preset="overnight-development", objective="same acceptance criteria",
        source_ref="ENG-117", branch="eng/117", worktree=str(tmp_path), parent_worker="claude-code",
        max_duration_minutes=120, permission_profile=PERMISSION_REPO_CONFIGURED_AUTO,
        codex_policy="unrestricted", codex_auto_eligible=True, max_codex_invocations=1,
        status=RUNBOOK_FAILED, task_id="RB-auto-session",
    )
    task = Task(
        id=runbook.task_id, task_ref="ENG-117", role="primary-implementation", worker="claude-code",
        state=TASK_FAILED, worktree=str(tmp_path), last_error="weekly limit reached; resets 8pm",
        failed_worker_id="claude-code", failure_reason_sanitized="weekly limit reached; resets 8pm",
        failure_category="QUOTA", launch_mode=LAUNCH_SESSION, runbook_id=runbook.id,
        permission_profile=PERMISSION_REPO_CONFIGURED_AUTO,
    )
    state.upsert_runbook(runbook)
    state.upsert_task(task)

    class FakeSupervisor:
        @staticmethod
        def launch_task(relaunched):
            relaunched.state = TASK_RUNNING
            relaunched.pid = 4242
            state.upsert_task(relaunched)
            return relaunched

    retried = automatic_fallback_runbook(
        state=state, registry=registry, supervisor=FakeSupervisor(), repo_root=tmp_path, runbook_id=runbook.id,
    )
    assert retried is not None
    assert retried.parent_worker == "codex-build"
    relaunched = state.get_task(task.id)
    assert relaunched.fallback_automatic is True
    assert relaunched.fallback_selected_worker == "codex-build"
    assert relaunched.failed_worker_id == "claude-code"


def test_prelaunch_claude_quota_uses_same_automatic_codex_fallback_in_place(tmp_path, monkeypatch):
    state = State(":memory:")
    registry = load_registry()
    reconcile_provider_states(state, registry)
    claude = state.get_provider_state("claude-code")
    claude.state = "QUOTA_EXHAUSTED"
    claude.reason = "weekly limit reached; resets 8pm (America/Toronto)"
    state.upsert_provider_state(claude)
    codex = state.get_provider_state("codex-build")
    codex.configured = True
    codex.state = "AVAILABLE"
    codex.reason = "AVAILABLE"
    state.upsert_provider_state(codex)
    monkeypatch.setattr("scripts.agents.registry.Worker.availability_reason", lambda self, probe=True: REASON_AVAILABLE)
    monkeypatch.setattr("scripts.agents.control_plane.runbooks.validate_target", lambda **kwargs: None)
    monkeypatch.setattr("scripts.agents.control_plane.runbooks.pid_is_alive", lambda pid: False)
    runbook = Runbook(
        id="RB-prelaunch", name="Continue Video Editor — V1-01", preset="overnight-development",
        objective="preserve this exact acceptance contract", source_ref="V1-01 (Local import.)",
        branch="video-editor/V1-01-local-import", worktree=str(tmp_path), parent_worker="claude-code",
        max_duration_minutes=120, permission_profile=PERMISSION_REPO_CONFIGURED_AUTO,
        codex_policy="unrestricted", codex_auto_eligible=True, max_codex_invocations=1,
    )
    state.upsert_runbook(runbook)
    launches = []

    class FakeSupervisor:
        @staticmethod
        def launch_task(task):
            launches.append(task.worker)
            task.state = TASK_RUNNING
            task.pid = 4245
            state.upsert_task(task)
            return task

    started = start_runbook(
        state=state, registry=registry, supervisor=FakeSupervisor(), repo_root=tmp_path,
        runbook_id=runbook.id,
    )
    task = state.get_task(started.task_id)
    usage = state.get_usage_governance(started.id)

    assert launches == ["codex-build"]
    assert started.id == runbook.id
    assert started.objective == "preserve this exact acceptance contract"
    assert started.source_ref == "V1-01 (Local import.)"
    assert started.branch == "video-editor/V1-01-local-import"
    assert started.worktree == str(tmp_path)
    assert started.permission_profile == PERMISSION_REPO_CONFIGURED_AUTO
    assert task.id == "RB-prelaunch-session"
    assert task.failed_worker_id == "claude-code"
    assert task.failure_category == "QUOTA"
    assert task.fallback_automatic is True
    assert [item["status"] for item in usage["route_history"]] == ["UNAVAILABLE", "RUNNING"]
    assert [item["worker"] for item in usage["route_history"]] == ["claude-code", "codex-build"]
    assert usage["codex_invocations"] == 1
    assert registry.get("codex-build").allow_api_billing is False


def test_prelaunch_unavailable_worker_stays_truthfully_blocked_without_safe_replacement(tmp_path, monkeypatch):
    state = State(":memory:")
    registry = load_registry()
    reconcile_provider_states(state, registry)
    claude = state.get_provider_state("claude-code")
    claude.state = "QUOTA_EXHAUSTED"
    claude.reason = "weekly quota exhausted"
    state.upsert_provider_state(claude)
    codex = state.get_provider_state("codex-build")
    codex.configured = True
    codex.state = "AVAILABLE"
    state.upsert_provider_state(codex)
    monkeypatch.setattr("scripts.agents.registry.Worker.availability_reason", lambda self, probe=True: REASON_AVAILABLE)
    monkeypatch.setattr("scripts.agents.control_plane.runbooks.validate_target", lambda **kwargs: None)
    runbook = Runbook(
        id="RB-prelaunch-blocked", name="Blocked", preset="overnight-development", objective="same contract",
        source_ref="ENG-AGENT-08", branch="eng/121", worktree=str(tmp_path), parent_worker="claude-code",
        max_duration_minutes=120, permission_profile=PERMISSION_REPO_CONFIGURED_AUTO,
        codex_policy="unrestricted", codex_auto_eligible=False, max_codex_invocations=1,
    )
    state.upsert_runbook(runbook)

    class NeverLaunch:
        @staticmethod
        def launch_task(task):
            raise AssertionError("no ineligible or incompatible fallback may launch")

    with pytest.raises(RunbookError, match="Automatic fallback blocked"):
        start_runbook(
            state=state, registry=registry, supervisor=NeverLaunch(), repo_root=tmp_path,
            runbook_id=runbook.id,
        )

    persisted = state.get_runbook(runbook.id)
    task = state.get_task(persisted.task_id)
    usage = state.get_usage_governance(runbook.id)
    assert persisted.status == RUNBOOK_FAILED
    assert task.state == TASK_FAILED
    assert task.fallback_automatic is False
    assert task.failed_worker_id == "claude-code"
    assert [item["worker"] for item in usage["route_history"]] == ["claude-code"]
    assert "runbook is not auto-eligible for Codex" in persisted.recovery_note
    assert "permission profile repo_configured_auto is unsupported" in persisted.recovery_note


def test_prelaunch_attempt_history_survives_restart_without_reselecting_unavailable_worker(tmp_path, monkeypatch):
    database = tmp_path / "prelaunch.db"
    registry = load_registry()
    monkeypatch.setattr("scripts.agents.registry.Worker.availability_reason", lambda self, probe=True: REASON_AVAILABLE)
    monkeypatch.setattr("scripts.agents.control_plane.runbooks.validate_target", lambda **kwargs: None)
    monkeypatch.setattr("scripts.agents.control_plane.runbooks.pid_is_alive", lambda pid: False)
    launches = []

    with State(database) as state:
        reconcile_provider_states(state, registry)
        claude = state.get_provider_state("claude-code")
        claude.state = "QUOTA_EXHAUSTED"
        claude.reason = "weekly quota exhausted"
        state.upsert_provider_state(claude)
        codex = state.get_provider_state("codex-build")
        codex.configured = True
        codex.state = "AVAILABLE"
        state.upsert_provider_state(codex)
        runbook = Runbook(
            id="RB-prelaunch-restart", name="Restart", preset="overnight-development", objective="same contract",
            source_ref="ENG-AGENT-08", branch="eng/121", worktree=str(tmp_path), parent_worker="claude-code",
            max_duration_minutes=120, permission_profile=PERMISSION_REPO_CONFIGURED_AUTO,
            codex_policy="unrestricted", codex_auto_eligible=True, max_codex_invocations=1,
        )
        state.upsert_runbook(runbook)

        class FakeSupervisor:
            @staticmethod
            def launch_task(task):
                launches.append(task.worker)
                task.state = TASK_RUNNING
                task.pid = 4246
                state.upsert_task(task)
                return task

        start_runbook(
            state=state, registry=registry, supervisor=FakeSupervisor(), repo_root=tmp_path,
            runbook_id=runbook.id,
        )

    with State(database) as restarted:
        reconcile_runbooks(state=restarted, registry=registry, supervisor=object(), repo_root=tmp_path)
        usage = restarted.get_usage_governance("RB-prelaunch-restart")

    assert launches == ["codex-build"]
    assert [item["worker"] for item in usage["route_history"]] == ["claude-code", "codex-build"]


def test_concurrent_prelaunch_reconcile_and_start_claim_exactly_one_replacement(tmp_path, monkeypatch):
    state = State(tmp_path / "fallback-race.db")
    registry = load_registry()
    reconcile_provider_states(state, registry)
    claude = state.get_provider_state("claude-code")
    claude.state = "QUOTA_EXHAUSTED"
    claude.reason = "weekly quota exhausted"
    state.upsert_provider_state(claude)
    codex = state.get_provider_state("codex-build")
    codex.configured = True
    codex.state = "AVAILABLE"
    state.upsert_provider_state(codex)
    monkeypatch.setattr("scripts.agents.registry.Worker.availability_reason", lambda self, probe=True: REASON_AVAILABLE)
    monkeypatch.setattr("scripts.agents.control_plane.runbooks.validate_target", lambda **kwargs: None)
    monkeypatch.setattr("scripts.agents.control_plane.runbooks.pid_is_alive", lambda pid: False)
    runbook = Runbook(
        id="RB-prelaunch-race", name="Race", preset="overnight-development", objective="same contract",
        source_ref="ENG-AGENT-09", branch="eng/123", worktree=str(tmp_path), parent_worker="claude-code",
        max_duration_minutes=120, permission_profile=PERMISSION_REPO_CONFIGURED_AUTO,
        codex_policy="balanced", codex_auto_eligible=True, max_codex_invocations=1,
        status=RUNBOOK_FAILED, task_id="RB-prelaunch-race-session",
    )
    task = Task(
        id=runbook.task_id, task_ref="ENG-AGENT-09", role="primary-implementation", worker="claude-code",
        state=TASK_FAILED, worktree=str(tmp_path), failed_worker_id="claude-code",
        failure_reason_sanitized="weekly quota exhausted", failure_category="QUOTA",
        launch_mode=LAUNCH_SESSION, runbook_id=runbook.id, fallback_automatic=None,
        permission_profile=PERMISSION_REPO_CONFIGURED_AUTO,
    )
    state.upsert_runbook(runbook)
    state.upsert_task(task)
    usage = new_usage_record(
        runbook_id=runbook.id, task_id=task.id, classification="routine",
        codex_policy="balanced", codex_auto_eligible=True, max_codex_invocations=1,
        context_manifest={"policy_manifest": {
            **compose_policy_bundle(
                root=tmp_path, registry=registry, worker_name="claude-code",
                route_role="primary-implementation", acceptance_criteria=runbook.objective,
            ).manifest,
        }},
    )
    usage["route_history"] = [
        {"worker": "claude-code", "status": "UNAVAILABLE", "failure_category": "QUOTA"},
    ]
    state.upsert_usage_governance(usage)
    launch_entered = threading.Event()
    release_launch = threading.Event()
    losing_claim_seen = threading.Event()
    launches = []
    original_claim = state.claim_pending_fallback

    def tracked_claim(task_id):
        claimed = original_claim(task_id)
        if not claimed:
            losing_claim_seen.set()
        return claimed

    monkeypatch.setattr(state, "claim_pending_fallback", tracked_claim)

    class FakeSupervisor:
        @staticmethod
        def launch_task(relaunched):
            launches.append(relaunched.worker)
            launch_entered.set()
            assert release_launch.wait(2)
            relaunched.state = TASK_RUNNING
            relaunched.pid = 4250
            state.upsert_task(relaunched)
            return relaunched

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            automatic_fallback_runbook,
            state=state, registry=registry, supervisor=FakeSupervisor(), repo_root=tmp_path,
            runbook_id=runbook.id,
        )
        assert launch_entered.wait(2)
        second = pool.submit(
            automatic_fallback_runbook,
            state=state, registry=registry, supervisor=FakeSupervisor(), repo_root=tmp_path,
            runbook_id=runbook.id,
        )
        assert losing_claim_seen.wait(2)
        release_launch.set()
        results = [first.result(timeout=2), second.result(timeout=2)]

    assert launches == ["codex-build"]
    assert all(result is not None and result.status == "RUNNING" for result in results)
    usage = state.get_usage_governance(runbook.id)
    assert usage["codex_invocations"] == 1
    assert [item["worker"] for item in usage["route_history"]] == ["claude-code", "codex-build"]


def test_reconcile_repairs_legacy_inflight_fallback_presentation_without_relaunch(tmp_path):
    state = State(":memory:")
    registry = load_registry()
    reconcile_provider_states(state, registry)
    claude = state.get_provider_state("claude-code")
    claude.state = "QUOTA_EXHAUSTED"
    state.upsert_provider_state(claude)
    runbook = Runbook(
        id="RB-inflight-repair", name="Repair", preset="overnight-development", objective="same contract",
        source_ref="ENG-AGENT-09", branch="eng/123", worktree=str(tmp_path), parent_worker="codex-build",
        max_duration_minutes=120, permission_profile=PERMISSION_REPO_CONFIGURED_AUTO,
        codex_policy="balanced", codex_auto_eligible=True, max_codex_invocations=1,
        status=RUNBOOK_FAILED, task_id="RB-inflight-repair-session",
    )
    task = Task(
        id=runbook.task_id, task_ref="ENG-AGENT-09", role="primary-implementation", worker="codex-build",
        state=TASK_RUNNING, pid=4251, worktree=str(tmp_path), failed_worker_id="claude-code",
        failure_reason_sanitized="worker unavailable before launch: QUOTA_EXHAUSTED",
        failure_category="UNKNOWN", fallback_selected_worker="codex-build", fallback_automatic=False,
        launch_mode=LAUNCH_SESSION, runbook_id=runbook.id,
    )
    state.upsert_runbook(runbook)
    state.upsert_task(task)
    usage = new_usage_record(
        runbook_id=runbook.id, task_id=task.id, classification="routine",
        codex_policy="balanced", codex_auto_eligible=True, max_codex_invocations=1,
    )
    usage["route_history"] = [
        {"worker": "claude-code", "status": "UNAVAILABLE", "failure_category": "UNKNOWN"},
        {"worker": "codex-build", "status": "RUNNING", "automatic": True, "pid": 4251},
    ]
    state.upsert_usage_governance(usage)

    result = reconcile_runbooks(state=state, registry=registry, supervisor=object(), repo_root=tmp_path)

    repaired_runbook = state.get_runbook(runbook.id)
    repaired_task = state.get_task(task.id)
    repaired_usage = state.get_usage_governance(runbook.id)
    assert result["auto_fallbacks"] == 0
    assert repaired_runbook.status == "RUNNING"
    assert repaired_task.fallback_automatic is True
    assert repaired_task.failure_category == "QUOTA"
    assert repaired_usage["route_history"][0]["failure_category"] == "QUOTA"
    assert repaired_usage["route_history"][1]["worker"] == "codex-build"


def test_reconcile_repairs_legacy_completed_fallback_presentation_without_relaunch(tmp_path):
    state = State(":memory:")
    registry = load_registry()
    reconcile_provider_states(state, registry)
    claude = state.get_provider_state("claude-code")
    claude.state = "QUOTA_EXHAUSTED"
    state.upsert_provider_state(claude)
    runbook = Runbook(
        id="RB-completed-repair", name="Repair completed", preset="overnight-development",
        objective="same contract", source_ref="ENG-AGENT-09", branch="eng/123", worktree=str(tmp_path),
        parent_worker="codex-build", max_duration_minutes=120,
        permission_profile=PERMISSION_REPO_CONFIGURED_AUTO, codex_policy="balanced",
        codex_auto_eligible=True, max_codex_invocations=1, status=RUNBOOK_FAILED,
        task_id="RB-completed-repair-session",
    )
    task = Task(
        id=runbook.task_id, task_ref="ENG-AGENT-09", role="primary-implementation", worker="codex-build",
        state=TASK_SUCCEEDED, pid=4252, result="PASS", worktree=str(tmp_path),
        failed_worker_id="claude-code",
        failure_reason_sanitized="worker unavailable before launch: QUOTA_EXHAUSTED",
        failure_category="UNKNOWN", fallback_selected_worker="codex-build", fallback_automatic=False,
        launch_mode=LAUNCH_SESSION, runbook_id=runbook.id,
    )
    state.upsert_runbook(runbook)
    state.upsert_task(task)
    usage = new_usage_record(
        runbook_id=runbook.id, task_id=task.id, classification="routine",
        codex_policy="balanced", codex_auto_eligible=True, max_codex_invocations=1,
    )
    usage["route_history"] = [
        {"worker": "claude-code", "status": "UNAVAILABLE", "failure_category": "UNKNOWN"},
        {"worker": "codex-build", "status": "SUCCEEDED", "automatic": True, "pid": 4252},
    ]
    state.upsert_usage_governance(usage)

    result = reconcile_runbooks(state=state, registry=registry, supervisor=object(), repo_root=tmp_path)

    repaired_runbook = state.get_runbook(runbook.id)
    repaired_task = state.get_task(task.id)
    repaired_usage = state.get_usage_governance(runbook.id)
    assert result["auto_fallbacks"] == 0
    assert result["finalized"] == 1
    # ENG-AGENT-13 (issue #138): a repaired fallback presentation still goes
    # through acceptance -- it never jumps straight to terminal SUCCEEDED.
    # This worktree is not a real Git repository, so the Test stage
    # truthfully fails and the runbook halts BLOCKED.
    assert repaired_runbook.status != "SUCCEEDED"
    assert repaired_runbook.status == "BLOCKED"
    assert repaired_runbook.acceptance_evidence["implementation"]["status"] == "PASS"
    assert repaired_runbook.ended_at is None
    assert repaired_runbook.report_markdown
    assert repaired_task.fallback_automatic is True
    assert repaired_task.failure_category == "QUOTA"
    assert repaired_usage["route_history"][0]["failure_category"] == "QUOTA"
    assert repaired_usage["route_history"][1]["worker"] == "codex-build"


def test_fallback_history_skips_failed_routes_and_selects_next_provider(tmp_path, monkeypatch):
    state = State(":memory:")
    registry = load_registry()
    reconcile_provider_states(state, registry)
    monkeypatch.setattr("scripts.agents.registry.Worker.availability_reason", lambda self, probe=True: REASON_AVAILABLE)
    monkeypatch.setattr("scripts.agents.control_plane.runbooks.validate_target", lambda **kwargs: None)
    monkeypatch.setattr("scripts.agents.control_plane.runbooks.pid_is_alive", lambda pid: False)
    runbook = Runbook(
        id="RB-chain", name="Chain", preset="finish-pr", objective="same objective", source_ref="ENG-119",
        branch="eng/119", worktree=str(tmp_path), parent_worker="codex-build", max_duration_minutes=120,
        codex_policy="unrestricted", codex_auto_eligible=True, max_codex_invocations=2,
        status=RUNBOOK_FAILED, task_id="RB-chain-session",
    )
    task = Task(
        id=runbook.task_id, task_ref="ENG-119", role="primary-implementation", worker="codex-build",
        state=TASK_FAILED, worktree=str(tmp_path), last_error="service unavailable",
        failed_worker_id="codex-build", failure_reason_sanitized="service unavailable",
        failure_category="PROVIDER_OUTAGE", launch_mode=LAUNCH_SESSION, runbook_id=runbook.id,
    )
    usage = new_usage_record(
        runbook_id=runbook.id, task_id=task.id, classification="routine",
        codex_policy="unrestricted", codex_auto_eligible=True, max_codex_invocations=2,
    )
    usage["route_history"] = [
        {"worker": "claude-code", "status": "FAILED", "failure_category": "QUOTA"},
        {"worker": "codex-build", "status": "FAILED", "failure_category": "PROVIDER_OUTAGE"},
    ]
    state.upsert_runbook(runbook)
    state.upsert_task(task)
    state.upsert_usage_governance(usage)
    launches = []

    class FakeSupervisor:
        @staticmethod
        def launch_task(relaunched):
            launches.append(relaunched.worker)
            relaunched.state = TASK_RUNNING
            relaunched.pid = 4243
            state.upsert_task(relaunched)
            return relaunched

    automatic_fallback_runbook(
        state=state, registry=registry, supervisor=FakeSupervisor(), repo_root=tmp_path,
        runbook_id=runbook.id,
    )

    assert launches == ["grok-build"]
    assert [item["worker"] for item in state.get_usage_governance(runbook.id)["route_history"]] == [
        "claude-code", "codex-build", "grok-build",
    ]


def test_automatic_fallback_persists_restart_decision_and_never_duplicates_launch(tmp_path, monkeypatch):
    database = tmp_path / "control-plane.db"
    registry = load_registry()
    monkeypatch.setattr("scripts.agents.registry.Worker.availability_reason", lambda self, probe=True: REASON_AVAILABLE)
    monkeypatch.setattr("scripts.agents.control_plane.runbooks.validate_target", lambda **kwargs: None)
    monkeypatch.setattr("scripts.agents.control_plane.runbooks.pid_is_alive", lambda pid: False)
    runbook = Runbook(
        id="RB-restart", name="Restart", preset="finish-pr", objective="same objective",
        source_ref="ENG-119", branch="eng/119", worktree=str(tmp_path), parent_worker="claude-code",
        max_duration_minutes=120, codex_policy="unrestricted", codex_auto_eligible=True,
        max_codex_invocations=1, status=RUNBOOK_FAILED, task_id="RB-restart-session",
    )
    task = Task(
        id=runbook.task_id, task_ref="ENG-119", role="primary-implementation", worker="claude-code",
        state=TASK_FAILED, worktree=str(tmp_path), failed_worker_id="claude-code",
        failure_reason_sanitized="weekly quota exhausted", failure_category="QUOTA",
        launch_mode=LAUNCH_SESSION, runbook_id=runbook.id, fallback_automatic=None,
    )
    with State(database) as state:
        reconcile_provider_states(state, registry)
        state.upsert_runbook(runbook)
        state.upsert_task(task)
        usage = new_usage_record(
            runbook_id=runbook.id, task_id=task.id, classification="routine",
            codex_policy="unrestricted", codex_auto_eligible=True, max_codex_invocations=1,
        )
        usage["route_history"] = [{"worker": "claude-code", "status": "FAILED"}]
        state.upsert_usage_governance(usage)

    launches = []

    class FakeSupervisor:
        @staticmethod
        def launch_task(relaunched):
            launches.append(relaunched.worker)
            relaunched.state = TASK_RUNNING
            relaunched.pid = 4244
            restarted.upsert_task(relaunched)
            return relaunched

    with State(database) as restarted:
        reconcile_runbooks(state=restarted, registry=registry, supervisor=FakeSupervisor(), repo_root=tmp_path)
        reconcile_runbooks(state=restarted, registry=registry, supervisor=FakeSupervisor(), repo_root=tmp_path)
        persisted = restarted.get_usage_governance(runbook.id)

    assert launches == ["codex-build"]
    assert persisted["codex_invocations"] == 1
    assert [item["worker"] for item in persisted["route_history"]] == ["claude-code", "codex-build"]


def test_live_failed_process_blocks_replacement_write_ownership(tmp_path, monkeypatch):
    state = State(":memory:")
    registry = load_registry()
    reconcile_provider_states(state, registry)
    monkeypatch.setattr("scripts.agents.registry.Worker.availability_reason", lambda self, probe=True: REASON_AVAILABLE)
    monkeypatch.setattr("scripts.agents.control_plane.runbooks.validate_target", lambda **kwargs: None)
    monkeypatch.setattr("scripts.agents.control_plane.runbooks.pid_is_alive", lambda pid: True)
    runbook = Runbook(
        id="RB-owner", name="Owner", preset="finish-pr", objective="same objective", source_ref="ENG-119",
        branch="eng/119", worktree=str(tmp_path), parent_worker="claude-code", max_duration_minutes=120,
        codex_policy="unrestricted", codex_auto_eligible=True, max_codex_invocations=1,
        status=RUNBOOK_FAILED, task_id="RB-owner-session",
    )
    task = Task(
        id=runbook.task_id, task_ref="ENG-119", role="primary-implementation", worker="claude-code",
        state=TASK_FAILED, pid=77, worktree=str(tmp_path), failed_worker_id="claude-code",
        failure_reason_sanitized="quota exhausted", launch_mode=LAUNCH_SESSION, runbook_id=runbook.id,
    )
    state.upsert_runbook(runbook)
    state.upsert_task(task)
    state.upsert_usage_governance(new_usage_record(
        runbook_id=runbook.id, task_id=task.id, classification="routine",
        codex_policy="unrestricted", codex_auto_eligible=True, max_codex_invocations=1,
    ))

    class NeverLaunch:
        @staticmethod
        def launch_task(relaunched):
            raise AssertionError("replacement must not launch while the prior writer PID is alive")

    assert automatic_fallback_runbook(
        state=state, registry=registry, supervisor=NeverLaunch(), repo_root=tmp_path,
        runbook_id=runbook.id,
    ) is None
    assert state.get_task(task.id).fallback_automatic is False
    assert "still has a live worker PID" in state.get_runbook(runbook.id).recovery_note


def test_structured_worker_failure_surfaces_redacted_provider_reason():
    output = json.dumps(
        {"is_error": True, "result": "You've hit your weekly limit; diagnostic sk-1234567890abcdef"}
    )
    reason = structured_failure(output)
    assert reason == "You've hit your weekly limit; diagnostic ***REDACTED***"
