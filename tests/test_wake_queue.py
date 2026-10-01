"""ENG-PC-03 (issue #31): durable wake queue and trigger coalescing.

Exercises coalescing under real concurrent writers (separate processes'-worth of
connections against one on-disk database, not a mocked queue), restart durability,
bounded retry/backoff into an explicit poisoned state, and -- the acceptance criterion
this program has twice named explicitly -- that coalescing a burst of duplicate wakes
plus draining them never causes the existing ``reconcile_runbooks`` advancement path to
finalize a runbook more than once.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from scripts.agents.control_plane import runbooks, wake_queue
from scripts.agents.control_plane.models import (
    RUNBOOK_RUNNING,
    RUNBOOK_SUCCEEDED,
    WAKE_CLAIMED,
    WAKE_COMPLETED,
    WAKE_FAILED,
    WAKE_PENDING,
    WAKE_POISONED,
    WAKE_REASONS,
    Task,
)
from scripts.agents.control_plane.state import State
from scripts.agents.registry import load_registry


def test_wake_reason_contract_matches_the_issue():
    assert WAKE_REASONS == {
        "TASK_ELIGIBLE",
        "TASK_UNBLOCKED",
        "IMPLEMENTATION_FINISHED",
        "TEST_FINISHED",
        "REVIEW_FINISHED",
        "APPROVAL_RESOLVED",
        "PROVIDER_RECOVERED",
        "QUOTA_RESET",
        "SCHEDULE",
        "OVERNIGHT_TICK",
        "MANUAL",
    }


def test_enqueue_rejects_an_untyped_reason():
    state = State(":memory:")
    with pytest.raises(wake_queue.InvalidWakeReason):
        wake_queue.enqueue(state, reason="SOMETHING_ELSE", source="test")


def test_single_enqueue_creates_a_pending_wake_with_one_contribution():
    state = State(":memory:")
    wake = wake_queue.enqueue(
        state, reason="TASK_ELIGIBLE", source="test.single", project_id="p1", task_id="t1", stage="implement"
    )
    assert wake.status == WAKE_PENDING
    assert wake.coalesced_count == 1
    assert wake.attempts == 0
    contributions = state.list_wake_contributions(wake.id)
    assert len(contributions) == 1
    assert contributions[0].reason == "TASK_ELIGIBLE"
    assert contributions[0].source == "test.single"


def test_duplicate_pending_wakes_coalesce_and_retain_reasons_and_provenance():
    state = State(":memory:")
    wake_queue.enqueue(state, reason="TASK_ELIGIBLE", source="a", project_id="p1", task_id="t1", stage="implement")
    second = wake_queue.enqueue(
        state, reason="TASK_UNBLOCKED", source="b", project_id="p1", task_id="t1", stage="implement"
    )
    third = wake_queue.enqueue(
        state, reason="TASK_UNBLOCKED", source="c", project_id="p1", task_id="t1", stage="implement"
    )
    assert second.id == third.id  # same coalesced row, not a new one
    assert third.coalesced_count == 3
    assert third.reason == "TASK_UNBLOCKED"  # most recent, for quick display
    contributions = state.list_wake_contributions(third.id)
    assert [c.reason for c in contributions] == ["TASK_ELIGIBLE", "TASK_UNBLOCKED", "TASK_UNBLOCKED"]
    assert [c.source for c in contributions] == ["a", "b", "c"]
    # Exactly one PENDING row exists for this key -- the queue, not a log.
    assert len(state.list_wakes(project_id="p1", status=WAKE_PENDING)) == 1


def test_a_different_task_or_stage_never_coalesces_with_an_unrelated_wake():
    state = State(":memory:")
    a = wake_queue.enqueue(state, reason="TASK_ELIGIBLE", source="x", project_id="p1", task_id="t1", stage="implement")
    b = wake_queue.enqueue(state, reason="TASK_ELIGIBLE", source="x", project_id="p1", task_id="t2", stage="implement")
    c = wake_queue.enqueue(state, reason="TASK_ELIGIBLE", source="x", project_id="p1", task_id="t1", stage="test")
    assert len({a.id, b.id, c.id}) == 3


def test_completing_a_wake_lets_a_fresh_trigger_start_a_new_row_rather_than_being_dropped():
    state = State(":memory:")
    first = wake_queue.enqueue(state, reason="TASK_ELIGIBLE", source="x", task_id="t1", stage="implement")
    claimed = wake_queue.claim(state, claimed_by="daemon")
    assert claimed.id == first.id
    assert wake_queue.complete(state, wake_id=claimed.id)
    second = wake_queue.enqueue(state, reason="SCHEDULE", source="y", task_id="t1", stage="implement")
    assert second.id != first.id
    assert second.coalesced_count == 1


def test_real_concurrent_enqueue_across_separate_connections_coalesces_without_loss(tmp_path: Path):
    """Real concurrent writers, not a mock: each worker opens its own ``State`` (and
    therefore its own SQLite connection) against the same on-disk database file."""

    db_path = tmp_path / "state.db"
    State(db_path).close()  # create the schema once up front

    def contribute(i: int) -> None:
        worker_state = State(db_path)
        try:
            wake_queue.enqueue(
                worker_state, reason="TASK_ELIGIBLE", source=f"worker-{i}",
                project_id="p1", task_id="t1", stage="implement",
            )
        finally:
            worker_state.close()

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(contribute, range(40)))

    verify_state = State(db_path)
    pending = verify_state.list_wakes(project_id="p1", status=WAKE_PENDING)
    assert len(pending) == 1
    assert pending[0].coalesced_count == 40
    assert len(verify_state.list_wake_contributions(pending[0].id)) == 40


def test_claim_picks_oldest_due_wake_and_never_double_claims():
    state = State(":memory:")
    first = wake_queue.enqueue(state, reason="TASK_ELIGIBLE", source="x", task_id="t1", stage="implement")
    second = wake_queue.enqueue(state, reason="TASK_ELIGIBLE", source="x", task_id="t2", stage="implement")
    claimed_first = wake_queue.claim(state, claimed_by="daemon")
    assert claimed_first.id == first.id
    claimed_second = wake_queue.claim(state, claimed_by="daemon")
    assert claimed_second.id == second.id
    assert wake_queue.claim(state, claimed_by="daemon") is None


def test_bounded_backoff_eventually_poisons_and_is_never_claimed_again(monkeypatch):
    # The backoff interval itself is already covered by
    # ``test_failed_wake_is_not_claimable_before_its_backoff_elapses``; here it is
    # collapsed to zero purely so each retry is immediately due, letting this test
    # drive ``attempts`` to ``max_attempts`` deterministically without sleeping.
    monkeypatch.setattr(wake_queue, "backoff_seconds", lambda attempts: -1)
    state = State(":memory:")
    wake_queue.enqueue(state, reason="TASK_ELIGIBLE", source="x", task_id="t1", stage="implement", max_attempts=2)
    seen_backoffs: list[str | None] = []
    for _ in range(2):
        claimed = wake_queue.claim(state, claimed_by="daemon")
        assert claimed is not None
        failed = wake_queue.fail(state, wake_id=claimed.id, error="boom")
        seen_backoffs.append(failed.status)
    assert seen_backoffs == [WAKE_FAILED, WAKE_POISONED]
    # A poisoned wake is dead-letter: it is never claimed again, retried or not.
    assert wake_queue.claim(state, claimed_by="daemon") is None
    attention = wake_queue.attention(state)
    assert len(attention) == 1
    assert attention[0]["reason"] == "TASK_ELIGIBLE"
    assert attention[0]["attempts"] == 2


def test_failed_wake_is_not_claimable_before_its_backoff_elapses():
    state = State(":memory:")
    wake_queue.enqueue(state, reason="TASK_ELIGIBLE", source="x", task_id="t1", stage="implement")
    claimed = wake_queue.claim(state, claimed_by="daemon")
    wake_queue.fail(state, wake_id=claimed.id, error="transient")
    # backoff is >0s for the first retry; nothing else is pending.
    assert wake_queue.claim(state, claimed_by="daemon") is None


def test_restart_recovers_a_claimed_wake_back_to_pending(tmp_path: Path):
    db_path = tmp_path / "state.db"
    state = State(db_path)
    wake_queue.enqueue(state, reason="TASK_ELIGIBLE", source="x", task_id="t1", stage="implement")
    claimed = wake_queue.claim(state, claimed_by="daemon")
    assert claimed is not None
    state.close()

    # A fresh process/daemon instance reopens the same durable database.
    restarted = State(db_path)
    recovered = wake_queue.recover_on_restart(restarted)
    assert recovered == 1
    wake = restarted.get_wake(claimed.id)
    assert wake.status == WAKE_PENDING
    assert wake.claimed_at is None
    assert wake.claimed_by is None
    # It is genuinely claimable again, not just reset in name.
    assert wake_queue.claim(restarted, claimed_by="daemon-2").id == wake.id


def test_an_emit_failure_during_claim_never_strands_the_row_claimed():
    """A logging failure between the commit of CLAIMED and the caller's next step must
    never propagate out of ``claim()`` -- see the module's ``_emit`` containment."""

    state = State(":memory:")
    wake_queue.enqueue(state, reason="TASK_ELIGIBLE", source="x", task_id="t1", stage="implement", run_id="run-1")

    def boom(*_a, **_k):
        raise ValueError("normalize_run_event rejected a bad field")

    original_record_run_event = state.record_run_event
    state.record_run_event = boom  # type: ignore[method-assign]
    claimed = wake_queue.claim(state, claimed_by="daemon")
    assert claimed is not None
    assert claimed.status == WAKE_CLAIMED
    # No timeline entry for the failed emit, but the claim itself is real and the
    # process can still proceed to complete it -- exactly as if emit had never failed.
    state.record_run_event = original_record_run_event
    assert wake_queue.complete(state, wake_id=claimed.id)
    assert state.get_wake(claimed.id).status == WAKE_COMPLETED


