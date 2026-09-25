"""ENG-AO-05 (issue #9): continuous overnight project advancement.

Deterministic: every project is a real temporary Git repository with a file ledger, the
Quick Start start path is a counting fake, merge/refresh are injected fakes, and time is
an explicit ``now``. No provider, network or billable call is made.
"""

from __future__ import annotations

import datetime as dt
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from scripts.agents import orchestrator
from scripts.agents.control_plane import overnight as ovn
from scripts.agents.control_plane import project_registry as pr
from scripts.agents.control_plane.commands import CommandContext
from scripts.agents.control_plane.dashboard_api import create_app
from scripts.agents.control_plane.models import (
    RUNBOOK_CANCELLED,
    RUNBOOK_FAILED,
    RUNBOOK_OWNER_ACTION_REQUIRED,
    RUNBOOK_RUNNING,
    RUNBOOK_SUCCEEDED,
    Runbook,
    Task,
)
from scripts.agents.control_plane.scheduler import ConcurrencyPolicy, Scheduler
from scripts.agents.control_plane.state import State
from scripts.agents.control_plane.supervisor import Supervisor
from scripts.agents.control_plane.task_sources import discover_tasks
from scripts.agents.registry import load_registry

LEDGER = "TASKS.md"
T0 = dt.datetime(2026, 1, 1, 0, 0, tzinfo=dt.timezone.utc)


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=Test", *args],
        cwd=str(root), check=True, capture_output=True,
    )


def write_ledger(root: Path, rows: list[tuple[str, str, str]]) -> None:
    lines = ["| ID | status | notes |", "|---|---|---|"]
    lines += [f"| {tid} | {status} | {notes} |" for tid, status, notes in rows]
    (root / LEDGER).write_text("\n".join(lines) + "\n", encoding="utf-8")


def make_project(root: Path, rows) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    (root / "POLICY.md").write_text("# project policy\n", encoding="utf-8")
    write_ledger(root, rows)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    return root


def register(state: State, project_id: str, root: Path, **capabilities) -> None:
    pr.register_project(
        state,
        {
            "project_id": project_id, "display_name": project_id.title(), "local_repo_root": str(root),
            "policy_entrypoints": ["POLICY.md"], "task_sources": [LEDGER],
            "capabilities": {"task_source_adapter": "file_ledger", **capabilities},
        },
    )


class Starter:
    """Counting fake for the normal Quick Start start path (the only launch route)."""

    def __init__(self, state: State) -> None:
        self.state = state
        self.calls: list[str] = []

    def __call__(self, key, project) -> Runbook:
        first = next(t for t in discover_tasks(project) if t.eligible)
        self.calls.append(first.task_id)
        runbook = Runbook(
            id=f"rb-{len(self.calls)}-{first.task_id.lower()}", name=f"Continue {project.project_id} — {first.task_id}",
            preset="overnight-development", objective="x", source_ref=f"{LEDGER} {first.task_id}",
            branch=f"{first.task_id.lower()}-work", worktree=f"/tmp/never-used-{first.task_id}",
            parent_worker="claude-code", max_duration_minutes=60, status=RUNBOOK_RUNNING, project_id=project.project_id,
        )
        self.state.upsert_runbook(runbook)
        return runbook


class FakeOps:
    """Injected refresh/merge seams. ``on_merge`` models the merged repository truth changing."""

    def __init__(self, on_merge=None) -> None:
        self.refreshes = 0
        self.merges: list[str] = []
        self.merged: set[str] = set()
        self.merge_ok = True
        self.on_merge = on_merge

    def refresh_truth(self, project):
        self.refreshes += 1
        return {"head_sha": "abc123def456"}

    def merge_state(self, project, runbook):
        return "merged" if runbook.id in self.merged else "open"

    def merge(self, state, project, runbook):
        self.merges.append(runbook.id)
        if not self.merge_ok:
            return False, "merge conflict against origin/main"
        self.merged.add(runbook.id)
        if self.on_merge:
            self.on_merge(runbook)
        return True, "merged"

    def ops(self) -> ovn.OvernightOps:
        return ovn.OvernightOps(refresh_truth=self.refresh_truth, merge_state=self.merge_state, merge=self.merge)


def accept(state: State, run_id: str) -> Runbook:
    """What a finished acceptance pipeline leaves behind (stage DONE, no FAIL evidence)."""

    rb = state.get_runbook(run_id)
    rb.status = RUNBOOK_SUCCEEDED
    rb.acceptance_stage = "DONE"
    rb.acceptance_evidence = {
        "test": {"status": "PASS"}, "review": {"status": "PASS"},
        "pr_readiness": {"status": "PASS", "pr_number": 7, "head_sha": "deadbeef"},
    }
    state.upsert_runbook(rb)
    return rb


@pytest.fixture()
def state(tmp_path: Path) -> State:
    return State(tmp_path / "state" / "cp.db")


@pytest.fixture()
def alpha(tmp_path: Path) -> Path:
    return make_project(
        tmp_path / "alpha", [("A-01", "pending", "first"), ("A-02", "pending", "second"), ("A-03", "pending", "third")]
    )


class Harness:
    def __init__(self, state: State, **kw) -> None:
        self.state = state
        self.starter = Starter(state)
        self.fake = FakeOps(on_merge=kw.pop("on_merge", None))
        self.registry = kw.pop("registry", None)
        self.supervisor = kw.pop("supervisor", None)

    def tick(self, when: dt.datetime = T0) -> None:
        ovn.tick(
            state=self.state, registry=self.registry, ops=self.fake.ops(), starter=self.starter, now=when,
            supervisor=self.supervisor,
        )

    def session(self, sid: str) -> dict:
        return self.state.get_overnight_session(sid)


def start(state, project="alpha", *, duration="10h", **kw) -> dict:
    return ovn.create_session(state, project_id=project, duration=duration, now=T0, **kw)


def mark_complete(root: Path, done: list[str], rest: list[str]) -> None:
    write_ledger(root, [(t, "complete", "done") for t in done] + [(t, "pending", "todo") for t in rest])


def events(state: State) -> list[str]:
    return [e.message for e in reversed(state.list_events(limit=500)) if e.category == "overnight"]


# 1 -------------------------------------------------------- create / persisted bounds


def test_create_persists_bounds_and_records_events(state, alpha):
    register(state, "alpha", alpha)
    session = start(state, duration="10h", max_tasks=3)

    stored = state.get_overnight_session(session["session_id"])
    assert stored["state"] == ovn.SESSION_ACTIVE
    assert stored["project_id"] == "alpha"
    assert stored["duration_seconds"] == 10 * 3600
    assert stored["started_at"] == "2026-01-01T00:00:00+00:00"
    assert stored["deadline_at"] == "2026-01-01T10:00:00+00:00"
    assert stored["max_tasks"] == 3
    assert stored["accepted_count"] == 0 and stored["current_runbook_id"] is None and stored["last_accepted"] is None
    assert stored["merge_authorized"] is False
    text = "\n".join(events(state))
    assert "session created" in text and "session started" in text


