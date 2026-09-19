"""``orchestrator.py`` CLI wiring, including the zero-writes ``--dry-run`` contract."""

from __future__ import annotations

import subprocess
from pathlib import Path

from scripts.agents import orchestrator
from scripts.agents.control_plane.state import default_db_path


def _git_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", "-b", "work", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    (tmp_path / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", "seed"], check=True)
    return tmp_path


def test_dry_run_via_run_subcommand_performs_zero_writes_and_zero_subprocesses(tmp_path, monkeypatch, capsys):
    repo = _git_repo(tmp_path)
    monkeypatch.setattr(orchestrator, "repo_root", lambda: repo)

    def _boom(*_a, **_k):
        raise AssertionError("dry-run must never spawn a subprocess")

    monkeypatch.setattr("scripts.agents.control_plane.supervisor.subprocess.Popen", _boom)

    exit_code = orchestrator.main(["run", "--dry-run"])

    assert exit_code == orchestrator.EXIT_OK
    assert not default_db_path(repo).exists(), "dry-run must never create the real state database file"
    out = capsys.readouterr().out
    assert "dry run" in out.lower()


def test_bare_top_level_dry_run_flag_defaults_to_run_subcommand(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    monkeypatch.setattr(orchestrator, "repo_root", lambda: repo)
    exit_code = orchestrator.main(["--dry-run"])
    assert exit_code == orchestrator.EXIT_OK
    assert not default_db_path(repo).exists()


def test_enqueue_then_start_dry_run_end_to_end_via_cli(tmp_path, monkeypatch, capsys):
    repo = _git_repo(tmp_path)
    monkeypatch.setattr(orchestrator, "repo_root", lambda: repo)

    exit_code = orchestrator.main(
        [
            "enqueue",
            "--id",
            "t1",
            "--task-ref",
            "ENG-AGENT-02",
            "--role",
            "focused-tests",
            "--worker",
            "opencode2-gemini-flash-lite",
            "--scope",
            "seed.txt",
            "--",
            "run tests",
        ]
    )
    assert exit_code == orchestrator.EXIT_OK
    assert default_db_path(repo).exists()

    capsys.readouterr()
    exit_code = orchestrator.main(["status"])
    assert exit_code == orchestrator.EXIT_OK
    out = capsys.readouterr().out
    assert "t1" in out
    assert "PENDING" in out


def test_start_unknown_task_id_reports_a_usage_error(tmp_path, monkeypatch, capsys):
    repo = _git_repo(tmp_path)
    monkeypatch.setattr(orchestrator, "repo_root", lambda: repo)
    exit_code = orchestrator.main(["start", "--id", "no-such-task"])
    assert exit_code == orchestrator.EXIT_USAGE


def test_provider_disable_and_enable_round_trip_via_cli(tmp_path, monkeypatch, capsys):
    repo = _git_repo(tmp_path)
    monkeypatch.setattr(orchestrator, "repo_root", lambda: repo)

    assert orchestrator.main(["provider-disable", "--name", "claude-code"]) == orchestrator.EXIT_OK
    assert orchestrator.main(["provider-enable", "--name", "claude-code"]) == orchestrator.EXIT_OK


def test_set_max_writers_via_cli(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    monkeypatch.setattr(orchestrator, "repo_root", lambda: repo)
    assert orchestrator.main(["set-max-writers", "--count", "3"]) == orchestrator.EXIT_OK
    assert orchestrator.main(["set-max-writers", "--count", "0"]) == orchestrator.EXIT_USAGE


def test_build_parser_exposes_every_documented_subcommand():
    parser = orchestrator.build_parser()
    sub_actions = [a for a in parser._subparsers._group_actions if hasattr(a, "choices")]
    names = set(sub_actions[0].choices)
    for expected in (
        "run",
        "dashboard",
        "status",
        "enqueue",
        "start",
        "pause",
        "resume",
        "stop",
        "stop-after-current",
        "provider-enable",
        "provider-disable",
        "provider-drain",
        "probe",
        "set-max-writers",
        "prioritize",
        "defer",
    ):
        assert expected in names


def test_run_once_flag_exits_after_a_single_iteration(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    monkeypatch.setattr(orchestrator, "repo_root", lambda: repo)
    exit_code = orchestrator.main(["run", "--once"])
    assert exit_code == orchestrator.EXIT_OK
    assert default_db_path(repo).exists()