def test_a_complete_failure_recovers_within_one_daemon_lifetime_not_only_on_restart(monkeypatch):
    """A ``complete_wake`` failure (e.g. a busy database under contention) must not
    strand the row CLAIMED forever within a still-running daemon -- it must be
    claimable and completable again on the very next ``drain_due`` pass, with no
    restart and no elapsed-time staleness check involved."""

    state = State(":memory:")
    wake_queue.enqueue(state, reason="TASK_ELIGIBLE", source="x", task_id="t1", stage="implement")
    claimed = wake_queue.claim(state, claimed_by="daemon")
    assert claimed is not None

    real_complete_wake = State.complete_wake

    def boom(self, *, wake_id, now=None):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(State, "complete_wake", boom)
    with pytest.raises(RuntimeError):
        wake_queue.complete(state, wake_id=claimed.id)
    # The failed completion left the row CLAIMED, not silently finalized.
    assert state.get_wake(claimed.id).status == WAKE_CLAIMED

    monkeypatch.setattr(State, "complete_wake", real_complete_wake)
    # Nothing else is PENDING, so any claim/complete this pass performs is only this
    # recovered row -- proving the recovery path itself, not a coincidental re-claim.
    drained = wake_queue.drain_due(state, claimed_by="daemon")
    assert drained == 1
    assert state.get_wake(claimed.id).status == WAKE_COMPLETED

    recovery_events = [
        e for e in state.list_events(limit=50)
        if e.category == "wake_queue" and "reset to PENDING" in e.message
    ]
    assert len(recovery_events) == 1
    assert "prior pass" in recovery_events[0].message