def test_create_validation_fails_closed(state, alpha):
    register(state, "alpha", alpha)
    with pytest.raises(ovn.OvernightError):
        ovn.create_session(state, project_id="nope", duration="1h")
    for bad in ("soon", "0h", "-1h", "100h"):
        with pytest.raises(ovn.OvernightError):
            ovn.create_session(state, project_id="alpha", duration=bad)
    with pytest.raises(ovn.OvernightError):
        ovn.create_session(state, project_id="alpha", duration="1h", max_tasks=0)
    start(state)
    with pytest.raises(ovn.OvernightError, match="already has a live"):
        start(state)


def test_parse_duration_forms():
    assert ovn.parse_duration("6h") == 6 * 3600
    assert ovn.parse_duration("2h30m") == 9000
    assert ovn.parse_duration("90m") == 5400
    assert ovn.parse_duration(15) == 15 * 3600
    for bad in ("10h10h", "10h junk", "h", ""):
        with pytest.raises(ovn.OvernightError):
            ovn.parse_duration(bad)


def test_merge_authorization_requires_operator_flag_and_project_opt_in(state, alpha):
    register(state, "alpha", alpha)
    with pytest.raises(ovn.OvernightError, match="opt in"):
        start(state, merge_authorized=True)
    pr.update_project(state, "alpha", {"capabilities": {"task_source_adapter": "file_ledger", "overnight_merge": "true"}})
    session = start(state, merge_authorized=True)
    assert session["merge_authorized"] is True and session["merge_authorized_at"]


# 2 ------------------------------------------------ current truth, one writer, restart


def test_first_task_is_resolved_from_current_repository_truth_not_a_stored_list(state, alpha):
    register(state, "alpha", alpha)
    sid = start(state)["session_id"]
    write_ledger(alpha, [("A-01", "complete", ""), ("A-02", "pending", "now first"), ("A-03", "pending", "")])
    h = Harness(state)

    h.tick()

    assert h.starter.calls == ["A-02"]  # ledger edited after session creation: current truth wins
    stored = h.session(sid)
    assert stored["current_task"]["task_id"] == "A-02" and stored["current_runbook_id"] == "rb-1-a-02"
    assert not any(isinstance(v, list) and v and isinstance(v[0], dict) and "task_id" in v[0] for v in stored.values())
    text = "\n".join(events(state))
    for expected in ("repository truth refreshed", "task selected: A-02", "runbook started", "provider selected by normal routing"):
        assert expected in text


def test_only_one_write_task_at_a_time_and_restart_never_duplicates(state, alpha, tmp_path):
    register(state, "alpha", alpha)
    sid = start(state)["session_id"]
    h = Harness(state)
    h.tick()
    h.tick(T0 + dt.timedelta(minutes=5))
    h.tick(T0 + dt.timedelta(minutes=10))
    assert h.starter.calls == ["A-01"]

    # Daemon restart: a fresh State/handle over the same durable DB, live worker still owned.
    reopened = State(state.db_path)
    assert ovn.recover_on_restart(reopened) == 1
    h2 = Harness(reopened)
    h2.tick(T0 + dt.timedelta(hours=1))
    assert h2.starter.calls == []  # no duplicate implementation
    stored = reopened.get_overnight_session(sid)
    assert stored["restart_count"] == 1 and stored["current_runbook_id"] == "rb-1-a-01"
    assert any("daemon restart" in m for m in events(reopened))


def test_foreign_active_runbook_in_project_blocks_start(state, alpha):
    register(state, "alpha", alpha)
    other = Runbook(
        id="rb-manual", name="manual", preset="overnight-development", objective="x", source_ref="manual",
        branch="m", worktree="/tmp/m", parent_worker="claude-code", max_duration_minutes=5,
        status=RUNBOOK_RUNNING, project_id="alpha",
    )
    state.upsert_runbook(other)
    sid = start(state)["session_id"]
    h = Harness(state)
    h.tick()
    stored = h.session(sid)
    assert stored["state"] == ovn.SESSION_STOPPED and stored["stop_kind"] == ovn.KIND_WRITER_CONFLICT
    assert h.starter.calls == []


# 3 --------------------------------------- accepted -> merge -> fresh truth -> next


def test_accept_merge_refresh_then_new_repository_truth_picks_the_next_task(state, alpha):
    register(state, "alpha", alpha, overnight_merge="true")
    sid = start(state, merge_authorized=True)["session_id"]

    def merged_truth_changes(_rb):  # after A-01 merges, the project re-prioritises: A-09 is now first
        write_ledger(alpha, [("A-01", "complete", ""), ("A-09", "pending", "urgent new"), ("A-02", "pending", "")])

    h = Harness(state, on_merge=merged_truth_changes)
    h.tick()
    assert h.starter.calls == ["A-01"]
    accept(state, "rb-1-a-01")
    refreshes_before = h.fake.refreshes
    h.tick(T0 + dt.timedelta(hours=1))

    assert h.fake.merges == ["rb-1-a-01"]
    assert h.starter.calls == ["A-01", "A-09"]  # fresh truth won over the original ordering
    assert h.fake.refreshes > refreshes_before
    stored = h.session(sid)
    assert stored["accepted_count"] == 1 and stored["last_accepted"]["merged"] is True
    assert stored["last_accepted"]["task_id"] == "A-01"
    assert stored["current_task"]["task_id"] == "A-09" and stored["state"] == ovn.SESSION_ACTIVE
    text = "\n".join(events(state))
    assert "accepted via rb-1-a-01" in text and "merge/reconciliation of rb-1-a-01 verified" in text


def test_overnight_successor_advancement_defers_to_another_runbook_owner_then_recovers(state, alpha, tmp_path):
    """ENG-AO-07: the overnight path shares the per-runbook single-writer lease with reconcile."""

    import subprocess
    import sys

    register(state, "alpha", alpha, overnight_merge="true")
    start(state, merge_authorized=True)
    h = Harness(state)
    h.tick()
    accept(state, "rb-1-a-01")
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import sys,time\n"
         "from scripts.agents.control_plane.advancement_lease import advancement_lease\n"
         "from scripts.agents.control_plane.state import State\n"
         "with advancement_lease(State(sys.argv[1]), sys.argv[2]) as o:\n"
         "    assert o; print('READY', flush=True); time.sleep(600)\n",
         str(state.db_path), "rb-1-a-01"],
        cwd=str(Path(__file__).resolve().parents[1]), stdout=subprocess.PIPE, text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "READY"
        h.tick(T0 + dt.timedelta(hours=1))
        assert h.starter.calls == ["A-01"]  # withheld: not started while another process owns rb-1-a-01
        assert any("withheld" in line for line in events(state))
    finally:
        holder.kill()
        holder.wait()
    write_ledger(alpha, [("A-01", "complete", ""), ("A-02", "pending", ""), ("A-03", "pending", "")])
    h.tick(T0 + dt.timedelta(hours=2))  # owner gone (crashed): the next tick advances normally
    assert h.starter.calls == ["A-01", "A-02"]


