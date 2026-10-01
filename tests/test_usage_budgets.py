"""ENG-PC-05 scope B: hierarchical budget brakes and truthful evidence."""

from __future__ import annotations

from types import SimpleNamespace

from scripts.agents.control_plane.dispatch import managed_admit
from scripts.agents.control_plane.models import (
    TASK_BLOCKED,
    TASK_RUNNING,
    ProviderState,
    Runbook,
    Task,
)
from scripts.agents.control_plane.pricing import ModelPricing, PricingLookup
from scripts.agents.control_plane.scheduler import Scheduler
from scripts.agents.control_plane.state import State
from scripts.agents.control_plane.supervisor import Supervisor
from scripts.agents.control_plane.usage_budgets import (
    CONSTRAINT_ATTEMPTS,
    CONSTRAINT_FALLBACKS,
    CONSTRAINT_METERED_CASH,
    CONSTRAINT_PROVIDER_QUOTA_RESERVE,
    CONSTRAINT_TOKENS,
    CONSTRAINT_WALL_CLOCK,
    PROGRESS_BASIS,
    SCOPE_GLOBAL,
    SCOPE_PROGRAM,
    SCOPE_PROVIDER,
    SCOPE_TASK,
    STATUS_BLOCKED,
    STATUS_UNKNOWN,
    STATUS_WARNING,
    BudgetContext,
    emit_decision,
    evaluate,
    progress,
    save_definition,
)
from scripts.agents.control_plane.usage_ledger import record_usage_attempts
from scripts.agents.control_plane.usage_telemetry import CLASS_UNKNOWN
from scripts.agents.registry import load_registry


class FixedBook:
    def lookup(self, *, provider, model):  # noqa: ARG002
        return PricingLookup(
            ModelPricing(
                catalog_model_id="test/model",
                input_per_1k_usd=1.0,
                output_per_1k_usd=1.0,
                matched_by="test",
            )
        )


class FakeSupervisor:
    def __init__(self, state):
        self.state = state
        self.launched = []

    def launch_task(self, task, *, dry_run=False):  # noqa: ARG002
        self.launched.append(task.id)
        task.state = TASK_RUNNING
        self.state.upsert_task(task)
        return task


def _worker(name="worker", *, cost_class="metered-configured"):
    return SimpleNamespace(
        name=name,
        provider="Provider",
        effective_model="model",
        default_model="model",
        execution_system="Provider CLI",
        cost_class=cost_class,
    )


def _registry(*workers):
    return SimpleNamespace(workers={worker.name: worker for worker in workers})


def _context(*, task_id="task-1", fallback=False):
    return BudgetContext(
        project_id="project-a",
        provider="Provider",
        task_id=task_id,
        task_ref="ENG-PC-05",
        run_id="runbook-1",
        is_fallback=fallback,
    )


def _attempt(
    run_id="run-1", *, quality="exact", tokens=(10, 2), cost_class="metered-configured",
    reported_cost=None, automatic=False,
):
    item = {
        "run_id": run_id,
        "worker": "worker",
        "provider": "Provider",
        "model": "model",
        "cost_class": cost_class,
        "status": "SUCCEEDED",
        "ended_at": "2026-09-26T12:01:00+00:00",
        "usage_recorded": True,
        "telemetry_quality": quality,
        "input_tokens": tokens[0],
        "output_tokens": tokens[1],
        "automatic": automatic,
    }
    if reported_cost is not None:
        item["reported_cost_usd"] = reported_cost
        item["reported_cost_source"] = "worker report"
    return item


def _record(state, attempt, *, task_id="task-1"):
    state.upsert_task(
        Task(
            id=task_id,
            task_ref="ENG-PC-05",
            role="primary-implementation",
            worker="worker",
            project_id="project-a",
        )
    )
    return record_usage_attempts(
        state,
        {
            "runbook_id": "runbook-1",
            "task_id": task_id,
            "telemetry_quality": attempt["telemetry_quality"],
            "input_tokens": attempt["input_tokens"],
            "output_tokens": attempt["output_tokens"],
            "route_history": [attempt],
            "updated_at": attempt["ended_at"],
        },
        project_id="project-a",
    )


def _budget(state, budget_id, *, scope=SCOPE_GLOBAL, key=None, constraint=CONSTRAINT_ATTEMPTS, limit=1):
    return save_definition(
        state,
        {
            "id": budget_id,
            "project_id": "project-a",
            "scope_type": scope,
            "scope_key": key,
            "constraint_type": constraint,
            "limit_value": limit,
            "warning_fraction": 0.8,
        },
    )


