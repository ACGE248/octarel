"""Durable, append-only per-run usage attribution (ENG-PC-05 scope A).

Ledger rows survive deletion of their mutable runbook and governance records.
Resolved project identity is copied at insert time. A pre-project row may have
that identity completed later by the project-adoption migration, whose narrow
trigger exception changes no financial evidence. Deletion therefore never
erases spend history or prevents its eventual project attribution.
"""

from __future__ import annotations

import uuid
from typing import Any

from .run_events import RunEvent
from .state import State
from .usage_telemetry import (
    CLASS_DERIVED,
    CLASS_MEASURED,
    CLASS_UNKNOWN,
    WorkerFacts,
    build_row,
)


def new_run_id() -> str:
    """A stable token for one concrete worker launch/route attempt."""

    return f"RUN-{uuid.uuid4().hex}"


def _provenance(quality: Any) -> str:
    normalized = str(quality or "unknown").lower()
    if normalized == "exact":
        return CLASS_MEASURED
    if normalized == "estimated":
        return CLASS_DERIVED
    return CLASS_UNKNOWN


def _emit(state: State, row: dict[str, Any]) -> None:
    """Emit after commit; event failure can never undo the ledger append."""

    run_id = row.get("run_id")
    if not run_id:
        return
    try:
        state.record_run_event(
            RunEvent(
                run_id=str(run_id),
                event_class="usage",
                event_type="usage.recorded",
                source="control_plane.usage_ledger",
                provenance=_provenance(row["source_record"].get("telemetry_quality")),
                project_id=row.get("project_id"),
                task_id=row.get("task_id"),
                provider=row.get("provider"),
                message=f"Usage recorded for {row['worker']}",
                data={
                    "ledger_id": row["id"],
                    "runbook_id": row["runbook_id"],
                    "worker": row["worker"],
                    "model": row.get("effective_model") or "NOT_REPORTED",
                },
            )
        )
    except Exception:  # noqa: BLE001 - telemetry emission follows a committed accounting write
        pass


def _attempt_record(record: dict[str, Any], attempt: dict[str, Any]) -> dict[str, Any]:
    """Project attempt-scoped evidence into the unchanged telemetry contract."""

    return {
        "runbook_id": record.get("runbook_id"),
        "task_id": record.get("task_id"),
        "telemetry_quality": attempt.get("telemetry_quality", record.get("telemetry_quality", "unknown")),
        "input_tokens": attempt.get("input_tokens", record.get("input_tokens")),
        "output_tokens": attempt.get("output_tokens", record.get("output_tokens")),
        "escalation_state": record.get("escalation_state"),
        "escalation_reason": record.get("escalation_reason"),
        "route_history": [attempt],
        "updated_at": attempt.get("ended_at") or record.get("updated_at"),
    }


