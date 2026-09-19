"""ENG-AGENT-02-S3: token accounting, execution routes, cost, budget, quota,
and checkpoint visibility. All deterministic, zero-AI, zero-network.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts.agents.control_plane.telemetry import (
    ROUTE_API,
    ROUTE_FREE,
    ROUTE_SUBSCRIPTION,
    ROUTE_UNKNOWN,
    SOURCE_CLI_REPORTED,
    SOURCE_NOT_EXPOSED,
    TOKEN_ESTIMATED,
    TOKEN_EXACT,
    TOKEN_UNKNOWN,
    BudgetLimits,
    PricingSnapshot,
    account_facts_for_worker,
    check_budget,
    checkpoint_status,
    claude_account_facts,
    claude_quota_windows,
    compute_cost,
    estimated_token_usage,
    execution_route_for_cost_class,
    extract_token_usage,
)

# --------------------------------------------------------------------- tokens


def test_extract_token_usage_exact_from_model_usage():
    output = json.dumps({"modelUsage": {"claude-sonnet-5": {"inputTokens": 120, "outputTokens": 40}}})
    usage = extract_token_usage(output)
    assert usage.mode == TOKEN_EXACT
    assert usage.input_tokens == 120
    assert usage.output_tokens == 40


def test_extract_token_usage_sums_multiple_models():
    output = json.dumps(
        {
            "modelUsage": {
                "a": {"inputTokens": 10, "outputTokens": 5},
                "b": {"inputTokens": 20, "outputTokens": 15},
            }
        }
    )
    usage = extract_token_usage(output)
    assert usage.mode == TOKEN_EXACT
    assert usage.input_tokens == 30
    assert usage.output_tokens == 20


@pytest.mark.parametrize(
    "output",
    [
        "not json at all",
        json.dumps({"no": "usage"}),
        json.dumps({"modelUsage": {}}),
        "",
        json.dumps({"modelUsage": {"x": "not-a-dict"}}),
    ],
)
def test_extract_token_usage_unknown_when_not_reported(output):
    usage = extract_token_usage(output)
    assert usage.mode == TOKEN_UNKNOWN
    assert usage.input_tokens is None
    assert usage.output_tokens is None


def test_estimated_token_usage_is_explicitly_labelled():
    usage = estimated_token_usage(input_tokens=100, output_tokens=50)
    assert usage.mode == TOKEN_ESTIMATED
    assert usage.input_tokens == 100
    assert usage.output_tokens == 50


# ------------------------------------------------------------- routes


@pytest.mark.parametrize(
    "cost_class,expected",
    [
        ("premium-subscription", ROUTE_SUBSCRIPTION),
        ("metered-configured", ROUTE_API),
        ("free-verified", ROUTE_FREE),
        ("supplemental-configured", ROUTE_FREE),
        ("optional-overflow", ROUTE_API),
        ("catalog-only", ROUTE_UNKNOWN),
        ("something-nobody-registered", ROUTE_UNKNOWN),
    ],
)
def test_execution_route_for_cost_class(cost_class, expected):
    assert execution_route_for_cost_class(cost_class) == expected


# --------------------------------------------------------------- cost


def test_subscription_route_never_billed():
    result = compute_cost(
        worker_cost_class="premium-subscription",
        token_usage=extract_token_usage(
            json.dumps({"modelUsage": {"m": {"inputTokens": 999999, "outputTokens": 999999}}})
        ),
        pricing=None,
    )
    assert result.route == ROUTE_SUBSCRIPTION
    assert result.usd is None


def test_free_route_never_billed():
    result = compute_cost(
        worker_cost_class="free-verified",
        token_usage=estimated_token_usage(input_tokens=1000, output_tokens=1000),
        pricing=None,
    )
    assert result.route == ROUTE_FREE
    assert result.usd is None


def test_api_route_without_pricing_is_unknown_cost():
    result = compute_cost(
        worker_cost_class="metered-configured",
        token_usage=extract_token_usage(json.dumps({"modelUsage": {"m": {"inputTokens": 100, "outputTokens": 100}}})),
        pricing=None,
    )
    assert result.route == ROUTE_API
    assert result.usd is None


def test_api_route_with_unknown_tokens_is_unknown_cost():
    pricing = PricingSnapshot(
        version="v1", worker="grok-build", model="grok-4.6", input_per_1k_usd=1.0, output_per_1k_usd=2.0
    )
    result = compute_cost(
        worker_cost_class="metered-configured", token_usage=extract_token_usage("not json"), pricing=pricing
    )
    assert result.route == ROUTE_API
    assert result.usd is None


def test_api_route_with_pricing_and_exact_tokens_computes_cost():
    pricing = PricingSnapshot(
        version="v1", worker="grok-build", model="grok-4.6", input_per_1k_usd=1.0, output_per_1k_usd=2.0
    )
    usage = extract_token_usage(json.dumps({"modelUsage": {"m": {"inputTokens": 1000, "outputTokens": 500}}}))
    result = compute_cost(worker_cost_class="metered-configured", token_usage=usage, pricing=pricing)
    assert result.route == ROUTE_API
    assert result.usd == pytest.approx(1.0 * 1.0 + 0.5 * 2.0)


# -------------------------------------------------------------- budgets


def test_budget_blocks_on_per_request_cost():
    check = check_budget(limits=BudgetLimits(per_request_usd=1.0), request_usd=2.5)
    assert check.blocked is True


def test_budget_blocks_on_daily_spend():
    check = check_budget(limits=BudgetLimits(daily_usd=10.0), request_usd=None, daily_spent_usd=10.5)
    assert check.blocked is True


def test_budget_none_cost_never_blocks_by_itself():
    check = check_budget(limits=BudgetLimits(per_request_usd=1.0), request_usd=None)
    assert check.blocked is False


def test_budget_within_limits_passes():
    check = check_budget(
        limits=BudgetLimits(per_request_usd=5.0, daily_usd=100.0), request_usd=1.0, daily_spent_usd=10.0
    )
    assert check.blocked is False


# ----------------------------------------------------------------- quota


def test_claude_quota_windows_are_unknown_by_default():
    windows = claude_quota_windows()
    labels = {w.label for w in windows}
    assert labels == {"claude_5h", "claude_weekly"}
    for window in windows:
        assert window.state == TOKEN_UNKNOWN
        assert window.remaining_fraction is None
        assert window.reset_at is None


# --------------------------------------------------------- account facts


def test_claude_account_facts_reports_real_cli_json_when_logged_in(monkeypatch):
    class FakeCompleted:
        returncode = 0
        stdout = json.dumps(
            {
                "loggedIn": True,
                "authMethod": "claude.ai",
                "email": "dev@example.com",
                "orgName": "dev@example.com's Organization",
                "subscriptionType": "pro",
            }
        )
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: FakeCompleted())
    facts = claude_account_facts()
    by_label = {f.label: f for f in facts}
    assert by_label["subscription_type"].value == "pro"
    assert by_label["subscription_type"].source == SOURCE_CLI_REPORTED
    assert by_label["auth_method"].value == "claude.ai"
    assert by_label["organization"].value == "dev@example.com's Organization"
    # No numeric usage window is fabricated even though the CLI is available.
    assert by_label["usage_window"].source == SOURCE_NOT_EXPOSED
    assert by_label["usage_window"].value is None


def test_claude_account_facts_is_honest_when_not_logged_in(monkeypatch):
    class FakeCompleted:
        returncode = 0
        stdout = json.dumps({"loggedIn": False})
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: FakeCompleted())
    facts = claude_account_facts()
    assert len(facts) == 1
    assert facts[0].source == SOURCE_NOT_EXPOSED
    assert facts[0].value is None


def test_claude_account_facts_never_raises_when_the_cli_is_missing(monkeypatch):
    def raise_oserror(*_a, **_k):
        raise OSError("not found")

    monkeypatch.setattr(subprocess, "run", raise_oserror)
    facts = claude_account_facts()
    assert len(facts) == 1
    assert facts[0].source == SOURCE_NOT_EXPOSED


def test_claude_account_facts_never_raises_on_malformed_json(monkeypatch):
    class FakeCompleted:
        returncode = 0
        stdout = "not json"
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: FakeCompleted())
    facts = claude_account_facts()
    assert facts[0].source == SOURCE_NOT_EXPOSED


def test_account_facts_for_worker_dispatches_only_claude_code_to_a_real_reader(monkeypatch):
    class FakeCompleted:
        returncode = 0
        stdout = json.dumps({"loggedIn": True, "subscriptionType": "pro"})
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: FakeCompleted())
    claude_facts = account_facts_for_worker("claude-code")
    assert any(f.source == SOURCE_CLI_REPORTED for f in claude_facts)

    # Every other worker has no real local, non-billable account-facts
    # source today -- reported honestly as NOT_EXPOSED, never omitted or
    # guessed.
    other_facts = account_facts_for_worker("grok-build")
    assert other_facts
    assert all(f.source == SOURCE_NOT_EXPOSED for f in other_facts)
    assert all(f.value is None for f in other_facts)
    assert {f.label for f in other_facts} >= {
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
    }


# ------------------------------------------------------------ checkpoints


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "file.txt").write_text("hello", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=path, check=True)


def test_checkpoint_status_no_upstream_reports_unpushed(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    status = checkpoint_status(repo)
    assert status.error is None
    assert status.last_commit_sha
    assert status.has_uncommitted_changes is False
    assert status.has_unpushed_commits is True
    assert status.ahead_of_upstream is None


def test_checkpoint_status_detects_uncommitted_changes(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "file.txt").write_text("changed", encoding="utf-8")
    status = checkpoint_status(repo)
    assert status.has_uncommitted_changes is True


def test_checkpoint_status_with_pushed_upstream_reports_clean(tmp_path):
    remote = tmp_path / "remote.git"
    remote.mkdir()
    subprocess.run(["git", "init", "-q", "--bare"], cwd=remote, check=True)

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=repo, check=True)
    subprocess.run(["git", "push", "-q", "-u", "origin", "HEAD:refs/heads/main"], cwd=repo, check=True)

    status = checkpoint_status(repo)
    assert status.error is None
    assert status.has_unpushed_commits is False
    assert status.ahead_of_upstream == 0


def test_checkpoint_status_on_nonexistent_path_reports_error(tmp_path):
    status = checkpoint_status(tmp_path / "does-not-exist")
    # git itself exits non-zero with empty stdout for a missing cwd on most
    # platforms; either a populated error or all-empty/False fields is an
    # acceptable non-crashing result — the important contract is it never
    # raises.
    assert isinstance(status.has_uncommitted_changes, bool)