def test_no_eligible_task_completes_cleanly(state, alpha):
    register(state, "alpha", alpha)
    write_ledger(alpha, [("A-01", "complete", ""), ("A-02", "complete", "")])
    sid = start(state)["session_id"]
    h = Harness(state)
    h.tick()
    stored = h.session(sid)
    assert stored["state"] == ovn.SESSION_COMPLETE and stored["stop_kind"] == ovn.KIND_NO_ELIGIBLE
    assert h.starter.calls == []


def test_owner_decision_required_stops_and_is_resumable(state, alpha):
    register(state, "alpha", alpha)
    write_ledger(alpha, [("A-01", "owner-decision", "pick a vendor"), ("A-02", "pending", "")])
    sid = start(state)["session_id"]
    h = Harness(state)
    h.tick()
    stored = h.session(sid)
    assert stored["state"] == ovn.SESSION_STOPPED and stored["stop_kind"] == ovn.KIND_OWNER_ACTION
    assert stored["resumable"] is True and h.starter.calls == []
    write_ledger(alpha, [("A-01", "complete", "decided"), ("A-02", "pending", "")])
    ovn.resume_session(state, sid, now=T0)
    h.tick()
    assert h.starter.calls == ["A-02"] and h.session(sid)["resume_count"] == 1


def test_unmerged_accepted_task_without_authorization_stops_at_owner_action_then_resumes(state, alpha):
    register(state, "alpha", alpha)
    sid = start(state)["session_id"]
    h = Harness(state)
    h.tick()
    accept(state, "rb-1-a-01")
    h.tick(T0 + dt.timedelta(hours=1))

    stored = h.session(sid)
    assert h.fake.merges == []  # never merges without explicit authorization
    assert stored["state"] == ovn.SESSION_STOPPED and stored["stop_kind"] == ovn.KIND_OWNER_ACTION
    assert "not authorised to merge" in stored["stop_reason"]
    assert stored["accepted_count"] == 1 and h.starter.calls == ["A-01"]

    # The owner merges the PR; repository truth now shows it and the ledger is reconciled.
    h.fake.merged.add("rb-1-a-01")
    mark_complete(alpha, ["A-01"], ["A-02", "A-03"])
    ovn.resume_session(state, sid, now=T0 + dt.timedelta(hours=2))
    h.tick(T0 + dt.timedelta(hours=2))
    assert h.starter.calls == ["A-01", "A-02"]
    assert h.session(sid)["accepted_count"] == 1  # not double counted


def test_authorized_merge_that_is_blocked_stops_without_next_task(state, alpha):
    register(state, "alpha", alpha, overnight_merge="true")
    sid = start(state, merge_authorized=True)["session_id"]
    h = Harness(state)
    h.fake.merge_ok = False
    h.tick()
    accept(state, "rb-1-a-01")
    h.tick(T0 + dt.timedelta(hours=1))
    stored = h.session(sid)
    assert stored["state"] == ovn.SESSION_STOPPED and stored["stop_kind"] == ovn.KIND_MERGE_BLOCKED
    assert "conflict" in stored["stop_reason"] and h.starter.calls == ["A-01"]


def test_merge_authorization_is_dropped_if_project_revokes_opt_in(state, alpha):
    register(state, "alpha", alpha, overnight_merge="true")
    sid = start(state, merge_authorized=True)["session_id"]
    h = Harness(state)
    h.tick()
    accept(state, "rb-1-a-01")
    pr.update_project(state, "alpha", {"capabilities": {"task_source_adapter": "file_ledger"}})
    h.tick(T0 + dt.timedelta(hours=1))
    assert h.fake.merges == [] and h.session(sid)["stop_kind"] == ovn.KIND_POLICY


# 4 --------------------------------------------------------- acceptance not weakened


def test_only_fully_accepted_runbooks_count_review_and_gate_are_never_skipped(state, alpha):
    register(state, "alpha", alpha)
    sid = start(state)["session_id"]
    h = Harness(state)
    h.tick()
    rb = state.get_runbook("rb-1-a-01")
    rb.status = RUNBOOK_SUCCEEDED
    rb.acceptance_stage = "review"  # review/exact-tree stages did not finish
    rb.acceptance_evidence = {"test": {"status": "PASS"}}
    state.upsert_runbook(rb)
    h.tick(T0 + dt.timedelta(hours=1))
    stored = h.session(sid)
    assert stored["state"] == ovn.SESSION_STOPPED and stored["stop_kind"] == ovn.KIND_BLOCKING_FAILURE
    assert stored["accepted_count"] == 0 and h.fake.merges == [] and h.starter.calls == ["A-01"]


@pytest.mark.parametrize(
    "status,kind",
    [
        (RUNBOOK_OWNER_ACTION_REQUIRED, ovn.KIND_OWNER_ACTION),
        (RUNBOOK_FAILED, ovn.KIND_BLOCKING_FAILURE),
        (RUNBOOK_CANCELLED, ovn.KIND_OPERATOR_CANCEL),
    ],
)
def test_non_accepted_terminal_runbook_stops_the_session_without_retry(state, alpha, status, kind):
    register(state, "alpha", alpha)
    sid = start(state)["session_id"]
    h = Harness(state)
    h.tick()
    rb = state.get_runbook("rb-1-a-01")
    rb.status = status
    state.upsert_runbook(rb)
    h.tick(T0 + dt.timedelta(hours=1))
    h.tick(T0 + dt.timedelta(hours=2))
    stored = h.session(sid)
    assert stored["state"] == ovn.SESSION_STOPPED and stored["stop_kind"] == kind
    assert h.starter.calls == ["A-01"]  # no second launch, no retry loop


# 5 ------------------------------------------------------------------ bounds


def test_deadline_before_next_task_launches_nothing(state, alpha):
    register(state, "alpha", alpha)
    sid = start(state, duration="1h")["session_id"]
    h = Harness(state)
    h.tick(T0 + dt.timedelta(hours=2))
    stored = h.session(sid)
    assert stored["state"] == ovn.SESSION_COMPLETE and stored["stop_kind"] == ovn.KIND_DEADLINE
    assert h.starter.calls == []