def record_usage_attempts(
    state: State,
    record: dict[str, Any],
    *,
    project_id: str | None = None,
    registry: Any = None,
) -> int:
    """Append every attributable attempt in ``record`` without inventing usage.

    New writers mark each real run with ``usage_recorded`` and attempt-scoped
    token fields.  For pre-ledger records, only the newest worker attempt may
    inherit the old runbook-level token fields; attributing those figures to
    earlier attempts would invent evidence the historical schema never kept.
    The newest legacy attempt is still a real run when those fields are absent,
    so it is recorded with UNKNOWN evidence rather than dropped.
    """

    history = record.get("route_history") or []
    worker_indexes = [
        index
        for index, attempt in enumerate(history)
        if isinstance(attempt, dict) and attempt.get("worker")
    ]
    legacy_index = worker_indexes[-1] if worker_indexes else None
    runbook = state.get_runbook(str(record.get("runbook_id") or ""))
    if runbook is not None:
        project_id = runbook.project_id
    if project_id is None and record.get("task_id"):
        # Runbook deletion intentionally retains tasks and ledger history. The
        # durable task is therefore the attribution source when reconciliation
        # races with, or follows, deletion of the mutable runbook row.
        task = state.get_task(str(record["task_id"]))
        if task is not None:
            project_id = task.project_id

    inserted_count = 0
    for index in worker_indexes:
        attempt = dict(history[index])
        attempt_scoped = bool(attempt.get("usage_recorded")) or any(
            key in attempt for key in ("telemetry_quality", "input_tokens", "output_tokens")
        )
        if not attempt_scoped and index != legacy_index:
            continue
        if not attempt_scoped:
            # A run id marks the new writer. Until that attempt itself carries
            # ``usage_recorded``, the run is still live and must not be frozen
            # into the immutable ledger with the governance row's default
            # UNKNOWN placeholders. Only genuinely legacy attempts (no run id)
            # may inherit the old record-level evidence.
            if attempt.get("run_id"):
                continue
        run_id = str(attempt.get("run_id") or f"{record['runbook_id']}:attempt:{index + 1}")
        session_id = attempt.get("session_id")
        configured_worker = registry.workers.get(str(attempt["worker"])) if registry is not None else None
        provider = attempt.get("provider") or getattr(configured_worker, "provider", None)
        effective_model = attempt.get("model") or (
            getattr(configured_worker, "effective_model", None)
            or getattr(configured_worker, "default_model", None)
        )
        source_record = _attempt_record(record, attempt)
        stored, inserted = state.append_usage_ledger(
            {
                "project_id": project_id,
                "task_id": record.get("task_id"),
                "runbook_id": record["runbook_id"],
                "run_id": run_id,
                "session_id": session_id,
                "worker": str(attempt["worker"]),
                "provider": provider,
                "effective_model": effective_model,
                "occurred_at": attempt.get("ended_at") or record.get("updated_at"),
                "source_record": source_record,
                "source_attempt": attempt,
            }
        )
        if inserted:
            inserted_count += 1
            _emit(state, stored)
    return inserted_count


def reconcile_usage_governance(
    state: State,
    *,
    project_id: str | None = None,
    registry: Any = None,
) -> int:
    """Idempotently promote legacy/current governance evidence into the ledger."""

    return sum(
        record_usage_attempts(state, record, project_id=project_id, registry=registry)
        for record in state.list_usage_governance()
    )


def build_ledger_rows(
    entries: list[dict[str, Any]],
    *,
    registry: Any,
    pricing_book: Any,
) -> list[dict[str, Any]]:
    """Render immutable ledger evidence through the existing truthful row builder."""

    rows: list[dict[str, Any]] = []
    for entry in entries:
        attempt = entry["source_attempt"]
        worker_name = str(entry["worker"])
        worker = registry.workers.get(worker_name)
        recorded_model = attempt.get("model") or None
        effective_model = recorded_model or entry.get("effective_model") or None
        recorded_cost_class = attempt.get("cost_class") or None
        facts = WorkerFacts(
            worker=worker_name,
            provider=attempt.get("provider") or entry.get("provider") or getattr(worker, "provider", None),
            execution_system=attempt.get("execution_system") or getattr(worker, "execution_system", None),
            model=effective_model
            or (getattr(worker, "effective_model", None) or getattr(worker, "default_model", None))
            or None,
            cost_class=recorded_cost_class or getattr(worker, "cost_class", None),
            model_from_record=bool(recorded_model),
            cost_class_from_record=bool(recorded_cost_class),
            reported_cost_usd=attempt.get("reported_cost_usd"),
            reported_cost_source=attempt.get("reported_cost_source"),
        )
        lookup = pricing_book.lookup(provider=facts.provider, model=facts.model)
        rows.append(
            build_row(
                entry["source_record"],
                facts=facts,
                project_id=entry.get("project_id"),
                run_id=entry["run_id"],
                session_id=entry.get("session_id"),
                pricing=lookup.pricing,
                pricing_reason=lookup.reason,
            )
        )
    return rows


__all__ = [
    "build_ledger_rows",
    "new_run_id",
    "reconcile_usage_governance",
    "record_usage_attempts",
]
