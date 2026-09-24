"""Controlled, AO-orchestrated read-only bot fan-out for a write-capable primary (ENG-AO-02).

Shape (one level only)::

    AO -> primary worker (only writer) <- findings <- N bounded read-only bots (parallel)

AO, not the model, owns the fan-out.  A worker opts in with a ``subagents`` block in
``workers.json`` (today only ``grok-build-bots``); nothing here changes ``grok-build`` or
``grok-build-review``, which keep ``--no-subagents``.  Before the primary starts, AO runs at most
:data:`BOT_HARD_CAP` read-only bot workers in parallel, each in the primary's checkout, each with a
differently scoped slice of the ENG-AO-01 Graphify context, and appends their concise findings to
the primary's prompt.  Then the primary alone writes.

Invariants enforced here (and tested):

* bots are separate registry workers that are read-only, cannot spawn subagents themselves, have no
  ``subagents`` block, and never enable API billing -- so there is no recursion and no second writer;
* the count is deterministic (``min(config max_bots, BOT_HARD_CAP, applicable roles)``) and fan-out
  only happens for an explicitly supplied eligible task class (small/mechanical work declines);
* a working-tree/index/HEAD change during the fan-out is a contract violation: findings are
  discarded and the primary is blocked, never launched on a mutated checkout;
* each bot gets exactly one attempt (no retries), a bounded timeout, and no model escalation --
  failures (including quota/context limits) are recorded, never re-driven;
* bot output is assistance for the primary, never independent review;
* everything is recorded in the run manifest (``policy_manifest.bot_fanout``); there is no second
  run-history system.

The transport seam (:class:`BotTransport`) keeps the orchestration independent of the native Grok CLI
invocation shape so a future bot-capable OpenCode/xAI transport can register beside ``grok-cli``.
"""

from __future__ import annotations

import json
import threading
import time as _time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol

from . import graph_context as _graph
from .redaction import redact_text
from .registry import RegistryError, Worker
from .runner import (
    kill_process_group,
    run_worker_process_group,
    structured_failure,
    worktree_snapshot,
)

BOT_HARD_CAP = 3
FANOUT_LEVELS = 1
MODE_AO_ORCHESTRATED = "ao-orchestrated"
DEFAULT_BOT_TIMEOUT_SECONDS = 600.0
MAX_BOT_TIMEOUT_SECONDS = 3600.0
MAX_REPORT_CHARS = 3_000
MAX_FINDINGS_CHARS = 9_000
MAX_OBJECTIVE_CHARS = 2_000
BOT_JOIN_GRACE_SECONDS = 30.0  # past a bot's own subprocess timeout before its thread is written off
ROLE_BOT_IMPLEMENTATION = "bot-implementation"
ROLE_BOT_INVESTIGATION = "bot-investigation"

# Mirrors control_plane.usage_policy.TASK_CLASSES; a test pins the relationship.  Mechanical and
# routine work is never eligible: it keeps using the normal single-agent route.
TASK_CLASS_SERIOUS_INTEGRATION = "serious-integration"
TASK_CLASS_HARD_DEBUGGING = "hard-debugging"
TASK_CLASS_ARCHITECTURE_HIGH_RISK = "architecture-high-risk"
ELIGIBLE_TASK_CLASSES = frozenset(
    {TASK_CLASS_SERIOUS_INTEGRATION, TASK_CLASS_HARD_DEBUGGING, TASK_CLASS_ARCHITECTURE_HIGH_RISK}
)

KNOWN_TASK_CLASSES = frozenset({"mechanical", "routine"}) | ELIGIBLE_TASK_CLASSES

DECISION_DECLINED = "declined"
DECISION_PLANNED = "planned"
DECISION_FANNED_OUT = "fanned-out"
DECISION_NO_USABLE_FINDINGS = "no-usable-findings"
DECISION_BLOCKED = "blocked-contract-violation"

RESULT_PASS = "PASS"
RESULT_FAIL = "FAIL"
RESULT_TIMEOUT = "TIMEOUT"


@dataclass(frozen=True)
class BotRole:
    """One bounded investigation assignment."""

    key: str
    title: str
    focus: str
    instruction: str


