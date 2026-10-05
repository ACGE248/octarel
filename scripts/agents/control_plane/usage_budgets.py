"""Hierarchical usage-budget brakes for managed dispatch (ENG-PC-05 scope B).

Budgets never select or authorize a route.  This module is called only after
the existing provider/cost/authorization path has selected an otherwise
eligible worker, and its answer can only leave that decision unchanged or
veto it.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Any, Mapping

from .pricing import load_pricing_book
from .run_events import RunEvent
from .usage_ledger import build_ledger_rows
from .usage_telemetry import CLASS_DERIVED, CLASS_MEASURED, CLASS_UNKNOWN

SCOPE_GLOBAL = "global"
SCOPE_PROVIDER = "provider"
SCOPE_PROGRAM = "program"
SCOPE_TASK = "task"
SCOPE_RUN = "run"
SCOPE_SESSION = "session"
SCOPE_TYPES = (SCOPE_GLOBAL, SCOPE_PROVIDER, SCOPE_PROGRAM, SCOPE_TASK, SCOPE_RUN, SCOPE_SESSION)
_SCOPE_RANK = {name: index for index, name in enumerate(SCOPE_TYPES)}

CONSTRAINT_METERED_CASH = "metered_cash_usd"
CONSTRAINT_TOKENS = "tokens"
CONSTRAINT_WALL_CLOCK = "wall_clock_seconds"
CONSTRAINT_ATTEMPTS = "attempts"
CONSTRAINT_FALLBACKS = "fallbacks"
CONSTRAINT_PROVIDER_QUOTA_RESERVE = "provider_quota_reserve"
CONSTRAINT_TYPES = frozenset(
    {
        CONSTRAINT_METERED_CASH,
        CONSTRAINT_TOKENS,
        CONSTRAINT_WALL_CLOCK,
        CONSTRAINT_ATTEMPTS,
        CONSTRAINT_FALLBACKS,
        CONSTRAINT_PROVIDER_QUOTA_RESERVE,
    }
)


@dataclass(frozen=True)
class ConstraintSemantics:
    includes_proposed_unit: bool


# Every constraint must declare whether enforcement evidence includes the unit
# currently being proposed. This controls both evidence construction and the
# hard-limit comparison; adding a constraint without choosing is a test failure.
CONSTRAINT_SEMANTICS = {
    CONSTRAINT_METERED_CASH: ConstraintSemantics(includes_proposed_unit=False),
    CONSTRAINT_TOKENS: ConstraintSemantics(includes_proposed_unit=False),
    CONSTRAINT_WALL_CLOCK: ConstraintSemantics(includes_proposed_unit=False),
    CONSTRAINT_ATTEMPTS: ConstraintSemantics(includes_proposed_unit=True),
    CONSTRAINT_FALLBACKS: ConstraintSemantics(includes_proposed_unit=True),
    CONSTRAINT_PROVIDER_QUOTA_RESERVE: ConstraintSemantics(includes_proposed_unit=False),
}

STATUS_OK = "OK"
STATUS_WARNING = "WARNING"
STATUS_BLOCKED = "BLOCKED"
STATUS_UNKNOWN = "UNKNOWN"

MODE_ADVISORY = "advisory"
MODE_ENFORCED = "enforced"
ENFORCEMENT_MODES = frozenset({MODE_ADVISORY, MODE_ENFORCED})


@dataclass(frozen=True)
class BudgetContext:
    project_id: str | None
    provider: str | None
    task_id: str | None
    task_ref: str | None
    run_id: str | None
    runbook_id: str | None = None
    session_id: str | None = None
    is_fallback: bool = False
    durable_program_ref: str | None = None

    @property
    def stable_task_ref(self) -> str | None:
        return self.task_ref.split(maxsplit=1)[0] if self.task_ref else None

    @property
    def program_ref(self) -> str | None:
        if self.durable_program_ref:
            return self.durable_program_ref
        stable = self.stable_task_ref
        return stable.rsplit("-", 1)[0] if stable and "-" in stable else stable


@dataclass(frozen=True)
class BudgetDecision:
    allowed: bool
    status: str
    reason: str
    bounding_scope: str | None
    bounding_budget_id: str | None
    evidence_class: str
    evaluations: tuple[dict[str, Any], ...]


def _timestamp(value: Any, *, field: str) -> _dt.datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"budget {field} must be a timezone-aware ISO-8601 timestamp")
    try:
        parsed = _dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"budget {field} must be a timezone-aware ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"budget {field} must be a timezone-aware ISO-8601 timestamp")
    return parsed.astimezone(_dt.timezone.utc)


def validate_definition(
    definition: Mapping[str, Any], *, require_persisted_fields: bool = True
) -> None:
    scope = str(definition.get("scope_type") or "")
    constraint = str(definition.get("constraint_type") or "")
    if scope not in SCOPE_TYPES:
        raise ValueError(f"unsupported budget scope {scope!r}")
    if constraint not in CONSTRAINT_TYPES:
        raise ValueError(f"unsupported budget constraint {constraint!r}")
    if scope == SCOPE_GLOBAL and definition.get("scope_key") not in {None, ""}:
        raise ValueError("a global budget must not carry scope_key")
    if scope != SCOPE_GLOBAL and not definition.get("scope_key"):
        raise ValueError(f"a {scope} budget requires scope_key")
    limit = float(definition.get("limit_value", -1))
    warning = float(definition.get("warning_fraction", 0.8))
    if limit != limit or limit in {float("inf"), float("-inf")} or limit < 0:
        raise ValueError("budget limit_value must be non-negative")
    if warning != warning or warning in {float("inf"), float("-inf")} or not 0 < warning <= 1:
        raise ValueError("budget warning_fraction must be greater than 0 and at most 1")
    mode = definition.get("enforcement_mode", MODE_ENFORCED)
    if mode not in ENFORCEMENT_MODES:
        raise ValueError(f"unsupported budget enforcement_mode {mode!r}")
    activated_at = definition.get("activated_at")
    if activated_at is None and require_persisted_fields:
        raise ValueError("budget activated_at is required")
    if activated_at is not None:
        _timestamp(activated_at, field="activated_at")


def save_definition(state, definition: Mapping[str, Any]) -> dict[str, Any]:
    # Persistence supplies safe defaults for a new definition and preserves an
    # existing definition's mode/activation when an ordinary update omits them.
    validate_definition(definition, require_persisted_fields=False)
    return state.upsert_usage_budget(dict(definition))


def _scope_matches(definition: Mapping[str, Any], context: BudgetContext) -> bool:
    if definition.get("project_id") is not None and definition.get("project_id") != context.project_id:
        return False
    scope = definition["scope_type"]
    key = definition.get("scope_key")
    candidates = {
        SCOPE_GLOBAL: {None, ""},
        SCOPE_PROVIDER: {context.provider},
        SCOPE_PROGRAM: {context.program_ref},
        SCOPE_TASK: {context.task_id, context.stable_task_ref},
        SCOPE_RUN: {context.run_id, context.runbook_id},
        # A runbook session is durably keyed by its one task before a native
        # provider continuation id exists. Once present, either identity can
        # bind the same narrowest scope.
        SCOPE_SESSION: {context.session_id, context.task_id},
    }[scope]
    return key in candidates


def _source_attempt(entry: Mapping[str, Any]) -> Mapping[str, Any] | None:
    source_attempt = entry.get("source_attempt")
    return source_attempt if isinstance(source_attempt, Mapping) else None


def _is_automatic_fallback(value: Any) -> bool:
    """True only when an automatic replacement was actually selected.

    ``False`` means fallback was considered/claimed but no automatic replacement
    was accepted, so it consumes neither proposed nor durable fallback budget.
    """

    return value is True


def _entry_context(state, entry: Mapping[str, Any]) -> BudgetContext:
    task = state.get_task(str(entry.get("task_id") or "")) if entry.get("task_id") else None
    source_attempt = _source_attempt(entry)
    return BudgetContext(
        project_id=entry.get("project_id"),
        provider=entry.get("provider"),
        task_id=entry.get("task_id"),
        task_ref=getattr(task, "task_ref", None),
        run_id=entry.get("run_id"),
        runbook_id=entry.get("runbook_id"),
        session_id=entry.get("session_id"),
        is_fallback=_is_automatic_fallback(source_attempt.get("automatic") if source_attempt else None),
        durable_program_ref=entry.get("program_ref"),
    )


def _entries_for(
    state, definition: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], int]:
    """Return active scoped entries plus rows whose activation relation is unknown.

    A timestamp-less or malformed row cannot be assumed to precede activation.
    It therefore remains explicit UNKNOWN evidence instead of disappearing from
    an enforced total or being treated as zero.
    """

    activation = _timestamp(definition["activated_at"], field="activated_at")
    active: list[dict[str, Any]] = []
    unknown_timestamps = 0
    for entry in state.list_usage_ledger():
        if not _scope_matches(definition, _entry_context(state, entry)):
            continue
        try:
            occurred_at = _timestamp(entry.get("occurred_at"), field="occurred_at")
        except ValueError:
            unknown_timestamps += 1
            continue
        if occurred_at >= activation:
            active.append(entry)
    return active, unknown_timestamps


def _effective_status(status: str, enforcement_mode: str) -> str:
    if enforcement_mode == MODE_ADVISORY and status in {STATUS_BLOCKED, STATUS_UNKNOWN}:
        return STATUS_WARNING
    return status


def _evaluation_identity(definition: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "budget_id": str(definition.get("id") or "UNKNOWN"),
        "scope_type": str(definition.get("scope_type") or "invalid"),
        "scope_key": definition.get("scope_key"),
        "constraint_type": str(definition.get("constraint_type") or "invalid"),
        "enforcement_mode": str(definition.get("enforcement_mode") or MODE_ENFORCED),
        "activated_at": definition.get("activated_at"),
    }


def _sum_metric(rows: list[dict[str, Any]], metric_name: str) -> tuple[float | None, str, str]:
    cells = [row["metrics"][metric_name] for row in rows]
    unknown = [
        cell
        for cell in cells
        if cell.get("class") not in {CLASS_MEASURED, CLASS_DERIVED}
        or isinstance(cell.get("value"), bool)
        or not isinstance(cell.get("value"), (int, float))
        or float(cell["value"]) != float(cell["value"])
        or float(cell["value"]) in {float("inf"), float("-inf")}
    ]
    if unknown:
        return None, CLASS_UNKNOWN, (
            f"{len(unknown)} matching ledger row(s) have no trustworthy {metric_name} source"
        )
    value = sum(float(cell["value"]) for cell in cells)
    klass = CLASS_DERIVED if any(cell.get("class") == CLASS_DERIVED for cell in cells) else CLASS_MEASURED
    return value, klass, f"sum of {len(cells)} matching append-only ledger row(s)"


def _duration_seconds(entry: Mapping[str, Any]) -> float | None:
    attempt = _source_attempt(entry)
    if attempt is None:
        return None
    start, end = attempt.get("started_at"), attempt.get("ended_at")
    if not start or not end:
        return None
    try:
        seconds = (_dt.datetime.fromisoformat(str(end)) - _dt.datetime.fromisoformat(str(start))).total_seconds()
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 0 else None


def _rows(entries: list[dict[str, Any]], *, registry: Any, pricing_book: Any) -> list[dict[str, Any]]:
    rows = build_ledger_rows(entries, registry=registry, pricing_book=pricing_book)
    for row, entry in zip(rows, entries, strict=True):
        duration = _duration_seconds(entry)
        if duration is not None:
            row["metrics"]["duration_seconds"] = {
                "value": duration,
                "class": CLASS_MEASURED,
                "source": "route attempt started_at/ended_at",
                "formula": "ended_at - started_at",
                "reason": None,
                "unit": "seconds",
            }
    return rows


def _evidence(
    definition: Mapping[str, Any], entries: list[dict[str, Any]], rows: list[dict[str, Any]],
    *, context: BudgetContext, quota_sources: Mapping[str, Mapping[str, Any]],
    include_proposed: bool,
) -> tuple[float | None, str, str]:
    constraint = definition["constraint_type"]
    semantics = CONSTRAINT_SEMANTICS[constraint]
    includes_proposed = include_proposed and semantics.includes_proposed_unit
    if constraint == CONSTRAINT_METERED_CASH:
        # The route-blind API-equivalent estimate is intentionally never read.
        return _sum_metric(rows, "actual_cost_usd")
    if constraint == CONSTRAINT_TOKENS:
        return _sum_metric(rows, "total_tokens")
    if constraint == CONSTRAINT_WALL_CLOCK:
        return _sum_metric(rows, "duration_seconds")
    if constraint == CONSTRAINT_ATTEMPTS:
        proposed = 1 if includes_proposed else 0
        source = (
            "ledger attempts + proposed launch"
            if proposed
            else "count of matching append-only ledger attempts"
        )
        return float(len(entries) + proposed), CLASS_MEASURED, source
    if constraint == CONSTRAINT_FALLBACKS:
        used = sum(
            1
            for entry in entries
            if _is_automatic_fallback((_source_attempt(entry) or {}).get("automatic"))
        )
        proposed = 1 if includes_proposed and context.is_fallback else 0
        source = (
            "ledger automatic fallbacks + proposed automatic fallback"
            if proposed
            else "count of matching automatic fallbacks in the append-only ledger"
        )
        return float(used + proposed), CLASS_MEASURED, source
    source = quota_sources.get(context.provider or "")
    if not source or source.get("class") not in {CLASS_MEASURED, CLASS_DERIVED}:
        return None, CLASS_UNKNOWN, "no trustworthy local provider quota source exists"
    value = source.get("remaining")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, CLASS_UNKNOWN, "local provider quota source did not report numeric remaining quota"
    return float(value), str(source["class"]), str(source.get("source") or "local provider quota source")


def evaluate(
    state,
    *,
    registry: Any,
    context: BudgetContext,
    pricing_book: Any | None = None,
    quota_sources: Mapping[str, Mapping[str, Any]] | None = None,
) -> BudgetDecision:
    """Return a pure veto/no-op decision for every applicable definition."""

    definitions: list[dict[str, Any]] = []
    invalid: list[tuple[dict[str, Any], str]] = []
    for item in state.list_usage_budgets(project_id=context.project_id, enabled_only=True):
        try:
            validate_definition(item)
        except (KeyError, TypeError, ValueError) as exc:
            # Stored configuration is untrusted input. Its intended scope cannot
            # be proven once malformed, so enforcement fails closed for the
            # selected project instead of letting the daemon path raise or pass.
            invalid.append((item, str(exc)))
            continue
        if _scope_matches(item, context):
            definitions.append(item)
    if not definitions and not invalid:
        return BudgetDecision(True, STATUS_OK, "no applicable usage budget", None, None, CLASS_UNKNOWN, ())
    book = pricing_book or load_pricing_book()
    quota = quota_sources or {}
    evaluations: list[dict[str, Any]] = []
    for definition, reason in invalid:
        mode = (
            MODE_ADVISORY
            if definition.get("enforcement_mode") == MODE_ADVISORY
            else MODE_ENFORCED
        )
        evaluations.append(
            {
                **_evaluation_identity(definition),
                # A recognizable explicit advisory remains non-vetoing even if
                # another field is malformed. An absent/invalid mode fails safe
                # as enforced because operator intent cannot be established.
                "enforcement_mode": mode,
                "status": _effective_status(STATUS_UNKNOWN, mode),
                "value": None,
                "limit": definition.get("limit_value"),
                "remaining": None,
                "evidence_class": CLASS_UNKNOWN,
                "source": f"malformed stored budget definition: {reason}",
                "basis": PROGRESS_BASIS,
            }
        )
    for definition in definitions:
        mode = str(definition["enforcement_mode"])
        entries, unknown_timestamps = _entries_for(state, definition)
        if unknown_timestamps:
            evaluations.append(
                {
                    **_evaluation_identity(definition),
                    "status": _effective_status(STATUS_UNKNOWN, mode),
                    "value": None,
                    "limit": float(definition["limit_value"]),
                    "remaining": None,
                    "evidence_class": CLASS_UNKNOWN,
                    "source": (
                        f"{unknown_timestamps} matching ledger row(s) have no trustworthy occurred_at "
                        f"relative to activation {definition['activated_at']}"
                    ),
                    "basis": PROGRESS_BASIS,
                }
            )
            continue
        malformed_entries = sum(1 for entry in entries if _source_attempt(entry) is None)
        if malformed_entries:
            evaluations.append(
                {
                    **_evaluation_identity(definition),
                    "status": _effective_status(STATUS_UNKNOWN, mode),
                    "value": None,
                    "limit": float(definition["limit_value"]),
                    "remaining": None,
                    "evidence_class": CLASS_UNKNOWN,
                    "source": (
                        f"{malformed_entries} matching ledger row(s) have malformed source_attempt JSON"
                    ),
                    "basis": PROGRESS_BASIS,
                }
            )
            continue
        rows = _rows(entries, registry=registry, pricing_book=book)
        value, evidence_class, source = _evidence(
            definition, entries, rows, context=context, quota_sources=quota, include_proposed=True
        )
        limit = float(definition["limit_value"])
        constraint = definition["constraint_type"]
        semantics = CONSTRAINT_SEMANTICS[constraint]
        if value is None:
            status = _effective_status(STATUS_UNKNOWN, mode)
            remaining = None
        else:
            remaining = (value - limit) if constraint == CONSTRAINT_PROVIDER_QUOTA_RESERVE else (limit - value)
            if constraint == CONSTRAINT_PROVIDER_QUOTA_RESERVE:
                breached = value <= limit
            elif semantics.includes_proposed_unit:
                # The proposed unit is already present in ``value``, so it is
                # permitted when it exactly fills the configured allowance.
                breached = value > limit
            else:
                # Reaching an incurred-usage threshold exhausts it. In
                # particular, zero metered cash means no paid work may start.
                breached = value >= limit
            if breached:
                status = _effective_status(STATUS_BLOCKED, mode)
            else:
                ratio = (limit / value) if constraint == CONSTRAINT_PROVIDER_QUOTA_RESERVE and value else (
                    value / limit if limit else 1.0
                )
                status = STATUS_WARNING if ratio >= float(definition["warning_fraction"]) else STATUS_OK
        evaluations.append(
            {
                **_evaluation_identity(definition),
                "status": status,
                "value": value,
                "limit": limit,
                "remaining": remaining,
                "evidence_class": evidence_class,
                "source": source,
                "basis": PROGRESS_BASIS,
            }
        )

    severity = {STATUS_BLOCKED: 0, STATUS_UNKNOWN: 1, STATUS_WARNING: 2, STATUS_OK: 3}
    evaluations.sort(
        key=lambda item: (
            severity[item["status"]],
            float("inf") if item["remaining"] is None else item["remaining"],
            _SCOPE_RANK.get(item["scope_type"], -1),
            item["budget_id"],
        )
    )
    binding = evaluations[0] if evaluations else None
    if binding is None:  # pragma: no cover - definitions above guarantees an evaluation
        return BudgetDecision(True, STATUS_OK, "no applicable usage budget", None, None, CLASS_UNKNOWN, ())
    allowed = not any(
        item["enforcement_mode"] == MODE_ENFORCED
        and item["status"] in {STATUS_BLOCKED, STATUS_UNKNOWN}
        for item in evaluations
    )
    scope = binding["scope_type"] + (
        f":{binding['scope_key']}" if binding.get("scope_key") is not None else ""
    )
    if binding["value"] is None:
        reason = (
            f"usage budget {binding['budget_id']} at {scope} is {binding['status']} "
            f"({binding['enforcement_mode']}, {binding['constraint_type']}, "
            f"{binding['evidence_class']}): {binding['source']}"
        )
    else:
        reason = (
            f"usage budget {binding['budget_id']} at {scope} is {binding['status']} "
            f"({binding['enforcement_mode']}) for {binding['constraint_type']}: "
            f"{binding['value']:g}/{binding['limit']:g} "
            f"({binding['evidence_class']})"
        )
    if binding["enforcement_mode"] == MODE_ADVISORY and binding["status"] == STATUS_WARNING:
        reason += "; advisory budgets report risk but never veto execution"
    return BudgetDecision(
        allowed, binding["status"], reason, scope, binding["budget_id"],
        binding["evidence_class"], tuple(evaluations),
    )


# A progress figure and an enforcement decision are deliberately computed from
# different bases: ``progress`` reports only durable usage that already happened,
# while ``evaluate`` additionally counts the launch or fallback being proposed. So
# a budget can read as having headroom here and still refuse the very next launch.
# That qualifier travels with the figure instead of living only in a docstring --
# an operator reading "2 of 5 attempts" would otherwise reasonably conclude a third
# is permitted.
PROGRESS_BASIS = (
    "usage already incurred; enforcement additionally counts the launch or fallback being proposed, "
    "so a budget with headroom here can still refuse the next launch"
)
INCURRED_PROGRESS_BASIS = "usage already incurred; enforcement compares this same incurred evidence"


def progress(
    state,
    *,
    registry: Any,
    project_id: str | None,
    pricing_book: Any | None = None,
    quota_sources: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Project enabled budgets into truthful, read-only UI progress rows.

    This deliberately does not call :func:`evaluate`: attempts and fallbacks in
    an enforcement decision include the proposed launch, while a progress view
    reports only durable usage that has already happened.  The threshold rules
    and evidence readers otherwise remain the same ones used by enforcement.
    """

    book = pricing_book or load_pricing_book()
    quota = quota_sources or {}
    rows: list[dict[str, Any]] = []
    for definition in state.list_usage_budgets(project_id=project_id, enabled_only=True):
        try:
            validate_definition(definition)
        except (KeyError, TypeError, ValueError) as exc:
            mode = (
                MODE_ADVISORY
                if definition.get("enforcement_mode") == MODE_ADVISORY
                else MODE_ENFORCED
            )
            rows.append(
                {
                    **_evaluation_identity(definition),
                    "enforcement_mode": mode,
                    "bounding_scope": "invalid",
                    "status": _effective_status(STATUS_UNKNOWN, mode),
                    "value": None,
                    "limit": definition.get("limit_value"),
                    "remaining": None,
                    "progress_percent": None,
                    "evidence_class": CLASS_UNKNOWN,
                    "source": f"malformed stored budget definition: {exc}",
                    "basis": PROGRESS_BASIS,
                }
            )
            continue
        mode = str(definition["enforcement_mode"])
        entries, unknown_timestamps = _entries_for(state, definition)
        if unknown_timestamps:
            rows.append(
                {
                    **_evaluation_identity(definition),
                    "bounding_scope": definition["scope_type"] + (
                        f":{definition['scope_key']}" if definition.get("scope_key") is not None else ""
                    ),
                    "status": _effective_status(STATUS_UNKNOWN, mode),
                    "value": None,
                    "limit": float(definition["limit_value"]),
                    "remaining": None,
                    "progress_percent": None,
                    "evidence_class": CLASS_UNKNOWN,
                    "source": (
                        f"{unknown_timestamps} matching ledger row(s) have no trustworthy occurred_at "
                        f"relative to activation {definition['activated_at']}"
                    ),
                    "basis": PROGRESS_BASIS,
                }
            )
            continue
        malformed_entries = sum(1 for entry in entries if _source_attempt(entry) is None)
        if malformed_entries:
            rows.append(
                {
                    **_evaluation_identity(definition),
                    "bounding_scope": definition["scope_type"] + (
                        f":{definition['scope_key']}" if definition.get("scope_key") is not None else ""
                    ),
                    "status": _effective_status(STATUS_UNKNOWN, mode),
                    "value": None,
                    "limit": float(definition["limit_value"]),
                    "remaining": None,
                    "progress_percent": None,
                    "evidence_class": CLASS_UNKNOWN,
                    "source": (
                        f"{malformed_entries} matching ledger row(s) have malformed source_attempt JSON"
                    ),
                    "basis": PROGRESS_BASIS,
                }
            )
            continue
        rendered = _rows(entries, registry=registry, pricing_book=book)
        constraint = definition["constraint_type"]
        semantics = CONSTRAINT_SEMANTICS[constraint]
        provider = definition.get("scope_key") if definition["scope_type"] == SCOPE_PROVIDER else None
        value, evidence_class, source = _evidence(
            definition,
            entries,
            rendered,
            context=BudgetContext(project_id, provider, None, None, None),
            quota_sources=quota,
            include_proposed=False,
        )
        basis = PROGRESS_BASIS if semantics.includes_proposed_unit else INCURRED_PROGRESS_BASIS

        limit = float(definition["limit_value"])
        remaining: float | None = None
        progress_percent: float | None = None
        if value is None:
            status = _effective_status(STATUS_UNKNOWN, mode)
        elif constraint == CONSTRAINT_PROVIDER_QUOTA_RESERVE:
            remaining = value - limit
            if value <= limit:
                status = _effective_status(STATUS_BLOCKED, mode)
            else:
                ratio = limit / value if value else 1.0
                status = STATUS_WARNING if ratio >= float(definition["warning_fraction"]) else STATUS_OK
            # A reserve threshold is not a total quota denominator.  Showing a
            # percentage would invent the provider's full allowance.
        else:
            remaining = limit - value
            breached = value >= limit
            if breached:
                status = _effective_status(STATUS_BLOCKED, mode)
            else:
                ratio = value / limit if limit else 1.0
                status = STATUS_WARNING if ratio >= float(definition["warning_fraction"]) else STATUS_OK
            if limit > 0:
                progress_percent = (value / limit) * 100.0

        rows.append(
            {
                **_evaluation_identity(definition),
                "bounding_scope": definition["scope_type"] + (
                    f":{definition['scope_key']}" if definition.get("scope_key") is not None else ""
                ),
                "status": status,
                "value": value,
                "limit": limit,
                "remaining": remaining,
                "progress_percent": progress_percent,
                "evidence_class": evidence_class,
                "source": source,
                "basis": basis,
            }
        )
    return rows


