"""ENG-AGENT-17 (issue #148): self-healing the OpenCode2 ``--agent`` preset

a candidate worktree's own tracked tree may predate, generically -- not a
V1-05-specific patch.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.ci import reviewer_presets


@pytest.fixture(autouse=True)
def _canonical_root_at_repo_root(monkeypatch):
    # The real canonical root is this repository's own checkout, which has
    # .opencode/agents/reviewer.md -- reuse it directly rather than faking a
    # second one, so these tests exercise the real preset content.
    real_root = Path(__file__).resolve().parents[1]
    assert (real_root / ".opencode" / "agents" / "reviewer.md").is_file()
    monkeypatch.setattr(reviewer_presets, "_canonical_root", lambda: real_root)


def test_self_heals_missing_preset_onto_a_historical_worktree(tmp_path) -> None:
    assert not (tmp_path / ".opencode" / "agents" / "reviewer.md").exists()

    healed = reviewer_presets.ensure_opencode_agent_preset(tmp_path, "reviewer")

    assert healed is True
    materialized = tmp_path / ".opencode" / "agents" / "reviewer.md"
    assert materialized.is_file()
    canonical = Path(__file__).resolve().parents[1] / ".opencode" / "agents" / "reviewer.md"
    assert materialized.read_text(encoding="utf-8") == canonical.read_text(encoding="utf-8")


def test_never_overwrites_an_existing_preset_even_if_older(tmp_path) -> None:
    existing = tmp_path / ".opencode" / "agents" / "reviewer.md"
    existing.parent.mkdir(parents=True)
    existing.write_text("an older/different reviewer preset\n", encoding="utf-8")

    healed = reviewer_presets.ensure_opencode_agent_preset(tmp_path, "reviewer")

    assert healed is True
    assert existing.read_text(encoding="utf-8") == "an older/different reviewer preset\n"


def test_materialized_preset_is_excluded_from_candidate_identity(tmp_path) -> None:
    import subprocess

    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@example.test"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    (tmp_path / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", "seed"], check=True)

    reviewer_presets.ensure_opencode_agent_preset(tmp_path, "reviewer")

    untracked = subprocess.run(
        ["git", "-C", str(tmp_path), "ls-files", "--others", "--exclude-standard"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert ".opencode/agents/reviewer.md" not in untracked


def test_missing_canonical_source_fails_closed_not_silently(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(reviewer_presets, "_canonical_root", lambda: tmp_path / "nonexistent-canonical-root")

    healed = reviewer_presets.ensure_opencode_agent_preset(tmp_path / "worktree", "reviewer")

    assert healed is False


def test_ensure_for_command_is_a_noop_for_non_opencode_workers() -> None:
    assert reviewer_presets.ensure_opencode_agent_preset_for_command(
        Path("/nonexistent"), ["claude", "--print", "hello"],
    ) is True


def test_ensure_for_command_is_a_noop_when_no_agent_flag_present(tmp_path) -> None:
    assert reviewer_presets.ensure_opencode_agent_preset_for_command(
        tmp_path, ["opencode", "run", "--model", "x/y", "hello"],
    ) is True


def test_ensure_for_command_self_heals_the_named_agent(tmp_path) -> None:
    assert reviewer_presets.ensure_opencode_agent_preset_for_command(
        tmp_path, ["opencode", "run", "--model", "x/y", "--agent", "reviewer", "hello"],
    ) is True
    assert (tmp_path / ".opencode" / "agents" / "reviewer.md").is_file()


def test_ensure_for_command_self_heals_the_single_token_agent_equals_form(tmp_path) -> None:
    assert reviewer_presets.ensure_opencode_agent_preset_for_command(
        tmp_path, ["opencode", "run", "--model", "x/y", "--agent=reviewer", "hello"],
    ) is True
    assert (tmp_path / ".opencode" / "agents" / "reviewer.md").is_file()
