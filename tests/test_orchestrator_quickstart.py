"""Quick Start: dynamic next-task resolution (ENG-AGENT-02-S7, issue #97).

Guards the exact defect the maintainer reported: the Runbook form used to
require typing a task ID/branch/worktree by hand, and any resolver for
"next Video Editor task" must never hard-code a specific ID (e.g. "V1-01")
as permanently "the" next one — it must be re-derived from the ledger every
call so a later ledger update is picked up automatically.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts.agents.control_plane.quickstart import (
    VIDEO_EDITOR_LEDGER_RELATIVE,
    LedgerTask,
    continue_video_editor_option,
    find_existing_worktree_for_task,
    list_quickstart_options,
    next_eligible_video_editor_task,
    parse_video_editor_ledger,
)

_SAMPLE_LEDGER = """\
# Standalone Video Editor — Revised Implementation Status

## Status values

`inherited-unverified | pending | in-progress | blocked | complete | superseded`

## Foundation and V1

| ID | Status | Notes/evidence |
|---|---|---|
| FND-08 | complete | Standalone React editor shell. |
| V1-01 | pending | Local import. |
| V1-02 | pending | Library import/media bin. |

### 2026-09-09 — some evidence heading

- Prose that must never be mistaken for a table row, even though it
  mentions | pipes | here.

## V1.5 and round-trip