def _available_dispatch_state():
    state = State(":memory:")
    registry = load_registry()
    for worker in registry.workers.values():
        state.upsert_provider_state(
            ProviderState(
                name=worker.name,
                execution_system=worker.execution_system,
                provider=worker.provider,
                cost_class=worker.cost_class,
                state="AVAILABLE",
                configured=True,
            )
        )
    return state, registry


def _dispatch(state, registry, task, tmp_path):
    supervisor = FakeSupervisor(state)
    state.upsert_task(task)
    result = managed_admit(
        state=state,
        registry=registry,
        scheduler=Scheduler(),
        supervisor=supervisor,
        repo_root=tmp_path,
        task=task,
        dry_run=True,
    )
    return result, supervisor


def test_budget_blocks_launch_and_fallback_with_bounding_scope_event(tmp_path):
    state, registry = _available_dispatch_state()
    _budget(state, "task-stop", scope=SCOPE_TASK, key="ENG-PC-05", limit=0)
    task = Task(
        id="task-1",
        task_ref="ENG-PC-05",
        role="primary-implementation",
        worker="grok-build",
        project_id="project-a",
        fallback_automatic=True,
        fallback_selected_worker="grok-build",
    )

    result, supervisor = _dispatch(state, registry, task, tmp_path)

    assert result.launched is False and result.task.state == TASK_BLOCKED
    assert supervisor.launched == []
    assert "task:ENG-PC-05" in result.reason
    event = state.list_run_events(event_class="usage")[-1]
    assert event.event_type == "usage.budget_blocked"
    assert event.data["bounding_scope"] == "task:ENG-PC-05"
    assert event.data["evidence_class"] == "MEASURED"


def test_ample_budget_never_authorizes_existing_paid_overflow_refusal(tmp_path):
    state, registry = _available_dispatch_state()
    _budget(state, "ample", limit=100)
    runbook = Runbook(
        id="rb-paid",
        name="Paid",
        preset="test-fix",
        objective="must remain refused",
        source_ref="ENG-PC-05",
        branch="eng/test",
        worktree=str(tmp_path),
        parent_worker="deepseek-overflow",
        max_duration_minutes=10,
        worker_routes={"overflow": ["deepseek-overflow"]},
        project_id="project-a",
    )
    state.upsert_runbook(runbook)
    task = Task(
        id="paid",
        task_ref="ENG-PC-05",
        role="overflow",
        worker="deepseek-overflow",
        runbook_id=runbook.id,
        project_id="project-a",
    )

    result, supervisor = _dispatch(state, registry, task, tmp_path)

    assert result.launched is False
    assert supervisor.launched == []
    assert "API/paid overflow is not eligible" in result.reason


def test_narrower_scope_tightens_but_cannot_loosen_wider_scope():
    state = State(":memory:")
    registry = _registry(_worker())
    _budget(state, "global-stop", limit=0)
    _budget(state, "task-ample", scope=SCOPE_TASK, key="ENG-PC-05", limit=100)
    decision = evaluate(state, registry=registry, context=_context(), pricing_book=FixedBook())
    assert decision.status == STATUS_BLOCKED
    assert decision.bounding_scope == "global"

    state.delete_usage_budget("global-stop")
    state.delete_usage_budget("task-ample")
    _budget(state, "global-ample", limit=100)
    _budget(state, "task-stop", scope=SCOPE_TASK, key="ENG-PC-05", limit=0)
    decision = evaluate(state, registry=registry, context=_context(), pricing_book=FixedBook())
    assert decision.status == STATUS_BLOCKED
    assert decision.bounding_scope == "task:ENG-PC-05"


def test_one_unknown_row_makes_the_whole_budget_unknown_without_a_partial_sum():
    state = State(":memory:")
    worker = _worker()
    _record(state, _attempt(run_id="known-run", tokens=(70, 10)))
    _record(state, _attempt(run_id="unknown-run", quality="unknown", tokens=(None, None)))
    _budget(state, "token-budget", constraint=CONSTRAINT_TOKENS, limit=100)
    decision = evaluate(state, registry=_registry(worker), context=_context(), pricing_book=FixedBook())
    assert decision.allowed is False
    assert decision.status == STATUS_UNKNOWN
    assert decision.evidence_class == CLASS_UNKNOWN
    assert decision.evaluations[0]["value"] is None
    assert "1 matching ledger row(s) have no trustworthy total_tokens source" in decision.reason

    [token_progress] = progress(
        state, registry=_registry(worker), project_id="project-a", pricing_book=FixedBook()
    )
    assert token_progress["status"] == STATUS_UNKNOWN
    assert token_progress["value"] is None
    assert "1 matching ledger row(s) have no trustworthy total_tokens source" in token_progress["source"]

    state.delete_usage_budget("token-budget")
    _budget(
        state,
        "quota-budget",
        scope=SCOPE_PROVIDER,
        key="Provider",
        constraint=CONSTRAINT_PROVIDER_QUOTA_RESERVE,
        limit=10,
    )
    decision = evaluate(state, registry=_registry(worker), context=_context(), pricing_book=FixedBook())
    assert decision.allowed is False
    assert decision.status == STATUS_UNKNOWN
    assert "no trustworthy local provider quota source" in decision.reason