def test_deadline_during_active_task_is_a_safe_checkpoint_not_a_kill(state, alpha):
    register(state, "alpha", alpha)
    sid = start(state, duration="1h")["session_id"]
    h = Harness(state)
    h.tick()
    h.tick(T0 + dt.timedelta(hours=2))  # clock expires mid-task

    stored = h.session(sid)
    assert stored["state"] == ovn.SESSION_STOPPING and stored["stop_requested"] == "deadline"
    assert state.get_runbook("rb-1-a-01").status == RUNBOOK_RUNNING  # worker untouched
    assert any("deadline reached during rb-1-a-01" in m for m in events(state))

    accept(state, "rb-1-a-01")
    h.fake.merged.add("rb-1-a-01")
    mark_complete(alpha, ["A-01"], ["A-02"])
    h.tick(T0 + dt.timedelta(hours=3))
    stored = h.session(sid)
    assert stored["state"] == ovn.SESSION_COMPLETE and stored["stop_kind"] == ovn.KIND_DEADLINE
    assert stored["accepted_count"] == 1 and h.starter.calls == ["A-01"]


def test_task_limit_stops_after_the_limit_even_with_work_remaining(state, alpha):
    register(state, "alpha", alpha, overnight_merge="true")
    sid = start(state, max_tasks=1, merge_authorized=True)["session_id"]
    h = Harness(state, on_merge=lambda _rb: mark_complete(alpha, ["A-01"], ["A-02", "A-03"]))
    h.tick()
    accept(state, "rb-1-a-01")
    h.tick(T0 + dt.timedelta(hours=1))
    stored = h.session(sid)
    assert stored["state"] == ovn.SESSION_COMPLETE and stored["stop_kind"] == ovn.KIND_TASK_LIMIT
    assert h.starter.calls == ["A-01"] and h.fake.merges == ["rb-1-a-01"]  # the accepted task was still merged


def test_task_limit_with_unmerged_task_completes_and_records_pending_merge(state, alpha):
    register(state, "alpha", alpha)
    sid = start(state, max_tasks=1)["session_id"]
    h = Harness(state)
    h.tick()
    accept(state, "rb-1-a-01")
    h.tick(T0 + dt.timedelta(hours=1))
    stored = h.session(sid)
    assert stored["state"] == ovn.SESSION_COMPLETE and stored["stop_kind"] == ovn.KIND_TASK_LIMIT
    assert stored["last_accepted"]["pending_merge"] is True and "not authorised to merge" in stored["stop_reason"]


# 6 -------------------------------------------------------- pause / stop controls


def test_pause_holds_advancement_and_resume_continues(state, alpha):
    register(state, "alpha", alpha, overnight_merge="true")
    sid = start(state, merge_authorized=True)["session_id"]
    h = Harness(state, on_merge=lambda _rb: mark_complete(alpha, ["A-01"], ["A-02"]))
    h.tick()
    ovn.pause_session(state, sid)
    accept(state, "rb-1-a-01")
    h.tick(T0 + dt.timedelta(hours=1))
    stored = h.session(sid)
    assert stored["state"] == ovn.SESSION_PAUSED and stored["accepted_count"] == 0
    assert h.starter.calls == ["A-01"] and h.fake.merges == []
    ovn.resume_session(state, sid, now=T0)
    h.tick(T0 + dt.timedelta(hours=1))
    assert h.session(sid)["accepted_count"] == 1 and h.starter.calls == ["A-01", "A-02"]
    with pytest.raises(ovn.OvernightError):
        ovn.resume_session(state, sid)  # ACTIVE cannot be "resumed"


def test_stop_after_current_finishes_the_task_then_stops_without_another(state, alpha):
    register(state, "alpha", alpha, overnight_merge="true")
    sid = start(state, merge_authorized=True)["session_id"]
    h = Harness(state, on_merge=lambda _rb: mark_complete(alpha, ["A-01"], ["A-02"]))
    h.tick()
    ovn.stop_after_current(state, sid)
    h.tick(T0 + dt.timedelta(minutes=30))
    assert h.session(sid)["state"] == ovn.SESSION_STOPPING and state.get_runbook("rb-1-a-01").status == RUNBOOK_RUNNING
    accept(state, "rb-1-a-01")
    h.tick(T0 + dt.timedelta(hours=1))
    stored = h.session(sid)
    assert stored["state"] == ovn.SESSION_STOPPED and stored["stop_kind"] == ovn.KIND_OPERATOR_STOP
    assert stored["accepted_count"] == 1 and h.starter.calls == ["A-01"]


def test_stop_now_terminates_owned_task_and_cancels_through_existing_runbook_stop(state, alpha, monkeypatch):
    register(state, "alpha", alpha)
    sid = start(state)["session_id"]
    h = Harness(state)
    h.tick()
    rb = state.get_runbook("rb-1-a-01")
    rb.task_id = "task-1"
    state.upsert_runbook(rb)
    terminated, stopped = [], []
    from scripts.agents.control_plane import runbooks as runbooks_module

    monkeypatch.setattr(runbooks_module, "stop_runbook", lambda **kw: stopped.append(kw["runbook_id"]))
    ovn.stop_now(state, sid, supervisor=SimpleNamespace(terminate_task=terminated.append))
    stored = h.session(sid)
    assert stored["state"] == ovn.SESSION_STOPPED and stored["stop_kind"] == ovn.KIND_OPERATOR_STOP
    assert terminated == ["task-1"] and stopped == ["rb-1-a-01"]
    h.tick(T0 + dt.timedelta(hours=1))
    assert h.starter.calls == ["A-01"]


def test_stale_daemon_copy_never_overwrites_operator_control_or_a_final_stop(state, alpha):
    register(state, "alpha", alpha)
    sid = start(state)["session_id"]
    h = Harness(state)
    h.tick()
    stale = state.get_overnight_session(sid)  # what a daemon mid-tick still holds
    ovn.pause_session(state, sid)
    stale["accepted_runbook_ids"] = ["rb-x"]
    ovn._save(state, stale)
    stored = h.session(sid)
    assert stored["state"] == ovn.SESSION_PAUSED  # operator pause survives
    assert stored["accepted_runbook_ids"] == ["rb-x"]  # daemon-owned progress still merged
    assert stored["current_runbook_id"] == "rb-1-a-01"  # and nothing operator-side dropped it

    ovn.resume_session(state, sid)
    ovn.stop_after_current(state, sid)
    stale2 = state.get_overnight_session(sid)
    ovn._finish(state, stale2, ovn.SESSION_COMPLETE, ovn.KIND_NO_ELIGIBLE, "done")
    late = dict(stored, state=ovn.SESSION_ACTIVE, current_runbook_id="rb-zzz")
    ovn._save(state, late)
    assert h.session(sid)["state"] == ovn.SESSION_COMPLETE and h.session(sid)["current_runbook_id"] == "rb-1-a-01"


def test_global_stop_after_current_withholds_launch(state, alpha):
    register(state, "alpha", alpha)
    sid = start(state)["session_id"]
    starter = Starter(state)
    ovn.tick(state=state, ops=FakeOps().ops(), starter=starter, now=T0, allow_launch=False)
    assert starter.calls == [] and state.get_overnight_session(sid)["current_runbook_id"] is None


