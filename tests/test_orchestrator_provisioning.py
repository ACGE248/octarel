"""Worktree auto-provisioning safety (ENG-AGENT-02-S7, issue #97).

This is the one write-shaped git/filesystem operation the Control Center can
trigger on an operator's behalf, so every unsafe-input path is exercised
here: path traversal, shell-metacharacter-shaped names, an already-existing
path, an already-existing branch, and a non-sibling target directory.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts.agents.control_plane.provisioning import (
    ProvisioningError,
    provision_worktree,
)


def _init_git_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    (root / "README.md").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)
    subprocess.run(["git", "branch", "-m", "main"], cwd=root, check=True)


def test_provision_worktree_happy_path_creates_a_real_sibling_worktree(tmp_path):
    repo_root = tmp_path / "repo"
    _init_git_repo(repo_root)
    target = tmp_path / "repo-v1-01"

    result = provision_worktree(repo_root=repo_root, worktree=str(target), branch="video-editor/v1-01-local-import")

    assert result.path == str(target)
    assert result.branch == "video-editor/v1-01-local-import"
    assert target.is_dir()
    assert (target / "README.md").exists()
    branches = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname:short)", "refs/heads/"],
        cwd=repo_root, capture_output=True, text=True, check=True,
    ).stdout
    assert "video-editor/v1-01-local-import" in branches


def test_provision_worktree_rejects_a_relative_path(tmp_path):
    repo_root = tmp_path / "repo"
    _init_git_repo(repo_root)
    with pytest.raises(ProvisioningError, match="absolute"):
        provision_worktree(repo_root=repo_root, worktree="repo-v1-01", branch="feature/x")


def test_provision_worktree_rejects_a_non_sibling_directory(tmp_path):
    repo_root = tmp_path / "repo"
    _init_git_repo(repo_root)
    nested = repo_root / "nested-worktree"
    with pytest.raises(ProvisioningError, match="sibling directory"):
        provision_worktree(repo_root=repo_root, worktree=str(nested), branch="feature/x")


def test_provision_worktree_rejects_an_unsafe_directory_name(tmp_path):
    repo_root = tmp_path / "repo"
    _init_git_repo(repo_root)
    unsafe = tmp_path / "repo-v1; rm -rf /"
    with pytest.raises(ProvisioningError, match="unsafe characters"):
        provision_worktree(repo_root=repo_root, worktree=str(unsafe), branch="feature/x")


def test_provision_worktree_rejects_an_unsafe_branch_name(tmp_path):
    repo_root = tmp_path / "repo"
    _init_git_repo(repo_root)
    target = tmp_path / "repo-v1-01"
    with pytest.raises(ProvisioningError, match="unsafe characters"):
        provision_worktree(repo_root=repo_root, worktree=str(target), branch="feature/x; rm -rf /")


def test_provision_worktree_rejects_path_traversal_in_branch_name(tmp_path):
    repo_root = tmp_path / "repo"
    _init_git_repo(repo_root)
    target = tmp_path / "repo-v1-01"
    with pytest.raises(ProvisioningError, match="unsafe characters"):
        provision_worktree(repo_root=repo_root, worktree=str(target), branch="../../escape")


def test_provision_worktree_refuses_to_overwrite_an_existing_path(tmp_path):
    repo_root = tmp_path / "repo"
    _init_git_repo(repo_root)
    target = tmp_path / "repo-v1-01"
    target.mkdir()
    (target / "already-here.txt").write_text("keep me", encoding="utf-8")

    with pytest.raises(ProvisioningError, match="already exists"):
        provision_worktree(repo_root=repo_root, worktree=str(target), branch="feature/x")
    assert (target / "already-here.txt").read_text(encoding="utf-8") == "keep me"


def test_provision_worktree_refuses_to_reuse_an_existing_branch(tmp_path):
    repo_root = tmp_path / "repo"
    _init_git_repo(repo_root)
    subprocess.run(["git", "branch", "already-taken"], cwd=repo_root, check=True)
    target = tmp_path / "repo-taken"

    with pytest.raises(ProvisioningError, match="already exists"):
        provision_worktree(repo_root=repo_root, worktree=str(target), branch="already-taken")
    assert not target.exists()


def test_provision_worktree_reports_underlying_git_failure(tmp_path):
    repo_root = tmp_path / "repo"
    _init_git_repo(repo_root)
    target = tmp_path / "repo-bad-base"

    with pytest.raises(ProvisioningError, match="git worktree add failed"):
        provision_worktree(repo_root=repo_root, worktree=str(target), branch="feature/x", base_ref="not-a-real-ref")
    assert not target.exists()


# --------------------------------------------------------------------------- ENG-AGENT-14: review checkouts


def test_resolve_pr_reference_parses_gh_output(tmp_path, monkeypatch):
    from scripts.agents.control_plane import provisioning

    def fake_run(argv, cwd, capture_output, text, timeout, check):
        assert argv[:3] == ["gh", "pr", "view"]
        assert argv[3] == "139"
        assert "--repo" in argv and "ACGE248/octages" in argv
        return subprocess.CompletedProcess(
            argv, 0,
            stdout='{"number": 139, "headRefName": "eng/ENG-AGENT-13-runbook-acceptance-pipeline", "headRefOid": "abc123", "state": "MERGED"}',
            stderr="",
        )

    monkeypatch.setattr(provisioning.subprocess, "run", fake_run)

    number, branch, head_sha = provisioning.resolve_pr_reference(
        repo_root=tmp_path, repository="ACGE248/octages", pr_reference="pr:139",
    )

    assert number == 139
    assert branch == "eng/ENG-AGENT-13-runbook-acceptance-pipeline"
    assert head_sha == "abc123"


@pytest.mark.parametrize("bad_ref", ["not-a-pr", "../139", "139; rm -rf /"])
def test_resolve_pr_reference_rejects_unsafe_or_malformed_input(tmp_path, bad_ref):
    from scripts.agents.control_plane import provisioning

    with pytest.raises(ProvisioningError):
        provisioning.resolve_pr_reference(repo_root=tmp_path, repository="ACGE248/octages", pr_reference=bad_ref)


def test_resolve_pr_reference_raises_on_gh_failure(tmp_path, monkeypatch):
    from scripts.agents.control_plane import provisioning

    monkeypatch.setattr(
        provisioning.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess([], 1, stdout="", stderr="no pull requests found"),
    )

    with pytest.raises(ProvisioningError, match="gh pr view"):
        provisioning.resolve_pr_reference(repo_root=tmp_path, repository="ACGE248/octages", pr_reference="999")


def test_provision_review_worktree_creates_a_detached_exact_head_checkout(tmp_path, monkeypatch):
    from scripts.agents.control_plane import provisioning

    repo_root = tmp_path / "repo"
    _init_git_repo(repo_root)
    head_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_root, capture_output=True, text=True, check=True,
    ).stdout.strip()

    monkeypatch.setattr(
        provisioning, "resolve_pr_reference",
        lambda **kwargs: (139, "eng/some-branch", head_sha),
    )

    result = provisioning.provision_review_worktree(
        repo_root=repo_root, repository="ACGE248/octages", pr_reference="139", existing=[],
    )

    assert result.reused_existing is False
    assert result.pr_number == 139
    assert result.head_sha == head_sha
    worktree_path = Path(result.path)
    assert worktree_path.is_dir()
    checked_out = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=worktree_path, capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert checked_out == head_sha
    # Detached: no branch was created for this checkout.
    branch = subprocess.run(
        ["git", "symbolic-ref", "-q", "HEAD"], cwd=worktree_path, capture_output=True, text=True, check=False,
    )
    assert branch.returncode != 0


def test_provision_review_worktree_reuses_an_existing_exact_head_checkout(tmp_path, monkeypatch):
    from scripts.agents.control_plane import provisioning

    repo_root = tmp_path / "repo"
    _init_git_repo(repo_root)
    head_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_root, capture_output=True, text=True, check=True,
    ).stdout.strip()
    monkeypatch.setattr(provisioning, "resolve_pr_reference", lambda **kwargs: (139, "eng/some-branch", head_sha))

    def _run_git_must_not_be_called(args, *, cwd, timeout=30.0):
        raise AssertionError(f"must not create a new worktree when an exact-head one is already registered: {args}")

    monkeypatch.setattr(provisioning, "_run_git", _run_git_must_not_be_called)
    already_existing_path = tmp_path / "already-there"
    already_existing_path.mkdir()

    result = provisioning.provision_review_worktree(
        repo_root=repo_root, repository="ACGE248/octages", pr_reference="139",
        existing=[(str(already_existing_path), head_sha)],
    )

    assert result.reused_existing is True
    assert result.path == str(already_existing_path)


def test_provision_review_worktree_never_reuses_a_worktree_for_a_different_head(tmp_path, monkeypatch):
    """A worktree recorded for a different (or no) review head must never be

    treated as a substitute -- this is what keeps main/DevCP/a product
    implementation worktree/another task's checkout from ever being
    implicitly reused.
    """

    from scripts.agents.control_plane import provisioning

    repo_root = tmp_path / "repo"
    _init_git_repo(repo_root)
    head_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_root, capture_output=True, text=True, check=True,
    ).stdout.strip()
    monkeypatch.setattr(provisioning, "resolve_pr_reference", lambda **kwargs: (139, "eng/some-branch", head_sha))

    result = provisioning.provision_review_worktree(
        repo_root=repo_root, repository="ACGE248/octages", pr_reference="139",
        existing=[("/some/unrelated/worktree", "deadbeef" * 5)],
    )

    assert result.reused_existing is False
    assert result.path != "/some/unrelated/worktree"


def test_provision_review_worktree_bootstraps_dependencies_only_when_requested_and_needed(tmp_path, monkeypatch):
    from scripts.agents.control_plane import provisioning

    repo_root = tmp_path / "repo"
    _init_git_repo(repo_root)
    (repo_root / "package-lock.json").write_text("{}", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo_root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add lockfile"], cwd=repo_root, check=True)
    head_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_root, capture_output=True, text=True, check=True,
    ).stdout.strip()
    monkeypatch.setattr(provisioning, "resolve_pr_reference", lambda **kwargs: (139, "eng/some-branch", head_sha))

    calls = []
    monkeypatch.setattr(
        "scripts.ci.environment.bootstrap_dependencies",
        lambda root, **kwargs: calls.append((root, kwargs)) or {"dependencies_status": "DEPENDENCIES_INSTALLED", "browser": {"status": "BROWSER_DOWNLOADED"}},
    )

    without_bootstrap = provisioning.provision_review_worktree(
        repo_root=repo_root, repository="ACGE248/octages", pr_reference="139", existing=[], bootstrap_ui=False,
    )
    assert without_bootstrap.bootstrap is None
    assert calls == []

    # A fresh PR reference (different head) so a new worktree is created again.
    monkeypatch.setattr(provisioning, "resolve_pr_reference", lambda **kwargs: (140, "eng/other-branch", head_sha + "0"))
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "second"], cwd=repo_root, check=True)
    real_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_root, capture_output=True, text=True, check=True,
    ).stdout.strip()
    monkeypatch.setattr(provisioning, "resolve_pr_reference", lambda **kwargs: (140, "eng/other-branch", real_head))

    with_bootstrap = provisioning.provision_review_worktree(
        repo_root=repo_root, repository="ACGE248/octages", pr_reference="140", existing=[], bootstrap_ui=True,
    )
    assert with_bootstrap.bootstrap == {"dependencies_status": "DEPENDENCIES_INSTALLED", "browser": {"status": "BROWSER_DOWNLOADED"}}
    assert len(calls) == 1
