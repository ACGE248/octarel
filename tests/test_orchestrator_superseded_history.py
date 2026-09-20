"""Failed/blocked leftovers of finished work must not be presented as current.

Found preparing the first real OctaScene run: the Control Center's Active Work
showed a 57-hour-old BLOCKED review task from an already-accepted run, and
Needs-attention listed eight old failures, every one belonging to work that had
since been accepted. A live task/run is never hidden; only settled leftovers are.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scripts.agents.control_plane.advancement import superseded_ids
from scripts.agents.control_plane.commands import CommandContext
from scripts.agents.control_plane.dashboard_api import create_app
from scripts.agents.control_plane.models import Runbook, Task
from scripts.agents.control_plane.provider_state import seed_provider_states
from scripts.agents.control_plane.scheduler import Scheduler
from scripts.agents.control_plane.state import State
from scripts.agents.control_plane.supervisor import Supervisor
from scripts.agents.registry import load_registry


def rb(rid, ref, status, *, stage="PENDING", created="2026-09-10T00:00:00+00:00", task_id=None) -> Runbook:
    return Runbook(
        id=rid, name=f"run {rid}", preset="overnight-development", objective="x", source_ref=f"{ref} (title)",
        branch="b", worktree="/w", parent_worker="claude-code", max_duration_minutes=60, status=status,
        acceptance_stage=stage, created_at=created, task_id=task_id,
    )


def task(tid, state, ref="X", role="diff-review") -> Task:
    return Task(id=tid, task_ref=ref, role=role, worker="claude-code", state=state)


def test_leftovers_of_an_accepted_run_are_history():
    runs = [rb("RB-1", "V1-07", "SUCCEEDED", stage="DONE")]
    tasks = [task("RB-1-review-aa", "FAILED"), task("RB-1-review-bb", "BLOCKED"), task("RB-1-session", "SUCCEEDED")]
    _runs, old = superseded_ids(runs, tasks)
    assert old == {"RB-1-review-aa", "RB-1-review-bb"}  # succeeded work is untouched


def test_a_failed_attempt_is_history_only_once_a_later_accepted_run_took_over():
    early_fail = rb("RB-a", "V1-08", "BLOCKED", created="2026-09-18T00:00:00+00:00")
    accepted = rb("RB-b", "V1-08", "SUCCEEDED", stage="DONE", created="2026-09-19T00:00:00+00:00")
    tasks = [task("RB-a-review-1", "FAILED")]
    assert superseded_ids([early_fail], tasks) == (set(), set())  # nothing has replaced it yet
    runs, old = superseded_ids([early_fail, accepted], tasks)
    assert runs == {"RB-a"} and old == {"RB-a-review-1"}


def test_an_older_accepted_run_does_not_hide_a_newer_failure():
    accepted = rb("RB-old", "V1-09", "SUCCEEDED", stage="DONE", created="2026-09-10T00:00:00+00:00")
    newer_fail = rb("RB-new", "V1-09", "FAILED", created="2026-09-15T00:00:00+00:00")
    runs, _old = superseded_ids([accepted, newer_fail], [])
    assert "RB-new" not in runs


def test_other_work_items_are_not_affected():
    accepted = rb("RB-b", "V1-08", "SUCCEEDED", stage="DONE", created="2026-09-19T00:00:00+00:00")
    other_fail = rb("RB-c", "V1-09", "FAILED", created="2026-09-01T00:00:00+00:00")
    runs, _ = superseded_ids([accepted, other_fail], [task("RB-c-session", "FAILED")])
    assert "RB-c" not in runs


def test_live_work_is_never_superseded():
    accepted = rb("RB-b", "V1-08", "SUCCEEDED", stage="DONE", created="2026-09-19T00:00:00+00:00")
    running = rb("RB-live", "V1-08", "RUNNING", created="2026-09-01T00:00:00+00:00")
    _, old = superseded_ids([accepted, running], [task("RB-live-session", "RUNNING"), task("RB-b-x", "QUEUED")])
    assert old == set()


@pytest.fixture()
def api(tmp_path: Path):
    registry = load_registry()
    state = State(tmp_path / "s" / "cp.db")
    for provider in seed_provider_states(registry):
        state.upsert_provider_state(provider)
    ctx = CommandContext(
        state=state, registry=registry, scheduler=Scheduler(),
        supervisor=Supervisor(registry=registry, repo_root=tmp_path, state=state), repo_root=tmp_path,
    )
    roadmap = tmp_path / "ROADMAP.md"
    roadmap.write_text("# r\n\n## Program index\n\n| a | b | c | d |\n|---|---|---|---|\n", encoding="utf-8")
    return state, TestClient(create_app(ctx, roadmap_path=roadmap))


def test_api_hides_history_from_attention_workflow_and_counts_but_keeps_the_fact(api):
    state, client = api
    state.upsert_runbook(rb("RB-1", "V1-07", "SUCCEEDED", stage="DONE", task_id="RB-1-session"))
    state.upsert_task(task("RB-1-session", "SUCCEEDED", ref="V1-07", role="primary-implementation"))
    state.upsert_task(task("RB-1-review-aa", "FAILED", ref="RB-1-REVIEW-AA"))
    state.upsert_task(task("RB-1-review-bb", "BLOCKED", ref="RB-1-REVIEW-BB"))

    attention = client.get("/api/attention").json()
    assert attention["tasks"] == [] and attention["runbooks"] == []
    workflow = client.get("/api/workflow").json()
    assert workflow["task"] is None or workflow["task"]["state"] not in {"BLOCKED", "FAILED"}
    counts = client.get("/api/overview").json()["task_counts"]
    assert counts["needs_attention"] == 0
    tasks = {t["id"]: t for t in client.get("/api/tasks").json()}
    assert tasks["RB-1-review-aa"]["superseded"] is True and tasks["RB-1-review-aa"]["projection"] == "HISTORICAL"
    assert tasks["RB-1-session"]["superseded"] is False  # the fact is still there for History


def test_api_still_surfaces_a_real_current_failure(api):
    state, client = api
    state.upsert_runbook(rb("RB-1", "V1-07", "SUCCEEDED", stage="DONE"))
    state.upsert_task(task("RB-1-review-aa", "FAILED"))
    state.upsert_runbook(rb("RB-2", "V1-09", "FAILED", created="2026-09-20T00:00:00+00:00", task_id="RB-2-session"))
    state.upsert_task(task("RB-2-session", "FAILED", ref="V1-09", role="primary-implementation"))
    attention = client.get("/api/attention").json()
    assert [t["id"] for t in attention["tasks"]] == ["RB-2-session"]
    assert [r["id"] for r in attention["runbooks"]] == ["RB-2"]