def test_a_claimed_row_stranded_under_a_different_identity_is_not_touched():
    """``_recover_stranded_claims`` is identity-scoped: a row CLAIMED by some other
    claimant must never be reset just because a differently-identified pass runs."""

    state = State(":memory:")
    wake_queue.enqueue(state, reason="TASK_ELIGIBLE", source="x", task_id="t1", stage="implement")
    claimed = wake_queue.claim(state, claimed_by="daemon-other")
    assert claimed is not None
    # A drain pass under a different identity must not recover the other claim.
    drained = wake_queue.drain_due(state, claimed_by="daemon")
    assert drained == 0
    assert state.get_wake(claimed.id).status == WAKE_CLAIMED


def test_queue_depth_and_oldest_age_are_local_reads_with_no_provider_call():
    state = State(":memory:")
    assert wake_queue.queue_depth(state).as_dict() == {
        "pending": 0, "claimed": 0, "failed_awaiting_retry": 0, "poisoned": 0,
        "oldest_pending_age_seconds": None,
    }
    wake_queue.enqueue(state, reason="TASK_ELIGIBLE", source="x", task_id="t1", stage="implement")
    depth = wake_queue.queue_depth(state)
    assert depth.pending == 1
    assert depth.oldest_pending_age_seconds is not None
    assert depth.oldest_pending_age_seconds >= 0.0


def test_wake_lifecycle_is_mirrored_into_the_existing_run_event_timeline():
    state = State(":memory:")
    wake = wake_queue.enqueue(
        state, reason="TASK_ELIGIBLE", source="x", task_id="t1", stage="implement", run_id="run-1"
    )
    wake_queue.enqueue(state, reason="TASK_UNBLOCKED", source="y", task_id="t1", stage="implement", run_id="run-1")
    claimed = wake_queue.claim(state, claimed_by="daemon")
    wake_queue.complete(state, wake_id=claimed.id)

    events = state.list_run_events(run_id="run-1")
    types = [e.event_type for e in events]
    assert types == ["wake.enqueued", "wake.coalesced", "wake.claimed", "wake.completed"]
    for event in events:
        assert event.event_class == "wake"
        assert event.data["reason"] in WAKE_REASONS
    assert events[-1].data["coalesced_count"] == 2


