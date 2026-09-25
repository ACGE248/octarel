"""The one shared command-application layer used by both CLI and dashboard."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.agents.control_plane.commands import (
    ALL_COMMANDS,
    CommandContext,
    CommandError,
    apply_command,
)
from scripts.agents.control_plane.models import (
    TASK_CANCELLED,
    TASK_PAUSED,
    TASK_PENDING,
)
from scripts.agents.control_plane.provider_state import (
    STATE_AVAILABLE,
    STATE_COOLING_DOWN,
    STATE_DISABLED,
    seed_provider_states,
)
from scripts.agents.control_plane.scheduler import Scheduler
from scripts.agents.control_plane.state import State
from scripts.agents.control_plane.supervisor import Supervisor
from scripts.agents.registry import load_registry


@pytest.fixture()
def ctx(tmp_path: Path) -> CommandContext:
    registry = load_registry()
    state = State(":memory:")
    for provider in seed_provider_states(registry):
        state.upsert_provider_state(provider)
    return CommandContext(
        state=state,
        registry=registry,
        scheduler=Scheduler(),
        supervisor=Supervisor(registry=registry, repo_root=tmp_path, state=state),
        repo_root=tmp_path,
    )


def test_all_commands_are_dispatchable():
    from scripts.agents.control_plane import commands as commands_module

    assert len(ALL_COMMANDS) >= 12
    for verb in ALL_COMMANDS:
        assert verb in commands_module._DISPATCH, f"{verb} has no registered handler"


def test_unknown_command_raises():
    with pytest.raises(CommandError):
        apply_command(None, "not-a-real-verb")  # type: ignore[arg-type]


def test_enqueue_creates_a_pending_task(ctx):
    result = apply_command(
        ctx,
        "enqueue",
        task_id="t1",
        task_ref="ENG-AGENT-02",
        role="focused-tests",
        worker="opencode2-gemini-flash-lite",
        prompt=["run tests"],
    )
    assert result.ok is True
    task = ctx.state.get_task("t1")
    assert task is not None
    assert task.state == TASK_PENDING
    assert task.role == "focused-tests"


def test_enqueue_escapes_prompt_text_that_looks_like_a_scope(ctx):
    apply_command(
        ctx,
        "enqueue",
        task_id="t1",
        task_ref="ENG-AGENT-02",
        role="focused-tests",
        worker="opencode2-gemini-flash-lite",
        scopes=["scripts/agents"],
        prompt=["scope:explain this literally"],
    )
    task = ctx.state.get_task("t1")
    assert task.command == ("scope:scripts/agents", "literal:scope:explain this literally")


def test_enqueue_refuses_duplicate_task_id(ctx):
    apply_command(
        ctx,
        "enqueue",
        task_id="t1",
        task_ref="ENG-AGENT-02",
        role="focused-tests",
        worker="opencode2-gemini-flash-lite",
    )
    with pytest.raises(CommandError):
        apply_command(
            ctx,
            "enqueue",
            task_id="t1",
            task_ref="ENG-AGENT-02",
            role="focused-tests",
            worker="opencode2-gemini-flash-lite",
        )


def test_enqueue_refuses_role_the_worker_does_not_declare(ctx):
    with pytest.raises(CommandError):
        apply_command(
            ctx,
            "enqueue",
            task_id="t1",
            task_ref="ENG-AGENT-02",
            role="doc-drift-review",
            worker="opencode2-gemini-flash-lite",
        )


def test_start_dry_run_never_reaches_a_real_worker_cli(ctx, monkeypatch):
    apply_command(
        ctx,
        "enqueue",
        task_id="t1",
        task_ref="ENG-AGENT-02",
        role="focused-tests",
        worker="opencode2-gemini-flash-lite",
        scopes=["scripts/agents"],
        prompt=["run tests"],
    )
    spawned = []
    monkeypatch.setattr(ctx.supervisor, "_spawn", lambda argv, cwd, env=None: spawned.append(argv) or _FakeProcess())
    result = apply_command(ctx, "start", task_id="t1", dry_run=True)
    assert result.ok is True
    assert spawned and "--dry-run" in spawned[0]


class _FakeProcess:
    pid = 999

    def poll(self):
        return None


def test_pause_holds_a_pending_task_and_resume_releases_it(ctx):
    apply_command(
        ctx,
        "enqueue",
        task_id="t1",
        task_ref="ENG-AGENT-02",
        role="focused-tests",
        worker="opencode2-gemini-flash-lite",
    )
    apply_command(ctx, "pause", task_id="t1")
    assert "t1" in ctx.held_task_ids
    assert ctx.state.get_task("t1").state == TASK_PAUSED

    apply_command(ctx, "resume", task_id="t1")
    assert "t1" not in ctx.held_task_ids
    assert ctx.state.get_task("t1").state == TASK_PENDING


def test_stop_marks_a_task_cancelled(ctx):
    apply_command(
        ctx,
        "enqueue",
        task_id="t1",
        task_ref="ENG-AGENT-02",
        role="focused-tests",
        worker="opencode2-gemini-flash-lite",
    )
    apply_command(ctx, "stop", task_id="t1")
    assert ctx.state.get_task("t1").state == TASK_CANCELLED


def test_stop_after_current_sets_the_context_flag(ctx):
    assert ctx.stop_after_current is False
    apply_command(ctx, "stop_after_current")
    assert ctx.stop_after_current is True


def test_controls_persist_across_independent_contexts(ctx):
    other = CommandContext(
        state=ctx.state,
        registry=ctx.registry,
        scheduler=Scheduler(),
        supervisor=Supervisor(registry=ctx.registry, repo_root=ctx.repo_root, state=ctx.state),
        repo_root=ctx.repo_root,
    )
    apply_command(ctx, "stop_after_current")
    apply_command(ctx, "set_max_writers", count=1)
    assert other.stop_after_current is True
    assert other.state.get_control_setting("max_write_workers") == "1"


def test_provider_enable_disable_drain_transitions(ctx):
    apply_command(ctx, "provider_disable", name="claude-code")
    assert ctx.state.get_provider_state("claude-code").state == STATE_DISABLED

    apply_command(ctx, "provider_enable", name="claude-code")
    assert ctx.state.get_provider_state("claude-code").state == STATE_AVAILABLE

    apply_command(ctx, "provider_drain", name="claude-code")
    assert ctx.state.get_provider_state("claude-code").state == STATE_COOLING_DOWN


def test_provider_enable_refuses_a_not_configured_catalog_row(ctx):
    # "glm" is a fixed, non-adapter catalog row (no CLI/adapter exists at all);
    # "openai-codex" moved out of this catalog in ENG-AGENT-02-S7 (issue #97)
    # once "codex-review" became a real, subscription-authenticated worker.
    result = apply_command(ctx, "provider_enable", name="glm")
    assert result.ok is False
    assert ctx.state.get_provider_state("glm").configured is False


def test_provider_commands_raise_for_unknown_provider(ctx):
    with pytest.raises(CommandError):
        apply_command(ctx, "provider_disable", name="does-not-exist")


def test_probe_reports_cli_availability_never_making_a_live_call(ctx):
    result = apply_command(ctx, "probe", name="opencode2-gemini-flash-lite")
    assert isinstance(result.ok, bool)
    assert isinstance(result.data["cli_available"], bool)
    assert result.data["reason"] in result.message


def test_probe_truthfully_reports_auth_failure_even_when_cli_exists(ctx, monkeypatch):
    import subprocess as subprocess_module

    class FakeNotAuthed:
        returncode = 1
        stdout = '{"loggedIn": false, "authMethod": "none"}'
        stderr = ""

    monkeypatch.setattr(subprocess_module, "run", lambda *a, **k: FakeNotAuthed())
    monkeypatch.setattr("scripts.agents.registry.shutil.which", lambda _bin: "/usr/local/bin/claude")

    result = apply_command(ctx, "probe", name="claude-code")
    assert result.ok is False
    assert result.data == {
        "cli_bin": "claude",
        "cli_available": True,
        "available": False,
        "reason": "NOT_AUTHENTICATED",
    }
    assert ctx.state.get_provider_state("claude-code").state != STATE_AVAILABLE


def test_probe_demotes_state_when_the_auth_session_has_expired(ctx, monkeypatch):
    """Grok Build review (issue #97): a probe used to only ever *promote* a

    provider toward AVAILABLE, so a provider that was AVAILABLE when its auth
    session was still valid stayed AVAILABLE (and routable) forever after,
    even once a probe's own check_auth() found it NOT_AUTHENTICATED. A probe
    must demote `state` exactly as readily as it promotes it.
    """

    import subprocess as subprocess_module

    provider = ctx.state.get_provider_state("codex-review")
    provider.state = STATE_AVAILABLE
    ctx.state.upsert_provider_state(provider)

    class FakeNotAuthed:
        returncode = 1
        stdout = "Not logged in\n"
        stderr = ""

    monkeypatch.setattr(subprocess_module, "run", lambda *a, **k: FakeNotAuthed())
    monkeypatch.setattr("scripts.agents.registry.shutil.which", lambda _bin: "/usr/local/bin/codex")

    apply_command(ctx, "probe", name="codex-review")
    updated = ctx.state.get_provider_state("codex-review")
    assert updated.reason == "NOT_AUTHENTICATED"
    assert updated.state != STATE_AVAILABLE


def test_set_max_writers_updates_the_scheduler_policy(ctx):
    apply_command(ctx, "set_max_writers", count=5)
    assert ctx.scheduler.policy.max_write_workers == 5


def test_set_max_writers_rejects_less_than_one(ctx):
    with pytest.raises(CommandError):
        apply_command(ctx, "set_max_writers", count=0)


def test_prioritize_and_defer(ctx):
    apply_command(
        ctx,
        "enqueue",
        task_id="t1",
        task_ref="ENG-AGENT-02",
        role="focused-tests",
        worker="opencode2-gemini-flash-lite",
    )
    apply_command(ctx, "prioritize", task_id="t1", priority=7)
    assert ctx.state.get_task("t1").priority == 7

    apply_command(ctx, "defer", task_id="t1")
    assert ctx.state.get_task("t1").priority == -1


def test_command_on_unknown_task_raises(ctx):
    with pytest.raises(CommandError):
        apply_command(ctx, "pause", task_id="does-not-exist")


def test_provider_cost_block_and_clear_round_trip(ctx):
    from scripts.agents.control_plane.provider_state import STATE_COST_BLOCKED

    result = apply_command(ctx, "provider_cost_block", name="grok-build", reason="daily budget exceeded")
    assert result.ok is True
    provider = ctx.state.get_provider_state("grok-build")
    assert provider.state == STATE_COST_BLOCKED
    assert provider.last_error == "daily budget exceeded"

    clear = apply_command(ctx, "provider_cost_clear", name="grok-build")
    assert clear.ok is True
    assert ctx.state.get_provider_state("grok-build").state == STATE_AVAILABLE


def test_provider_cost_clear_refuses_when_not_cost_blocked(ctx):
    result = apply_command(ctx, "provider_cost_clear", name="grok-build")
    assert result.ok is False


def test_provider_cost_block_raises_for_unknown_provider(ctx):
    with pytest.raises(CommandError):
        apply_command(ctx, "provider_cost_block", name="does-not-exist")


def test_provider_failure_clear_resets_consecutive_failures_and_restores_availability(ctx):
    """Issue #159: a single transient diff-review failure left a provider's
    ``consecutive_failures``/``last_error`` set forever, with no operator
    recovery path -- ``provider_enable`` only ever touched ``state``/``reason``.
    """

    from scripts.agents.control_plane.provider_state import STATE_FAILED

    provider = ctx.state.get_provider_state("grok-build")
    provider.state = STATE_FAILED
    provider.consecutive_failures = 1
    provider.last_error = "headless-mode tool-permission auto-denial"
    ctx.state.upsert_provider_state(provider)

    result = apply_command(ctx, "provider_failure_clear", name="grok-build")
    assert result.ok is True

    recovered = ctx.state.get_provider_state("grok-build")
    assert recovered.state == STATE_AVAILABLE
    assert recovered.consecutive_failures == 0
    assert recovered.last_error is None


def test_provider_failure_clear_refuses_when_not_failed(ctx):
    result = apply_command(ctx, "provider_failure_clear", name="grok-build")
    assert result.ok is False
    # Not FAILED to begin with, so nothing should have changed.
    assert ctx.state.get_provider_state("grok-build").consecutive_failures == 0


def test_provider_failure_clear_does_not_touch_a_cost_blocked_provider(ctx):
    """A hard budget block is a distinct gate; recovering a resolved flake must
    never accidentally resurrect routing that the cost gate deliberately shut
    off (mirrors the symmetric guard on ``probe``, issue #97)."""

    from scripts.agents.control_plane.provider_state import STATE_COST_BLOCKED

    apply_command(ctx, "provider_cost_block", name="grok-build", reason="daily budget exceeded")
    result = apply_command(ctx, "provider_failure_clear", name="grok-build")
    assert result.ok is False
    assert ctx.state.get_provider_state("grok-build").state == STATE_COST_BLOCKED


def test_provider_failure_clear_raises_for_unknown_provider(ctx):
    with pytest.raises(CommandError):
        apply_command(ctx, "provider_failure_clear", name="does-not-exist")


def test_probe_never_silently_clears_a_cost_block(ctx):
    """Found by independent Grok Build review: a probe must not resurrect a
    hard-budget-blocked provider just because its CLI is installed."""

    from scripts.agents.control_plane.provider_state import STATE_COST_BLOCKED

    apply_command(ctx, "provider_cost_block", name="opencode2-gemini-flash-lite", reason="budget exceeded")
    apply_command(ctx, "probe", name="opencode2-gemini-flash-lite")
    assert ctx.state.get_provider_state("opencode2-gemini-flash-lite").state == STATE_COST_BLOCKED


# --------------------------------------------------------------------------- runbooks


def _git_repo(tmp_path: Path, *, branch: str = "eng/test-runbook") -> Path:
    import subprocess

    subprocess.run(["git", "init", "-q", "-b", branch, str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    (tmp_path / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", "seed"], check=True)
    return tmp_path


def test_runbook_create_start_pause_resume_stop_round_trip(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    registry = load_registry()
    state = State(":memory:")
    supervisor = Supervisor(registry=registry, repo_root=repo, state=state)

    class FakeProcess:
        pid = 999

    monkeypatch.setattr(supervisor, "_spawn", lambda argv, cwd, env=None: FakeProcess())
    monkeypatch.setattr("scripts.agents.control_plane.supervisor.assert_write_safety", lambda *a, **k: None)
    # The fake worker is not a process this Supervisor owns, and it is genuinely live: stop must report
    # STOPPING, not CANCELLED (a dead/no-PID task is cancelled immediately, see stop_runbook).
    monkeypatch.setattr("scripts.agents.control_plane.runbooks.pid_is_alive", lambda pid: pid == FakeProcess.pid)

    ctx = CommandContext(state=state, registry=registry, scheduler=Scheduler(), supervisor=supervisor, repo_root=repo)

    created = apply_command(
        ctx,
        "runbook_create",
        name="Nightly",
        preset="test-fix",
        source_ref="ENG-X",
        branch="eng/test-runbook",
        worktree=str(repo),
    )
    assert created.ok is True
    runbook_id = created.data["runbook_id"]
    assert created.data["status"] == "DRAFT"

    started = apply_command(ctx, "runbook_start", runbook_id=runbook_id)
    assert started.ok is True
    assert started.data["status"] == "RUNNING"

    paused = apply_command(ctx, "runbook_pause", runbook_id=runbook_id)
    assert paused.data["status"] == "PAUSED"

    resumed = apply_command(ctx, "runbook_resume", runbook_id=runbook_id)
    assert resumed.data["status"] == "RUNNING"

    stopped = apply_command(ctx, "runbook_stop", runbook_id=runbook_id)
    assert stopped.data["status"] == "STOPPING"


def test_runbook_create_raises_command_error_for_unknown_preset(ctx):
    with pytest.raises(CommandError):
        apply_command(
            ctx, "runbook_create", name="x", preset="nope", source_ref="x", branch="b", worktree=str(ctx.repo_root)
        )


def test_runbook_start_raises_command_error_when_worktree_is_unregistered(ctx):
    created = apply_command(
        ctx,
        "runbook_create",
        name="x",
        preset="test-fix",
        source_ref="x",
        branch="b",
        worktree=str(ctx.repo_root),
    )
    with pytest.raises(CommandError):
        apply_command(ctx, "runbook_start", runbook_id=created.data["runbook_id"])