| ID | Status | Notes/evidence |
|---|---|---|
| V15-01R | pending | Portable keyframes + markers. |
"""


def _repo(tmp_path: Path, ledger_text: str) -> Path:
    ledger_path = tmp_path / VIDEO_EDITOR_LEDGER_RELATIVE
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(ledger_text, encoding="utf-8")
    return tmp_path


def test_parse_skips_headers_separators_and_prose_pipes():
    tasks = parse_video_editor_ledger(_SAMPLE_LEDGER)
    assert tasks == [
        LedgerTask(task_id="FND-08", status="complete", notes="Standalone React editor shell."),
        LedgerTask(task_id="V1-01", status="pending", notes="Local import."),
        LedgerTask(task_id="V1-02", status="pending", notes="Library import/media bin."),
        LedgerTask(task_id="V15-01R", status="pending", notes="Portable keyframes + markers."),
    ]


def test_next_eligible_is_the_first_pending_row_in_document_order(tmp_path):
    repo_root = _repo(tmp_path, _SAMPLE_LEDGER)
    task = next_eligible_video_editor_task(repo_root)
    assert task == LedgerTask(task_id="V1-01", status="pending", notes="Local import.")


def test_resolver_is_never_hard_coded_and_tracks_ledger_changes(tmp_path):
    """The exact defect requirement: if the ledger says V1-01 is done and V1-02
    is next, the resolver must report V1-02 -- with no code change required.
    """

    repo_root = _repo(tmp_path, _SAMPLE_LEDGER)
    assert next_eligible_video_editor_task(repo_root).task_id == "V1-01"

    advanced_ledger = _SAMPLE_LEDGER.replace("| V1-01 | pending |", "| V1-01 | complete |")
    (repo_root / VIDEO_EDITOR_LEDGER_RELATIVE).write_text(advanced_ledger, encoding="utf-8")
    assert next_eligible_video_editor_task(repo_root).task_id == "V1-02"


def test_next_eligible_is_none_when_nothing_is_pending(tmp_path):
    all_done = _SAMPLE_LEDGER.replace("pending", "complete")
    repo_root = _repo(tmp_path, all_done)
    assert next_eligible_video_editor_task(repo_root) is None


def test_next_eligible_is_none_when_the_ledger_file_is_missing(tmp_path):
    assert next_eligible_video_editor_task(tmp_path) is None


def _init_git_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    (root / "README.md").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)


@pytest.mark.parametrize("has_worktree", [True, False])
def test_continue_video_editor_option_reflects_worktree_existence(tmp_path, has_worktree):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _init_git_repo(repo_root)
    (repo_root / VIDEO_EDITOR_LEDGER_RELATIVE).parent.mkdir(parents=True, exist_ok=True)
    (repo_root / VIDEO_EDITOR_LEDGER_RELATIVE).write_text(_SAMPLE_LEDGER, encoding="utf-8")

    if has_worktree:
        worktree_path = tmp_path / "repo-v1-01"
        subprocess.run(
            ["git", "worktree", "add", "-q", str(worktree_path), "-b", "video-editor/V1-01-local-import"],
            cwd=repo_root,
            check=True,
        )

    option = continue_video_editor_option(repo_root)
    assert option.key == "continue-video-editor"
    assert option.source_ref.startswith("V1-01")
    assert option.continues_existing_worktree is has_worktree
    # A missing worktree is never `unavailable_reason` (Grok Build review,
    # issue #97): start_quickstart_option() auto-provisions it, so the option
    # must stay startable ("Start Development" enabled) either way. Only
    # `continues_existing_worktree` distinguishes the two cases.
    assert option.unavailable_reason is None
    if has_worktree:
        assert option.worktree == str(worktree_path)
        assert option.branch == "video-editor/V1-01-local-import"
    else:
        assert option.worktree == str(tmp_path / "repo-v1-01")
        assert option.branch == "video-editor/v1-01-local-import"


def test_continue_video_editor_option_is_honest_when_nothing_is_pending(tmp_path):
    repo_root = tmp_path
    (repo_root / VIDEO_EDITOR_LEDGER_RELATIVE).parent.mkdir(parents=True, exist_ok=True)
    (repo_root / VIDEO_EDITOR_LEDGER_RELATIVE).write_text(
        _SAMPLE_LEDGER.replace("pending", "complete"), encoding="utf-8"
    )

    option = continue_video_editor_option(repo_root)
    assert option.unavailable_reason is not None
    assert option.source_ref == ""
    assert option.branch is None and option.worktree is None


def test_find_existing_worktree_for_task_matches_case_insensitively(tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _init_git_repo(repo_root)
    worktree_path = tmp_path / "repo-v1-03"
    subprocess.run(
        ["git", "worktree", "add", "-q", str(worktree_path), "-b", "video-editor/v1-03-media-derivatives"],
        cwd=repo_root,
        check=True,
    )
    record = find_existing_worktree_for_task(repo_root, "V1-03")
    assert record is not None
    assert record.path == str(worktree_path)
    assert find_existing_worktree_for_task(repo_root, "V1-99") is None


def test_find_existing_worktree_for_task_never_matches_the_primary_checkout(tmp_path):
    """Grok Build review (issue #97): a real task worktree is always a

    dedicated sibling directory; the primary checkout merely happening to be
    on a similarly-named branch must never be treated as a substitute, since
    that would launch an unattended write-capable session directly against
    the maintainer's main checkout.
    """

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "video-editor/v1-01-local-import", str(repo_root)], check=True)
    subprocess.run(["git", "-C", str(repo_root), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo_root), "config", "user.name", "Test"], check=True)
    (repo_root / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo_root), "commit", "-q", "-m", "seed"], check=True)

    assert find_existing_worktree_for_task(repo_root, "V1-01") is None


def test_find_existing_worktree_for_task_rejects_a_similar_but_longer_task_id(tmp_path):
    """ "V1-01" must not match inside "v1-010-..." (Grok Build review, issue #97)."""

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _init_git_repo(repo_root)
    worktree_path = tmp_path / "repo-v1-010"
    subprocess.run(
        ["git", "worktree", "add", "-q", str(worktree_path), "-b", "video-editor/v1-010-unrelated-task"],
        cwd=repo_root,
        check=True,
    )
    assert find_existing_worktree_for_task(repo_root, "V1-01") is None
    assert find_existing_worktree_for_task(repo_root, "V1-010") is not None


def test_list_quickstart_options_against_the_real_repository_ledger():
    """Integration smoke test against this repository's actual ledger.

    Deliberately does not assert which task ID is next (that would
    reintroduce the exact hard-coding this feature exists to avoid) --
    only that the resolver produces a well-formed, self-consistent option
    against whatever the ledger currently says.
    """

    from tests.octarel_paths import octascene_checkout

    repo_root = octascene_checkout()
    if repo_root is None:
        pytest.skip("set OCTAREL_OCTASCENE_ROOT to a real Octages checkout")
    options = list_quickstart_options(repo_root)
    assert [option["key"] for option in options] == [
        "continue-video-editor",
        "continue-octascene",
        "finish-current-pr",
        "focused-test-fix",
        "review-current-diff",
        "custom-run",
    ]
    option = options[0]
    assert option["key"] == "continue-video-editor"
    if option["ready"]:
        assert option["source_ref"]
        assert option["branch"]
        assert option["worktree"]
    else:
        assert option["unavailable_reason"]
    assert option["program"] == "Standalone Video Editor"
    assert option["codex_policy"] == "balanced"
    assert option["codex_auto_eligible"] is True
    assert option["max_codex_invocations"] == 1
    assert option["why_next"]
    assert option["dependency_state"]
    assert option["proposed_tester"]
    assert option["proposed_reviewer"]
    assert option["expected_checks"]
    assert options[-1]["action"] == "advanced"