def emit_decision(state, *, task: Any, decision: BudgetDecision) -> None:
    if decision.status not in {STATUS_WARNING, STATUS_BLOCKED, STATUS_UNKNOWN}:
        return
    binding = next(
        (
            item
            for item in decision.evaluations
            if item.get("budget_id") == decision.bounding_budget_id
        ),
        {},
    )
    try:
        state.record_run_event(
            RunEvent(
                run_id=str(task.runbook_id or task.id),
                event_class="usage",
                event_type="usage.budget_warning" if decision.status == STATUS_WARNING else "usage.budget_blocked",
                source="control_plane.usage_budgets",
                provenance=decision.evidence_class,
                project_id=task.project_id,
                task_id=task.id,
                level="warning" if decision.status == STATUS_WARNING else "error",
                message=decision.reason,
                data={
                    "status": decision.status,
                    "budget_id": decision.bounding_budget_id,
                    "bounding_scope": decision.bounding_scope,
                    "evidence_class": decision.evidence_class,
                    "enforcement_mode": binding.get("enforcement_mode"),
                    "activated_at": binding.get("activated_at"),
                },
            )
        )
    except Exception:  # noqa: BLE001 - a committed veto/warning must survive event failure
        pass


def context_for_task(task: Any, *, provider: str | None = None) -> BudgetContext:
    return BudgetContext(
        project_id=task.project_id,
        provider=provider or task.selected_provider,
        task_id=task.id,
        task_ref=task.task_ref,
        run_id=task.runbook_id,
        runbook_id=task.runbook_id,
        is_fallback=_is_automatic_fallback(task.fallback_automatic),
    )


__all__ = [
    "BudgetContext", "BudgetDecision", "CONSTRAINT_ATTEMPTS", "CONSTRAINT_FALLBACKS",
    "CONSTRAINT_METERED_CASH", "CONSTRAINT_PROVIDER_QUOTA_RESERVE", "CONSTRAINT_TOKENS",
    "CONSTRAINT_SEMANTICS", "CONSTRAINT_TYPES", "CONSTRAINT_WALL_CLOCK", "ENFORCEMENT_MODES",
    "MODE_ADVISORY", "MODE_ENFORCED", "SCOPE_GLOBAL",
    "SCOPE_PROGRAM", "SCOPE_PROVIDER", "SCOPE_RUN",
    "SCOPE_SESSION", "SCOPE_TASK", "STATUS_BLOCKED", "STATUS_OK", "STATUS_UNKNOWN",
    "STATUS_WARNING", "context_for_task", "emit_decision", "evaluate", "progress", "save_definition",
]