# 7 ------------------------------------------------------- fail closed on project


def test_disabled_removed_or_changed_project_fails_closed(state, alpha, tmp_path):
    register(state, "alpha", alpha)
    sid = start(state)["session_id"]
    h = Harness(state)
    h.tick()
    pr.update_project(state, "alpha", {"github_remote": "someone-else/other"})
    h.tick(T0 + dt.timedelta(minutes=1))
    stored = h.session(sid)
    assert stored["state"] == ovn.SESSION_STOPPED and stored["stop_kind"] == ovn.KIND_PROJECT
    assert "changed" in stored["stop_reason"]

    beta = make_project(tmp_path / "beta", [("B-01", "pending", "")])
    register(state, "beta", beta)
    sid2 = start(state, "beta")["session_id"]
    pr.set_project_enabled(state, "beta", False)
    h.tick(T0 + dt.timedelta(minutes=2))
    assert h.session(sid2)["stop_kind"] == ovn.KIND_PROJECT
    assert h.starter.calls == ["A-01"]


def test_missing_policy_file_blocks_advancement(state, alpha):
    register(state, "alpha", alpha)
    sid = start(state)["session_id"]
    (alpha / "POLICY.md").unlink()
    h = Harness(state)
    h.tick()
    assert h.session(sid)["stop_kind"] == ovn.KIND_POLICY and h.starter.calls == []


def test_unexpected_error_fails_closed(state, alpha):
    register(state, "alpha", alpha)
    sid = start(state)["session_id"]
    h = Harness(state)
    h.fake.refresh_truth = lambda project: (_ for _ in ()).throw(RuntimeError("boom"))
    h.tick()
    stored = h.session(sid)
    assert stored["state"] == ovn.SESSION_STOPPED and stored["stop_kind"] == ovn.KIND_INTERNAL


# 8 -------------------------------------------------------- provider / no paid fallback


def _registry(*, cost_class="optional-overflow", api=True):
    worker = SimpleNamespace(enabled=True, allow_api_billing=api, cost_class=cost_class, provider="paid")
    return SimpleNamespace(route=lambda role: ["paid-only"], workers={"paid-only": worker})


def test_only_paid_or_api_route_stops_the_session_without_starting(state, alpha):
    register(state, "alpha", alpha)
    sid = start(state)["session_id"]
    h = Harness(state, registry=_registry())
    h.tick()
    stored = h.session(sid)
    assert stored["state"] == ovn.SESSION_STOPPED and stored["stop_kind"] == ovn.KIND_PROVIDER
    assert "never used" in stored["stop_reason"] and h.starter.calls == []


def test_unavailable_provider_stops_instead_of_looping(state, alpha):
    register(state, "alpha", alpha)
    sid = start(state)["session_id"]
    worker = SimpleNamespace(enabled=False, allow_api_billing=False, cost_class="subscription", provider="p")
    reg = SimpleNamespace(route=lambda role: ["w"], workers={"w": worker})
    h = Harness(state, registry=reg)
    h.tick()
    h.tick(T0 + dt.timedelta(minutes=1))
    assert h.session(sid)["stop_kind"] == ovn.KIND_PROVIDER and h.starter.calls == []


def test_overnight_module_delegates_provider_choice_to_normal_routing():
    """#5-#8 (Graphify, Grok bots, free OpenCode fallback, native model freshness) live in the normal
    runbook/routing path. The session must not name providers, models or billing switches."""

    source = Path(ovn.__file__).read_text(encoding="utf-8").lower()
    for token in ("claude", "grok", "codex", "opencode", "gemini", "graphify", "allow_api_billing = true", "api_key"):
        assert token not in source.replace("allow_api_billing", ""), token
    assert "_route_gate" not in source and "registry.route" not in source


def test_default_starter_is_the_normal_quickstart_path_with_no_provider_argument(state, alpha, monkeypatch):
    register(state, "alpha", alpha)
    captured: dict = {}

    def fake_quickstart(**kwargs):
        captured.update(kwargs)
        return Starter(state)(kwargs["key"], kwargs["project"])

    from scripts.agents.control_plane import quickstart

    monkeypatch.setattr(quickstart, "start_quickstart_option", fake_quickstart)
    sid = start(state)["session_id"]
    sup, sched = object(), Scheduler()
    good = SimpleNamespace(
        route=lambda role: ["w"],
        workers={"w": SimpleNamespace(enabled=True, allow_api_billing=False, cost_class="subscription", provider="p")},
    )
    ovn.tick(state=state, registry=good, supervisor=sup, scheduler=sched, ops=FakeOps().ops(), now=T0)
    assert captured["key"] == "continue-next-task" and captured["supervisor"] is sup and captured["scheduler"] is sched
    assert not {"worker", "provider", "model"} & set(captured)
    assert state.get_overnight_session(sid)["current_runbook_id"]


# 9 ---------------------------------------------------- scheduler / generic advance


def test_session_never_touches_scheduler_caps_and_owns_generic_advancement(state, alpha):
    register(state, "alpha", alpha, auto_advance="true")
    sid = start(state)["session_id"]
    policy = ConcurrencyPolicy()
    scheduler = Scheduler(policy)
    h = Harness(state)
    h.tick()
    assert scheduler.policy == ConcurrencyPolicy() and scheduler.policy.max_write_workers == policy.max_write_workers
    rb = state.get_runbook("rb-1-a-01")
    assert ovn.owns_runbook(state, rb)  # generic auto_advance is suppressed for this run
    ovn._finish(state, state.get_overnight_session(sid), ovn.SESSION_STOPPED, ovn.KIND_OPERATOR_STOP, "test")
    assert not ovn.owns_runbook(state, state.get_runbook("rb-1-a-01"))


def test_restart_after_accepted_task_refreshes_truth_before_continuing(state, alpha):
    register(state, "alpha", alpha, overnight_merge="true")
    sid = start(state, merge_authorized=True)["session_id"]
    h = Harness(state)
    h.tick()
    accept(state, "rb-1-a-01")
    h.fake.merged.add("rb-1-a-01")  # merged, then the daemon died before the session noticed
    mark_complete(alpha, ["A-01"], ["A-02"])

    reopened = State(state.db_path)
    ovn.recover_on_restart(reopened)
    h2 = Harness(reopened)
    h2.tick(T0 + dt.timedelta(hours=3))
    assert h2.fake.refreshes >= 1 and h2.starter.calls == ["A-02"]
    assert reopened.get_overnight_session(sid)["accepted_count"] == 1


# 10 ---------------------------------------------------- API / CLI / commands


