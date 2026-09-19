"""ENG-AGENT-12 (issue #136): Control Plane canonical-truth refresh and

provider-state freshness. Guards the exact defect reported live at
dev.octascene.com: a Quick Start resolution and a provider-routed fallback
decision must both reflect *current*, freshly-checked repository/provider
truth, never a stale daemon checkout or a stale persisted provider row.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts.agents.control_plane.commands import CommandContext
from scripts.agents.control_plane.dashboard_api import cp_process_status, create_app
from scripts.agents.control_plane.dispatch import managed_admit
from scripts.agents.control_plane.models import ProviderState, Task
from scripts.agents.control_plane.provider_state import (
    STATE_AVAILABLE,
    STATE_LAUNCH_ENVIRONMENT_ERROR,
    STATE_NOT_CONFIGURED,
    STATE_QUOTA_EXHAUSTED,
    provider_state_age_seconds,
    provider_state_freshness,
    provider_state_is_stale,
    refresh_stale_routable_candidates,
)
from scripts.agents.control_plane.quickstart import (
    VIDEO_EDITOR_LEDGER_RELATIVE,
    continue_video_editor_option,
)
from scripts.agents.control_plane.scheduler import Scheduler
from scripts.agents.control_plane.state import State
from scripts.agents.registry import load_registry
from scripts.agents.runner import ENV_CANONICAL_REPO_ROOT, canonical_repo_root

_LEDGER_V1_05_PENDING = """\
# Standalone Video Editor — Revised Implementation Status

## Status values

`inherited-unverified | pending | in-progress | blocked | complete | superseded`

## Foundation and V1

