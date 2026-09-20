"""Quick Start must not silently adopt a stale, unowned draft branch.

Found preparing OctaScene's next task: V1-09's existing worktree was a stacked draft
forked from an obsolete head (hundreds of commits behind main, carrying an old
unreconciled copy of another task). The resolver proposed "continue existing worktree".
It now proposes a fresh ``-reconciled`` branch/worktree and leaves the draft untouched,
while a branch an Octarel run owns, or one that already contains main, is still a
legitimate continuation. No provider or worker CLI is ever launched.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts.agents.control_plane.models import Runbook
from scripts.agents.control_plane.quickstart import (
    VIDEO_EDITOR_LEDGER_RELATIVE,
    resolve_quickstart_option,
    start_quickstart_option,
)
from scripts.agents.control_plane.state import State
from scripts.agents.registry import load_registry

LEDGER = "| ID | Status | Notes/evidence |\n|---|---|---|\n| V1-09 | pending | Browser preview. |\n"
BRANCH = "video-editor/v1-09-browser-preview"


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=T", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / "README.md").write_text("x", encoding="utf-8")
    ledger = root / VIDEO_EDITOR_LEDGER_RELATIVE
    ledger.parent.mkdir(parents=True)
    ledger.write_text(LEDGER, encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "init")
    return root


def add_draft(repo: Path) -> Path:
    """A task worktree forked from the initial commit; main then moves on."""

    worktree = repo.parent / "repo-v1-09"
    git(repo, "worktree", "add", "-q", str(worktree), "-b", BRANCH, "main")
    (worktree / "draft.txt").write_text("draft", encoding="utf-8")
    git(worktree, "add", "-A")
    git(worktree, "commit", "-qm", "draft work")
    (repo / "later.txt").write_text("later", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "main moves on")
    return worktree


@pytest.fixture()
def state(tmp_path: Path) -> State:
    return State(tmp_path / "s" / "cp.db")


def option(repo: Path, state: State):
    return resolve_quickstart_option(repo, "continue-video-editor", state=state)


def test_an_unowned_stale_draft_is_not_adopted(repo, state):
    draft = add_draft(repo)
    opt = option(repo, state)
    assert opt.continues_existing_worktree is False
    assert opt.branch == f"{BRANCH}-reconciled" and opt.worktree == f"{draft}-reconciled"
    assert "stale ancestry" in opt.checkpoint_pr_behavior and BRANCH in opt.checkpoint_pr_behavior
    assert opt.unavailable_reason is None  # still startable, just not on the stale draft


def test_a_draft_that_already_contains_main_is_a_legitimate_continuation(repo, state):
    draft = add_draft(repo)
    git(draft, "merge", "-q", "main", "-m", "sync")
    opt = option(repo, state)
    assert opt.continues_existing_worktree is True and opt.branch == BRANCH and opt.worktree == str(draft)


def test_a_branch_an_octarel_run_owns_is_a_continuation_even_if_behind(repo, state):
    draft = add_draft(repo)
    state.upsert_runbook(Runbook(
        id="RB-1", name="Continue Video Editor — V1-09", preset="overnight-development", objective="x", source_ref="V1-09 (x)",
        branch=BRANCH, worktree=str(draft), parent_worker="claude-code", max_duration_minutes=60, status="RUNNING",
    ))
    opt = option(repo, state)
    assert opt.continues_existing_worktree is True and opt.worktree == str(draft)


def test_without_state_the_legacy_behaviour_is_unchanged(repo):
    draft = add_draft(repo)
    opt = resolve_quickstart_option(repo, "continue-video-editor")
    assert opt.continues_existing_worktree is True and opt.worktree == str(draft)


def test_start_provisions_the_fresh_worktree_from_current_main_and_leaves_the_draft_alone(repo, state, monkeypatch):
    from scripts.agents.control_plane.supervisor import Supervisor

    monkeypatch.setattr(Supervisor, "_spawn", lambda self, argv, cwd: type("P", (), {"pid": 4242})())  # noqa: ARG005
    monkeypatch.setattr("scripts.agents.control_plane.supervisor.assert_write_safety", lambda *a, **k: None)
    draft = add_draft(repo)
    draft_head = git(draft, "rev-parse", "HEAD")
    registry = load_registry()
    runbook = start_quickstart_option(
        state=state, registry=registry, supervisor=Supervisor(registry=registry, repo_root=repo, state=state),
        repo_root=repo, key="continue-video-editor", dry_run=True,
    )
    fresh = Path(f"{draft}-reconciled")
    assert runbook.branch == f"{BRANCH}-reconciled" and runbook.worktree == str(fresh) and fresh.is_dir()
    assert (fresh / "later.txt").exists() and not (fresh / "draft.txt").exists()  # forked from current main, not the draft
    assert git(draft, "rev-parse", "HEAD") == draft_head and (draft / "draft.txt").exists()  # draft untouched
    # A second resolve re-adopts the worktree it just provisioned instead of inventing another name.
    again = option(repo, state)
    assert again.branch == runbook.branch and again.worktree == runbook.worktree