@pytest.fixture()
def api(tmp_path, alpha):
    registry = load_registry()
    st = State(tmp_path / "api" / "cp.db")
    ctx = CommandContext(
        state=st, registry=registry, scheduler=Scheduler(),
        supervisor=Supervisor(registry=registry, repo_root=alpha, state=st), repo_root=alpha,
    )
    register(st, "alpha", alpha)
    pr.select_project(st, "alpha")
    return ctx, TestClient(create_app(ctx))


def test_api_start_status_pause_resume_and_confirmed_stop(api):
    ctx, client = api
    started = client.post("/api/commands/overnight_start", json={"duration": "6h", "max_tasks": 2}).json()
    assert started["ok"] and "daemon" in started["message"]
    sid = started["data"]["session_id"]
    assert started["data"]["max_tasks"] == 2 and started["data"]["time_remaining_seconds"] > 0

    view = client.get("/api/overnight").json()
    assert view["project_id"] == "alpha" and view["current"]["session_id"] == sid and view["current"]["state"] == "ACTIVE"
    assert view["current"]["accepted_count"] == 0 and view["current"]["deadline_at"]

    assert client.post("/api/commands/overnight_pause", json={}).json()["data"]["state"] == "PAUSED"
    assert client.post("/api/commands/overnight_resume", json={}).json()["data"]["state"] == "ACTIVE"
    assert client.post("/api/commands/overnight_stop_after_current", json={}).json()["data"]["state"] == "STOPPING"
    assert client.post("/api/commands/overnight_stop", json={}).status_code == 409  # destructive: needs confirm
    stopped = client.post("/api/commands/overnight_stop", json={"confirm": True}).json()
    assert stopped["data"]["state"] == "STOPPED"
    assert client.post("/api/commands/overnight_start", json={"duration": "bogus"}).status_code == 400


def test_api_does_not_advance_anything_the_daemon_does(api):
    ctx, client = api
    client.post("/api/commands/overnight_start", json={"duration": "6h"})
    client.get("/api/overnight")
    assert ctx.state.list_runbooks() == [] and ctx.state.list_tasks() == []


def test_cli_overnight_parser_and_command_defaults():
    parser = orchestrator.build_parser()
    args = parser.parse_args(["overnight", "start", "alpha", "--duration", "10h", "--max-tasks", "5", "--authorize-merge"])
    assert (args.project, args.duration, args.max_tasks, args.authorize_merge) == ("alpha", "10h", 5, True)
    assert parser.parse_args(["overnight", "stop-after-current"]).overnight_cmd == "stop-after-current"
    assert parser.parse_args(["overnight", "start", "--duration", "1h"]).authorize_merge is False


def test_cli_start_uses_selected_project_default(tmp_path, alpha, monkeypatch, capsys):
    monkeypatch.setenv("OCTAREL_STATE_DIR", str(tmp_path / "cli-state"))
    monkeypatch.setenv("OCTAGES_ORCH_CANONICAL_REPO_ROOT", str(alpha))
    ctx = orchestrator._build_context(alpha, state_root=orchestrator._standalone_state_root())
    register(ctx.state, "alpha", alpha)
    pr.select_project(ctx.state, "alpha")
    rc = orchestrator.main(["--canonical-repo-root", str(alpha), "overnight", "start", "--duration", "2h", "--max-tasks", "1"])
    assert rc == 0 and "started for alpha" in capsys.readouterr().out
    rc = orchestrator.main(["--canonical-repo-root", str(alpha), "overnight", "status"])
    assert rc == 0 and '"max_tasks": 1' in capsys.readouterr().out  # defaults to the selected project


# 11 ------------------------------------------- stale truth / interrupted start


def test_refresh_failure_stops_instead_of_advancing_from_stale_truth(state, alpha):
    register(state, "alpha", alpha)
    sid = start(state)["session_id"]
    h = Harness(state)
    h.fake.refresh_truth = lambda project: {"ok": False, "note": "git fetch origin failed"}
    h.tick()
    stored = h.session(sid)
    assert stored["state"] == ovn.SESSION_STOPPED and stored["stop_kind"] == ovn.KIND_STALE_TRUTH
    assert "stale repository truth" in stored["stop_reason"] and h.starter.calls == []


def _draft(state, run_id="rb-draft") -> Runbook:
    rb = Runbook(
        id=run_id, name="draft", preset="overnight-development", objective="x", source_ref="TASKS.md A-01",
        branch="a-01", worktree="/tmp/d", parent_worker="claude-code", max_duration_minutes=5, project_id="alpha",
    )
    state.upsert_runbook(rb)
    return rb


def test_draft_left_by_an_interrupted_start_blocks_a_duplicate_launch(state, alpha):
    register(state, "alpha", alpha)
    sid = start(state)["session_id"]
    _draft(state)  # created after the session began: a crash between create and start
    h = Harness(state)
    h.tick()
    stored = h.session(sid)
    assert stored["stop_kind"] == ovn.KIND_WRITER_CONFLICT and "interrupted start" in stored["stop_reason"]
    assert h.starter.calls == []


def test_older_operator_draft_does_not_block(state, alpha):
    register(state, "alpha", alpha)
    rb = _draft(state)
    rb.created_at = "2025-01-01T00:00:00+00:00"
    state.upsert_runbook(rb)
    sid = start(state)["session_id"]
    h = Harness(state)
    h.tick()
    assert h.starter.calls == ["A-01"] and h.session(sid)["state"] == ovn.SESSION_ACTIVE


# 12 ------------------------------------------ default git seams (real repositories)


def _origin_and_clone(tmp_path: Path) -> tuple[Path, Path]:
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    work = make_project(tmp_path / "work", [("A-01", "pending", "")])
    _git(work, "remote", "add", "origin", str(origin))
    _git(work, "push", "-q", "origin", "main")
    return origin, work


def _project(root: Path):
    return SimpleNamespace(local_repo_root=root, default_branch="main", github_remote=None)


def test_default_refresh_fast_forwards_a_clean_default_branch_checkout(tmp_path):
    origin, work = _origin_and_clone(tmp_path)
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(origin), str(other)], check=True)
    write_ledger(other, [("A-01", "complete", "")])
    _git(other, "add", "-A")
    _git(other, "commit", "-qm", "merged upstream")
    _git(other, "push", "-q", "origin", "main")

    truth = ovn.default_refresh_truth(_project(work))
    assert truth["ok"] and truth["fast_forwarded"]
    assert "complete" in (work / LEDGER).read_text(encoding="utf-8")


def test_default_refresh_fails_closed_when_origin_cannot_be_fetched(tmp_path):
    origin, work = _origin_and_clone(tmp_path)
    origin.rename(tmp_path / "gone.git")
    truth = ovn.default_refresh_truth(_project(work))
    assert truth["ok"] is False and "fetch" in truth["note"]