def test_enqueue_without_a_derivable_run_id_still_queues_but_emits_no_event():
    state = State(":memory:")
    wake = wake_queue.enqueue(state, reason="SCHEDULE", source="daemon.tick")
    assert wake.status == WAKE_PENDING
    assert state.list_run_events(limit=10) == []


# --------------------------------------------------------- no duplicate advancement


def _review_only_runbook(state: State, worktree: Path, *, task_state: str):
    """A non-acceptance-applicable runbook+task pair (role != 'primary-implementation'),
    mirroring the existing ``test_reconcile_runbooks_finalizes_a_failed_task_as_failed``
    fixture shape so a single ``reconcile_runbooks`` call finalizes it directly via
    ``_TASK_TO_RUNBOOK_TERMINAL`` rather than entering the multi-stage acceptance
    pipeline -- the simplest real advancement path to prove idempotency against.
    """

    task = Task(id="t1", task_ref="x", role="diff-review", worker="grok-build-review", state=task_state)
    state.upsert_task(task)
    rb = runbooks.create_runbook(
        state=state, registry=load_registry(), name="x", preset="review-only",
        source_ref="x", branch="b", worktree=str(worktree),
    )
    rb.task_id = task.id
    rb.status = RUNBOOK_RUNNING
    state.upsert_runbook(rb)
    return rb, task


def test_coalesced_concurrent_wakes_never_cause_reconcile_runbooks_to_advance_twice(tmp_path: Path):
    """The acceptance criterion this program names explicitly: concurrent duplicate
    triggers for the same runbook must not duplicate advancement. Proven two ways at
    once -- coalescing collapses the burst into one queue row, and real concurrent
    callers of the existing ``reconcile_runbooks`` (each with its own ``State``
    connection against the same on-disk database, so ``advancement_lease``'s real OS
    flock is what arbitrates, not an in-process mock) still finalize exactly once.
    """

    db_path = tmp_path / "state.db"
    worktree = tmp_path / "wt"
    worktree.mkdir()
    setup_state = State(db_path)
    rb, task = _review_only_runbook(setup_state, worktree, task_state="SUCCEEDED")
    runbook_id = rb.id
    setup_state.close()

    def contribute(i: int) -> None:
        worker_state = State(db_path)
        try:
            wake_queue.enqueue(
                worker_state, reason="IMPLEMENTATION_FINISHED", source=f"worker-{i}",
                task_id=runbook_id, stage="finalize",
            )
        finally:
            worker_state.close()

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(contribute, range(20)))

    coalesce_check = State(db_path)
    pending = coalesce_check.list_wakes(status=WAKE_PENDING)
    assert len(pending) == 1
    assert pending[0].coalesced_count == 20
    coalesce_check.close()

    results: list[dict[str, int]] = []

    def drain_and_reconcile(_: int) -> None:
        worker_state = State(db_path)
        try:
            claimed = wake_queue.claim(worker_state, claimed_by="daemon")
            result = runbooks.reconcile_runbooks(state=worker_state, repo_root=tmp_path)
            results.append(result)
            if claimed is not None:
                wake_queue.complete(worker_state, wake_id=claimed.id)
        finally:
            worker_state.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(drain_and_reconcile, range(8)))

    total_finalized = sum(r["finalized"] for r in results)
    assert total_finalized == 1

    final_state = State(db_path)
    assert final_state.get_runbook(runbook_id).status == RUNBOOK_SUCCEEDED
    finalize_events = [
        e for e in final_state.list_events(limit=200)
        if e.category == "runbook" and "finalized as" in e.message
    ]
    assert len(finalize_events) == 1


# ------------------------------------------------------------- overnight compatibility


def test_wake_queue_activity_does_not_interfere_with_an_overnight_tick(tmp_path: Path):
    """Overnight compatibility: draining/enqueuing wakes concurrently with the existing
    overnight-session tick must not corrupt either durable table or raise."""

    from scripts.agents.control_plane import overnight

    db_path = tmp_path / "state.db"
    state = State(db_path)

    def enqueue_many() -> None:
        worker_state = State(db_path)
        try:
            for i in range(10):
                wake_queue.enqueue(
                    worker_state, reason="OVERNIGHT_TICK", source="overnight.tick", task_id=f"t{i}", stage="poll"
                )
        finally:
            worker_state.close()

    def run_ticks() -> None:
        worker_state = State(db_path)
        try:
            for _ in range(10):
                overnight.tick(state=worker_state)
        finally:
            worker_state.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda fn: fn(), [enqueue_many, run_ticks]))

    depth = wake_queue.queue_depth(state, project_id=None)
    assert depth.pending == 10