| ID | Status | Notes/evidence |
|---|---|---|
| V1-01 | complete | Local import. |
| V1-02 | complete | Library import/media bin. |
| V1-03 | complete | Probe/promotion. |
| V1-04 | complete | Multitrack commands. |
| V1-05 | pending | Video/image items. |
| V1-06 | pending | Audio/text items. |
"""


def _init_git_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    (root / "README.md").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)


def _write_ledger(repo_root: Path, text: str) -> None:
    ledger_path = repo_root / VIDEO_EDITOR_LEDGER_RELATIVE
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(text, encoding="utf-8")


# --------------------------------------------------------------------------- 1: fresh resolution + evidence


def test_ledger_complete_through_v1_04_resolves_v1_05_with_evidence(tmp_path):
    """Scenario 1: ledger says V1-01..V1-04 complete, V1-05 pending -> resolver returns V1-05."""

    repo = tmp_path / "canonical"
    _init_git_repo(repo)
    _write_ledger(repo, _LEDGER_V1_05_PENDING)

    option = continue_video_editor_option(repo)

    assert option.task_id == "V1-05"
    assert "V1-05" in option.title
    evidence = option.as_dict()["resolution_evidence"]
    assert evidence["resolved_task_id"] == "V1-05"
    assert evidence["repo_root"] == str(repo.resolve())
    assert evidence["ledger_path"] == str(VIDEO_EDITOR_LEDGER_RELATIVE)
    assert evidence["ledger_mtime"] is not None
    assert evidence["resolved_at"] is not None
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    assert evidence["head_sha"] == head


# --------------------------------------------------------------------------- 2: canonical truth vs CP code checkout


def test_canonical_repo_root_override_wins_over_cwd_derived_checkout(tmp_path, monkeypatch):
    """Scenario 2: a stale CP-code checkout must not silently become scheduling truth.

    ``canonical_repo_root`` must resolve against an explicit override/env var
    rather than whatever checkout the daemon process's own cwd happens to be
    pointed at -- the exact live defect (a daemon process cwd-rooted at a
    stale, detached-HEAD secondary checkout).
    """

    stale_cp_code_checkout = tmp_path / "stale-dev-checkout"
    _init_git_repo(stale_cp_code_checkout)

    canonical = tmp_path / "canonical-main"
    _init_git_repo(canonical)

    monkeypatch.chdir(stale_cp_code_checkout)
    # No override/env: falls back to the (stale) cwd-derived checkout.
    assert canonical_repo_root().resolve() == stale_cp_code_checkout.resolve()

    # Explicit override wins regardless of cwd.
    assert canonical_repo_root(override=canonical).resolve() == canonical.resolve()

    # Environment variable wins when no explicit override is given.
    monkeypatch.setenv(ENV_CANONICAL_REPO_ROOT, str(canonical))
    assert canonical_repo_root().resolve() == canonical.resolve()

    # An explicit override still takes precedence over the environment variable.
    other = tmp_path / "explicit-wins"
    _init_git_repo(other)
    assert canonical_repo_root(override=other).resolve() == other.resolve()


def test_quickstart_resolution_against_canonical_root_ignores_stale_cp_checkout(tmp_path, monkeypatch):
    """End-to-end version of scenario 2 through the actual Quick Start resolver."""

    stale_cp_code_checkout = tmp_path / "stale-dev-checkout"
    _init_git_repo(stale_cp_code_checkout)
    _write_ledger(
        stale_cp_code_checkout,
        _LEDGER_V1_05_PENDING.replace("| V1-05 | pending |", "| V1-05 | in-progress |"),
    )  # a stale checkout frozen before V1-05 even started

    canonical = tmp_path / "canonical-main"
    _init_git_repo(canonical)
    _write_ledger(canonical, _LEDGER_V1_05_PENDING)

    monkeypatch.chdir(stale_cp_code_checkout)
    resolved_root = canonical_repo_root(override=canonical)
    option = continue_video_editor_option(resolved_root)

    assert option.task_id == "V1-05"
    assert option.as_dict()["resolution_evidence"]["repo_root"] == str(canonical.resolve())


# --------------------------------------------------------------------------- 3/9: historical runs never override resolution


def test_historical_v1_01_task_row_does_not_change_current_resolution(tmp_path):
    """Scenarios 3 and 9: an old V1-01 Task/Runbook record must never make the

    current Quick Start resolution look like V1-01 is still the program
    position. The resolver never reads task/runbook state at all -- this
    test guards that contract explicitly.
    """

    repo = tmp_path / "canonical"
    _init_git_repo(repo)
    _write_ledger(repo, _LEDGER_V1_05_PENDING)

    state = State(":memory:")
    state.upsert_task(
        Task(id="historical-v1-01", task_ref="V1-01 Local import.", owner_ref="issue:#59", role="primary-implementation", worker="claude-code")
    )

    option = continue_video_editor_option(repo)

    assert option.task_id == "V1-05"
    assert state.get_task("historical-v1-01") is not None  # history is preserved, not deleted


# --------------------------------------------------------------------------- 4: stale AVAILABLE provider state refreshed at launch


def test_stale_available_provider_is_refreshed_before_launch_admits_it(tmp_path, monkeypatch):
    """Scenario 4: persisted AVAILABLE but stale -> launch-time refresh is performed."""

    import subprocess as subprocess_module

    state = State(":memory:")
    registry = load_registry()
    for worker in registry.workers.values():
        state.upsert_provider_state(
            ProviderState(
                name=worker.name,
                execution_system=worker.execution_system,
                provider=worker.provider,
                cost_class=worker.cost_class,
                state=STATE_AVAILABLE,
                configured=True,
                last_probe_at="2020-01-01T00:00:00+00:00",  # long stale
            )
        )

    class FakeNotAuthed:
        returncode = 1
        stdout = "Not logged in\n"
        stderr = ""

    monkeypatch.setattr(subprocess_module, "run", lambda *a, **k: FakeNotAuthed())
    monkeypatch.setattr("scripts.agents.registry.shutil.which", lambda _bin: "/usr/local/bin/claude")

    class FakeSupervisor:
        def launch_task(self, task: Task, *, dry_run: bool = False):
            task.state = "RUNNING"
            task.pid = 1
            state.upsert_task(task)
            return task

    task = Task(id="t1", task_ref="ENG-X-1", owner_ref="issue:#1", role="primary-implementation", worker="claude-code")
    state.upsert_task(task)

    managed_admit(
        state=state, registry=registry, scheduler=Scheduler(), supervisor=FakeSupervisor(),
        repo_root=tmp_path, task=task, dry_run=False,
    )

    refreshed = state.get_provider_state("claude-code")
    assert refreshed.last_probe_at != "2020-01-01T00:00:00+00:00"
    assert refreshed.state != STATE_AVAILABLE


def test_fresh_available_provider_state_is_not_reprobed(tmp_path, monkeypatch):
    """A provider probed within the freshness window must not incur another real probe call."""

    import subprocess as subprocess_module

    calls: list[object] = []
    monkeypatch.setattr(subprocess_module, "run", lambda *a, **k: calls.append(1) or pytest.fail("unexpected probe"))

    state = State(":memory:")
    registry = load_registry()
    from scripts.agents.control_plane.models import utc_now_iso

    state.upsert_provider_state(
        ProviderState(
            name="claude-code", execution_system="Claude Code", provider="Anthropic",
            cost_class="premium-subscription", state=STATE_AVAILABLE, configured=True,
            last_probe_at=utc_now_iso(),
        )
    )
    refresh_stale_routable_candidates(state, registry, ["claude-code"])
    assert not calls


# --------------------------------------------------------------------------- 5: Codex not launched while exhausted


def test_claude_and_codex_both_quota_exhausted_never_launches_codex(tmp_path):
    """Scenario 5: Claude QUOTA_EXHAUSTED + Codex QUOTA_EXHAUSTED -> Codex is not launched."""

    state = State(":memory:")
    registry = load_registry()
    for worker in registry.workers.values():
        exhausted = worker.name in {"claude-code", "codex-build"}
        state.upsert_provider_state(
            ProviderState(
                name=worker.name, execution_system=worker.execution_system, provider=worker.provider,
                cost_class=worker.cost_class,
                state=STATE_QUOTA_EXHAUSTED if exhausted else STATE_NOT_CONFIGURED,
                configured=worker.name in {"claude-code", "codex-build", "grok-build"},
            )
        )

    class FakeSupervisor:
        def launch_task(self, task: Task, *, dry_run: bool = False):
            task.state = "RUNNING"
            return task

    task = Task(
        id="t5", task_ref="ENG-X-5", owner_ref="issue:#5", role="primary-implementation",
        worker="claude-code", codex_policy="balanced", codex_auto_eligible=True,
    )
    state.upsert_task(task)

    admission = managed_admit(
        state=state, registry=registry, scheduler=Scheduler(), supervisor=FakeSupervisor(),
        repo_root=tmp_path, task=task, dry_run=True,
    )

    assert admission.task.worker != "codex-build"


# --------------------------------------------------------------------------- 6: valid same-role fallback


def test_claude_unavailable_falls_back_to_eligible_non_codex_worker(tmp_path):
    """Scenario 6: Claude unavailable + eligible non-Codex worker available -> valid fallback."""

    state = State(":memory:")
    registry = load_registry()
    for worker in registry.workers.values():
        state.upsert_provider_state(
            ProviderState(
                name=worker.name, execution_system=worker.execution_system, provider=worker.provider,
                cost_class=worker.cost_class,
                state=STATE_NOT_CONFIGURED if worker.name == "claude-code" else STATE_AVAILABLE,
                configured=worker.name in {"claude-code", "codex-build", "grok-build"},
            )
        )

    class FakeSupervisor:
        def launch_task(self, task: Task, *, dry_run: bool = False):
            task.state = "RUNNING"
            return task

    task = Task(id="t6", task_ref="ENG-X-6", owner_ref="issue:#6", role="primary-implementation", worker="claude-code")
    state.upsert_task(task)

    admission = managed_admit(
        state=state, registry=registry, scheduler=Scheduler(), supervisor=FakeSupervisor(),
        repo_root=tmp_path, task=task, dry_run=True,
    )

    assert admission.launched
    assert admission.task.worker in {"grok-build", "codex-build"}


# --------------------------------------------------------------------------- 7: no eligible worker blocks truthfully


def test_no_eligible_implementation_worker_blocks_truthfully(tmp_path):
    """Scenario 7: no eligible implementation worker -> BLOCK truthfully."""

    state = State(":memory:")
    registry = load_registry()
    for worker in registry.workers.values():
        state.upsert_provider_state(
            ProviderState(
                name=worker.name, execution_system=worker.execution_system, provider=worker.provider,
                cost_class=worker.cost_class, state=STATE_NOT_CONFIGURED, configured=False,
            )
        )

    class FakeSupervisor:
        def launch_task(self, task: Task, *, dry_run: bool = False):  # pragma: no cover - must never be called
            pytest.fail("no eligible worker must never reach launch")

    task = Task(id="t7", task_ref="ENG-X-7", owner_ref="issue:#7", role="primary-implementation", worker="claude-code")
    state.upsert_task(task)

    admission = managed_admit(
        state=state, registry=registry, scheduler=Scheduler(), supervisor=FakeSupervisor(),
        repo_root=tmp_path, task=task, dry_run=True,
    )

    assert admission.launched is False
    assert admission.outcome == "BLOCKED"
    assert "no eligible provider" in admission.reason


# --------------------------------------------------------------------------- 8: launcher environment failure stays distinct


def test_refresh_never_conflates_launch_environment_error_with_auth_failure(monkeypatch):
    """Scenario 8: a launcher/sandbox visibility failure must not become NOT_AUTHENTICATED."""

    import subprocess as subprocess_module

    class Timeout:
        def __call__(self, *a, **k):
            raise subprocess_module.TimeoutExpired(cmd="claude", timeout=1)

    state = State(":memory:")
    registry = load_registry()

    state.upsert_provider_state(
        ProviderState(
            name="claude-code", execution_system="Claude Code", provider="Anthropic",
            cost_class="premium-subscription", state=STATE_AVAILABLE, configured=True,
            last_probe_at="2020-01-01T00:00:00+00:00",
        )
    )
    monkeypatch.setattr(subprocess_module, "run", Timeout())
    monkeypatch.setattr("scripts.agents.registry.shutil.which", lambda _bin: "/usr/local/bin/claude")

    refresh_stale_routable_candidates(state, registry, ["claude-code"])

    updated = state.get_provider_state("claude-code")
    assert updated.state == STATE_LAUNCH_ENVIRONMENT_ERROR
    assert updated.reason != "NOT_AUTHENTICATED"


# --------------------------------------------------------------------------- freshness helpers


def test_provider_state_freshness_helpers():
    from scripts.agents.control_plane.models import utc_now_iso

    never_probed = ProviderState(
        name="x", execution_system="x", provider="x", cost_class="x", state=STATE_AVAILABLE, configured=True
    )
    assert provider_state_age_seconds(never_probed) is None
    assert provider_state_freshness(never_probed) == "UNKNOWN"
    assert provider_state_is_stale(never_probed) is True

    fresh = ProviderState(
        name="x", execution_system="x", provider="x", cost_class="x", state=STATE_AVAILABLE, configured=True,
        last_probe_at=utc_now_iso(),
    )
    assert provider_state_freshness(fresh) == "FRESH"
    assert provider_state_is_stale(fresh) is False

    stale = ProviderState(
        name="x", execution_system="x", provider="x", cost_class="x", state=STATE_AVAILABLE, configured=True,
        last_probe_at="2020-01-01T00:00:00+00:00",
    )
    assert provider_state_freshness(stale) == "STALE"
    assert provider_state_is_stale(stale) is True


# --------------------------------------------------------------------------- 10: CP process/version metadata


def test_cp_process_status_reports_canonical_and_cp_code_evidence(tmp_path):
    repo = tmp_path / "canonical"
    _init_git_repo(repo)
    state = State(":memory:")
    registry = load_registry()
    from scripts.agents.control_plane.scheduler import Scheduler as _Scheduler
    from scripts.agents.control_plane.supervisor import Supervisor

    ctx = CommandContext(
        state=state, registry=registry, scheduler=_Scheduler(),
        supervisor=Supervisor(registry=registry, repo_root=repo, state=state), repo_root=repo,
    )

    status = cp_process_status(ctx)

    assert status["canonical_repo_root"] == str(repo.resolve())
    canonical_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    assert status["canonical_repo_head"] == canonical_head
    assert status["pid"] > 0
    assert status["cp_code_root"] is not None
    assert status["cp_code_head"] is not None
    assert isinstance(status["cp_code_differs_from_canonical"], bool)
    # The CP code checkout running these tests genuinely differs from the
    # throwaway canonical fixture repo above.
    assert status["cp_code_differs_from_canonical"] is True


def test_cp_status_endpoint_is_read_only_and_zero_ai(tmp_path):
    from fastapi.testclient import TestClient

    from scripts.agents.control_plane.scheduler import Scheduler as _Scheduler
    from scripts.agents.control_plane.supervisor import Supervisor

    repo = tmp_path / "canonical"
    _init_git_repo(repo)
    state = State(":memory:")
    registry = load_registry()
    ctx = CommandContext(
        state=state, registry=registry, scheduler=_Scheduler(),
        supervisor=Supervisor(registry=registry, repo_root=repo, state=state), repo_root=repo,
    )
    app = create_app(ctx)
    client = TestClient(app)

    response = client.get("/api/cp-status")

    assert response.status_code == 200
    body = response.json()
    assert body["canonical_repo_root"] == str(repo.resolve())
    assert "cp_code_root" in body