def test_api_equivalent_estimate_never_consumes_metered_cash_budget():
    state = State(":memory:")
    subscription = _worker(cost_class="premium-subscription")
    _record(state, _attempt(tokens=(10_000, 10_000), cost_class="premium-subscription"))
    _budget(state, "cash", constraint=CONSTRAINT_METERED_CASH, limit=0.01)

    decision = evaluate(
        state,
        registry=_registry(subscription),
        context=_context(),
        pricing_book=FixedBook(),
    )

    cash = next(item for item in decision.evaluations if item["budget_id"] == "cash")
    assert cash["value"] == 0.0
    assert decision.allowed is True


def test_reaching_any_hard_limit_blocks_including_zero_measured_cash():
    state = State(":memory:")
    registry = _registry(_worker())
    _record(state, _attempt(reported_cost=0.0))
    _budget(state, "cash-zero", constraint=CONSTRAINT_METERED_CASH, limit=0)
    cash = evaluate(state, registry=registry, context=_context(), pricing_book=FixedBook())
    assert cash.status == STATUS_BLOCKED
    assert cash.evaluations[0]["value"] == 0.0

    state.delete_usage_budget("cash-zero")
    _budget(state, "attempt-equality", limit=1)
    attempts = evaluate(state, registry=registry, context=_context(), pricing_book=FixedBook())
    assert attempts.status == STATUS_BLOCKED
    assert attempts.evaluations[0]["value"] == 2.0


def test_each_consumption_budget_allows_n_units_and_blocks_the_next():
    cases = (
        (CONSTRAINT_METERED_CASH, _attempt(tokens=(0, 0), reported_cost=1.0), 0.0, 1.0),
        (CONSTRAINT_TOKENS, _attempt(tokens=(1, 0)), 0.0, 1.0),
        (CONSTRAINT_WALL_CLOCK, _attempt(tokens=(0, 0)), 0.0, 1.0),
        (CONSTRAINT_ATTEMPTS, _attempt(tokens=(0, 0)), 1.0, 2.0),
        (CONSTRAINT_FALLBACKS, _attempt(tokens=(0, 0), automatic=True), 0.0, 1.0),
    )
    for constraint, incurred, nth_value, next_value in cases:
        state = State(":memory:")
        registry = _registry(_worker())
        _budget(state, constraint, constraint=constraint, limit=1)

        nth = evaluate(state, registry=registry, context=_context(), pricing_book=FixedBook())
        assert nth.allowed is True, constraint
        assert nth.evaluations[0]["value"] == nth_value, constraint

        if constraint == CONSTRAINT_WALL_CLOCK:
            incurred["started_at"] = "2026-09-26T12:00:59+00:00"
        _record(state, incurred)
        next_attempt = evaluate(
            state, registry=registry, context=_context(), pricing_book=FixedBook()
        )
        assert next_attempt.status == STATUS_BLOCKED, constraint
        assert next_attempt.evaluations[0]["value"] == next_value, constraint


def test_malformed_ledger_source_attempt_is_an_unknown_veto(tmp_path):
    state, registry = _available_dispatch_state()
    state.append_usage_ledger(
        {
            "project_id": "project-a",
            "program_ref": "ENG-PC",
            "task_id": "task-1",
            "runbook_id": "runbook-1",
            "run_id": "malformed-run",
            "session_id": None,
            "worker": "grok-build",
            "provider": "xAI",
            "effective_model": "grok-code-fast-1",
            "occurred_at": "2026-09-26T12:01:00+00:00",
            "source_record": {},
            "source_attempt": "not-an-object",
        }
    )
    _budget(state, "malformed-row", limit=100)

    decision = evaluate(state, registry=registry, context=_context(), pricing_book=FixedBook())
    assert decision.allowed is False
    assert decision.status == STATUS_UNKNOWN
    assert "malformed source_attempt JSON" in decision.reason

    task = Task(
        id="task-1",
        task_ref="ENG-PC-05",
        role="primary-implementation",
        worker="grok-build",
        project_id="project-a",
    )
    result, supervisor = _dispatch(state, registry, task, tmp_path)
    assert result.launched is False
    assert supervisor.launched == []
    assert "malformed source_attempt JSON" in result.reason