def test_default_refresh_fails_closed_on_a_dirty_or_off_branch_checkout_and_never_touches_it(tmp_path):
    _origin, work = _origin_and_clone(tmp_path)
    (work / "scratch.txt").write_text("wip", encoding="utf-8")
    truth = ovn.default_refresh_truth(_project(work))
    assert truth["ok"] is False and not truth["fast_forwarded"] and "dirty" in truth["note"]
    assert (work / "scratch.txt").read_text(encoding="utf-8") == "wip"  # left exactly as found
    (work / "scratch.txt").unlink()
    _git(work, "checkout", "-q", "-b", "feature")
    truth = ovn.default_refresh_truth(_project(work))
    assert truth["ok"] is False and "feature" in truth["note"]
    local = make_project(tmp_path / "local", [("L-01", "pending", "")])
    assert ovn.default_refresh_truth(_project(local))["ok"] is True  # local-only: nothing to be stale against


def test_run_orphaned_by_a_crash_before_its_pointer_was_saved_is_never_adopted_or_duplicated(state, alpha):
    register(state, "alpha", alpha)
    sid = start(state)["session_id"]
    h = Harness(state)
    h.starter("continue-next-task", pr.get_project(state, "alpha"))  # started, then the daemon died before saving the pointer
    h.tick(T0 + dt.timedelta(minutes=1))
    stored = h.session(sid)
    assert stored["state"] == ovn.SESSION_STOPPED and stored["stop_kind"] == ovn.KIND_WRITER_CONFLICT
    assert "not provably owned" in stored["stop_reason"]
    assert stored["current_runbook_id"] is None and h.starter.calls == ["A-01"]  # no second launch
    assert state.get_runbook("rb-1-a-01").status == RUNBOOK_RUNNING  # and the run itself is left untouched


# 13 --------------------- operator control landing in the middle of a daemon tick


def test_pause_landing_mid_tick_holds_the_session_instead_of_killing_it(state, alpha):
    register(state, "alpha", alpha)
    sid = start(state)["session_id"]
    h = Harness(state)
    original = h.fake.refresh_truth

    def refresh_then_operator_pauses(project):
        ovn.pause_session(state, sid)  # the dashboard pauses while the daemon is refreshing truth
        return original(project)

    h.fake.refresh_truth = refresh_then_operator_pauses
    h.tick()
    stored = h.session(sid)
    assert stored["state"] == ovn.SESSION_PAUSED and stored["stop_kind"] is None and h.starter.calls == []
    h.fake.refresh_truth = original
    ovn.resume_session(state, sid)
    h.tick(T0 + dt.timedelta(minutes=1))
    assert h.starter.calls == ["A-01"]


def test_pause_landing_before_an_authorized_merge_holds_the_merge(state, alpha):
    register(state, "alpha", alpha, overnight_merge="true")
    sid = start(state, merge_authorized=True)["session_id"]
    h = Harness(state)
    h.tick()
    accept(state, "rb-1-a-01")
    h.fake.refresh_truth = lambda project: (ovn.pause_session(state, sid), {"head_sha": "x"})[1]
    h.tick(T0 + dt.timedelta(hours=1))
    assert h.fake.merges == [] and h.session(sid)["state"] == ovn.SESSION_PAUSED


def test_stop_after_current_landing_during_task_start_keeps_the_started_task(state, alpha):
    register(state, "alpha", alpha)
    sid = start(state)["session_id"]
    h = Harness(state)
    inner = h.starter

    def start_then_operator_requests_stop(key, project):
        ovn.stop_after_current(state, sid)
        return inner(key, project)

    h.starter = start_then_operator_requests_stop
    h.tick()
    stored = h.session(sid)
    assert stored["state"] == ovn.SESSION_STOPPING and stored["current_runbook_id"] == "rb-1-a-01"
    assert state.get_runbook("rb-1-a-01").status == RUNBOOK_RUNNING  # finishes, then stops


def test_stop_landing_during_task_start_cancels_the_just_started_runbook(state, alpha, monkeypatch):
    register(state, "alpha", alpha)
    sid = start(state)["session_id"]
    terminated, stopped = [], []
    from scripts.agents.control_plane import runbooks as runbooks_module

    monkeypatch.setattr(runbooks_module, "stop_runbook", lambda **kw: stopped.append(kw["runbook_id"]))
    h = Harness(state, supervisor=SimpleNamespace(terminate_task=terminated.append))
    inner = h.starter

    def stop_now_while_starting(key, project):
        ovn.stop_now(state, sid)  # nothing to cancel yet: the runbook does not exist
        rb = inner(key, project)
        rb.task_id = "task-9"
        state.upsert_runbook(rb)
        return rb

    h.starter = stop_now_while_starting
    h.tick()
    assert h.session(sid)["state"] == ovn.SESSION_STOPPED
    assert terminated == ["task-9"] and stopped == ["rb-1-a-01"]  # no unowned writer left running
    assert any("stop arrived during task start" in m for m in events(state))


def test_session_stops_when_the_canonical_checkout_is_dirty_even_though_fetch_worked(state, tmp_path):
    _origin, work = _origin_and_clone(tmp_path)
    register(state, "work", work)
    (work / "scratch.txt").write_text("wip", encoding="utf-8")
    sid = start(state, "work")["session_id"]
    starter = Starter(state)
    ovn.tick(state=state, ops=ovn.default_ops(), starter=starter, now=T0)
    stored = state.get_overnight_session(sid)
    assert stored["state"] == ovn.SESSION_STOPPED and stored["stop_kind"] == ovn.KIND_STALE_TRUTH
    assert starter.calls == []


def test_pause_or_stop_after_current_landing_during_settle_refresh_is_not_overwritten(state, alpha):
    register(state, "alpha", alpha)
    for action, expected_state, resumable in (
        (ovn.pause_session, ovn.SESSION_PAUSED, None),
        (ovn.stop_after_current, ovn.SESSION_STOPPED, False),
    ):
        st = State(state.db_path.parent / f"{action.__name__}.db")
        register(st, "alpha", alpha)
        sid = start(st)["session_id"]
        h = Harness(st)
        h.tick()
        accept(st, "rb-1-a-01")
        original = h.fake.refresh_truth
        h.fake.refresh_truth = lambda project, a=action, s_=st, i=sid: (a(s_, i), original(project))[1]
        h.tick(T0 + dt.timedelta(hours=1))
        stored = st.get_overnight_session(sid)
        assert stored["state"] == expected_state
        if expected_state == ovn.SESSION_STOPPED:
            assert stored["stop_kind"] == ovn.KIND_OPERATOR_STOP and stored["resumable"] is resumable
        assert h.starter.calls == ["A-01"] and h.fake.merges == []


def test_a_live_runbook_that_predates_the_session_is_never_adopted(state, alpha):
    register(state, "alpha", alpha)
    old = _draft(state, "rb-operator")
    old.status, old.created_at = RUNBOOK_RUNNING, "2025-01-01T00:00:00+00:00"
    state.upsert_runbook(old)  # the operator already started A-01 by hand
    sid = start(state)["session_id"]
    h = Harness(state)
    h.tick()
    stored = h.session(sid)
    assert stored["state"] == ovn.SESSION_STOPPED and stored["stop_kind"] == ovn.KIND_WRITER_CONFLICT
    assert stored["current_runbook_id"] is None and h.starter.calls == []


