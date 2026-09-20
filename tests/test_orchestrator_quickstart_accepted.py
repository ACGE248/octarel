"""Quick Start must not present an already-accepted task as startable.

Found preparing the first real OctaScene run: the ledger still listed V1-08 as
``pending`` (its PR was open, unmerged) while Octarel already held a SUCCEEDED,
fully accepted run for it, and the Control Center offered "Start Development".
The task source cannot know about Octarel's own runs, so Quick Start overlays
them: no replay of accepted work, and the tester/reviewer shown are routable.

No provider is called; the worker CLI is never spawned.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts.agents.control_plane.models import (
    RUNBOOK_FAILED,
    RUNBOOK_SUCCEEDED,
    ProviderState,
    Runbook,
)
from scripts.agents.control_plane.quickstart import (
    VIDEO_EDITOR_LEDGER_RELATIVE,
    QuickStartError,
    list_quickstart_options,
    resolve_quickstart_option,
    start_quickstart_option,
)
from scripts.agents.control_plane.state import State
from scripts.agents.registry import load_registry

LEDGER = """\
| ID | Status | Notes/evidence |
|---|---|---|
| V1-07 | complete | done |
| V1-08 | pending | Split/trim/ripple/replace. |
| V1-09 | pending | Browser preview. |
"""


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@example.com"], ["config", "user.name", "T"]):
        subprocess.run(["git", *args], cwd=root, check=True)
    ledger = root / VIDEO_EDITOR_LEDGER_RELATIVE
    ledger.parent.mkdir(parents=True)
    ledger.write_text(LEDGER, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)
    subprocess.run(["git", "branch", "-m", "main"], cwd=root, check=True)
    return root


@pytest.fixture()
def state(tmp_path: Path) -> State:
    return State(tmp_path / "s" / "cp.db")


def run(state: State, task: str, *, status=RUNBOOK_SUCCEEDED, stage="DONE", project_id=None, rid=None) -> Runbook:
    runbook = Runbook(
        id=rid or f"RB-{task}-{status}-{stage}", name=f"Continue Video Editor — {task}", preset="overnight-development",
        objective="x", source_ref=f"{task} (x)", branch=f"video-editor/{task.lower()}-work", worktree="/w",
        parent_worker="claude-code", max_duration_minutes=60, status=status, acceptance_stage=stage, project_id=project_id,
        acceptance_evidence={"test": {"status": "PASS"}},
    )
    state.upsert_runbook(runbook)
    return runbook


def option(repo, state, **kw):
    return resolve_quickstart_option(repo, "continue-video-editor", state=state, **kw)


def test_ready_when_no_accepted_run_exists(repo, state):
    opt = option(repo, state)
    assert opt.task_id == "V1-08" and opt.unavailable_reason is None


def test_accepted_run_blocks_the_option_and_names_the_run(repo, state):
    accepted = run(state, "V1-08")
    opt = option(repo, state)
    assert opt.task_id == "V1-08"  # the ledger is still the source of *which* task
    assert opt.unavailable_reason and accepted.id in opt.unavailable_reason
    assert "will not start it again" in opt.unavailable_reason
    assert opt.dependency_state.startswith("Blocked")
    assert "already has accepted run" in opt.why_next


def test_listing_marks_the_option_not_ready(repo, state):
    run(state, "V1-08")
    listed = {o["key"]: o for o in list_quickstart_options(repo, state=state)}
    assert listed["continue-video-editor"]["ready"] is False
    assert listed["continue-octascene"]["ready"] is False
    assert listed["continue-video-editor"]["unavailable_reason"]


def test_start_is_refused_server_side_and_creates_nothing(repo, state):
    run(state, "V1-08", rid="RB-accepted")
    before = {r.id for r in state.list_runbooks()}
    with pytest.raises(QuickStartError, match="already implemented and accepted"):
        start_quickstart_option(
            state=state, registry=load_registry(), supervisor=object(), repo_root=repo,
            key="continue-video-editor", dry_run=True,
        )
    assert {r.id for r in state.list_runbooks()} == before
    assert state.list_worktrees() == [] and not (repo.parent / "repo-v1-08").exists()  # no worktree provisioned


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(task="V1-09"),                                   # a different task
        dict(task="V1-08", status=RUNBOOK_FAILED),            # not accepted
        dict(task="V1-08", stage="review"),                   # acceptance not finished
    ],
)
def test_runs_that_are_not_an_accepted_implementation_of_the_task_do_not_block(repo, state, kwargs):
    run(state, **kwargs)
    assert option(repo, state).unavailable_reason is None


def test_accepted_run_of_another_project_does_not_block(repo, state):
    run(state, "V1-08", project_id="other-project")
    from scripts.agents.control_plane.project import ProjectContract

    project = ProjectContract(project_id="octascene", display_name="OctaScene", local_repo_root=repo)
    assert resolve_quickstart_option(repo, "continue-video-editor", project=project, state=state).unavailable_reason is None
    run(state, "V1-08", project_id="octascene", rid="RB-mine")
    assert resolve_quickstart_option(repo, "continue-video-editor", project=project, state=state).unavailable_reason


def test_without_state_behaviour_is_unchanged(repo):
    assert resolve_quickstart_option(repo, "continue-video-editor").unavailable_reason is None


def test_proposed_reviewer_and_tester_are_workers_that_can_actually_run(repo, state):
    registry = load_registry()
    for name, worker in registry.workers.items():
        state.upsert_provider_state(ProviderState(
            name=name, execution_system="cli", provider=worker.provider, cost_class=worker.cost_class,
            state="DISABLED" if name.startswith("codex") else "AVAILABLE",
        ))
    opt = option(repo, state, registry=registry)
    assert opt.proposed_reviewer != "codex-review"  # the old hard-coded default is DISABLED here
    assert opt.proposed_reviewer == "antigravity-diff-review" and opt.proposed_tester == "opencode2-gemini-flash-lite"
    # If the first reviewer becomes unavailable the next free route is shown, never an API-billed one.
    state.upsert_provider_state(ProviderState(
        name="antigravity-diff-review", execution_system="cli", provider="Google", cost_class="supplemental-configured",
        state="QUOTA_EXHAUSTED",
    ))
    assert option(repo, state, registry=registry).proposed_reviewer == "opencode2-gemini-flash-lite-review"
    assert not registry.workers[option(repo, state, registry=registry).proposed_reviewer].allow_api_billing


def test_no_routable_reviewer_is_reported_not_papered_over(repo, state):
    registry = load_registry()
    for name, worker in registry.workers.items():
        state.upsert_provider_state(ProviderState(
            name=name, execution_system="cli", provider=worker.provider, cost_class=worker.cost_class,
            state="QUOTA_EXHAUSTED" if "review" in name or name.startswith("antigravity") else "AVAILABLE",
        ))
    assert option(repo, state, registry=registry).proposed_reviewer == "(no routable worker)"