def test_program_budget_keeps_ledger_usage_after_task_deletion():
    state = State(":memory:")
    registry = _registry(_worker())
    _record(state, _attempt())
    _budget(state, "program", scope=SCOPE_PROGRAM, key="ENG-PC", limit=3)
    before = evaluate(state, registry=registry, context=_context(), pricing_book=FixedBook())
    assert before.evaluations[0]["value"] == 2.0
    assert state.list_usage_ledger()[0]["program_ref"] == "ENG-PC"

    state.delete_task("task-1")
    after = evaluate(state, registry=registry, context=_context(), pricing_book=FixedBook())
    assert after.evaluations[0]["value"] == before.evaluations[0]["value"]


def test_malformed_stored_budget_fails_closed_without_raising(tmp_path):
    state, registry = _available_dispatch_state()
    state.upsert_usage_budget(
        {
            "id": "malformed",
            "project_id": "project-a",
            "scope_type": "not-a-scope",
            "scope_key": None,
            "constraint_type": CONSTRAINT_ATTEMPTS,
            "limit_value": 1,
            "warning_fraction": 0.8,
        }
    )

    decision = evaluate(state, registry=registry, context=_context(), pricing_book=FixedBook())
    assert decision.allowed is False
    assert decision.status == STATUS_UNKNOWN
    assert "malformed stored budget definition" in decision.reason

    task = Task(
        id="malformed-task",
        task_ref="ENG-PC-05",
        role="primary-implementation",
        worker="grok-build",
        project_id="project-a",
    )
    result, supervisor = _dispatch(state, registry, task, tmp_path)
    assert result.launched is False
    assert supervisor.launched == []
    assert "malformed stored budget definition" in result.reason

    direct = Task(
        id="direct-malformed-task",
        task_ref="ENG-PC-05",
        role="primary-implementation",
        worker="grok-build",
        project_id="project-a",
    )
    state.upsert_task(direct)
    contained = Supervisor(registry=registry, repo_root=tmp_path, state=state).launch_task(
        direct, dry_run=True
    )
    assert contained.state == TASK_BLOCKED
    assert "malformed stored budget definition" in str(contained.last_error)


def test_warning_and_hard_block_are_distinct():
    state = State(":memory:")
    registry = _registry(_worker())
    _record(state, _attempt())
    _budget(state, "warn", limit=2.5)
    warning = evaluate(state, registry=registry, context=_context(), pricing_book=FixedBook())
    assert warning.allowed is True and warning.status == STATUS_WARNING
    task = state.get_task("task-1")
    emit_decision(state, task=task, decision=warning)

    state.delete_usage_budget("warn")
    _budget(state, "stop", limit=1)
    blocked = evaluate(state, registry=registry, context=_context(), pricing_book=FixedBook())
    assert blocked.allowed is False and blocked.status == STATUS_BLOCKED
    emit_decision(state, task=task, decision=blocked)
    assert [event.event_type for event in state.list_run_events(event_class="usage")[-2:]] == [
        "usage.budget_warning",
        "usage.budget_blocked",
    ]


def test_restart_persists_definition_and_idempotent_ledger_consumption(tmp_path):
    database = tmp_path / "state.db"
    with State(database) as state:
        _budget(state, "attempts", limit=2)
        attempt = _attempt()
        assert _record(state, attempt) == 1
        assert _record(state, attempt) == 0

    with State(database) as reopened:
        assert reopened.get_usage_budget("attempts")["limit_value"] == 2
        decision = evaluate(
            reopened,
            registry=_registry(_worker()),
            context=_context(),
            pricing_book=FixedBook(),
        )
        evaluation = decision.evaluations[0]
        assert evaluation["value"] == 2  # one durable attempt + this proposed launch
        assert len(reopened.list_usage_ledger()) == 1


def test_progress_carries_the_basis_that_separates_it_from_enforcement():
    """A progress figure must say it is incurred-only, because enforcement is not.

    ``progress`` counts durable usage that already happened; ``evaluate``
    additionally counts the launch or fallback being proposed. Those are
    different numbers by design, so a budget can show headroom and still refuse
    the very next launch. An operator reading "2 of 5 attempts" with only a
    ``source`` of "count of matching append-only ledger attempts" would
    reasonably conclude a third attempt is permitted.

    The qualifier therefore has to travel with the figure. ``source`` says where
    the number came from, which is a different question.
    """

    state, registry = _available_dispatch_state()
    _budget(state, "basis-check", limit=100)
    rows = progress(state, registry=registry, project_id="project-a", pricing_book=FixedBook())

    assert rows, "an enabled budget must produce a progress row"
    for row in rows:
        assert row["basis"] == PROGRESS_BASIS
        # The basis is a distinct claim from provenance and from origin.
        assert row["basis"] != row.get("source")
        assert row["basis"] != row.get("evidence_class")
        assert "proposed" in row["basis"]