def test_orphaned_run_with_its_own_running_write_task_still_fails_closed_without_duplicate(state, alpha):
    register(state, "alpha", alpha)
    sid = start(state)["session_id"]
    h = Harness(state)
    rb = h.starter("continue-next-task", pr.get_project(state, "alpha"))
    state.upsert_task(Task(
        id=f"{rb.id}-session", task_ref="A-01", role="primary-implementation", worker="claude-code",
        state="RUNNING", worktree=rb.worktree, runbook_id=rb.id, project_id="alpha",
    ))
    h.tick(T0 + dt.timedelta(minutes=1))
    assert h.session(sid)["stop_kind"] == ovn.KIND_WRITER_CONFLICT and h.starter.calls == ["A-01"]


def test_resume_refuses_when_another_live_session_exists(state, alpha):
    register(state, "alpha", alpha)
    first = start(state)["session_id"]
    ovn._finish(state, state.get_overnight_session(first), ovn.SESSION_STOPPED, ovn.KIND_OWNER_ACTION, "x", resumable=True)
    second = start(state)["session_id"]
    with pytest.raises(ovn.OvernightError, match="another live"):
        ovn.resume_session(state, first, now=T0)
    assert state.get_overnight_session(second)["state"] == ovn.SESSION_ACTIVE


def test_same_task_run_started_by_someone_else_after_session_start_is_not_adopted(state, alpha):
    register(state, "alpha", alpha)
    sid = start(state)["session_id"]
    h = Harness(state)
    manual = h.starter("continue-next-task", pr.get_project(state, "alpha"))  # a manual Quick Start
    manual.created_at = "2026-01-01T00:00:30+00:00"  # after the session began, before its start intent
    state.upsert_runbook(manual)
    h.tick(T0 + dt.timedelta(minutes=1))
    stored = h.session(sid)
    assert stored["state"] == ovn.SESSION_STOPPED and stored["stop_kind"] == ovn.KIND_WRITER_CONFLICT
    assert stored["current_runbook_id"] is None and stored["accepted_count"] == 0


def test_pause_committed_while_the_merge_state_is_checked_prevents_the_merge(state, alpha):
    register(state, "alpha", alpha, overnight_merge="true")
    sid = start(state, merge_authorized=True)["session_id"]
    h = Harness(state)
    h.tick()
    accept(state, "rb-1-a-01")
    original = h.fake.merge_state

    def slow_check_then_pause(project, runbook):
        result = original(project, runbook)
        ovn.pause_session(state, sid)  # lands while gh/git is being consulted
        return result

    h.fake.merge_state = slow_check_then_pause
    h.tick(T0 + dt.timedelta(hours=1))
    assert h.fake.merges == [] and h.session(sid)["state"] == ovn.SESSION_PAUSED


def test_stop_now_terminates_every_live_task_of_the_run_including_acceptance_reviewers(state, alpha, monkeypatch):
    register(state, "alpha", alpha)
    sid = start(state)["session_id"]
    h = Harness(state)
    h.tick()
    rb = state.get_runbook("rb-1-a-01")
    rb.task_id = "impl-1"
    state.upsert_runbook(rb)
    state.upsert_task(Task(
        id="rb-1-a-01-review-1", task_ref="A-01", role="diff-review", worker="grok-build-review",
        kind="read", state="RUNNING", runbook_id=rb.id, project_id="alpha",
    ))
    terminated = []
    from scripts.agents.control_plane import runbooks as runbooks_module

    monkeypatch.setattr(runbooks_module, "stop_runbook", lambda **kw: None)
    ovn.stop_now(state, sid, supervisor=SimpleNamespace(terminate_task=terminated.append))
    assert sorted(terminated) == ["impl-1", "rb-1-a-01-review-1"]


def test_run_started_by_the_session_is_cancelled_when_another_writer_appears_during_the_start(state, alpha, monkeypatch):
    register(state, "alpha", alpha)
    sid = start(state)["session_id"]
    h = Harness(state, supervisor=SimpleNamespace(terminate_task=lambda t: None))
    inner = h.starter

    def start_while_another_writer_appears(key, project):
        rb = inner(key, project)
        other = _draft(state, "rb-other")
        other.status, other.source_ref, other.created_at = RUNBOOK_RUNNING, "manual", "2025-01-01T00:00:00+00:00"
        state.upsert_runbook(other)
        return rb

    h.starter = start_while_another_writer_appears
    stopped = []
    from scripts.agents.control_plane import runbooks as runbooks_module

    monkeypatch.setattr(runbooks_module, "stop_runbook", lambda **kw: stopped.append(kw["runbook_id"]))
    h.tick()
    assert h.session(sid)["stop_kind"] == ovn.KIND_WRITER_CONFLICT
    assert stopped == ["rb-1-a-01"]  # ours cancelled; the foreign run is untouched


def test_stop_racing_a_tick_that_commits_a_new_pointer_still_cancels_that_run(state, alpha, monkeypatch):
    register(state, "alpha", alpha)
    sid = start(state)["session_id"]
    h = Harness(state)
    h.tick()
    stopped = []
    from scripts.agents.control_plane import runbooks as runbooks_module

    monkeypatch.setattr(runbooks_module, "stop_runbook", lambda **kw: stopped.append(kw["runbook_id"]))
    real_get = ovn.get_session

    def get_then_daemon_commits_a_new_pointer(st, session_id, project_id=None):
        snapshot = real_get(st, session_id, project_id)  # operator reads the session...
        ovn._save(st, dict(snapshot, current_runbook_id="rb-1-a-01"))  # ...daemon commits meanwhile
        return snapshot

    monkeypatch.setattr(ovn, "get_session", get_then_daemon_commits_a_new_pointer)
    # the operator's stale snapshot said no run; the committed record points at rb-1-a-01
    session = state.get_overnight_session(sid)
    session["current_runbook_id"] = None
    ovn._save(state, session)
    ovn.stop_now(state, sid, supervisor=SimpleNamespace(terminate_task=lambda t: None))
    assert h.session(sid)["state"] == ovn.SESSION_STOPPED
    assert h.session(sid)["current_runbook_id"] == "rb-1-a-01" and stopped == ["rb-1-a-01"]


def test_concurrent_start_cannot_create_two_live_sessions(state, alpha):
    register(state, "alpha", alpha)
    first = start(state)
    assert not state.insert_overnight_session_if_none_live(dict(first, session_id="ovn-dup"), ovn.LIVE_STATES)
    assert len(state.list_overnight_sessions(project_id="alpha")) == 1
