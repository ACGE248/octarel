"""OCTAREL-OPS-02 (issue #2): advance accepted runs to the current next eligible task.

Deterministic and repository-owned: every managed project is a real temporary
Git repository, no provider is called, and no OctaScene state is read or written.
The "starter" is a counting fake, so "the same task is never started twice" is
asserted directly rather than inferred.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scripts.agents.control_plane import advancement as adv
from scripts.agents.control_plane import project_registry as pr
from scripts.agents.control_plane import runbooks as runbooks_module
from scripts.agents.control_plane.commands import CommandContext
from scripts.agents.control_plane.dashboard_api import create_app
from scripts.agents.control_plane.models import (
    RUNBOOK_RUNNING,
    RUNBOOK_SUCCEEDED,
    ProviderState,
    Runbook,
)
from scripts.agents.control_plane.scheduler import Scheduler
from scripts.agents.control_plane.state import State
from scripts.agents.control_plane.supervisor import Supervisor
from scripts.agents.registry import load_registry

LEDGER = "TASKS.md"


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=Test", *args],
        cwd=str(root), check=True, capture_output=True,
    )


def make_project(root: Path, rows: list[tuple[str, str, str]]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    (root / "POLICY.md").write_text("# project policy\n", encoding="utf-8")
    write_ledger(root, rows)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    return root


def write_ledger(root: Path, rows: list[tuple[str, str, str]]) -> None:
    lines = ["| ID | status | notes |", "|---|---|---|"]
    lines += [f"| {tid} | {status} | {notes} |" for tid, status, notes in rows]
    (root / LEDGER).write_text("\n".join(lines) + "\n", encoding="utf-8")


def register(state: State, project_id: str, root: Path, **capabilities) -> None:
    pr.register_project(
        state,
        {
            "project_id": project_id,
            "display_name": project_id.title(),
            "local_repo_root": str(root),
            "policy_entrypoints": ["POLICY.md"],
            "task_sources": [LEDGER],
            "capabilities": {"task_source_adapter": "file_ledger", **capabilities},
        },
    )


def done_runbook(state: State, project_id: str, task_id: str, *, run_id: str | None = None) -> Runbook:
    runbook = Runbook(
        id=run_id or f"rb-{project_id}-{task_id.lower()}",
        name=f"Continue {project_id} — {task_id}",
        preset="overnight-development",
        objective="x",
        source_ref=f"{LEDGER} {task_id}",
        branch=f"{task_id.lower()}-work",
        worktree=f"/tmp/never-used-{project_id}-{task_id.lower()}",
        parent_worker="claude-code",
        max_duration_minutes=60,
        status=RUNBOOK_SUCCEEDED,
        acceptance_stage="DONE",
        acceptance_evidence={"test": {"status": "PASS"}, "review": {"status": "PASS"}},
        project_id=project_id,
    )
    state.upsert_runbook(runbook)
    return runbook


class Starter:
    """Counting fake for the normal Quick Start start path."""

    def __init__(self, state: State) -> None:
        self.state = state
        self.calls: list[tuple[str, str]] = []

    def __call__(self, key: str, project) -> Runbook:
        task = adv.discover_tasks(project)
        first = next(t for t in task if t.eligible)
        self.calls.append((project.project_id, first.task_id))
        runbook = Runbook(
            id=f"rb-next-{project.project_id}-{len(self.calls)}",
            name=f"Continue {project.project_id} — {first.task_id}",
            preset="overnight-development", objective="x", source_ref=f"{LEDGER} {first.task_id}",
            branch=f"{first.task_id.lower()}-work", worktree=f"/tmp/never-used-next-{first.task_id}",
            parent_worker="claude-code", max_duration_minutes=60, status=RUNBOOK_RUNNING,
            project_id=project.project_id,
        )
        self.state.upsert_runbook(runbook)
        return runbook


@pytest.fixture()
def state(tmp_path: Path) -> State:
    return State(tmp_path / "state" / "cp.db")


@pytest.fixture()
def alpha(tmp_path: Path) -> Path:
    return make_project(
        tmp_path / "alpha",
        [("A-01", "complete", "done"), ("A-02", "pending", "second"), ("A-03", "pending", "third")],
    )


def advance(state, runbook, **kwargs):
    return adv.advance_after_success(state=state, runbook=runbook, **kwargs)


def events(state: State) -> list[str]:
    return [e.message for e in state.list_events(limit=500) if e.category == "advancement"]


# 1 ------------------------------------------------------------------ success


def test_success_selects_next_eligible_task_with_reason_and_dependency_status(state, alpha):
    register(state, "alpha", alpha)
    write_ledger(alpha, [("A-01", "complete", "done"), ("A-02", "pending", "second depends on A-01"), ("A-03", "pending", "x")])
    finished = done_runbook(state, "alpha", "A-01")

    record = advance(state, finished)

    assert record["state"] == adv.ADV_NEXT_SELECTED
    assert record["next_task"]["task_id"] == "A-02"
    assert record["next_task"]["title"]
    assert "first eligible task" in record["selection_reason"]
    assert record["dependency_status"] == [{"task_id": "A-01", "status": "satisfied"}]
    assert record["completed_task_id"] == "A-01"
    assert record["evidence"]["head_sha"]
    assert record["started_runbook_id"] is None  # prepared only: auto_advance is off by default


def test_auto_advance_goes_through_advancing_then_starts_via_starter(state, alpha):
    register(state, "alpha", alpha, auto_advance="true")
    starter = Starter(state)
    seen_states: list[str] = []

    def spying_starter(key, project):
        seen_states.append(state.get_advancement("rb-alpha-a-01")["state"])
        return starter(key, project)

    record = advance(state, done_runbook(state, "alpha", "A-01"), starter=spying_starter)

    assert seen_states == [adv.ADV_ADVANCING]  # ADVANCING is persisted before anything starts
    assert record["state"] == adv.ADV_NEXT_SELECTED
    assert record["started_runbook_id"] == "rb-next-alpha-1"
    assert starter.calls == [("alpha", "A-02")]


# 2 ------------------------------------------------------- no replay of a task


def test_completed_task_is_not_replayed_when_ledger_was_not_updated(state, alpha):
    register(state, "alpha", alpha, auto_advance="true")
    write_ledger(alpha, [("A-01", "pending", "worker never updated the ledger"), ("A-02", "pending", "b")])
    starter = Starter(state)

    record = advance(state, done_runbook(state, "alpha", "A-01"), starter=starter)

    assert record["state"] == adv.ADV_BLOCKED
    assert record["stop_kind"] == adv.STOP_STALE_REPOSITORY_STATE
    assert "not replaying" in record["reason"]
    assert starter.calls == []


def test_previously_accepted_task_is_not_replayed_by_a_later_run(state, alpha):
    register(state, "alpha", alpha)
    done_runbook(state, "alpha", "A-02", run_id="rb-old")  # accepted earlier
    write_ledger(alpha, [("A-01", "complete", ""), ("A-02", "pending", "reopened?"), ("A-03", "pending", "")])

    record = advance(state, done_runbook(state, "alpha", "A-01"))

    assert record["state"] == adv.ADV_BLOCKED and record["stop_kind"] == adv.STOP_STALE_REPOSITORY_STATE


# 3 --------------------------------------------------------- no eligible task


def test_no_eligible_task(state, alpha):
    register(state, "alpha", alpha)
    write_ledger(alpha, [("A-01", "complete", ""), ("A-02", "complete", "")])
    record = advance(state, done_runbook(state, "alpha", "A-02"))
    assert record["state"] == adv.ADV_NO_ELIGIBLE_TASK
    assert record["next_task"] is None and record["reason"]


# 4 -------------------------------------------------------- dependency blocked


def test_dependency_blocked_names_the_unmet_dependency(state, alpha):
    register(state, "alpha", alpha, auto_advance="true")
    write_ledger(alpha, [("A-01", "complete", ""), ("A-02", "pending", "depends on A-09"), ("A-09", "in-progress", "")])
    starter = Starter(state)
    record = advance(state, done_runbook(state, "alpha", "A-01"), starter=starter)
    assert record["state"] == adv.ADV_BLOCKED
    assert record["stop_kind"] == adv.STOP_DEPENDENCY_BLOCKED
    assert "A-09" in record["reason"]
    assert record["dependency_status"][0]["status"].startswith("unsatisfied")
    assert starter.calls == []


# 5 ------------------------------------------- conflicting active writer/worktree


def test_conflicting_active_writer_blocks_advancement(state, alpha):
    register(state, "alpha", alpha, auto_advance="true")
    from scripts.agents.control_plane.quickstart import resolve_quickstart_option

    project = pr.get_project(state, "alpha")
    option = resolve_quickstart_option(alpha, "continue-next-task", project=project)
    squatter = Runbook(
        id="rb-squatter", name="unrelated manual run", preset="overnight-development", objective="x",
        source_ref="manual", branch="manual", worktree=option.worktree, parent_worker="claude-code",
        max_duration_minutes=60, status=RUNBOOK_RUNNING, project_id="alpha",
    )
    state.upsert_runbook(squatter)
    starter = Starter(state)

    record = advance(state, done_runbook(state, "alpha", "A-01"), starter=starter)

    assert record["state"] == adv.ADV_BLOCKED and record["stop_kind"] == adv.STOP_ACTIVE_WRITER_CONFLICT
    assert "rb-squatter" in record["reason"]
    assert starter.calls == []


# 6 ------------------------------------------------------------- stale state


def test_stale_cached_next_task_is_replaced_by_current_repository_truth(state, alpha):
    register(state, "alpha", alpha)
    finished = done_runbook(state, "alpha", "A-01")
    first = advance(state, finished)
    assert first["next_task"]["task_id"] == "A-02"  # Octarel now "believes" A-02 is next

    # Repository truth changes before advancement: A-02 is taken elsewhere, B-01 becomes the real next task.
    write_ledger(alpha, [("A-01", "complete", ""), ("A-02", "complete", "merged elsewhere"), ("B-01", "pending", "urgent"), ("A-03", "pending", "")])
    second = advance(state, finished)

    assert second["next_task"]["task_id"] == "B-01"
    assert second["state"] == adv.ADV_NEXT_SELECTED
    assert state.get_advancement(finished.id)["next_task"]["task_id"] == "B-01"


def test_stale_selection_is_recomputed_immediately_before_start(state, alpha):
    register(state, "alpha", alpha, auto_advance="true")
    starter = Starter(state)
    finished = done_runbook(state, "alpha", "A-01")
    write_ledger(alpha, [("A-01", "complete", ""), ("A-02", "complete", ""), ("A-03", "pending", "")])
    advance(state, finished, starter=starter)
    assert starter.calls == [("alpha", "A-03")]  # never the older A-02 belief


# 7 -------------------------------------------------------- owner decision


def test_owner_decision_required_stops_without_starting(state, alpha):
    register(state, "alpha", alpha, auto_advance="true")
    write_ledger(alpha, [("A-01", "complete", ""), ("A-02", "owner-decision", "pick vendor"), ("A-03", "pending", "")])
    starter = Starter(state)
    record = advance(state, done_runbook(state, "alpha", "A-01"), starter=starter)
    assert record["state"] == adv.ADV_OWNER_DECISION_REQUIRED
    assert "A-02" in record["reason"]
    assert starter.calls == []


# 8 -------------------------------------------------- provider/quota/safety


def test_provider_quota_exhausted_stops_advancement(state, alpha):
    register(state, "alpha", alpha, auto_advance="true")
    registry = load_registry()
    for name in registry.route("primary-implementation"):
        worker = registry.workers[name]
        state.upsert_provider_state(
            ProviderState(name=name, execution_system=worker.execution_system if hasattr(worker, "execution_system") else "cli",
                          provider=worker.provider, cost_class=worker.cost_class, state="QUOTA_EXHAUSTED")
        )
    starter = Starter(state)
    record = advance(state, done_runbook(state, "alpha", "A-01"), registry=registry, starter=starter)
    assert record["state"] == adv.ADV_BLOCKED and record["stop_kind"] == adv.STOP_PROVIDER_UNAVAILABLE
    assert "QUOTA_EXHAUSTED" in record["reason"]
    assert starter.calls == []


def test_repository_policy_failure_and_incomplete_acceptance_block(state, alpha):
    register(state, "alpha", alpha, auto_advance="true")
    starter = Starter(state)

    failed = done_runbook(state, "alpha", "A-01", run_id="rb-failed-acceptance")
    failed.acceptance_evidence = {"test": {"status": "FAIL"}}
    state.upsert_runbook(failed)
    rec = advance(state, failed, starter=starter)
    assert rec["stop_kind"] == adv.STOP_ACCEPTANCE_INCOMPLETE and "acceptance not actually complete" in rec["reason"]

    (alpha / "POLICY.md").unlink()
    rec = advance(state, done_runbook(state, "alpha", "A-01", run_id="rb-ok"), starter=starter)
    assert rec["stop_kind"] == adv.STOP_POLICY_BLOCKED
    assert starter.calls == []


# 9 ------------------------------------------------------------ idempotency


def test_repeated_advancement_starts_once_and_records_history_once(state, alpha):
    register(state, "alpha", alpha, auto_advance="true")
    starter = Starter(state)
    finished = done_runbook(state, "alpha", "A-01")

    first = advance(state, finished, starter=starter)
    second = advance(state, finished, starter=starter)
    third = advance(state, finished, starter=starter)

    assert first["started_runbook_id"] == second["started_runbook_id"] == third["started_runbook_id"] == "rb-next-alpha-1"
    assert starter.calls == [("alpha", "A-02")]
    assert len([r for r in state.list_runbooks(project_id="alpha") if r.name.endswith("A-02")]) == 1
    assert len(state.list_advancements(project_id="alpha")) == 1
    count = len(events(state))
    advance(state, finished, starter=starter)
    assert len(events(state)) == count


def test_unchanged_stop_does_not_duplicate_history(state, alpha):
    register(state, "alpha", alpha)
    write_ledger(alpha, [("A-01", "complete", "")])
    finished = done_runbook(state, "alpha", "A-01")
    advance(state, finished)
    count = len(events(state))
    advance(state, finished)
    advance(state, finished)
    assert len(events(state)) == count == 1


def test_already_active_next_task_is_adopted_not_duplicated(state, alpha):
    register(state, "alpha", alpha, auto_advance="true")
    Starter(state)(  # someone (or a crashed earlier attempt) already started A-02
        "continue-next-task", pr.get_project(state, "alpha")
    )
    starter = Starter(state)
    record = advance(state, done_runbook(state, "alpha", "A-01"), starter=starter)
    assert record["started_runbook_id"] == "rb-next-alpha-1"
    assert starter.calls == []


# 10 ------------------------------------------------- restart / persistence


def test_advancement_survives_restart_and_stays_idempotent(tmp_path, alpha):
    db = tmp_path / "persist" / "cp.db"
    first = State(db)
    register(first, "alpha", alpha, auto_advance="true")
    starter = Starter(first)
    finished = done_runbook(first, "alpha", "A-01")
    original = advance(first, finished, starter=starter)

    restarted = State(db)
    assert restarted.get_advancement(finished.id)["started_runbook_id"] == original["started_runbook_id"]
    again_starter = Starter(restarted)
    again = advance(restarted, restarted.get_runbook(finished.id), starter=again_starter)
    assert again["started_runbook_id"] == original["started_runbook_id"]
    assert again_starter.calls == []


def test_reconcile_leaves_legacy_projectless_runbooks_without_an_advancement(state, alpha, tmp_path):
    """reconcile_runbooks never raises on, and never fabricates a record for, a project-less runbook."""

    register(state, "alpha", alpha)
    legacy = done_runbook(state, "alpha", "A-01")
    legacy.project_id = None
    legacy.id = "rb-legacy"
    state.upsert_runbook(legacy)
    registry = load_registry()
    runbooks_module.reconcile_runbooks(state=state, repo_root=tmp_path, registry=registry)
    assert state.get_advancement("rb-legacy") is None


# 11 -------------------------------------------------------- project isolation


def test_second_project_isolation(state, alpha, tmp_path):
    beta = make_project(tmp_path / "beta", [("B-01", "complete", ""), ("B-02", "pending", "beta only")])
    register(state, "alpha", alpha)
    register(state, "beta", beta)
    write_ledger(alpha, [("A-01", "complete", ""), ("A-02", "complete", "")])  # alpha exhausted

    a = advance(state, done_runbook(state, "alpha", "A-01"))
    b = advance(state, done_runbook(state, "beta", "B-01"))

    assert a["state"] == adv.ADV_NO_ELIGIBLE_TASK  # never borrows beta's B-02
    assert b["state"] == adv.ADV_NEXT_SELECTED and b["next_task"]["task_id"] == "B-02"
    assert b["evidence"]["repo_root"] != a["evidence"]["repo_root"]
    assert [r["runbook_id"] for r in state.list_advancements(project_id="beta")] == ["rb-beta-b-01"]
    assert [r["runbook_id"] for r in state.list_advancements(project_id="alpha")] == ["rb-alpha-a-01"]


# 12 ------------------------------------------------------------- API state


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


def test_api_exposes_advancement_states_distinctly(api, alpha):
    ctx, client = api

    def detail(run_id):
        return client.get(f"/api/runbooks/{run_id}").json()["advancement"]

    nxt = done_runbook(ctx.state, "alpha", "A-01")
    assert detail(nxt.id) is None  # nothing decided yet: no fabricated state
    posted = client.post(f"/api/runbooks/{nxt.id}/advance").json()
    assert posted["state"] == "NEXT_SELECTED"
    got = detail(nxt.id)
    assert got["next_task"]["task_id"] == "A-02" and got["selection_reason"]
    assert any(r["advancement"]["state"] == "NEXT_SELECTED" for r in client.get("/api/runbooks").json() if r["advancement"])

    write_ledger(alpha, [("A-01", "complete", ""), ("A-02", "pending", "depends on A-77")])
    assert client.post(f"/api/runbooks/{nxt.id}/advance").json()["state"] == "BLOCKED"
    assert detail(nxt.id)["state"] == "BLOCKED" and "A-77" in detail(nxt.id)["reason"]

    write_ledger(alpha, [("A-01", "complete", ""), ("A-02", "complete", "")])
    assert client.post(f"/api/runbooks/{nxt.id}/advance").json()["state"] == "NO_ELIGIBLE_TASK"

    write_ledger(alpha, [("A-01", "complete", ""), ("A-02", "owner-decision", "")])
    assert client.post(f"/api/runbooks/{nxt.id}/advance").json()["state"] == "OWNER_DECISION_REQUIRED"
    assert client.post("/api/runbooks/missing/advance").status_code == 404


def test_advancing_state_is_visible_while_start_is_in_flight(state, alpha):
    register(state, "alpha", alpha, auto_advance="true")
    observed = {}

    def starter(key, project):
        observed["record"] = state.get_advancement("rb-alpha-a-01")
        raise RuntimeError("worktree provisioning failed")

    record = advance(state, done_runbook(state, "alpha", "A-01"), starter=starter)
    assert observed["record"]["state"] == "ADVANCING"
    assert record["state"] == "BLOCKED" and record["stop_kind"] == adv.STOP_START_FAILED
    assert record["started_runbook_id"] is None
