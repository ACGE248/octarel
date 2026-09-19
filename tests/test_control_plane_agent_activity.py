"""Focused tests for OCTAREL-UI-01: the read-only Agent Activity evidence API.

These tests cover the exact acceptance criteria called out for this feature:
running output refresh, completed output, failed output, missing/pruned
output, redaction (defense in depth on top of write-time redaction),
distinguishing multiple attempts by run_id, bounded/truncated long output,
and authorization scoping (an unknown task_id must never leak evidence).

No worker is ever launched and no provider/network call is made: every
fixture writes the exact on-disk manifest.json/summary.md/logs/run.log shape
that scripts/agents/manifest.py already produces, and the tests only read it
back through scripts/agents/control_plane/agent_activity.py and the
/api/agent-activity/* routes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scripts.agents.control_plane.agent_activity import (
    UnsafeIdentifier,
    list_attempts,
    read_attempt,
)
from scripts.agents.control_plane.commands import CommandContext
from scripts.agents.control_plane.dashboard_api import create_app
from scripts.agents.control_plane.models import Task
from scripts.agents.control_plane.provider_state import seed_provider_states
from scripts.agents.control_plane.scheduler import Scheduler
from scripts.agents.control_plane.state import State
from scripts.agents.control_plane.supervisor import Supervisor
from scripts.agents.registry import load_registry

TASK_ID = "t1"
WORKER = "opencode2-gemini-flash-lite"


def _write_attempt(
    repo_root: Path,
    *,
    run_id: str,
    result: str = "PASS",
    exit_status: int | None = 0,
    started_at: str = "2026-09-18T10:00:00+00:00",
    finished_at: str | None = "2026-09-18T10:00:05+00:00",
    log_text: str | None = "line one\nline two\n",
    notes: list[str] | None = None,
) -> Path:
    """Write manifest.json/summary.md/logs/run.log exactly as manifest.py does."""

    run_dir = repo_root / ".agent-output" / TASK_ID / WORKER / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict = {
        "manifest_version": 1,
        "task": TASK_ID,
        "role": "focused-tests",
        "worker": WORKER,
        "planned": {
            "execution_system": "cli",
            "provider": "opencode2",
            "model": "gemini-flash-lite",
            "intensity": "standard",
            "why_this_worker": "fixture",
        },
        "requested_command": ["opencode2", "run"],
        "actual": {
            "execution_system": "cli",
            "provider": "opencode2",
            "model": "gemini-flash-lite",
            "intensity": "standard",
        },
        "result": result,
        "exit_status": exit_status,
        "duration_seconds": 5,
        "started_at": started_at,
        "finished_at": finished_at,
        "files_changed": ["scripts/agents/control_plane/agent_activity.py"],
        "tests_or_checks": ["pytest tests/test_control_plane_agent_activity.py"],
        "notes": notes or [],
        "candidate_tree_sha": "deadbeef",
        "policy_manifest": {},
        "paths": {},
        "redaction_applied": True,
        "ci_invocation_allowed": False,
    }

    if log_text is not None:
        logs_dir = run_dir / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        log_path = logs_dir / "run.log"
        log_path.write_text(log_text, encoding="utf-8")
        manifest["paths"]["log"] = str(log_path.relative_to(repo_root))

    manifest_path = run_dir / "manifest.json"
    summary_path = run_dir / "summary.md"
    manifest["paths"]["manifest"] = str(manifest_path.relative_to(repo_root))
    manifest["paths"]["summary"] = str(summary_path.relative_to(repo_root))
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    summary_path.write_text(f"# Delegation summary — {TASK_ID} / {WORKER}\n\nresult: {result}\n", encoding="utf-8")
    return run_dir


@pytest.fixture()
def ctx(tmp_path: Path) -> CommandContext:
    registry = load_registry()
    state = State(":memory:")
    for provider in seed_provider_states(registry):
        state.upsert_provider_state(provider)
    task = Task(
        # Internal dashboard-state id is deliberately DIFFERENT from TASK_ID:
        # .agent-output/<task_ref>/<worker>/<run_id> is keyed by task_ref (see
        # orchestrate.py / supervisor.py, which pass task.task_ref -- not
        # task.id -- into run_delegation), and the /api/agent-activity/*
        # routes authorize by that same task_ref. A test that let id ==
        # task_ref could not catch a regression back to authorizing by id.
        id="internal-row-1",
        task_ref=TASK_ID,
        role="focused-tests",
        worker=WORKER,
        dependencies=(),
    )
    state.upsert_task(task)
    return CommandContext(
        state=state,
        registry=registry,
        scheduler=Scheduler(),
        supervisor=Supervisor(registry=registry, repo_root=tmp_path, state=state),
        repo_root=tmp_path,
    )


@pytest.fixture()
def roadmap_file(tmp_path: Path) -> Path:
    path = tmp_path / "PRODUCT_ROADMAP.md"
    path.write_text("# Roadmap\n", encoding="utf-8")
    return path


@pytest.fixture()
def client(ctx, roadmap_file) -> TestClient:
    app = create_app(ctx, roadmap_path=roadmap_file)
    return TestClient(app)


# --------------------------------------------------------------------------- unit: list_attempts


def test_list_attempts_empty_for_unknown_worker(tmp_path: Path) -> None:
    assert list_attempts(tmp_path, TASK_ID, WORKER) == []


def test_list_attempts_distinguishes_multiple_runs_most_recent_first(tmp_path: Path) -> None:
    _write_attempt(tmp_path, run_id="run-a", started_at="2026-09-18T10:00:00+00:00")
    _write_attempt(tmp_path, run_id="run-b", started_at="2026-09-18T11:00:00+00:00")
    attempts = list_attempts(tmp_path, TASK_ID, WORKER)
    assert [a["run_id"] for a in attempts] == ["run-b", "run-a"]


# --------------------------------------------------------------------------- unit: read_attempt


def test_read_attempt_completed_output_available(tmp_path: Path) -> None:
    _write_attempt(tmp_path, run_id="run-a", result="PASS", log_text="hello\nworld\n")
    attempt = read_attempt(tmp_path, TASK_ID, WORKER, "run-a")
    assert attempt["status"] == "ok"
    assert attempt["output"]["status"] == "available"
    assert "hello" in attempt["output"]["content"]
    assert attempt["details"]["result"] == "PASS"


def test_read_attempt_output_is_available_when_the_repo_root_is_behind_a_symlink(tmp_path: Path) -> None:
    """macOS temp roots (/var -> /private/var) must not turn a real log into a 'missing' output."""

    real = tmp_path / "real-root"
    real.mkdir()
    link = tmp_path / "link-root"
    link.symlink_to(real, target_is_directory=True)
    _write_attempt(link, run_id="run-a", log_text="through the symlink\n")
    attempt = read_attempt(link, TASK_ID, WORKER, "run-a")
    assert attempt["output"]["status"] == "available"
    assert "through the symlink" in attempt["output"]["content"]
    assert attempt["evidence"]["log_relpath"] == f".agent-output/{TASK_ID}/{WORKER}/run-a/logs/run.log"


def test_read_attempt_failed_output_still_inspectable(tmp_path: Path) -> None:
    _write_attempt(tmp_path, run_id="run-a", result="FAIL", exit_status=1, log_text="boom: traceback\n")
    attempt = read_attempt(tmp_path, TASK_ID, WORKER, "run-a")
    assert attempt["details"]["result"] == "FAIL"
    assert attempt["details"]["exit_status"] == 1
    assert attempt["output"]["status"] == "available"
    assert "boom" in attempt["output"]["content"]


def test_read_attempt_running_task_has_no_finished_at_but_output_still_reads(tmp_path: Path) -> None:
    _write_attempt(tmp_path, run_id="run-a", result=None, exit_status=None, finished_at=None, log_text="starting up\n")
    attempt = read_attempt(tmp_path, TASK_ID, WORKER, "run-a")
    assert attempt["details"]["finished_at"] is None
    assert attempt["output"]["status"] == "available"


def test_read_attempt_missing_run_id_is_explicit_not_an_error(tmp_path: Path) -> None:
    attempt = read_attempt(tmp_path, TASK_ID, WORKER, "nonexistent-run")
    assert attempt["status"] == "missing"
    assert attempt["output"]["status"] == "missing"
    assert attempt["details"] is None


def test_read_attempt_pruned_log_reports_missing_output_not_empty_string(tmp_path: Path) -> None:
    """A run whose manifest/summary survive but whose log was pruned/rotated
    away must say so explicitly, never silently show blank output as if the
    attempt produced nothing."""

    _write_attempt(tmp_path, run_id="run-a", log_text=None)
    attempt = read_attempt(tmp_path, TASK_ID, WORKER, "run-a")
    assert attempt["status"] == "ok"
    assert attempt["output"]["status"] == "missing"
    assert attempt["output"]["content"] == ""


def test_read_attempt_redacts_secrets_in_output_as_defense_in_depth(tmp_path: Path) -> None:
    """Evidence on disk is already redacted at write time (manifest.py). This
    checks the read path redacts too, so an older manifest that predates a
    redaction rule -- or a future writer regression -- still never surfaces a
    live-looking secret through this viewer."""

    _write_attempt(tmp_path, run_id="run-a", log_text="Authorization: Bearer sk-ABCDEFGHIJKLMNOPQRSTUVWX\n")
    attempt = read_attempt(tmp_path, TASK_ID, WORKER, "run-a")
    assert "sk-ABCDEFGHIJKLMNOPQRSTUVWX" not in attempt["output"]["content"]
    assert "REDACTED" in attempt["output"]["content"]


def test_read_attempt_redacts_secrets_in_notes(tmp_path: Path) -> None:
    _write_attempt(tmp_path, run_id="run-a", notes=["token=ghp_1234567890ABCDEFGHIJKLMNOPQRSTUVWXYZ"])
    attempt = read_attempt(tmp_path, TASK_ID, WORKER, "run-a")
    assert all("ghp_1234567890" not in note for note in attempt["details"]["notes"])


def test_read_attempt_bounds_long_output_and_marks_truncated(tmp_path: Path) -> None:
    long_text = "x" * 5000 + "\n"
    _write_attempt(tmp_path, run_id="run-a", log_text=long_text)
    attempt = read_attempt(tmp_path, TASK_ID, WORKER, "run-a")
    from scripts.agents.control_plane import agent_activity as mod

    original_cap = mod.MAX_OUTPUT_BYTES
    mod.MAX_OUTPUT_BYTES = 100
    try:
        bounded = read_attempt(tmp_path, TASK_ID, WORKER, "run-a")
    finally:
        mod.MAX_OUTPUT_BYTES = original_cap
    assert bounded["output"]["truncated"] is True
    assert len(bounded["output"]["content"]) <= 100
    # Unbounded (fits under the real cap) read is not marked truncated.
    assert attempt["output"]["truncated"] is False


@pytest.mark.parametrize(
    "task_id,worker,run_id",
    [
        ("../escape", WORKER, "run-a"),
        (TASK_ID, "../../etc", "run-a"),
        (TASK_ID, WORKER, "../../../etc/passwd"),
        (TASK_ID, WORKER, ""),
        (TASK_ID, "worker/with/slash", "run-a"),
    ],
)
def test_path_traversal_attempts_are_rejected(tmp_path: Path, task_id: str, worker: str, run_id: str) -> None:
    with pytest.raises(UnsafeIdentifier):
        read_attempt(tmp_path, task_id, worker, run_id)


# --------------------------------------------------------------------------- HTTP: /api/agent-activity


def test_api_lists_attempts_for_known_task(client: TestClient, ctx: CommandContext) -> None:
    _write_attempt(ctx.repo_root, run_id="run-a")
    resp = client.get(f"/api/agent-activity/{TASK_ID}/{WORKER}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["task"] == TASK_ID
    assert [a["run_id"] for a in body["attempts"]] == ["run-a"]


def test_api_reads_one_attempt(client: TestClient, ctx: CommandContext) -> None:
    _write_attempt(ctx.repo_root, run_id="run-a", log_text="ok\n")
    resp = client.get(f"/api/agent-activity/{TASK_ID}/{WORKER}/run-a")
    assert resp.status_code == 200, resp.text
    assert resp.json()["output"]["status"] == "available"


def test_api_rejects_unknown_task_id(client: TestClient) -> None:
    """Authorization scoping: this viewer is task-scoped. A task_id the
    dashboard's own state has never heard of must never be used to fish for
    evidence that happens to live on disk under that id."""

    resp = client.get("/api/agent-activity/never-registered-task/some-worker")
    assert resp.status_code == 404


def test_api_rejects_path_traversal_in_url_segments(client: TestClient) -> None:
    resp = client.get(f"/api/agent-activity/{TASK_ID}/{WORKER}/..%2f..%2f..%2fetc%2fpasswd")
    assert resp.status_code in (400, 404)


def test_api_missing_run_id_for_known_task_returns_missing_status_not_500(
    client: TestClient, ctx: CommandContext
) -> None:
    resp = client.get(f"/api/agent-activity/{TASK_ID}/{WORKER}/never-ran")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "missing"
