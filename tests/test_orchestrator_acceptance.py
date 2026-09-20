"""ENG-AGENT-13 (issue #138): the runbook acceptance pipeline.

Focused, deterministic coverage for the state machine that continues a
runbook from a successful implementation worker exit through Test -> Review
-> Checkpoint -> PR readiness before it may report terminal SUCCEEDED. No
live/billable provider calls: every provider-shaped dependency (the reviewer
Task, ``run_gate``, ``gh``) is monkeypatched or faked exactly like the rest of
this test suite already does for subprocess/worker launches.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from scripts.agents.control_plane import acceptance, runbooks
from scripts.agents.control_plane.models import (
    RUNBOOK_ACCEPTANCE_PENDING,
    RUNBOOK_BLOCKED,
    RUNBOOK_OWNER_ACTION_REQUIRED,
    RUNBOOK_RUNNING,
    RUNBOOK_SUCCEEDED,
    TASK_BLOCKED,
    TASK_FAILED,
    TASK_PENDING,
    TASK_RUNNING,
    TASK_SUCCEEDED,
    Task,
)
from scripts.agents.control_plane.scheduler import Scheduler
from scripts.agents.control_plane.state import State
from scripts.agents.registry import (
    PERMISSION_REPO_CONFIGURED_AUTO,
    PERMISSION_STANDARD,
    load_registry,
)
from scripts.ci.runtime_paths import CP_RUNTIME_DIRNAMES


def _git_repo(tmp_path: Path, *, branch: str = "eng/acceptance-test") -> Path:
    subprocess.run(["git", "init", "-q", "-b", branch, str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    (tmp_path / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", "seed"], check=True)
    return tmp_path


def _implementation_runbook(state: State, worktree: Path, *, runbook_id: str = "RB-acceptance"):
    task = Task(
        id=f"{runbook_id}-session", task_ref="V-TEST", role="primary-implementation",
        worker="claude-code", state=TASK_SUCCEEDED, result="PASS", worktree=str(worktree),
    )
    state.upsert_task(task)
    rb = runbooks.create_runbook(
        state=state, registry=load_registry(), name="acceptance test", preset="test-fix",
        source_ref="V-TEST", branch="eng/acceptance-test", worktree=str(worktree),
    )
    rb.id = runbook_id
    rb.task_id = task.id
    rb.status = RUNBOOK_RUNNING
    task.runbook_id = rb.id
    state.upsert_task(task)
    state.upsert_runbook(rb)
    return rb, task


class FakeSupervisor:
    def __init__(self):
        self.launched = []

    def launch_task(self, task, *, dry_run=False):  # noqa: ARG002
        self.launched.append(task)
        return task


def _fake_run_gate(**kwargs):
    return {"result": "pass", "evidence_path": ".local-gate/fake.json"}


def _failing_run_gate(**kwargs):
    return {"result": "fail", "prerequisite_failures": ["a required test failed"], "evidence_path": None}


def test_acceptance_review_dispatch_honors_pinned_route_on_initial_and_resumed_stage(tmp_path):
    repo = _git_repo(tmp_path)
    state = State(":memory:")
    rb, task = _implementation_runbook(state, repo)
    rb.worker_routes[acceptance.ACCEPTANCE_REVIEW_ROLE] = ["opencode2-gemini-flash-lite-review"]
    rb.acceptance_stage = acceptance.STAGE_REVIEW
    state.upsert_runbook(rb)
    supervisor = FakeSupervisor()

    acceptance._advance_review_stage(
        state=state, repo_root=repo, registry=load_registry(), scheduler=Scheduler(), supervisor=supervisor,
        runbook=rb, task=task, worktree=repo, tree="tree-1", changed_paths=["seed.txt"],
        evidence=rb.acceptance_evidence, base_ref="HEAD",
    )

    [review_task] = supervisor.launched
    assert review_task.worker == "opencode2-gemini-flash-lite-review"
    assert "antigravity-diff-review" not in review_task.selection_alternatives

    review_task.state = TASK_BLOCKED
    review_task.admission_reason = "pinned reviewer unavailable"
    state.upsert_task(review_task)
    rb.status = RUNBOOK_BLOCKED
    state.upsert_runbook(rb)
    runbooks.retry_acceptance(state=state, runbook_id=rb.id)
    assert state.get_runbook(rb.id).acceptance_stage == acceptance.STAGE_REVIEW


# --------------------------------------------------------------------------- Test stage


def test_low_risk_test_pass_marks_review_not_applicable_and_advances_to_checkpoint(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    state = State(":memory:")
    rb, task = _implementation_runbook(state, repo)
    acceptance.start_acceptance(runbook=rb, task=task)

    monkeypatch.setattr(acceptance, "classify", lambda paths: type("R", (), {"review_level": "low"})())
    monkeypatch.setattr(acceptance, "run_gate", _fake_run_gate)

    acceptance.advance_acceptance_pipeline(
        state=state, repo_root=repo, registry=None, scheduler=None, supervisor=None, runbook=rb, task=task,
        base_ref="HEAD",
    )

    assert rb.acceptance_evidence["review"]["status"] == "NOT_APPLICABLE"
    assert rb.acceptance_evidence["test"]["status"] == "PASS"
    assert rb.acceptance_stage == acceptance.STAGE_CHECKPOINT
    assert rb.status == RUNBOOK_ACCEPTANCE_PENDING


def test_test_stage_failure_blocks_without_finalizing_succeeded(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    state = State(":memory:")
    rb, task = _implementation_runbook(state, repo)
    acceptance.start_acceptance(runbook=rb, task=task)

    monkeypatch.setattr(acceptance, "classify", lambda paths: type("R", (), {"review_level": "low"})())
    monkeypatch.setattr(acceptance, "run_gate", _failing_run_gate)

    acceptance.advance_acceptance_pipeline(
        state=state, repo_root=repo, registry=None, scheduler=None, supervisor=None, runbook=rb, task=task,
        base_ref="HEAD",
    )

    assert rb.status == RUNBOOK_BLOCKED
    assert rb.status != RUNBOOK_SUCCEEDED
    assert rb.acceptance_evidence["test"]["status"] == "FAIL"
    assert "a required test failed" in rb.acceptance_evidence["test"]["reason"]
    assert rb.acceptance_stage != acceptance.STAGE_CHECKPOINT


# --------------------------------------------------------------------------- _review_scope_paths (issue #144)


def test_review_scope_paths_dedupes_and_normalizes_changed_paths(tmp_path):
    repo = _git_repo(tmp_path)
    scopes = acceptance._review_scope_paths(
        repo, ["./AGENTS.md", "a/b/c.py", "a/b/c.py", "old/name.py", "new/name.py"],
    )
    assert scopes == ["AGENTS.md", "a/b/c.py", "old/name.py", "new/name.py"]


def test_review_scope_paths_falls_back_to_top_level_tracked_entries_when_empty(tmp_path):
    repo = _git_repo(tmp_path)
    (repo / "second.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "second file"], check=True)
    assert set(acceptance._review_scope_paths(repo, [])) == {"seed.txt", "second.txt"}


def test_review_scope_paths_never_treats_a_bare_dot_as_a_real_changed_path(tmp_path):
    # Independent-review finding: a literal "." in changed_paths must never
    # normalize into the unsafe repository-root scope this function exists to
    # avoid -- it must fall through to the top-level-tracked-entries fallback,
    # exactly like a genuinely empty list.
    repo = _git_repo(tmp_path)
    assert acceptance._review_scope_paths(repo, ["."]) == ["seed.txt"]
    assert "." not in acceptance._review_scope_paths(repo, [".", "AGENTS.md"])


def test_review_scope_paths_accepts_none_changed_paths(tmp_path):
    repo = _git_repo(tmp_path)
    assert acceptance._review_scope_paths(repo, None) == ["seed.txt"]


# --------------------------------------------------------------------------- Review stage


def test_high_risk_dispatches_review_before_running_any_test(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    state = State(":memory:")
    rb, task = _implementation_runbook(state, repo)
    acceptance.start_acceptance(runbook=rb, task=task)

    monkeypatch.setattr(acceptance, "classify", lambda paths: type("R", (), {"review_level": "high"})())

    def _run_gate_must_not_be_called(**kwargs):
        raise AssertionError("run_gate must not run before independent review evidence exists")

    monkeypatch.setattr(acceptance, "run_gate", _run_gate_must_not_be_called)
    registry = load_registry()
    scheduler = Scheduler()

    acceptance.advance_acceptance_pipeline(
        state=state, repo_root=repo, registry=registry, scheduler=scheduler, supervisor=FakeSupervisor(),
        runbook=rb, task=task, base_ref="HEAD",
    )

    assert rb.acceptance_stage == acceptance.STAGE_REVIEW
    assert rb.status == RUNBOOK_ACCEPTANCE_PENDING
    review_evidence = rb.acceptance_evidence["review"]
    assert review_evidence["status"] == "NOT_REPORTED"
    review_task_id = review_evidence["task_id"]
    review_task = state.get_task(review_task_id)
    assert review_task is not None
    assert review_task.role == "diff-review"
    assert review_task.dependencies == (task.id,)
    # A read-only reviewer must never be selected from a write-capable-only
    # implementation route, and Codex must never appear on the review route.
    assert review_task.worker != "codex-build" and "codex" not in review_task.worker
    # issue #144: with zero individually-changed paths (this fixture's HEAD
    # equals its base ref), whole-candidate review semantics are preserved by
    # scoping every top-level tracked entry -- never the bare, always-rejected
    # "scope:." -- and a non-empty worker prompt is always included.
    assert "scope:." not in review_task.command
    scope_entries = [c.removeprefix("scope:") for c in review_task.command if c.startswith("scope:")]
    assert scope_entries == ["seed.txt"]
    assert any(not c.startswith("scope:") and c.strip() for c in review_task.command)


def test_review_dispatch_carries_the_configured_base_ref_to_the_worker(tmp_path, monkeypatch):
    """ENG-AGENT-18 (issue #155): the dispatched diff-review Task's ``command``

    must carry the pipeline's configured base ref as a ``base:<ref>`` entry so
    the daemon (``supervisor._build_argv``) can pass ``--diff-base`` through to
    ``orchestrate run``. Without it, a clean already-committed candidate's
    review always fails on an empty working-tree diff, regardless of how far
    ahead of the base its committed history actually is.
    """

    repo = _git_repo(tmp_path)
    state = State(":memory:")
    rb, task = _implementation_runbook(state, repo)
    acceptance.start_acceptance(runbook=rb, task=task)

    monkeypatch.setattr(acceptance, "classify", lambda paths: type("R", (), {"review_level": "critical"})())
    registry = load_registry()
    scheduler = Scheduler()

    acceptance.advance_acceptance_pipeline(
        state=state, repo_root=repo, registry=registry, scheduler=scheduler, supervisor=FakeSupervisor(),
        runbook=rb, task=task, base_ref="HEAD",
    )

    review_task_id = rb.acceptance_evidence["review"]["task_id"]
    review_task = state.get_task(review_task_id)
    base_entries = [c.removeprefix("base:") for c in review_task.command if c.startswith("base:")]
    assert base_entries == ["HEAD"]


def test_review_dispatch_covers_every_changed_path_added_nested_and_root_level(tmp_path, monkeypatch):
    """issue #144: the real changed-path list (already computed by

    gate_candidate for the Test stage) becomes one --scope entry per file --
    multi-file, nested, and root-level paths alike -- instead of the always-
    rejected bare "scope:.".
    """

    repo = _git_repo(tmp_path)
    (repo / "AGENTS.md").write_text("root doc change\n", encoding="utf-8")
    nested_dir = repo / "scripts" / "agents" / "control_plane"
    nested_dir.mkdir(parents=True)
    (nested_dir / "new_module.py").write_text("x = 1\n", encoding="utf-8")
    state = State(":memory:")
    rb, task = _implementation_runbook(state, repo)
    acceptance.start_acceptance(runbook=rb, task=task)

    monkeypatch.setattr(acceptance, "classify", lambda paths: type("R", (), {"review_level": "critical"})())
    registry = load_registry()
    scheduler = Scheduler()

    acceptance.advance_acceptance_pipeline(
        state=state, repo_root=repo, registry=registry, scheduler=scheduler, supervisor=FakeSupervisor(),
        runbook=rb, task=task, base_ref="HEAD",
    )

    review_task_id = rb.acceptance_evidence["review"]["task_id"]
    review_task = state.get_task(review_task_id)
    scope_entries = {c.removeprefix("scope:") for c in review_task.command if c.startswith("scope:")}

    assert "scope:." not in review_task.command
    assert scope_entries == {"AGENTS.md", "scripts/agents/control_plane/new_module.py"}


def test_review_dispatch_covers_a_path_the_candidate_deleted(tmp_path, monkeypatch):
    """issue #144: a file the candidate deletes must still be covered by the

    review scope even though it no longer exists on disk in the worktree.
    """

    repo = _git_repo(tmp_path)
    (repo / "seed.txt").unlink()
    state = State(":memory:")
    rb, task = _implementation_runbook(state, repo)
    acceptance.start_acceptance(runbook=rb, task=task)

    monkeypatch.setattr(acceptance, "classify", lambda paths: type("R", (), {"review_level": "critical"})())
    registry = load_registry()
    scheduler = Scheduler()

    acceptance.advance_acceptance_pipeline(
        state=state, repo_root=repo, registry=registry, scheduler=scheduler, supervisor=FakeSupervisor(),
        runbook=rb, task=task, base_ref="HEAD",
    )

    review_task_id = rb.acceptance_evidence["review"]["task_id"]
    review_task = state.get_task(review_task_id)
    scope_entries = {c.removeprefix("scope:") for c in review_task.command if c.startswith("scope:")}

    assert "scope:." not in review_task.command
    assert scope_entries == {"seed.txt"}


def test_review_dispatch_on_an_empty_worktree_blocks_truthfully(tmp_path, monkeypatch):
    """issue #144 independent-review finding: a candidate worktree with no

    tracked files at all has no possible narrow scope (neither a real
    changed path nor a top-level tracked entry), so dispatch must record a
    truthful FAIL and BLOCKED status instead of silently doing nothing or
    fabricating an unsafe scope.
    """

    repo = tmp_path / "empty-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "eng/acceptance-test", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "empty"], check=True)

    state = State(":memory:")
    rb, task = _implementation_runbook(state, repo)
    acceptance.start_acceptance(runbook=rb, task=task)

    monkeypatch.setattr(acceptance, "classify", lambda paths: type("R", (), {"review_level": "critical"})())

    acceptance.advance_acceptance_pipeline(
        state=state, repo_root=repo, registry=load_registry(), scheduler=Scheduler(), supervisor=FakeSupervisor(),
        runbook=rb, task=task, base_ref="HEAD",
    )

    assert rb.status == RUNBOOK_BLOCKED
    assert rb.acceptance_evidence["review"]["status"] == "FAIL"
    assert "no tracked files" in rb.acceptance_evidence["review"]["reason"]


def test_review_task_failure_blocks_without_running_tests(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    state = State(":memory:")
    rb, task = _implementation_runbook(state, repo)
    acceptance.start_acceptance(runbook=rb, task=task)
    rb.acceptance_stage = acceptance.STAGE_REVIEW
    review_task = Task(
        id="review-1", task_ref="review-1", role="diff-review", worker="grok-build-review",
        state=TASK_FAILED, last_error="the reviewer found a real correctness bug", runbook_id=rb.id,
    )
    state.upsert_task(review_task)
    rb.acceptance_evidence = {
        **rb.acceptance_evidence,
        "review": {"status": "NOT_REPORTED", "reason": "dispatched", "task_id": review_task.id},
    }

    monkeypatch.setattr(acceptance, "classify", lambda paths: type("R", (), {"review_level": "high"})())

    def _run_gate_must_not_be_called(**kwargs):
        raise AssertionError("run_gate must not run while review has failed")

    monkeypatch.setattr(acceptance, "run_gate", _run_gate_must_not_be_called)
    registry = load_registry()
    scheduler = Scheduler()

    acceptance.advance_acceptance_pipeline(
        state=state, repo_root=repo, registry=registry, scheduler=scheduler, supervisor=FakeSupervisor(),
        runbook=rb, task=task, base_ref="HEAD",
    )

    assert rb.status == RUNBOOK_BLOCKED
    assert rb.acceptance_evidence["review"]["status"] == "FAIL"
    assert "correctness bug" in rb.acceptance_evidence["review"]["reason"]


# ------------------------------------------------------- ENG-AGENT-19 (issue #157)
#
# A dispatched diff-review Task's subprocess runs real Git commands
# (write-tree, diff) against the candidate worktree for its whole run. The
# next reconcile tick used to call gate_candidate() unconditionally -- which
# runs `git add -A` on that same worktree -- before ever checking whether
# review had actually resolved, racing the two on the worktree's
# index.lock and blocking the runbook on a transient collision even though
# the review Task itself went on to pass.


def _dispatched_review_runbook(tmp_path, *, review_task_state, review_task_result=None):
    repo = _git_repo(tmp_path)
    state = State(":memory:")
    rb, task = _implementation_runbook(state, repo)
    acceptance.start_acceptance(runbook=rb, task=task)
    rb.acceptance_stage = acceptance.STAGE_REVIEW
    review_task = Task(
        id="review-inflight", task_ref="review-inflight", role="diff-review", worker="grok-build-review",
        state=review_task_state, result=review_task_result, runbook_id=rb.id,
    )
    state.upsert_task(review_task)
    rb.acceptance_evidence = {
        **rb.acceptance_evidence,
        "review": {"status": "NOT_REPORTED", "reason": "independent review is dispatched and not yet finished", "task_id": review_task.id},
    }
    return repo, state, rb, task, review_task


def test_in_flight_review_task_prevents_test_and_gate_candidate(tmp_path, monkeypatch):
    """issue #157: while the dispatched review Task is still RUNNING, the

    reconcile tick must not touch the candidate worktree's Git index at
    all -- neither `gate_candidate` nor the `git add -A` that precedes it in
    `_advance_test_and_review` may run, since that races the review
    subprocess's own Git reads on the identical worktree.
    """

    repo, state, rb, task, review_task = _dispatched_review_runbook(tmp_path, review_task_state=TASK_RUNNING)

    def _gate_candidate_must_not_run(*a, **k):
        raise AssertionError("gate_candidate must not run while the review Task is still in flight")

    monkeypatch.setattr(acceptance, "gate_candidate", _gate_candidate_must_not_run)
    monkeypatch.setattr(
        acceptance, "classify", lambda paths: (_ for _ in ()).throw(AssertionError("classify must not run either"))
    )
    monkeypatch.setattr(
        acceptance, "run_gate", lambda **k: (_ for _ in ()).throw(AssertionError("run_gate must not run either"))
    )

    acceptance.advance_acceptance_pipeline(
        state=state, repo_root=repo, registry=load_registry(), scheduler=Scheduler(), supervisor=FakeSupervisor(),
        runbook=rb, task=task, base_ref="HEAD",
    )

    # Nothing was touched this tick: review evidence is exactly as the
    # dispatch tick left it, and the runbook is not (re)blocked.
    assert rb.acceptance_evidence["review"] == {
        "status": "NOT_REPORTED", "reason": "independent review is dispatched and not yet finished",
        "task_id": review_task.id,
    }
    assert rb.status != RUNBOOK_BLOCKED


def test_pending_and_queued_review_task_also_prevents_gate_candidate(tmp_path, monkeypatch):
    """The in-flight guard covers PENDING/QUEUED, not only RUNNING: the

    subprocess may not have started yet, but nothing is gained -- and the
    race window is only widened -- by touching the worktree before it does.
    """

    repo, state, rb, task, review_task = _dispatched_review_runbook(tmp_path, review_task_state=TASK_PENDING)
    monkeypatch.setattr(
        acceptance, "gate_candidate",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("gate_candidate must not run for a queued review Task")),
    )

    acceptance.advance_acceptance_pipeline(
        state=state, repo_root=repo, registry=load_registry(), scheduler=Scheduler(), supervisor=FakeSupervisor(),
        runbook=rb, task=task, base_ref="HEAD",
    )

    assert rb.acceptance_evidence["review"]["status"] == "NOT_REPORTED"


def test_successful_review_is_consumed_on_a_later_reconcile_and_test_advances(tmp_path, monkeypatch):
    """issue #157: once the review Task reaches TASK_SUCCEEDED (no longer

    in flight), the very next reconcile tick must consume its manifest as
    STAGE_REVIEW PASS and advance straight into Test -- gate_candidate now
    runs safely because the review subprocess is no longer alive.
    """

    repo, state, rb, task, review_task = _dispatched_review_runbook(
        tmp_path, review_task_state=TASK_SUCCEEDED, review_task_result="PASS",
    )
    monkeypatch.setattr(acceptance, "classify", lambda paths: type("R", (), {"review_level": "critical"})())

    from scripts.ci.local_gate import candidate as gate_candidate_real

    tree, _head, _paths = gate_candidate_real(repo, "HEAD")
    run_dir = repo / ".agent-output" / "review-inflight" / "grok-build-review" / "run-1"
    (run_dir / "logs").mkdir(parents=True)
    (run_dir / "logs" / "run.log").write_text(
        "### 1. Blockers\n- None.\n\n### 2. Important findings\n- None.\n\n"
        "### 3. Minor findings\n- None.\n\n### 4. Test gaps\n- None.\n\n**READY**\n",
        encoding="utf-8",
    )
    (run_dir / "manifest.json").write_text(
        __import__("json").dumps({
            "role": "diff-review", "result": "PASS", "files_changed": [],
            "candidate_tree_sha": tree, "actual": {"provider": "xAI"},
            "paths": {"log": ".agent-output/review-inflight/grok-build-review/run-1/logs/run.log"},
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(acceptance, "run_gate", lambda **kwargs: {"result": "pass", "evidence_path": None})

    acceptance.advance_acceptance_pipeline(
        state=state, repo_root=repo, registry=None, scheduler=None, supervisor=None, runbook=rb,
        task=task, base_ref="HEAD",
    )

    assert rb.acceptance_evidence["review"]["status"] == "PASS"
    assert rb.acceptance_evidence["test"]["status"] == "PASS"
    assert rb.acceptance_stage == acceptance.STAGE_CHECKPOINT
    assert rb.status == RUNBOOK_ACCEPTANCE_PENDING


def test_failed_review_task_is_consumed_and_blocks_once_resolved(tmp_path, monkeypatch):
    """issue #157: symmetric to the success case -- once the review Task

    resolves to a genuine failure (TASK_BLOCKED here, not TASK_FAILED, to
    exercise the other branch of ``_advance_review_stage``'s terminal
    handling), the next tick must still record FAIL evidence and block the
    runbook; the in-flight guard only defers this, it never suppresses it.
    """

    repo, state, rb, task, review_task = _dispatched_review_runbook(tmp_path, review_task_state=TASK_BLOCKED)
    review_task.admission_reason = "no eligible read-only reviewer is currently available"
    state.upsert_task(review_task)
    monkeypatch.setattr(acceptance, "classify", lambda paths: type("R", (), {"review_level": "critical"})())

    def _run_gate_must_not_run(**kwargs):
        raise AssertionError("run_gate must not run once review has resolved to a failure")

    monkeypatch.setattr(acceptance, "run_gate", _run_gate_must_not_run)

    acceptance.advance_acceptance_pipeline(
        state=state, repo_root=repo, registry=load_registry(), scheduler=Scheduler(), supervisor=FakeSupervisor(),
        runbook=rb, task=task, base_ref="HEAD",
    )

    assert rb.status == RUNBOOK_BLOCKED
    assert rb.acceptance_evidence["review"]["status"] == "FAIL"
    assert "no eligible read-only reviewer" in rb.acceptance_evidence["review"]["reason"]


def test_retry_while_review_in_flight_never_dispatches_a_second_review_task(tmp_path, monkeypatch):
    """issue #157: two reconcile ticks in a row while the review Task is

    still RUNNING must both simply defer -- never admit a second review
    Task for the same runbook (idempotent restart/retry, no duplicate
    dispatch).
    """

    repo, state, rb, task, review_task = _dispatched_review_runbook(tmp_path, review_task_state=TASK_RUNNING)
    monkeypatch.setattr(
        acceptance, "gate_candidate",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("gate_candidate must not run while in flight")),
    )
    registry = load_registry()
    scheduler = Scheduler()

    for _ in range(2):
        acceptance.advance_acceptance_pipeline(
            state=state, repo_root=repo, registry=registry, scheduler=scheduler, supervisor=FakeSupervisor(),
            runbook=rb, task=task, base_ref="HEAD",
        )

    review_tasks = [t for t in state.list_tasks() if t.role == "diff-review"]
    assert [t.id for t in review_tasks] == [review_task.id]
    assert rb.acceptance_evidence["review"]["task_id"] == review_task.id


def test_no_in_flight_review_first_dispatch_tick_still_calls_gate_candidate(tmp_path, monkeypatch):
    """issue #157 regression guard: the in-flight skip is scoped to an

    *already-dispatched* review Task only. The ordinary first tick -- no
    review evidence recorded yet -- must still call gate_candidate exactly
    as before, since that is what determines risk/scope and dispatches
    review in the first place.
    """

    repo = _git_repo(tmp_path)
    state = State(":memory:")
    rb, task = _implementation_runbook(state, repo)
    acceptance.start_acceptance(runbook=rb, task=task)

    monkeypatch.setattr(acceptance, "classify", lambda paths: type("R", (), {"review_level": "high"})())
    registry = load_registry()
    scheduler = Scheduler()

    acceptance.advance_acceptance_pipeline(
        state=state, repo_root=repo, registry=registry, scheduler=scheduler, supervisor=FakeSupervisor(),
        runbook=rb, task=task, base_ref="HEAD",
    )

    review_evidence = rb.acceptance_evidence["review"]
    assert review_evidence["status"] == "NOT_REPORTED"
    review_task = state.get_task(review_evidence["task_id"])
    assert review_task is not None and review_task.role == "diff-review"


def test_resolved_review_status_never_triggers_the_in_flight_guard(tmp_path, monkeypatch):
    """issue #157 regression guard: a runbook whose review evidence already

    reads a resolved status (PASS/NOT_APPLICABLE/FAIL) must never be treated
    as "in flight" -- the guard keys only off the literal ``NOT_REPORTED``
    sentinel a pending dispatch uses, matching every terminal state this
    module itself already records for review.
    """

    repo, state, rb, task, _review_task = _dispatched_review_runbook(tmp_path, review_task_state=TASK_RUNNING)
    from scripts.ci.local_gate import candidate as gate_candidate_real

    tree, _head, _paths = gate_candidate_real(repo, "HEAD")
    rb.acceptance_evidence = {
        **rb.acceptance_evidence,
        "review": {
            "status": "PASS", "reason": "already resolved on a prior tick", "task_id": "review-inflight",
            "tree_sha": tree,
        },
    }
    monkeypatch.setattr(acceptance, "classify", lambda paths: type("R", (), {"review_level": "critical"})())
    monkeypatch.setattr(acceptance, "run_gate", lambda **kwargs: {"result": "pass", "evidence_path": None})

    acceptance.advance_acceptance_pipeline(
        state=state, repo_root=repo, registry=load_registry(), scheduler=Scheduler(), supervisor=FakeSupervisor(),
        runbook=rb, task=task, base_ref="HEAD",
    )

    # gate_candidate ran (it must, for an already-resolved review) and Test
    # proceeded -- confirmed by the stage advancing past Review.
    assert rb.acceptance_stage == acceptance.STAGE_CHECKPOINT


def test_review_pass_feeds_manifest_evidence_into_run_gate(tmp_path, monkeypatch):
    # The daemon's own repo_root is deliberately a different directory than
    # the runbook's worktree here, exactly like the real ENG-AGENT-13 setup
    # (a dedicated CP checkout scheduling a separate feature worktree such as
    # OctagesApp-v1-05) -- a prior version of this code looked for the review
    # manifest under repo_root and could never find it.
    daemon_repo_root = tmp_path / "daemon-repo-root"
    daemon_repo_root.mkdir()
    repo = _git_repo(tmp_path / "worktree")
    state = State(":memory:")
    rb, task = _implementation_runbook(state, repo)
    acceptance.start_acceptance(runbook=rb, task=task)
    rb.acceptance_stage = acceptance.STAGE_REVIEW
    review_task = Task(
        id="review-2", task_ref="review-2", role="diff-review", worker="grok-build-review",
        state=TASK_SUCCEEDED, result="PASS", runbook_id=rb.id,
    )
    state.upsert_task(review_task)
    rb.acceptance_evidence = {
        **rb.acceptance_evidence,
        "review": {"status": "NOT_REPORTED", "reason": "dispatched", "task_id": review_task.id},
    }

    monkeypatch.setattr(acceptance, "classify", lambda paths: type("R", (), {"review_level": "critical"})())
    manifest_lookup_roots = []

    def fake_find_manifest(**kwargs):
        manifest_lookup_roots.append(kwargs["repo_root"])
        return ("agent-output/review-2/manifest.json", {"actual": {"provider": "xAI"}}, None)

    monkeypatch.setattr(acceptance, "_find_review_manifest", fake_find_manifest)

    seen = {}

    def _capturing_run_gate(**kwargs):
        seen.update(kwargs)
        return {"result": "pass", "evidence_path": ".local-gate/fake.json"}

    monkeypatch.setattr(acceptance, "run_gate", _capturing_run_gate)

    acceptance.advance_acceptance_pipeline(
        state=state, repo_root=daemon_repo_root, registry=None, scheduler=None, supervisor=None, runbook=rb,
        task=task, base_ref="HEAD",
    )

    assert manifest_lookup_roots == [repo]

    assert rb.acceptance_evidence["review"]["status"] == "PASS"
    assert seen["review_provider"] == "xAI"
    assert seen["review_evidence"] == "agent-output/review-2/manifest.json"
    # ENG-AGENT-18 (issue #150): the worker registry is Control Plane
    # orchestration infrastructure, not part of the candidate worktree's own
    # product diff -- a candidate branch that predates
    # scripts/agents/control_plane entirely (like the real OctagesApp-v1-05
    # setup this test mirrors) must still resolve it against the daemon's
    # own canonical repo_root, never against the candidate worktree.
    assert seen["registry_root"] == daemon_repo_root
    assert seen["root"] == repo


def test_markdown_formatted_ready_review_is_accepted_end_to_end(tmp_path, monkeypatch):
    """ENG-AGENT-17 (issue #148): a real reviewer response using ordinary

    markdown headings/lists (not the old brittle literal-line template) must
    reach STAGE_REVIEW PASS through the real, unmocked manifest+log lookup
    and response parsing -- not merely the isolated parser unit tests.
    """

    import json

    repo = _git_repo(tmp_path)
    state = State(":memory:")
    rb, task = _implementation_runbook(state, repo)
    acceptance.start_acceptance(runbook=rb, task=task)
    rb.acceptance_stage = acceptance.STAGE_REVIEW
    review_task = Task(
        id="review-md", task_ref="review-md", role="diff-review", worker="antigravity-diff-review",
        state=TASK_SUCCEEDED, result="PASS", runbook_id=rb.id,
    )
    state.upsert_task(review_task)
    rb.acceptance_evidence = {
        **rb.acceptance_evidence,
        "review": {"status": "NOT_REPORTED", "reason": "dispatched", "task_id": review_task.id},
    }
    monkeypatch.setattr(acceptance, "classify", lambda paths: type("R", (), {"review_level": "critical"})())

    from scripts.ci.local_gate import candidate as gate_candidate_real

    tree, _head, _paths = gate_candidate_real(repo, "HEAD")
    run_dir = repo / ".agent-output" / "review-md" / "antigravity-diff-review" / "run-1"
    (run_dir / "logs").mkdir(parents=True)
    (run_dir / "logs" / "run.log").write_text(
        "### 1. Blockers\n- None.\n\n### 2. Important findings\n- None.\n\n"
        "### 3. Minor findings\n- None.\n\n### 4. Test gaps\n- None.\n\n**READY**\n",
        encoding="utf-8",
    )
    (run_dir / "manifest.json").write_text(
        json.dumps({
            "role": "diff-review", "result": "PASS", "files_changed": [],
            "candidate_tree_sha": tree, "actual": {"provider": "Google"},
            "paths": {"log": ".agent-output/review-md/antigravity-diff-review/run-1/logs/run.log"},
        }),
        encoding="utf-8",
    )

    monkeypatch.setattr(acceptance, "run_gate", lambda **kwargs: {"result": "pass", "evidence_path": None})

    acceptance.advance_acceptance_pipeline(
        state=state, repo_root=repo, registry=None, scheduler=None, supervisor=None, runbook=rb,
        task=task, base_ref="HEAD",
    )

    assert rb.acceptance_evidence["review"]["status"] == "PASS"
    assert rb.acceptance_evidence["review"]["provider"] == "Google"


def test_review_task_exit_zero_without_a_ready_conclusion_is_not_accepted_as_pass(tmp_path, monkeypatch):
    """Independent-review finding (round 2): a read-only reviewer that exits 0

    but writes a normal review (real blockers, not a bare READY conclusion)
    must not be recorded review PASS here. run_gate's own review_evidence
    prerequisite (local_gate.review_response_verdict, the exact same
    function this reuses) would reject such a manifest anyway -- but by then
    STAGE_REVIEW would already be durably marked PASS and never
    reconsidered, since a Test failure only clears Test evidence (see the
    round-1 stage-pin fix). This must be caught here, before evidence is
    ever recorded PASS.
    """

    repo = _git_repo(tmp_path)
    state = State(":memory:")
    rb, task = _implementation_runbook(state, repo)
    acceptance.start_acceptance(runbook=rb, task=task)
    rb.acceptance_stage = acceptance.STAGE_REVIEW
    review_task = Task(
        id="review-4", task_ref="review-4", role="diff-review", worker="grok-build-review",
        state=TASK_SUCCEEDED, result="PASS", runbook_id=rb.id,
    )
    state.upsert_task(review_task)
    rb.acceptance_evidence = {
        **rb.acceptance_evidence,
        "review": {"status": "NOT_REPORTED", "reason": "dispatched", "task_id": review_task.id},
    }

    monkeypatch.setattr(acceptance, "classify", lambda paths: type("R", (), {"review_level": "high"})())

    from scripts.ci.local_gate import candidate as gate_candidate_real

    tree, _head, _paths = gate_candidate_real(repo, "HEAD")
    manifest_dir = repo / ".agent-output" / "review-4" / "grok-build-review" / "run-1"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "manifest.json").write_text(
        __import__("json").dumps(
            {
                "role": "diff-review", "result": "PASS", "files_changed": [],
                "candidate_tree_sha": tree, "actual": {"provider": "xAI"},
            }
        ),
        encoding="utf-8",
    )

    from scripts.ci.review_contract import ReviewVerdict

    monkeypatch.setattr(
        acceptance, "review_response_verdict",
        lambda manifest_path, manifest: ReviewVerdict(False, "response was not READY (test double)"),
    )

    def _run_gate_must_not_be_called(**kwargs):
        raise AssertionError("run_gate must not run once review is correctly recorded as not-ready/failed")

    monkeypatch.setattr(acceptance, "run_gate", _run_gate_must_not_be_called)

    acceptance.advance_acceptance_pipeline(
        state=state, repo_root=repo, registry=None, scheduler=None, supervisor=None, runbook=rb, task=task,
        base_ref="HEAD",
    )

    assert rb.acceptance_evidence["review"]["status"] == "FAIL"
    assert rb.status == RUNBOOK_BLOCKED


def test_find_review_manifest_prefers_the_most_specific_reason_not_the_newest_manifest(tmp_path) -> None:
    """Independent-review finding: a stray newer manifest with a generic

    structural mismatch (wrong role) must never bury a genuinely more
    specific, actionable reason (a real content-parse rejection) found on an
    older manifest for the same task.
    """

    import json
    import time

    from scripts.ci.local_gate import candidate as gate_candidate_real

    repo = _git_repo(tmp_path)
    tree, _head, _paths = gate_candidate_real(repo, "HEAD")

    older = repo / ".agent-output" / "review-x" / "grok-build-review" / "run-1"
    older.mkdir(parents=True)
    (older / "manifest.json").write_text(
        json.dumps({
            "role": "diff-review", "result": "PASS", "files_changed": [],
            "candidate_tree_sha": tree, "actual": {"provider": "xAI"},
            "paths": {"log": ".agent-output/review-x/grok-build-review/run-1/logs/run.log"},
        }),
        encoding="utf-8",
    )
    (older / "logs").mkdir()
    (older / "logs" / "run.log").write_text(
        "Blockers: the acceptance stage double-dispatches review on retry\n"
        "Important findings: None\nMinor findings: None\nTest gaps: None\nBLOCKED\n",
        encoding="utf-8",
    )
    time.sleep(0.01)
    newer = repo / ".agent-output" / "review-x" / "grok-build-review" / "run-2-stray"
    newer.mkdir(parents=True)
    (newer / "manifest.json").write_text(
        json.dumps({"role": "some-other-role", "result": "PASS"}), encoding="utf-8",
    )

    _manifest_path, manifest, reason = acceptance._find_review_manifest(
        repo_root=repo, task_ref="review-x", tree=tree,
    )

    assert manifest is None
    # The specific content-parse rejection (from the real review response)
    # wins over the stray newer manifest's generic structural mismatch.
    assert "not READY" in reason
    assert "role" not in reason


def test_review_task_never_inherits_the_runbooks_write_permission_profile(tmp_path, monkeypatch):
    """Independent-review finding: Worker.supports_permission_profile() rejects

    every read-only worker for any profile besides "standard", so a review
    Task built with runbook.permission_profile (repo_configured_auto for the
    Overnight Development preset -- the exact preset issue #138 was filed
    against) made managed_admit permanently TASK_BLOCK every diff-review
    candidate, deadlocking acceptance forever at NOT_REPORTED.
    """

    repo = _git_repo(tmp_path)
    state = State(":memory:")
    rb, task = _implementation_runbook(state, repo)
    rb.permission_profile = PERMISSION_REPO_CONFIGURED_AUTO
    acceptance.start_acceptance(runbook=rb, task=task)

    monkeypatch.setattr(acceptance, "classify", lambda paths: type("R", (), {"review_level": "high"})())
    registry = load_registry()
    scheduler = Scheduler()

    acceptance.advance_acceptance_pipeline(
        state=state, repo_root=repo, registry=registry, scheduler=scheduler, supervisor=FakeSupervisor(),
        runbook=rb, task=task, base_ref="HEAD",
    )

    review_task_id = rb.acceptance_evidence["review"]["task_id"]
    review_task = state.get_task(review_task_id)
    assert review_task.permission_profile == PERMISSION_STANDARD


def test_permanently_blocked_review_admission_halts_blocked_not_stuck_forever(tmp_path, monkeypatch):
    """Independent-review finding: a permanently-blocked review Task admission

    (TASK_BLOCKED, e.g. no eligible candidate) is neither SUCCEEDED nor
    FAILED. Without explicit handling the review stage sat at NOT_REPORTED
    on every tick forever -- the runbook could never reach BLOCKED (for the
    operator to see and repair) nor SUCCEEDED.
    """

    repo = _git_repo(tmp_path)
    state = State(":memory:")
    rb, task = _implementation_runbook(state, repo)
    acceptance.start_acceptance(runbook=rb, task=task)
    rb.acceptance_stage = acceptance.STAGE_REVIEW
    review_task = Task(
        id="review-blocked", task_ref="review-blocked", role="diff-review", worker="grok-build-review",
        state=TASK_BLOCKED, admission_reason="no eligible provider: policy preservation failed", runbook_id=rb.id,
    )
    state.upsert_task(review_task)
    rb.acceptance_evidence = {
        **rb.acceptance_evidence,
        "review": {"status": "NOT_REPORTED", "reason": "dispatched", "task_id": review_task.id},
    }

    monkeypatch.setattr(acceptance, "classify", lambda paths: type("R", (), {"review_level": "high"})())

    def _run_gate_must_not_be_called(**kwargs):
        raise AssertionError("run_gate must not run while review is permanently blocked")

    monkeypatch.setattr(acceptance, "run_gate", _run_gate_must_not_be_called)

    acceptance.advance_acceptance_pipeline(
        state=state, repo_root=repo, registry=load_registry(), scheduler=Scheduler(), supervisor=FakeSupervisor(),
        runbook=rb, task=task, base_ref="HEAD",
    )

    assert rb.status == RUNBOOK_BLOCKED
    assert rb.status != RUNBOOK_ACCEPTANCE_PENDING  # not stuck NOT_REPORTED forever
    assert rb.acceptance_evidence["review"]["status"] == "FAIL"
    assert "policy preservation" in rb.acceptance_evidence["review"]["reason"]


def test_review_task_sets_avoid_provider_openai_not_a_cosmetic_prefilter(tmp_path, monkeypatch):
    """Independent-review finding (round 2): a client-side pre-filter of the

    route candidate list is cosmetic -- managed_admit re-derives the full,
    unfiltered route via its own _candidate_scores and can still reassign
    task.worker to codex-review if it is the only eligible candidate at
    admission time. The real, honored exclusion mechanism is
    Task.avoid_provider (dispatch.py's provider-diversity gate), so the
    dispatched review Task must set it rather than relying on a pre-filtered
    candidate list that admission ignores.
    """

    repo = _git_repo(tmp_path)
    state = State(":memory:")
    rb, task = _implementation_runbook(state, repo)
    acceptance.start_acceptance(runbook=rb, task=task)

    monkeypatch.setattr(acceptance, "classify", lambda paths: type("R", (), {"review_level": "high"})())

    captured = {}

    def fake_managed_admit(*, state, registry, scheduler, supervisor, repo_root, task):
        captured["task"] = task
        return None

    monkeypatch.setattr(acceptance, "managed_admit", fake_managed_admit)

    acceptance.advance_acceptance_pipeline(
        state=state, repo_root=repo, registry=load_registry(), scheduler=Scheduler(),
        supervisor=FakeSupervisor(), runbook=rb, task=task, base_ref="HEAD",
    )

    assert captured["task"].role == "diff-review"
    assert captured["task"].avoid_provider == "OpenAI"


def test_permanently_blocked_review_from_avoid_provider_still_halts_blocked(tmp_path, monkeypatch):
    """End-to-end companion to the avoid_provider test above: when the only

    route candidate is Codex and admission genuinely excludes it via
    avoid_provider, the review Task lands TASK_BLOCKED (real dispatch
    behavior), and the round-1 fix must still convert that into a truthful
    runbook BLOCKED rather than a silent NOT_REPORTED stall.
    """

    repo = _git_repo(tmp_path)
    state = State(":memory:")
    rb, task = _implementation_runbook(state, repo)
    acceptance.start_acceptance(runbook=rb, task=task)

    monkeypatch.setattr(acceptance, "classify", lambda paths: type("R", (), {"review_level": "high"})())

    class _CodexOnlyRoute:
        """Wraps the real registry, overriding only route() to force codex-review

        as the sole candidate; every other attribute/method (workers,
        repository_data_reuse_allowed, etc.) delegates to the real registry
        so this exercises real dispatch.managed_admit eligibility scoring.
        """

        def __init__(self, real):
            self._real = real

        def route(self, role):
            return ["codex-review"]

        def __getattr__(self, name):
            return getattr(self._real, name)

    registry = _CodexOnlyRoute(load_registry())
    scheduler = Scheduler()

    # Dispatch tick: managed_admit sees only "codex-review" as a route
    # candidate, avoid_provider="OpenAI" excludes it, so admission blocks.
    acceptance.advance_acceptance_pipeline(
        state=state, repo_root=repo, registry=registry, scheduler=scheduler, supervisor=FakeSupervisor(),
        runbook=rb, task=task, base_ref="HEAD",
    )
    review_task_id = rb.acceptance_evidence["review"]["task_id"]
    assert state.get_task(review_task_id).state == TASK_BLOCKED

    # Reconcile tick: the blocked admission must halt the runbook truthfully.
    acceptance.advance_acceptance_pipeline(
        state=state, repo_root=repo, registry=registry, scheduler=scheduler, supervisor=FakeSupervisor(),
        runbook=rb, task=task, base_ref="HEAD",
    )
    assert rb.status == RUNBOOK_BLOCKED
    assert rb.acceptance_evidence["review"]["status"] == "FAIL"


def test_test_failure_after_review_pass_pins_stage_to_test_for_correct_resume(tmp_path, monkeypatch):
    """Independent-review finding: on high-risk risk, once Review passes the

    code falls through to run_gate in the same tick without ever setting
    acceptance_stage back to "test". If run_gate then fails, acceptance_stage
    was left at "review" from the earlier dispatch, so retry_acceptance
    (which pops only runbook.acceptance_stage's own evidence entry) deleted
    the *passing* review evidence instead of the failing test evidence, and
    the next tick dispatched a wasteful duplicate review Task.
    """

    repo = _git_repo(tmp_path)
    state = State(":memory:")
    rb, task = _implementation_runbook(state, repo)
    acceptance.start_acceptance(runbook=rb, task=task)
    rb.acceptance_stage = acceptance.STAGE_REVIEW
    review_task = Task(
        id="review-3", task_ref="review-3", role="diff-review", worker="grok-build-review",
        state=TASK_SUCCEEDED, result="PASS", runbook_id=rb.id,
    )
    state.upsert_task(review_task)
    rb.acceptance_evidence = {
        **rb.acceptance_evidence,
        "review": {"status": "NOT_REPORTED", "reason": "dispatched", "task_id": review_task.id},
    }

    monkeypatch.setattr(acceptance, "classify", lambda paths: type("R", (), {"review_level": "critical"})())
    monkeypatch.setattr(
        acceptance, "_find_review_manifest",
        lambda **kwargs: ("agent-output/review-3/manifest.json", {"actual": {"provider": "xAI"}}, None),
    )
    monkeypatch.setattr(acceptance, "run_gate", _failing_run_gate)

    acceptance.advance_acceptance_pipeline(
        state=state, repo_root=repo, registry=None, scheduler=None, supervisor=None, runbook=rb, task=task,
        base_ref="HEAD",
    )

    assert rb.status == RUNBOOK_BLOCKED
    assert rb.acceptance_evidence["review"]["status"] == "PASS"  # review evidence is preserved
    assert rb.acceptance_evidence["test"]["status"] == "FAIL"
    assert rb.acceptance_stage == acceptance.STAGE_TEST  # not left at "review"

    resumed = runbooks.retry_acceptance(state=state, runbook_id=rb.id)

    assert resumed.status == RUNBOOK_ACCEPTANCE_PENDING
    # Only the failing "test" evidence is cleared; the passing review is reused.
    assert "test" not in resumed.acceptance_evidence
    assert resumed.acceptance_evidence["review"]["status"] == "PASS"
    assert resumed.acceptance_evidence["review"]["task_id"] == review_task.id

    monkeypatch.setattr(acceptance, "run_gate", _fake_run_gate)
    acceptance.advance_acceptance_pipeline(
        state=state, repo_root=repo, registry=None, scheduler=None, supervisor=None, runbook=rb, task=task,
        base_ref="HEAD",
    )

    # Retrying test must reuse the still-valid review evidence -- no second
    # review Task is ever dispatched.
    assert len([t for t in state.list_tasks() if t.role == "diff-review"]) == 1
    assert rb.acceptance_evidence["test"]["status"] == "PASS"
    assert rb.acceptance_stage == acceptance.STAGE_CHECKPOINT


def test_repaired_candidate_after_test_halt_re_dispatches_a_stale_review(tmp_path, monkeypatch):
    """Independent-review finding (round 4): a stored Review PASS is only

    valid for the exact tree it was computed against. If an operator repairs
    the candidate after a Test halt (producing a *new* tree), the stale
    Review PASS must never be reused for the new tree -- run_gate's own
    candidate_tree_sha check would reject it anyway (never a false
    SUCCEEDED), but without this fix nothing would ever re-dispatch a fresh
    review, deadlocking every subsequent resume on the same stale evidence.
    """

    repo = _git_repo(tmp_path)
    state = State(":memory:")
    rb, task = _implementation_runbook(state, repo)
    acceptance.start_acceptance(runbook=rb, task=task)

    monkeypatch.setattr(acceptance, "classify", lambda paths: type("R", (), {"review_level": "critical"})())
    monkeypatch.setattr(
        acceptance, "_find_review_manifest",
        lambda **kwargs: ("agent-output/review-5/manifest.json", {"actual": {"provider": "xAI"}}, None),
    )
    registry = load_registry()
    scheduler = Scheduler()

    # Tick 1: dispatch review for tree T1.
    monkeypatch.setattr(acceptance, "run_gate", _failing_run_gate)
    acceptance.advance_acceptance_pipeline(
        state=state, repo_root=repo, registry=registry, scheduler=scheduler, supervisor=FakeSupervisor(),
        runbook=rb, task=task, base_ref="HEAD",
    )
    first_review_task_id = rb.acceptance_evidence["review"]["task_id"]
    first_review_task = state.get_task(first_review_task_id)
    first_review_task.state = TASK_SUCCEEDED
    state.upsert_task(first_review_task)

    # Tick 2: review PASSes for T1, then run_gate fails Test -> BLOCKED at "test".
    acceptance.advance_acceptance_pipeline(
        state=state, repo_root=repo, registry=registry, scheduler=scheduler, supervisor=FakeSupervisor(),
        runbook=rb, task=task, base_ref="HEAD",
    )
    assert rb.status == RUNBOOK_BLOCKED
    assert rb.acceptance_evidence["review"]["status"] == "PASS"
    tree_t1 = rb.acceptance_evidence["review"]["tree_sha"]

    # Operator repairs the candidate: a genuinely different tree (T2).
    (repo / "seed.txt").write_text("repaired\n", encoding="utf-8")
    runbooks.retry_acceptance(state=state, runbook_id=rb.id)

    # Tick 3: Test is retried on the new tree; the stale T1 review must be
    # dropped and a fresh review Task dispatched for T2 -- not reused, and
    # not misreported as a failed review for the new tree.
    acceptance.advance_acceptance_pipeline(
        state=state, repo_root=repo, registry=registry, scheduler=scheduler, supervisor=FakeSupervisor(),
        runbook=rb, task=task, base_ref="HEAD",
    )

    assert rb.acceptance_stage == acceptance.STAGE_REVIEW
    second_review_task_id = rb.acceptance_evidence["review"]["task_id"]
    assert second_review_task_id != first_review_task_id
    assert rb.acceptance_evidence["review"]["status"] == "NOT_REPORTED"
    assert len([t for t in state.list_tasks() if t.role == "diff-review"]) == 2

    # Tick 4: the new review passes for T2 and it is genuinely a new tree.
    second_review_task = state.get_task(second_review_task_id)
    second_review_task.state = TASK_SUCCEEDED
    state.upsert_task(second_review_task)
    monkeypatch.setattr(acceptance, "run_gate", _fake_run_gate)
    acceptance.advance_acceptance_pipeline(
        state=state, repo_root=repo, registry=registry, scheduler=scheduler, supervisor=FakeSupervisor(),
        runbook=rb, task=task, base_ref="HEAD",
    )

    assert rb.acceptance_evidence["review"]["status"] == "PASS"
    assert rb.acceptance_evidence["review"]["tree_sha"] != tree_t1
    assert rb.acceptance_evidence["test"]["status"] == "PASS"
    assert rb.acceptance_stage == acceptance.STAGE_CHECKPOINT


# --------------------------------------------------------------------------- Checkpoint stage


def test_checkpoint_commits_and_pushes_a_dirty_worktree(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    (repo / "seed.txt").write_text("changed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    state = State(":memory:")
    rb, task = _implementation_runbook(state, repo)
    rb.acceptance_stage = acceptance.STAGE_CHECKPOINT

    real_run = acceptance._run

    def fake_run(argv, cwd, *, timeout=60):
        if argv[:2] == ["git", "push"]:
            return subprocess.CompletedProcess(argv, 0, stdout="pushed (fake remote)", stderr="")
        return real_run(argv, cwd, timeout=timeout)

    monkeypatch.setattr(acceptance, "_run", fake_run)

    acceptance.advance_acceptance_pipeline(
        state=state, repo_root=repo, registry=None, scheduler=None, supervisor=None, runbook=rb, task=task,
    )

    assert rb.acceptance_evidence["checkpoint"]["status"] == "PASS"
    assert rb.acceptance_stage == acceptance.STAGE_PR_READINESS
    assert rb.status == RUNBOOK_ACCEPTANCE_PENDING
    assert not acceptance._worktree_is_dirty(repo)


def test_checkpoint_never_commits_on_a_protected_or_detached_branch(tmp_path):
    """Independent-review finding (round 3): the branch guard must run before

    any commit, not only before the push -- a dirty worktree on main/master/
    detached HEAD must never get a local checkpoint commit at all.
    """

    repo = _git_repo(tmp_path, branch="main")
    (repo / "seed.txt").write_text("changed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    head_before = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    state = State(":memory:")
    rb, task = _implementation_runbook(state, repo)
    rb.acceptance_stage = acceptance.STAGE_CHECKPOINT

    acceptance.advance_acceptance_pipeline(
        state=state, repo_root=repo, registry=None, scheduler=None, supervisor=None, runbook=rb, task=task,
    )

    head_after = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert head_before == head_after  # no commit was made
    assert rb.status == RUNBOOK_OWNER_ACTION_REQUIRED
    assert rb.acceptance_evidence["checkpoint"]["status"] == "FAIL"


def test_has_unpushed_commits_fails_closed_when_rev_list_is_unreadable(tmp_path, monkeypatch):
    """Independent-review finding (round 3): a failed/timed-out rev-list

    (the exact-tree ahead/behind check) must be treated as "unpushed" rather
    than "nothing to push" -- fail-closed, not fail-open, matching the
    no-upstream-configured branch just above it.
    """

    repo = _git_repo(tmp_path)

    def fake_git(cwd, *args, timeout=30):
        if args[:1] == ("rev-list",):
            return None  # simulates a failed/timed-out rev-list
        if args[:3] == ("rev-parse", "--abbrev-ref", "--symbolic-full-name"):
            return "origin/eng/acceptance-test"  # an upstream *is* configured
        raise AssertionError(f"unexpected git invocation in this test: {args}")

    monkeypatch.setattr(acceptance, "_git", fake_git)

    assert acceptance._has_unpushed_commits(repo) is True


def test_checkpoint_push_failure_is_owner_action_required_not_success(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    (repo / "seed.txt").write_text("changed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    state = State(":memory:")
    rb, task = _implementation_runbook(state, repo)
    rb.acceptance_stage = acceptance.STAGE_CHECKPOINT

    real_run = acceptance._run

    def fake_run(argv, cwd, *, timeout=60):
        if argv[:2] == ["git", "push"]:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="remote: permission denied")
        return real_run(argv, cwd, timeout=timeout)

    monkeypatch.setattr(acceptance, "_run", fake_run)

    acceptance.advance_acceptance_pipeline(
        state=state, repo_root=repo, registry=None, scheduler=None, supervisor=None, runbook=rb, task=task,
    )

    assert rb.status == RUNBOOK_OWNER_ACTION_REQUIRED
    assert rb.status != RUNBOOK_SUCCEEDED
    assert "operator authority" in rb.acceptance_evidence["checkpoint"]["reason"]
    assert rb.acceptance_stage == acceptance.STAGE_CHECKPOINT


def test_clean_but_unpushed_worktree_checkpoint_still_pushes(tmp_path, monkeypatch):
    """A clean worktree with no unpushed commits is a true no-op PASS, but a

    clean worktree whose HEAD was never pushed (e.g. a resumed checkpoint
    after an earlier push failure) must still attempt the push rather than
    reporting PASS purely because there is nothing left to commit.
    """

    repo = _git_repo(tmp_path)
    state = State(":memory:")
    rb, task = _implementation_runbook(state, repo)
    rb.acceptance_stage = acceptance.STAGE_CHECKPOINT
    head_before = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()

    real_run = acceptance._run
    push_calls = []

    def fake_run(argv, cwd, *, timeout=60):
        if argv[:2] == ["git", "push"]:
            push_calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout="pushed (fake remote)", stderr="")
        return real_run(argv, cwd, timeout=timeout)

    monkeypatch.setattr(acceptance, "_run", fake_run)

    acceptance.advance_acceptance_pipeline(
        state=state, repo_root=repo, registry=None, scheduler=None, supervisor=None, runbook=rb, task=task,
    )

    head_after = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert rb.acceptance_evidence["checkpoint"]["status"] == "PASS"
    assert head_before == head_after  # nothing to commit -- push is the only action
    assert push_calls  # but the push must still have been attempted
    assert rb.acceptance_stage == acceptance.STAGE_PR_READINESS


def test_resuming_a_failed_push_retries_the_push_not_just_the_commit(tmp_path, monkeypatch):
    """ENG-AGENT-13 code-review finding: a resumed checkpoint after a push

    failure must not report PASS merely because the worktree is clean (the
    commit already succeeded locally) -- it must actually retry the push.
    """

    repo = _git_repo(tmp_path)
    (repo / "seed.txt").write_text("changed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    state = State(":memory:")
    rb, task = _implementation_runbook(state, repo)
    rb.acceptance_stage = acceptance.STAGE_CHECKPOINT

    real_run = acceptance._run
    push_attempts = {"n": 0}

    def failing_push_run(argv, cwd, *, timeout=60):
        if argv[:2] == ["git", "push"]:
            push_attempts["n"] += 1
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="remote: permission denied")
        return real_run(argv, cwd, timeout=timeout)

    monkeypatch.setattr(acceptance, "_run", failing_push_run)
    acceptance.advance_acceptance_pipeline(
        state=state, repo_root=repo, registry=None, scheduler=None, supervisor=None, runbook=rb, task=task,
    )
    assert rb.status == RUNBOOK_OWNER_ACTION_REQUIRED
    assert push_attempts["n"] == 1
    # The commit itself already landed locally; the worktree is now clean.
    assert not acceptance._worktree_is_dirty(repo)

    resumed = runbooks.retry_acceptance(state=state, runbook_id=rb.id)
    assert resumed.status == RUNBOOK_ACCEPTANCE_PENDING

    def succeeding_push_run(argv, cwd, *, timeout=60):
        if argv[:2] == ["git", "push"]:
            push_attempts["n"] += 1
            return subprocess.CompletedProcess(argv, 0, stdout="pushed (fake remote)", stderr="")
        return real_run(argv, cwd, timeout=timeout)

    monkeypatch.setattr(acceptance, "_run", succeeding_push_run)
    acceptance.advance_acceptance_pipeline(
        state=state, repo_root=repo, registry=None, scheduler=None, supervisor=None, runbook=rb, task=task,
    )

    assert rb.acceptance_evidence["checkpoint"]["status"] == "PASS"
    assert push_attempts["n"] == 2  # the failed attempt, then the real retry
    assert rb.acceptance_stage == acceptance.STAGE_PR_READINESS


# --------------------------------------------------------------------------- PR readiness stage


def test_pr_readiness_creates_a_pr_when_none_exists(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    state = State(":memory:")
    rb, task = _implementation_runbook(state, repo)
    rb.acceptance_stage = acceptance.STAGE_PR_READINESS
    real_run = acceptance._run

    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    calls = []
    view_calls = {"n": 0}

    def fake_run(argv, cwd, *, timeout=60):
        calls.append(argv)
        if argv[:3] == ["gh", "pr", "view"]:
            view_calls["n"] += 1
        if argv[:3] == ["gh", "pr", "view"] and view_calls["n"] == 1:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="no pull requests found")
        if argv[:3] == ["gh", "pr", "create"]:
            return subprocess.CompletedProcess(argv, 0, stdout="https://github.com/x/y/pull/1", stderr="")
        if argv[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(
                argv, 0,
                stdout=f'{{"number": 1, "state": "OPEN", "url": "https://github.com/x/y/pull/1", "headRefOid": "{head}"}}',
                stderr="",
            )
        return real_run(argv, cwd, timeout=timeout)

    monkeypatch.setattr(acceptance, "_run", fake_run)

    acceptance.advance_acceptance_pipeline(
        state=state, repo_root=repo, registry=None, scheduler=None, supervisor=None, runbook=rb, task=task,
    )

    assert rb.acceptance_evidence["pr_readiness"]["status"] == "PASS"
    assert rb.acceptance_evidence["pr_readiness"]["pr_number"] == 1
    assert rb.acceptance_stage == acceptance.STAGE_DONE
    assert any(c[:3] == ["gh", "pr", "create"] for c in calls)


def test_pr_readiness_failure_is_owner_action_required(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    state = State(":memory:")
    rb, task = _implementation_runbook(state, repo)
    rb.acceptance_stage = acceptance.STAGE_PR_READINESS
    real_run = acceptance._run

    def fake_run(argv, cwd, *, timeout=60):
        if argv[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="no pull requests found")
        if argv[:3] == ["gh", "pr", "create"]:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="gh: not authenticated")
        return real_run(argv, cwd, timeout=timeout)

    monkeypatch.setattr(acceptance, "_run", fake_run)

    acceptance.advance_acceptance_pipeline(
        state=state, repo_root=repo, registry=None, scheduler=None, supervisor=None, runbook=rb, task=task,
    )

    assert rb.status == RUNBOOK_OWNER_ACTION_REQUIRED
    assert rb.status != RUNBOOK_SUCCEEDED
    assert rb.acceptance_stage == acceptance.STAGE_PR_READINESS


def test_stale_merged_pr_for_a_reused_branch_does_not_satisfy_pr_readiness(tmp_path, monkeypatch):
    """Independent-review finding (round 3): gh pr view can return a MERGED

    PR for a reused branch name. A MERGED PR is not currently open, so it
    must never satisfy PR readiness -- a new PR must be opened instead.
    """

    repo = _git_repo(tmp_path)
    state = State(":memory:")
    rb, task = _implementation_runbook(state, repo)
    rb.acceptance_stage = acceptance.STAGE_PR_READINESS
    real_run = acceptance._run
    calls = {"view": 0}

    def fake_run(argv, cwd, *, timeout=60):
        if argv[:3] == ["gh", "pr", "view"]:
            calls["view"] += 1
            if calls["view"] == 1:
                return subprocess.CompletedProcess(
                    argv, 0,
                    stdout='{"number": 5, "state": "MERGED", "url": "https://x/5", "headRefOid": "stale-sha"}',
                    stderr="",
                )
            head = real_run(["git", "rev-parse", "HEAD"], cwd, timeout=timeout).stdout.strip()
            return subprocess.CompletedProcess(
                argv, 0, stdout=f'{{"number": 6, "state": "OPEN", "url": "https://x/6", "headRefOid": "{head}"}}',
                stderr="",
            )
        if argv[:3] == ["gh", "pr", "create"]:
            return subprocess.CompletedProcess(argv, 0, stdout="https://x/6", stderr="")
        return real_run(argv, cwd, timeout=timeout)

    monkeypatch.setattr(acceptance, "_run", fake_run)

    acceptance.advance_acceptance_pipeline(
        state=state, repo_root=repo, registry=None, scheduler=None, supervisor=None, runbook=rb, task=task,
    )

    assert rb.acceptance_evidence["pr_readiness"]["status"] == "PASS"
    assert rb.acceptance_evidence["pr_readiness"]["pr_number"] == 6  # the new PR, not the stale merged one


def test_open_pr_with_mismatched_head_sha_does_not_satisfy_pr_readiness(tmp_path, monkeypatch):
    """Independent-review finding (round 3): an OPEN PR whose head SHA does

    not match the checkpointed HEAD must not be accepted as PASS -- it does
    not actually contain the exact-tree candidate this pipeline just pushed.
    """

    repo = _git_repo(tmp_path)
    state = State(":memory:")
    rb, task = _implementation_runbook(state, repo)
    rb.acceptance_stage = acceptance.STAGE_PR_READINESS
    real_run = acceptance._run

    def fake_run(argv, cwd, *, timeout=60):
        if argv[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(
                argv, 0, stdout='{"number": 7, "state": "OPEN", "url": "https://x/7", "headRefOid": "not-the-real-head"}',
                stderr="",
            )
        return real_run(argv, cwd, timeout=timeout)

    monkeypatch.setattr(acceptance, "_run", fake_run)

    acceptance.advance_acceptance_pipeline(
        state=state, repo_root=repo, registry=None, scheduler=None, supervisor=None, runbook=rb, task=task,
    )

    assert rb.status == RUNBOOK_OWNER_ACTION_REQUIRED
    assert rb.status != RUNBOOK_SUCCEEDED
    assert rb.acceptance_evidence["pr_readiness"]["status"] == "FAIL"
    assert "does not match" in rb.acceptance_evidence["pr_readiness"]["reason"]


# --------------------------------------------------------------------------- end to end + recovery


def test_all_stages_pass_or_not_applicable_before_terminal_succeeded(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    state = State(":memory:")
    rb, task = _implementation_runbook(state, repo)
    acceptance.start_acceptance(runbook=rb, task=task)

    monkeypatch.setattr(acceptance, "classify", lambda paths: type("R", (), {"review_level": "low"})())
    monkeypatch.setattr(acceptance, "run_gate", _fake_run_gate)

    # PR readiness's first ``gh pr view`` finds nothing, so it creates one;
    # the second (after create) must report the newly opened PR.
    calls = {"n": 0}

    def fake_run_view_then_create(argv, cwd, *, timeout=60):
        if argv[:3] == ["gh", "pr", "view"]:
            calls["n"] += 1
            if calls["n"] == 1:
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="no pull requests found")
            current_head = real_run(["git", "rev-parse", "HEAD"], cwd, timeout=timeout).stdout.strip()
            return subprocess.CompletedProcess(
                argv, 0,
                stdout=f'{{"number": 9, "state": "OPEN", "url": "https://github.com/x/y/pull/9", "headRefOid": "{current_head}"}}',
                stderr="",
            )
        if argv[:3] == ["gh", "pr", "create"]:
            return subprocess.CompletedProcess(argv, 0, stdout="https://github.com/x/y/pull/9", stderr="")
        if argv[:2] == ["git", "push"]:
            return subprocess.CompletedProcess(argv, 0, stdout="pushed (fake remote)", stderr="")
        return real_run(argv, cwd, timeout=timeout)

    real_run = acceptance._run
    monkeypatch.setattr(acceptance, "_run", fake_run_view_then_create)

    for _ in range(4):
        if rb.status == RUNBOOK_SUCCEEDED:
            break
        acceptance.advance_acceptance_pipeline(
            state=state, repo_root=repo, registry=None, scheduler=None, supervisor=None, runbook=rb, task=task,
            base_ref="HEAD",
        )

    assert rb.status == RUNBOOK_SUCCEEDED
    for stage_name in ("implementation", "test", "review", "checkpoint", "pr_readiness"):
        assert rb.acceptance_evidence[stage_name]["status"] in ("PASS", "NOT_APPLICABLE")


def test_retry_acceptance_resumes_a_halted_stage_without_rerunning_implementation(tmp_path):
    repo = _git_repo(tmp_path)
    state = State(":memory:")
    rb, task = _implementation_runbook(state, repo)
    rb.status = RUNBOOK_OWNER_ACTION_REQUIRED
    rb.acceptance_stage = acceptance.STAGE_CHECKPOINT
    rb.acceptance_evidence = {
        "implementation": {"status": "PASS", "reason": "ok"},
        "test": {"status": "PASS", "reason": "ok"},
        "review": {"status": "NOT_APPLICABLE", "reason": "low risk"},
        "checkpoint": {"status": "FAIL", "reason": "push requires operator authority"},
    }
    state.upsert_runbook(rb)

    resumed = runbooks.retry_acceptance(state=state, runbook_id=rb.id)

    assert resumed.status == RUNBOOK_ACCEPTANCE_PENDING
    assert resumed.acceptance_stage == acceptance.STAGE_CHECKPOINT
    # The stage that failed is cleared for a fresh attempt; earlier passed
    # stages are left untouched (reused, not recomputed).
    assert "checkpoint" not in resumed.acceptance_evidence
    assert resumed.acceptance_evidence["test"]["status"] == "PASS"
    refreshed_task = state.get_task(task.id)
    assert refreshed_task.state == TASK_SUCCEEDED  # never rerun


def test_review_only_preset_never_enters_the_acceptance_pipeline(tmp_path):
    repo = _git_repo(tmp_path)
    state = State(":memory:")
    task = Task(
        id="review-only-session", task_ref="V-REVIEW", role="diff-review", worker="grok-build-review",
        state=TASK_SUCCEEDED, result="PASS", worktree=str(repo),
    )
    state.upsert_task(task)
    rb = runbooks.create_runbook(
        state=state, registry=load_registry(), name="review only", preset="review-only",
        source_ref="V-REVIEW", branch="eng/acceptance-test", worktree=str(repo),
    )
    rb.task_id = task.id
    rb.status = RUNBOOK_RUNNING
    state.upsert_runbook(rb)

    result = runbooks.reconcile_runbooks(state=state, repo_root=repo)

    assert result["finalized"] == 1
    finalized = state.get_runbook(rb.id)
    assert finalized.status == RUNBOOK_SUCCEEDED
    assert finalized.acceptance_evidence == {}


# --------------------------------------------------------------------------- ENG-AGENT-15 runtime freshness


def _write_acceptance_module_stub(repo_root: Path, *, contract_version: str) -> None:
    module_dir = repo_root / "scripts" / "agents" / "control_plane"
    module_dir.mkdir(parents=True, exist_ok=True)
    (module_dir / "acceptance.py").write_text(
        f'ACCEPTANCE_PIPELINE_CONTRACT_VERSION = "{contract_version}"\n', encoding="utf-8"
    )


def test_check_runtime_freshness_is_none_when_nothing_is_on_disk(tmp_path):
    """A repo_root fixture with no such file (most existing tests) is never

    itself evidence of staleness -- only a real, differing on-disk value is.
    """

    assert acceptance.check_runtime_freshness(repo_root=tmp_path) is None


def test_check_runtime_freshness_is_none_when_disk_matches_the_loaded_module(tmp_path):
    _write_acceptance_module_stub(tmp_path, contract_version=acceptance.ACCEPTANCE_PIPELINE_CONTRACT_VERSION)
    assert acceptance.check_runtime_freshness(repo_root=tmp_path) is None


def test_check_runtime_freshness_detects_a_process_running_behind_disk(tmp_path):
    """ENG-AGENT-15 (issue #142): reproduces the actual incident mechanism --

    a process that loaded an older acceptance contract than what is currently
    checked out at ``repo_root`` (e.g. a daemon merged past while it kept
    running) must be told, truthfully, that it is stale.
    """

    _write_acceptance_module_stub(tmp_path, contract_version="some-future-contract")

    reason = acceptance.check_runtime_freshness(repo_root=tmp_path)

    assert reason is not None
    assert "stale" in reason.lower() or "restart" in reason.lower()
    assert acceptance.ACCEPTANCE_PIPELINE_CONTRACT_VERSION in reason
    assert "some-future-contract" in reason


# --------------------------------------------------------------------------- ENG-AGENT-16 (issue #146)
#
# Candidate-tree/changed-path identity must never be contaminated by Control
# Plane runtime/evidence writes (.agent-output/, .orchestrator-state/,
# .local-gate/), including on a worktree whose tracked .gitignore -- like
# ``_git_repo``'s fixture below, which writes none at all -- predates every
# one of those directories.


def _write_cp_runtime_evidence(worktree: Path, *, stamp: str) -> None:
    for name in CP_RUNTIME_DIRNAMES:
        directory = worktree / name
        directory.mkdir(exist_ok=True)
        (directory / f"{stamp}.json").write_text("{}", encoding="utf-8")


def test_cp_runtime_writes_between_ticks_never_leak_into_review_scope_or_move_the_candidate(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    state = State(":memory:")
    rb, task = _implementation_runbook(state, repo)
    acceptance.start_acceptance(runbook=rb, task=task)

    monkeypatch.setattr(acceptance, "classify", lambda paths: type("R", (), {"review_level": "critical"})())
    registry = load_registry()
    scheduler = Scheduler()

    # Simulate the Test stage's own runtime writes happening before review is
    # even dispatched -- exactly the ENG-AGENT-13 acceptance pipeline order.
    _write_cp_runtime_evidence(repo, stamp="pre-dispatch")

    acceptance.advance_acceptance_pipeline(
        state=state, repo_root=repo, registry=registry, scheduler=scheduler, supervisor=FakeSupervisor(),
        runbook=rb, task=task, base_ref="HEAD",
    )

    review_task_id = rb.acceptance_evidence["review"]["task_id"]
    review_task = state.get_task(review_task_id)
    scope_entries = {c.removeprefix("scope:") for c in review_task.command if c.startswith("scope:")}
    assert not any(entry.split("/")[0] in CP_RUNTIME_DIRNAMES for entry in scope_entries)
    # This fixture's HEAD already equals base_ref, so the whole-candidate
    # fallback applies -- the runtime directories still must never appear in it.
    assert scope_entries == {"seed.txt"}

    # More runtime evidence lands (the review Task's own dispatch/eval writes)
    # before it is observed SUCCEEDED on a later tick.
    _write_cp_runtime_evidence(repo, stamp="post-dispatch")
    review_task.state = TASK_SUCCEEDED
    state.upsert_task(review_task)
    monkeypatch.setattr(
        acceptance, "_find_review_manifest",
        lambda **kwargs: ("agent-output/review/manifest.json", {"actual": {"provider": "xAI"}}, None),
    )
    captured_test_gate = {}

    def _capturing_run_gate(**kwargs):
        captured_test_gate.update(kwargs)
        return {"result": "pass", "evidence_path": ".local-gate/fake.json"}

    monkeypatch.setattr(acceptance, "run_gate", _capturing_run_gate)

    acceptance.advance_acceptance_pipeline(
        state=state, repo_root=repo, registry=registry, scheduler=scheduler, supervisor=FakeSupervisor(),
        runbook=rb, task=task, base_ref="HEAD",
    )

    # Test and Review evidence are bound to the identical product candidate:
    # neither round of runtime writes moved the tree the review PASS covers.
    assert rb.acceptance_evidence["review"]["status"] == "PASS"
    assert rb.acceptance_evidence["test"]["status"] == "PASS"
    assert rb.acceptance_evidence["test"]["tree_sha"] == rb.acceptance_evidence["review"]["tree_sha"]


def test_checkpoint_never_commits_cp_runtime_artifacts(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    (repo / "real_change.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    _write_cp_runtime_evidence(repo, stamp="checkpoint-run")
    state = State(":memory:")
    rb, task = _implementation_runbook(state, repo)
    rb.acceptance_stage = acceptance.STAGE_CHECKPOINT

    real_run = acceptance._run

    def fake_run(argv, cwd, *, timeout=60):
        if argv[:2] == ["git", "push"]:
            return subprocess.CompletedProcess(argv, 0, stdout="pushed (fake remote)", stderr="")
        return real_run(argv, cwd, timeout=timeout)

    monkeypatch.setattr(acceptance, "_run", fake_run)

    acceptance.advance_acceptance_pipeline(
        state=state, repo_root=repo, registry=None, scheduler=None, supervisor=None, runbook=rb, task=task,
    )

    assert rb.acceptance_evidence["checkpoint"]["status"] == "PASS"
    committed_files = subprocess.run(
        ["git", "-C", str(repo), "show", "--stat", "--name-only", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "real_change.py" in committed_files
    assert not any(name in committed_files for name in CP_RUNTIME_DIRNAMES)
    # The runtime directories are still on disk (never deleted) -- merely
    # never committed and never read as making the worktree dirty.
    for name in CP_RUNTIME_DIRNAMES:
        assert (repo / name).is_dir()
    assert not acceptance._worktree_is_dirty(repo)


def test_restart_reconciliation_is_idempotent_and_never_double_dispatches_review(tmp_path, monkeypatch):
    """Restart/reconciliation: repeated ticks (as a restarted daemon's

    reconcile loop would produce), each preceded by fresh CP runtime writes
    (a stand-in for the daemon re-provisioning/re-evaluating the worktree on
    restart), must never create a second review Task nor change which tree
    the pipeline is validating.
    """

    repo = _git_repo(tmp_path)
    state = State(":memory:")
    rb, task = _implementation_runbook(state, repo)
    acceptance.start_acceptance(runbook=rb, task=task)

    monkeypatch.setattr(acceptance, "classify", lambda paths: type("R", (), {"review_level": "critical"})())
    registry = load_registry()
    scheduler = Scheduler()

    for stamp in ("restart-1", "restart-2", "restart-3"):
        _write_cp_runtime_evidence(repo, stamp=stamp)
        acceptance.advance_acceptance_pipeline(
            state=state, repo_root=repo, registry=registry, scheduler=scheduler, supervisor=FakeSupervisor(),
            runbook=rb, task=task, base_ref="HEAD",
        )

    review_tasks = [t for t in state.list_tasks() if t.role == "diff-review" and t.runbook_id == rb.id]
    assert len(review_tasks) == 1
    assert rb.acceptance_evidence["review"]["status"] == "NOT_REPORTED"
    assert rb.status == RUNBOOK_ACCEPTANCE_PENDING