BOT_ROLES: tuple[BotRole, ...] = (
    BotRole(
        "dependency",
        "dependency and blast-radius analysis",
        _graph.FOCUS_DEPENDENCY,
        "Trace what the scoped code imports, calls, and is used by. Report the concrete blast radius: which "
        "modules, callers, and contracts a change to the scope could break.",
    ),
    BotRole(
        "tests",
        "relevant tests and regression-risk analysis",
        _graph.FOCUS_TESTS,
        "Find the existing tests that cover the scope and the behavior they pin. Report likely regression "
        "risks and coverage gaps, and name the exact focused test files/selectors worth running.",
    ),
    BotRole(
        "impact",
        "UI, API, and documentation impact",
        _graph.FOCUS_IMPACT,
        "Identify user-facing UI, HTTP/API contracts, configuration, and maintained documentation that a "
        "change to the scope would affect or leave stale.",
    ),
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- transport seam


class BotTransport(Protocol):
    """How one read-only bot is launched and its result read; the only per-CLI code."""

    name: str

    def validate(self, primary: Worker, bot: Worker) -> None:
        """Raise :class:`RegistryError` when the pair cannot be bounded/read-only on this transport."""

    def build_command(self, bot: Worker, *, model: str | None, intensity: str, prompt: str) -> list[str]: ...

    def extract_report(self, output: str) -> str: ...

    def extract_usage(self, output: str) -> dict[str, int]: ...


def _first_json_object(output: str) -> dict[str, Any] | None:
    try:
        payload, _ = json.JSONDecoder().raw_decode(output.lstrip())
    except (json.JSONDecodeError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def _flag_value(template: Iterable[str], flag: str) -> str | None:
    tokens = list(template)
    for index, token in enumerate(tokens[:-1]):
        if token == flag:
            return tokens[index + 1]
    return None


class GrokCliTransport:
    """Native ``grok`` CLI: one headless ``--single`` process per bot (subscription/OIDC session)."""

    name = "grok-cli"

    def validate(self, primary: Worker, bot: Worker) -> None:
        if primary.cli_bin != "grok" or bot.cli_bin != "grok":
            raise RegistryError("grok-cli bot transport requires grok primary and bot workers")
        if "--no-subagents" not in primary.cli_template:
            raise RegistryError(f"primary {primary.name!r} must keep --no-subagents (AO owns the fan-out)")
        if "--no-subagents" not in bot.cli_template:
            raise RegistryError(f"bot worker {bot.name!r} must set --no-subagents (no recursive fan-out)")
        if "--disable-web-search" not in bot.cli_template:
            raise RegistryError(f"bot worker {bot.name!r} must set --disable-web-search")
        if _flag_value(bot.cli_template, "--sandbox") != "read-only":
            raise RegistryError(f"bot worker {bot.name!r} must use --sandbox read-only")
        if _flag_value(bot.cli_template, "--permission-mode") != "plan":
            raise RegistryError(f"bot worker {bot.name!r} must use --permission-mode plan")
        forbidden = {"--always-approve", "--allow-write", "--dangerously-skip-permissions"}
        if forbidden & set(bot.cli_template) or _flag_value(bot.cli_template, "--permission-mode") == "bypassPermissions":
            raise RegistryError(f"bot worker {bot.name!r} must not auto-approve or allow writes")

    def build_command(self, bot: Worker, *, model: str | None, intensity: str, prompt: str) -> list[str]:
        return bot.build_command(model=model, intensity=intensity, prompt=prompt)

    def extract_report(self, output: str) -> str:
        payload = _first_json_object(output)
        if payload is not None:
            for key in ("result", "response", "text"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return output.strip()

    def extract_usage(self, output: str) -> dict[str, int]:
        payload = _first_json_object(output)
        usage = payload.get("usage") if payload else None
        if not isinstance(usage, dict):
            return {}
        return {
            str(key): int(value)
            for key, value in usage.items()
            if isinstance(value, int) and not isinstance(value, bool) and "token" in str(key).lower()
        }


TRANSPORTS: dict[str, BotTransport] = {GrokCliTransport.name: GrokCliTransport()}


def register_transport(transport: BotTransport) -> None:
    """Register a future bot-capable transport (e.g. OpenCode/xAI); it must pass the same validation."""

    TRANSPORTS[transport.name] = transport


# --------------------------------------------------------------------------- registry validation


def validate_subagent_configs(workers: Mapping[str, Worker]) -> None:
    """Fail closed on any ``subagents`` block that could permit unbounded/writing/recursive bots."""

    for worker in workers.values():
        config = worker.subagents
        if not config:
            continue
        where = f"worker {worker.name!r} subagents"
        if config.get("mode") != MODE_AO_ORCHESTRATED:
            raise RegistryError(f"{where}: mode must be {MODE_AO_ORCHESTRATED!r}")
        if not worker.is_write_capable:
            raise RegistryError(f"{where}: only a write-capable primary may own a bot fan-out")
        if worker.allow_api_billing:
            raise RegistryError(f"{where}: API billing is never allowed")
        if config.get("levels") != FANOUT_LEVELS:
            raise RegistryError(f"{where}: fan-out is exactly one level")
        max_bots = config.get("max_bots")
        if not isinstance(max_bots, int) or isinstance(max_bots, bool) or not 1 <= max_bots <= BOT_HARD_CAP:
            raise RegistryError(f"{where}: max_bots must be an integer 1..{BOT_HARD_CAP}")
        timeout = config.get("bot_timeout_seconds", DEFAULT_BOT_TIMEOUT_SECONDS)
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0 < timeout <= MAX_BOT_TIMEOUT_SECONDS:
            raise RegistryError(f"{where}: bot_timeout_seconds must be in (0, {MAX_BOT_TIMEOUT_SECONDS:.0f}]")
        classes = config.get("eligible_task_classes")
        if not classes or not set(classes) <= ELIGIBLE_TASK_CLASSES:
            raise RegistryError(f"{where}: eligible_task_classes must be a non-empty subset of {sorted(ELIGIBLE_TASK_CLASSES)}")
        transport = TRANSPORTS.get(str(config.get("transport")))
        if transport is None:
            raise RegistryError(f"{where}: unknown transport {config.get('transport')!r}")
        bot = workers.get(str(config.get("bot_worker")))
        if bot is None:
            raise RegistryError(f"{where}: unknown bot_worker {config.get('bot_worker')!r}")
        if not bot.is_read_only:
            raise RegistryError(f"{where}: bot worker {bot.name!r} must be read-only")
        if bot.subagents:
            raise RegistryError(f"{where}: bot worker {bot.name!r} may not itself fan out (no recursive trees)")
        if bot.allow_api_billing or ROLE_BOT_INVESTIGATION not in bot.roles:
            raise RegistryError(f"{where}: bot worker {bot.name!r} must declare {ROLE_BOT_INVESTIGATION!r} and no API billing")
        transport.validate(worker, bot)


# --------------------------------------------------------------------------- planning


@dataclass(frozen=True)
class FanoutPlan:
    primary: str
    bot_worker: str
    transport: str
    task_class: str | None
    enabled: bool
    reason: str
    max_bots: int
    bot_timeout_seconds: float
    roles: tuple[BotRole, ...] = ()
    skipped_roles: tuple[str, ...] = ()

    def evidence(self) -> dict[str, Any]:
        return {
            "schema": 1,
            "mode": MODE_AO_ORCHESTRATED,
            "transport": self.transport,
            "primary_worker": self.primary,
            "bot_worker": self.bot_worker,
            "task_class": self.task_class,
            "decision": DECISION_PLANNED if self.enabled else DECISION_DECLINED,
            "reason": self.reason,
            "used": False,
            "max_bots": self.max_bots,
            "bot_count": len(self.roles),
            "planned_roles": [role.key for role in self.roles],
            "skipped_roles": list(self.skipped_roles),
            "levels": FANOUT_LEVELS,
            "bots_read_only": True,
            "primary_only_writer": True,
            "retries": 0,
            "model_escalation": False,
            "api_billing": False,
            "independent_review": False,
            "independent_review_note": "bot output is assistance for the primary, not independent review",
            "bots": [],
        }


def plan_fanout(worker: Worker, task_class: str | None, scope_paths: Iterable[str]) -> FanoutPlan | None:
    """Deterministic, pure fan-out decision.  ``None`` for a worker that has no bot mode at all."""

    config = worker.subagents
    if not config:
        return None
    max_bots = min(int(config["max_bots"]), BOT_HARD_CAP)
    base: dict[str, Any] = {
        "primary": worker.name,
        "bot_worker": str(config["bot_worker"]),
        "transport": str(config["transport"]),
        "task_class": task_class,
        "max_bots": max_bots,
        "bot_timeout_seconds": float(config.get("bot_timeout_seconds", DEFAULT_BOT_TIMEOUT_SECONDS)),
    }
    if not task_class:
        return FanoutPlan(
            enabled=False,
            reason="no task class supplied; bot fan-out is never automatic (single-agent primary)",
            **base,
        )
    if task_class not in set(config["eligible_task_classes"]):
        return FanoutPlan(
            enabled=False,
            reason=f"task class {task_class!r} is not eligible for bot fan-out; small/mechanical work stays single-agent",
            **base,
        )
    paths = [path for path in scope_paths if not _graph.is_sensitive_path(path)]
    applicable: list[BotRole] = []
    skipped: list[str] = []
    for role in BOT_ROLES:
        if role.key == "impact" and not any(_graph.is_impact_path(path) for path in paths):
            skipped.append(f"{role.key}: no UI/API/documentation surface in scope")
            continue
        applicable.append(role)
    chosen = tuple(applicable[:max_bots])
    skipped.extend(f"{role.key}: over the {max_bots}-bot bound" for role in applicable[max_bots:])
    return FanoutPlan(
        enabled=True,
        reason=f"eligible task class {task_class!r}; {len(chosen)} bounded read-only bot(s)",
        roles=chosen,
        skipped_roles=tuple(skipped),
        **base,
    )


# --------------------------------------------------------------------------- execution


@dataclass
class FanoutOutcome:
    evidence: dict[str, Any]
    prompt_section: str = ""
    blocked_reason: str | None = None
    elapsed_seconds: float = 0.0
    logs: dict[str, str] | None = None


def _bot_prompt(
    role: BotRole, bot_id: str, task: str, objective: str, scope: list[str], policy_prompt: str, context_text: str
) -> str:
    scope_line = ", ".join(scope) if scope else "(no path scope; inspect only what the assignment needs)"
    envelope = (
        f"\n--- BOT TASK ENVELOPE ---\nTask: {task}. Bot: {bot_id} ({role.title}). Repository scope: {scope_line}. "
        "Access: read-only; do not create, modify, or delete any file. Do not commit, push, merge, or change "
        "branches or repository state. Do not spawn subagents or bots. Do not use the web. Do not read .env, "
        "data/, or credential/secret files, and do not expose secrets. Make no live or billable provider calls. "
        f"Assignment: {role.instruction} Overall work the primary worker will do (context only, do not do it): "
        f"{redact_text(objective)[:MAX_OBJECTIVE_CHARS]}\n"
        "Reply with concise findings (about 300 words or fewer) as a short bullet list naming repository "
        "paths you verified in the current source, and say plainly what you could not verify. This is "
        "assistance for the primary worker, not an independent review or approval."
    )
    return policy_prompt + context_text + envelope


def _failure_category(text: str) -> str:
    from .control_plane.usage_policy import classify_failure

    return classify_failure(text)


def run_fanout(
    plan: FanoutPlan,
    *,
    bot: Worker,
    root: Path,
    task: str,
    objective: str,
    scope_paths: list[str],
    extra_seed_paths: Iterable[str],
    policy_prompt: str,
    project_id: str | None,
    model: str | None = None,
    intensity: str | None = None,
    log_dir: Path | None = None,
    timeout_cap: float | None = None,
) -> FanoutOutcome:
    """Run ``plan``'s bots in parallel, read-only, one attempt each; never raises for a bot failure."""

    transport = TRANSPORTS[plan.transport]
    evidence = plan.evidence()
    if not plan.enabled or not plan.roles:
        return FanoutOutcome(evidence=evidence)
    resolved_intensity = intensity or bot.default_intensity
    bot_timeout = plan.bot_timeout_seconds if timeout_cap is None else max(1.0, min(plan.bot_timeout_seconds, timeout_cap))
    # Every bot sees only non-sensitive scope paths; the graph slices are derived from the same seeds.
    scope = [path for path in scope_paths if not _graph.is_sensitive_path(path)]
    seeds = [*scope, *(path for path in extra_seed_paths if not _graph.is_sensitive_path(path))]
    contexts = _graph.build_graph_contexts(
        root, seeds, [role.focus for role in plan.roles], project_id=project_id
    )
    before = worktree_snapshot(root)
    started = _time.monotonic()
    groups: dict[str, int] = {}  # live bot process groups, so a bot that outlives its bound is reaped
    groups_lock = threading.Lock()

    def run_one(index: int, role: BotRole) -> dict[str, Any]:
        bot_id = f"bot-{index}-{role.key}"
        context = contexts[role.focus]
        prompt = _bot_prompt(role, bot_id, task, objective, scope, policy_prompt, context.text)
        command = transport.build_command(bot, model=model, intensity=resolved_intensity, prompt=prompt)
        record: dict[str, Any] = {
            "id": bot_id,
            "role": role.key,
            "title": role.title,
            "focus": role.focus,
            "scope": scope,
            "worker": bot.name,
            "provider": bot.provider,
            "model": model or bot.effective_model,
            "intensity": resolved_intensity,
            "read_only": True,
            "attempts": 1,
            "retries": 0,
            "graph_context": {
                "supplied": context.injected,
                "status": context.evidence.get("status"),
                "reason": context.evidence.get("reason"),
                "focus": role.focus,
                "context_characters": context.evidence.get("context_characters", 0),
            },
            "started_at": _now(),
        }
        began = _time.monotonic()

        def track(pgid: int | None, _bot_id: str = bot_id) -> None:
            with groups_lock:
                if pgid is None:
                    groups.pop(_bot_id, None)
                else:
                    groups[_bot_id] = pgid

        try:
            exit_status, output = run_worker_process_group(command, root, timeout=bot_timeout, on_group=track)
        except Exception as exc:  # noqa: BLE001 - one bot's launch failure must not fail the fan-out
            exit_status, output = 1, f"bot launch failed: {type(exc).__name__}"
        record["finished_at"] = _now()
        record["duration_seconds"] = round(_time.monotonic() - began, 3)
        record["exit_status"] = exit_status
        failure = structured_failure(output)
        if exit_status == 124:
            record.update(result=RESULT_TIMEOUT, failure_reason=f"timed out after {bot_timeout:g}s")
        elif failure or exit_status != 0:
            record.update(result=RESULT_FAIL, failure_reason=redact_text(failure or f"exit status {exit_status}")[:400])
        else:
            record.update(result=RESULT_PASS, failure_reason=None)
        if record["result"] != RESULT_PASS:
            # Recorded only: a quota/rate/context failure never escalates the model or retries.
            record["failure_category"] = _failure_category(f"{record['failure_reason']}\n{output[-2000:]}")
        record["usage"] = transport.extract_usage(output)
        record["_report"] = redact_text(transport.extract_report(output))[:MAX_REPORT_CHARS] if record["result"] == RESULT_PASS else ""
        record["_log"] = output
        return record

    def _failed_record(index: int, role: BotRole, result: str, reason: str, category: str) -> dict[str, Any]:
        return {
            "id": f"bot-{index}-{role.key}", "role": role.key, "title": role.title, "focus": role.focus,
            "scope": scope, "worker": bot.name, "provider": bot.provider, "model": model or bot.effective_model,
            "intensity": resolved_intensity, "read_only": True, "attempts": 1, "retries": 0,
            "graph_context": {"supplied": False, "status": None, "reason": None, "focus": role.focus,
                              "context_characters": 0},
            "result": result, "failure_reason": reason, "failure_category": category, "usage": {},
            "_report": "", "_log": "",
        }

    # One thread per bot (at most BOT_HARD_CAP); results keep the deterministic role order.
    slots: list[dict[str, Any] | None] = [None] * len(plan.roles)

    def worker(index: int, role: BotRole) -> None:
        try:
            slots[index - 1] = run_one(index, role)
        except Exception as exc:  # noqa: BLE001 - a bot-side defect is a recorded bot failure, never a lost bot
            slots[index - 1] = _failed_record(index, role, RESULT_FAIL, f"bot could not start: {type(exc).__name__}", "capability")

    threads = [
        threading.Thread(target=worker, args=(index, role), name=f"ao-bot-{role.key}", daemon=True)
        for index, role in enumerate(plan.roles, start=1)
    ]
    for thread in threads:
        thread.start()
    deadline = _time.monotonic() + bot_timeout + BOT_JOIN_GRACE_SECONDS
    for thread in threads:
        thread.join(max(0.0, deadline - _time.monotonic()))
    stuck = [thread.name.removeprefix("ao-bot-") for thread in threads if thread.is_alive()]
    if stuck:
        with groups_lock:
            leftover = list(groups.values())
        for pgid in leftover:
            kill_process_group(pgid)
        for thread in threads:
            thread.join(2.0)
        stuck = [thread.name.removeprefix("ao-bot-") for thread in threads if thread.is_alive()]
    records = []
    for index, role in enumerate(plan.roles, start=1):
        record = slots[index - 1]
        if record is None:  # thread never returned: recorded as a timeout; the fan-out then fails closed instead of hanging
            record = _failed_record(index, role, "TIMEOUT", "bot did not finish within its bound", "provider-outage")
        records.append(record)
    elapsed = round(_time.monotonic() - started, 3)

    after = worktree_snapshot(root)
    changed = sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))
    logs = {record["id"]: record.pop("_log") for record in records}
    reports = {record["id"]: record.pop("_report") for record in records}
    passed = [record for record in records if record["result"] == RESULT_PASS]
    evidence.update(
        bots=records,
        bot_count=len(records),
        graph_context_supplied=any(record["graph_context"]["supplied"] for record in records),
        elapsed_seconds=elapsed,
        succeeded=len(passed),
        failed=len(records) - len(passed),
        partial_failure=0 < len(passed) < len(records),
    )
    if stuck:
        # A bot that outlived its bound may still touch the checkout: never start the writer beside it.
        reason = "bot(s) still running after their bound: " + ", ".join(stuck)
        evidence.update(decision=DECISION_BLOCKED, used=False, reason=reason, contract_violation=[])
        return FanoutOutcome(evidence=evidence, blocked_reason=reason + ". Fail closed: the primary was not launched.", elapsed_seconds=elapsed, logs=logs)
    if changed:
        reason = "read-only bot fan-out changed the working tree: " + ", ".join(changed)
        evidence.update(decision=DECISION_BLOCKED, used=False, reason=reason, contract_violation=changed)
        return FanoutOutcome(evidence=evidence, blocked_reason=reason + ". Treat as a contract violation.", elapsed_seconds=elapsed, logs=logs)
    if not passed:
        evidence.update(
            decision=DECISION_NO_USABLE_FINDINGS,
            used=False,
            reason="no bot produced usable findings; primary continues single-agent (no retry, no escalation)",
        )
        return FanoutOutcome(evidence=evidence, elapsed_seconds=elapsed, logs=logs)
    evidence.update(decision=DECISION_FANNED_OUT, used=True, reason="bot findings supplied to the primary worker")
    sections = [
        f"[{record['id']} - {record['title']} - {record['result']}]\n{reports[record['id']]}" for record in passed
    ]
    failed = [f"{record['id']}: {record['result']} ({record['failure_reason']})" for record in records if record["result"] != RESULT_PASS]
    body = "\n\n".join(sections)[:MAX_FINDINGS_CHARS]
    if failed:
        body += "\n\nBots that produced no findings (recorded, not retried): " + "; ".join(failed)
    section = (
        "\n--- BOT FINDINGS (advisory read-only investigation; NOT independent review) ---\n"
        "Bounded read-only Grok bots inspected the current checkout in parallel before you started. Their output is "
        "unverified assistance: verify against the actual source, treat the source tree and policy as authoritative, "
        "and you alone may write. It never replaces the provider-diverse independent review.\n"
        f"{body}\n"
    )
    return FanoutOutcome(evidence=evidence, prompt_section=section, elapsed_seconds=elapsed, logs=logs)


def write_bot_logs(outcome: FanoutOutcome, worker_dir: Path) -> None:
    """Persist each bot's redacted log under the primary's evidence directory."""

    for bot_id, text in (outcome.logs or {}).items():
        directory = worker_dir / "bots" / bot_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "run.log").write_text(redact_text(text or ""), encoding="utf-8")
    for record in outcome.evidence.get("bots", []):
        if record["id"] in (outcome.logs or {}):
            record["log"] = f"bots/{record['id']}/run.log"
