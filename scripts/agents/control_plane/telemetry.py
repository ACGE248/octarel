"""Slice 3 telemetry: token accounting, execution-route classification, cost
accounting, hard budget checks, quota visibility, and checkpoint status.

Everything here is a deterministic, local computation over data the control
plane already has (a worker's structured CLI JSON, ``workers.json``
``cost_class``, and local ``git`` plumbing). Nothing in this module makes a
network call or a provider/model request — it is safe to call on every
dashboard refresh, matching the "zero-AI dashboard refresh" guarantee the
rest of ``control_plane`` already provides.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.agents.probe import run_probe
from scripts.ci.runtime_paths import has_non_runtime_changes

# ------------------------------------------------------------------ tokens

TOKEN_EXACT = "EXACT"
TOKEN_ESTIMATED = "ESTIMATED"
TOKEN_UNKNOWN = "UNKNOWN"
TOKEN_MODES = frozenset({TOKEN_EXACT, TOKEN_ESTIMATED, TOKEN_UNKNOWN})


@dataclass(frozen=True)
class TokenUsage:
    mode: str = TOKEN_UNKNOWN
    input_tokens: int | None = None
    output_tokens: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"mode": self.mode, "input_tokens": self.input_tokens, "output_tokens": self.output_tokens}


def extract_token_usage(output: str) -> TokenUsage:
    """Read exact token counts from a worker CLI's structured JSON, if present.

    Reads the same ``modelUsage`` field ``runner.structured_actual_model``
    already parses. Conservative by construction: this function only ever
    returns ``EXACT`` (numeric counts actually present) or ``UNKNOWN`` — it
    never guesses at an ``ESTIMATED`` figure itself. A caller that wants an
    estimate must build one explicitly via :func:`estimated_token_usage` so
    an estimate can never be silently mistaken for a real count.
    """

    try:
        payload, _ = json.JSONDecoder().raw_decode((output or "").lstrip())
    except (json.JSONDecodeError, TypeError):
        return TokenUsage()
    if not isinstance(payload, dict):
        return TokenUsage()
    usage = payload.get("modelUsage")
    if not isinstance(usage, dict) or not usage:
        return TokenUsage()
    total_in = 0
    total_out = 0
    found = False
    for entry in usage.values():
        if not isinstance(entry, dict):
            continue
        in_tokens = entry.get("inputTokens")
        out_tokens = entry.get("outputTokens")
        if isinstance(in_tokens, int):
            total_in += in_tokens
            found = True
        if isinstance(out_tokens, int):
            total_out += out_tokens
            found = True
    if not found:
        return TokenUsage()
    return TokenUsage(mode=TOKEN_EXACT, input_tokens=total_in, output_tokens=total_out)


def estimated_token_usage(*, input_tokens: int, output_tokens: int) -> TokenUsage:
    """Build an explicitly labelled ``ESTIMATED`` usage. Never inferred silently."""

    return TokenUsage(mode=TOKEN_ESTIMATED, input_tokens=input_tokens, output_tokens=output_tokens)


# --------------------------------------------------------- execution routes

ROUTE_SUBSCRIPTION = "SUBSCRIPTION"
ROUTE_API = "API"
ROUTE_FREE = "FREE"
ROUTE_UNKNOWN = "UNKNOWN"
EXECUTION_ROUTES = frozenset({ROUTE_SUBSCRIPTION, ROUTE_API, ROUTE_FREE, ROUTE_UNKNOWN})

# workers.json ``cost_class`` -> execution route. Hand-maintained on purpose,
# the same way ``provider_state.py`` hand-seeds its catalog: a new cost_class
# value defaults to UNKNOWN rather than silently becoming billable or free.
_COST_CLASS_ROUTES: dict[str, str] = {
    "premium-subscription": ROUTE_SUBSCRIPTION,
    "metered-configured": ROUTE_API,
    "free-verified": ROUTE_FREE,
    "supplemental-configured": ROUTE_FREE,
    "free-dynamic": ROUTE_FREE,
    "optional-overflow": ROUTE_API,
    "catalog-only": ROUTE_UNKNOWN,
}


def execution_route_for_cost_class(cost_class: str) -> str:
    return _COST_CLASS_ROUTES.get(cost_class, ROUTE_UNKNOWN)


# ------------------------------------------------------------ cost accounting


@dataclass(frozen=True)
class PricingSnapshot:
    """One versioned, hand-entered $/1K-token rate for an API-route worker/model."""

    version: str
    worker: str
    model: str
    input_per_1k_usd: float
    output_per_1k_usd: float


@dataclass(frozen=True)
class CostResult:
    route: str
    usd: float | None
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {"route": self.route, "usd": self.usd, "reason": self.reason}


def compute_cost(
    *,
    worker_cost_class: str,
    token_usage: TokenUsage,
    pricing: PricingSnapshot | None,
) -> CostResult:
    """Compute a run's dollar cost, or explain why there isn't one.

    Subscription and free routes never produce a fictional API dollar
    figure, regardless of token counts. An API route without both a pricing
    snapshot and known (``EXACT``/``ESTIMATED``) token counts also reports
    ``usd=None`` rather than a guess.
    """

    route = execution_route_for_cost_class(worker_cost_class)
    if route == ROUTE_SUBSCRIPTION:
        return CostResult(route=route, usd=None, reason="subscription route: not billed per token")
    if route == ROUTE_FREE:
        return CostResult(route=route, usd=None, reason="free route: no API cost")
    if route == ROUTE_UNKNOWN:
        return CostResult(route=route, usd=None, reason="execution route unknown: no cost_class mapping")
    # route == ROUTE_API
    if pricing is None:
        return CostResult(route=route, usd=None, reason="API route but no pricing snapshot configured")
    if token_usage.mode == TOKEN_UNKNOWN or token_usage.input_tokens is None or token_usage.output_tokens is None:
        return CostResult(route=route, usd=None, reason="API route but token counts are UNKNOWN")
    usd = (token_usage.input_tokens / 1000.0) * pricing.input_per_1k_usd
    usd += (token_usage.output_tokens / 1000.0) * pricing.output_per_1k_usd
    return CostResult(route=route, usd=round(usd, 6), reason=f"pricing snapshot {pricing.version}")


# ------------------------------------------------------------------ budgets


@dataclass(frozen=True)
class BudgetLimits:
    per_request_usd: float | None = None
    daily_usd: float | None = None
    weekly_usd: float | None = None
    monthly_usd: float | None = None


@dataclass(frozen=True)
class BudgetCheck:
    blocked: bool
    reason: str


def check_budget(
    *,
    limits: BudgetLimits,
    request_usd: float | None,
    daily_spent_usd: float = 0.0,
    weekly_spent_usd: float = 0.0,
    monthly_spent_usd: float = 0.0,
) -> BudgetCheck:
    """Hard budget gate feeding the ``COST_BLOCKED`` provider state.

    A ``None`` request cost (subscription/free/unknown route, or an API
    route with unknown tokens) never trips a dollar budget by itself — only
    an actually computed API cost, or already-accumulated spend, can.
    """

    if request_usd is not None and limits.per_request_usd is not None and request_usd > limits.per_request_usd:
        return BudgetCheck(True, f"per-request cost ${request_usd:.4f} exceeds limit ${limits.per_request_usd:.4f}")
    if limits.daily_usd is not None and daily_spent_usd > limits.daily_usd:
        return BudgetCheck(True, f"daily spend ${daily_spent_usd:.4f} exceeds limit ${limits.daily_usd:.4f}")
    if limits.weekly_usd is not None and weekly_spent_usd > limits.weekly_usd:
        return BudgetCheck(True, f"weekly spend ${weekly_spent_usd:.4f} exceeds limit ${limits.weekly_usd:.4f}")
    if limits.monthly_usd is not None and monthly_spent_usd > limits.monthly_usd:
        return BudgetCheck(True, f"monthly spend ${monthly_spent_usd:.4f} exceeds limit ${limits.monthly_usd:.4f}")
    return BudgetCheck(False, "within budget")


# --------------------------------------------------------------------- quota


@dataclass(frozen=True)
class QuotaWindow:
    label: str
    state: str = TOKEN_UNKNOWN
    remaining_fraction: float | None = None
    reset_at: str | None = None
    reason: str = "no legitimate local source for this quota window"

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "state": self.state,
            "remaining_fraction": self.remaining_fraction,
            "reset_at": self.reset_at,
            "reason": self.reason,
        }


def claude_quota_windows() -> list[QuotaWindow]:
    """Claude 5-hour + weekly quota/reset state, where legitimately available.

    No locally installed tool in this registry (``claude`` CLI) exposes
    account-level quota/reset telemetry today — only per-run ``modelUsage``
    token counts, which :func:`extract_token_usage` already handles. This
    returns explicit ``UNKNOWN`` windows rather than a fabricated number.
    Swap in a real reader here the day such a source exists; every caller
    already treats these windows as opaque.
    """

    return [QuotaWindow(label="claude_5h"), QuotaWindow(label="claude_weekly")]


# --------------------------------------------------------------- account facts

# ENG-AGENT-02-S7 (issue #97): per-provider account/usage facts, each labelled
# with exactly how it was obtained. The Control Center must never present a
# number it invented — every fact below is either a real local CLI status
# query (never a billable API call) or an explicit "not exposed" report.
SOURCE_CLI_REPORTED = "CLI_REPORTED"
SOURCE_LIVE_PROVIDER = "LIVE_PROVIDER"
SOURCE_LOCAL_ACCOUNTING = "LOCAL_ACCOUNTING"
SOURCE_CONFIGURED_LIMIT = "CONFIGURED_LIMIT"
SOURCE_NOT_EXPOSED = "NOT_EXPOSED"
ACCOUNT_FACT_SOURCES = frozenset(
    {
        SOURCE_LIVE_PROVIDER,
        SOURCE_CLI_REPORTED,
        SOURCE_LOCAL_ACCOUNTING,
        SOURCE_CONFIGURED_LIMIT,
        SOURCE_NOT_EXPOSED,
    }
)


@dataclass(frozen=True)
class AccountFact:
    worker: str
    label: str
    value: str | None
    source: str
    note: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "worker": self.worker,
            "label": self.label,
            "value": self.value,
            "source": self.source,
            "note": self.note,
        }


def _not_exposed(worker: str, label: str, note: str | None = None) -> AccountFact:
    return AccountFact(worker=worker, label=label, value=None, source=SOURCE_NOT_EXPOSED, note=note)


_STANDARD_USAGE_LABELS = (
    "session_usage",
    "rolling_window_usage",
    "weekly_usage",
    "reset_time",
    "rpm",
    "rpd",
    "tpm",
    "balance",
    "provider_spend",
    "configured_spend_ceiling",
)


def _unexposed_usage_facts(worker: str) -> list[AccountFact]:
    notes = {}
    if worker == "codex-review":
        notes["rolling_window_usage"] = "Subscription quota not exposed by Codex CLI"
    elif worker.startswith("grok"):
        notes["rolling_window_usage"] = "Account-wide quota is not exposed; Grok reports usage per session only"
    elif "gemini" in worker or worker.startswith("antigravity"):
        notes["rolling_window_usage"] = "Rate-limit data is not exposed by the configured local CLI"
    return [_not_exposed(worker, label, notes.get(label)) for label in _STANDARD_USAGE_LABELS]


def claude_account_facts(*, timeout: float = 5.0) -> list[AccountFact]:
    """Real, locally CLI-reported Anthropic account facts for ``claude-code``.

    Runs ``claude auth status --json`` — a status query against the CLI's
    own already-authenticated local session, never a billable API request
    and never a request this module makes with any other party's
    credentials. Deliberately not part of the dashboard's 2-second refresh
    poll (spawning a CLI process that often is neither cheap nor
    necessary); callers should fetch this only on demand or behind a
    short server-side TTL cache (see ``dashboard_api.py``'s ``/api/usage``).

    No numeric usage/quota-remaining/reset-time fact is ever included here:
    this CLI does not report any, so asking for one would require guessing.
    """

    worker = "claude-code"
    try:
        result = run_probe(["claude", "auth", "status", "--json"], timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return [_not_exposed(worker, "subscription_type")]

    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return [_not_exposed(worker, "subscription_type")]

    if not payload.get("loggedIn"):
        return [_not_exposed(worker, "subscription_type")]

    facts = []
    subscription = payload.get("subscriptionType")
    facts.append(
        AccountFact(worker=worker, label="subscription_type", value=subscription, source=SOURCE_CLI_REPORTED)
        if subscription
        else _not_exposed(worker, "subscription_type")
    )
    auth_method = payload.get("authMethod")
    if auth_method:
        facts.append(AccountFact(worker=worker, label="auth_method", value=auth_method, source=SOURCE_CLI_REPORTED))
    org_name = payload.get("orgName")
    if org_name:
        facts.append(AccountFact(worker=worker, label="organization", value=org_name, source=SOURCE_CLI_REPORTED))
    # Numeric usage (session/5h/weekly percent used, requests remaining, reset
    # time) is not reported by this CLI at all — explicit, not a guess.
    facts.append(_not_exposed(worker, "usage_window"))
    facts.extend(_unexposed_usage_facts(worker))
    return facts


def account_facts_for_worker(worker_name: str) -> list[AccountFact]:
    """Dispatch to the one real per-worker account-facts reader that exists today.

    Every other worker in this registry has no local, non-billable way to
    report account/subscription facts (see module docstring's truthfulness
    requirement) — reported as a single explicit ``NOT_EXPOSED`` fact rather
    than silently omitted, so the Control Center can render "Usage
    unavailable from provider" instead of just showing nothing.
    """

    if worker_name == "claude-code":
        return claude_account_facts()
    return [_not_exposed(worker_name, "subscription_type"), *_unexposed_usage_facts(worker_name)]


# ------------------------------------------------------- checkpoint visibility


@dataclass(frozen=True)
class CheckpointStatus:
    worktree: str
    branch: str | None
    last_commit_sha: str | None
    last_commit_at: str | None
    has_uncommitted_changes: bool
    has_unpushed_commits: bool
    ahead_of_upstream: int | None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "worktree": self.worktree,
            "branch": self.branch,
            "last_commit_sha": self.last_commit_sha,
            "last_commit_at": self.last_commit_at,
            "has_uncommitted_changes": self.has_uncommitted_changes,
            "has_unpushed_commits": self.has_unpushed_commits,
            "ahead_of_upstream": self.ahead_of_upstream,
            "error": self.error,
        }


def _run_git(args: list[str], cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=10, check=False)
    return (result.stdout or "").strip()


def checkpoint_status(worktree: Path) -> CheckpointStatus:
    """Deterministic, local ``git`` read of one worktree's recovery state.

    Only local refs and the working tree are read — never a network
    fetch/push — so this reflects state as of the last time something
    fetched, exactly like ``git branch -vv`` does.
    """

    try:
        branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], worktree) or None
        sha = _run_git(["rev-parse", "HEAD"], worktree) or None
        commit_at = _run_git(["log", "-1", "--format=%cI"], worktree) or None
        # ENG-AGENT-16 (issue #146): CP runtime/evidence writes are not
        # product changes and must not read as "uncommitted" here. Read the
        # porcelain status directly (has_non_runtime_changes) rather than via
        # this module's own `.strip()`-based _run_git, which can corrupt the
        # first porcelain line's fixed-width status prefix.
        has_uncommitted = has_non_runtime_changes(worktree, untracked="normal")
        upstream = _run_git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], worktree)
        ahead: int | None = None
        has_unpushed = False
        if upstream:
            counts = _run_git(["rev-list", "--left-right", "--count", f"{upstream}...HEAD"], worktree)
            if counts:
                _behind, _, ahead_text = counts.partition("\t")
                try:
                    ahead = int(ahead_text)
                    has_unpushed = ahead > 0
                except ValueError:
                    ahead = None
        else:
            has_unpushed = True
        return CheckpointStatus(
            worktree=str(worktree),
            branch=branch,
            last_commit_sha=sha,
            last_commit_at=commit_at,
            has_uncommitted_changes=has_uncommitted,
            has_unpushed_commits=has_unpushed,
            ahead_of_upstream=ahead,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return CheckpointStatus(
            worktree=str(worktree),
            branch=None,
            last_commit_sha=None,
            last_commit_at=None,
            has_uncommitted_changes=False,
            has_unpushed_commits=False,
            ahead_of_upstream=None,
            error=str(exc),
        )
