"""ENG-PC-11 (issue #39): structured adapter capability and result contract.

Representative contract tests for Claude, Codex, Grok and OpenCode; proof that
building the typed contract never touches a subprocess/network, never widens
``workers.json`` routing, and never claims a capability this stack does not
implement anywhere. Also covers the typed ``RunResult`` view and its
redaction discipline.
"""

from __future__ import annotations

import dataclasses
import subprocess
from pathlib import Path

import pytest

from scripts.agents import orchestrate
from scripts.agents.adapter_contract import (
    MODEL_DISCOVERY_NATIVE_VERIFIED,
    MODEL_DISCOVERY_RUNTIME_POOL,
    MODEL_DISCOVERY_STATIC,
    AdapterCapabilities,
    capabilities_for,
    run_result_from_record,
    structural_usage_keys,
)
from scripts.agents.control_plane.usage_telemetry import (
    CLASS_DERIVED,
    CLASS_MEASURED,
    CLASS_NOT_EXPOSED,
    CLASS_UNKNOWN,
)
from scripts.agents.manifest import (
    FAILURE_NONE,
    FAILURE_NOT_RUN,
    FAILURE_PERMISSION_DENIED,
    FAILURE_READ_ONLY_VIOLATION,
    FAILURE_TIMEOUT,
    FAILURE_WORKER_ERROR,
    RESULT_FAIL,
    RESULT_PASS,
    RunRecord,
)
from scripts.agents.registry import PERMISSION_STANDARD, load_registry

REGISTRY = load_registry()


# --------------------------------------------------------------------------- purity


def test_capabilities_for_never_touches_a_subprocess(monkeypatch):
    """Building capabilities for every registered worker must stay pure.

    A capability contract that has to guess is exactly what this task exists
    to replace, but a contract that spawns a probe per read would be its own
    new problem (surprise CLI launches from a dashboard listing). Patch every
    subprocess entry point to explode and prove none of them fire.
    """

    def _boom(*_args, **_kwargs):
        raise AssertionError("capabilities_for must not spawn a subprocess")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)

    for worker in REGISTRY.workers.values():
        caps = capabilities_for(worker)
        assert caps.worker == worker.name
        caps.as_dict()  # must serialize without raising or calling anything


# --------------------------------------------------------------------------- representative workers


@pytest.mark.parametrize(
    "worker_name, expect_structured_usage",
    [
        ("claude-code", True),
        ("codex-build", True),
        ("grok-build", True),
        ("opencode2-gemini-flash-lite", False),
        ("opencode-free-review", False),
    ],
)
def test_representative_workers_report_structured_usage_explicitly(worker_name, expect_structured_usage):
    caps = capabilities_for(REGISTRY.get(worker_name))
    assert caps.reports_structured_usage is expect_structured_usage
    if expect_structured_usage:
        assert caps.structured_usage_reason is None
    else:
        assert caps.structured_usage_reason and worker_name in caps.structured_usage_reason


def test_claude_code_reports_write_auth_probe_and_static_model_discovery():
    caps = capabilities_for(REGISTRY.get("claude-code"))
    assert caps.can_write is True
    assert caps.capability == "write"
    assert caps.supports_auth_probe is True
    assert caps.effective_model_discovery == MODEL_DISCOVERY_STATIC


def test_codex_build_reports_native_verified_model_discovery():
    caps = capabilities_for(REGISTRY.get("codex-build"))
    assert caps.effective_model_discovery == MODEL_DISCOVERY_NATIVE_VERIFIED
    assert "codex" in caps.effective_model_discovery_reason


def test_grok_build_bots_and_grok_build_both_report_native_subagents_unsupported():
    """The ENG-AO-02 ``subagents`` block is an orchestrator-run bot fan-out, not a model-native
    subagent channel, so it must not flip this capability -- even for the one worker that carries
    the block and even though its own CLI template passes ``--no-subagents``."""

    bots = capabilities_for(REGISTRY.get("grok-build-bots"))
    solo = capabilities_for(REGISTRY.get("grok-build"))
    assert bots.supports_native_subagents is False
    assert bots.native_subagents_reason
    assert solo.supports_native_subagents is False
    assert solo.native_subagents_reason == bots.native_subagents_reason


def test_opencode_free_review_reports_runtime_pool_model_discovery():
    caps = capabilities_for(REGISTRY.get("opencode-free-review"))
    assert caps.effective_model_discovery == MODEL_DISCOVERY_RUNTIME_POOL
    assert caps.can_write is False


# --------------------------------------------------------------------------- explicit-not-inferred invariant


def test_no_worker_claims_a_capability_this_stack_has_not_built(monkeypatch):
    """Global proof of the task's central rule.

    Session resume (ENG-PC-02), worker-native incremental streaming (distinct
    from ENG-PC-04's orchestrator-owned persisted timeline), and on-demand
    cancellation do not exist anywhere in this stack yet. Every worker --
    regardless of how new/expensive/fancy its model name is -- must report
    all three as unsupported with a concrete reason, never silently true.
    """

    for worker in REGISTRY.workers.values():
        caps = capabilities_for(worker)
        assert caps.can_resume_session is False
        assert caps.resume_unavailable_reason
        assert caps.supports_streaming_events is False
        assert caps.streaming_unavailable_reason
        assert caps.supports_cancellation is False
        assert caps.cancellation_unavailable_reason
        assert caps.context_limit_class == CLASS_NOT_EXPOSED
        assert caps.context_limit_reason


def test_compatibility_with_every_current_registry_worker():
    """``capabilities_for`` must agree with the registry's own existing facts."""

    for worker in REGISTRY.workers.values():
        caps = capabilities_for(worker)
        assert caps.capability == worker.capability
        assert caps.can_write == worker.is_write_capable
        assert caps.supports_auth_probe == bool(worker.auth_check_args)
        assert caps.supports_native_subagents is False
        assert PERMISSION_STANDARD in caps.permission_profiles
        for profile in worker.permission_profile_templates:
            assert profile in caps.permission_profiles
            assert worker.supports_permission_profile(profile)


# --------------------------------------------------------------------------- no routing drift


def test_building_every_capability_does_not_change_registry_routes():
    before = {role: tuple(names) for role, names in REGISTRY.routes.items()}
    for worker in REGISTRY.workers.values():
        capabilities_for(worker)
    reloaded = load_registry()
    after = {role: tuple(names) for role, names in reloaded.routes.items()}
    assert before == after
    for role, names in before.items():
        assert reloaded.route(role) == names


# --------------------------------------------------------------------------- structural usage keys + redaction


def _fake_capabilities(*, reports_structured_usage: bool) -> AdapterCapabilities:
    base = capabilities_for(load_registry().get("claude-code"))
    return dataclasses.replace(
        base,
        reports_structured_usage=reports_structured_usage,
        structured_usage_reason=None if reports_structured_usage else "worker does not request structured output",
    )


def test_structural_usage_keys_not_exposed_when_worker_cannot_report_it():
    caps = _fake_capabilities(reports_structured_usage=False)
    cell = structural_usage_keys(caps, "irrelevant plain text output")
    assert cell["class"] == CLASS_NOT_EXPOSED
    assert cell["value"] is None


def test_structural_usage_keys_unknown_when_output_is_not_json():
    caps = _fake_capabilities(reports_structured_usage=True)
    cell = structural_usage_keys(caps, "not json at all")
    assert cell["class"] == CLASS_UNKNOWN


def test_structural_usage_keys_unknown_when_no_model_usage_breakdown():
    caps = _fake_capabilities(reports_structured_usage=True)
    cell = structural_usage_keys(caps, '{"result": "ok"}')
    assert cell["class"] == CLASS_UNKNOWN


def test_structural_usage_keys_reports_measured_categories_and_redacts_secret_shaped_keys():
    caps = _fake_capabilities(reports_structured_usage=True)
    # A deliberately secret-shaped "category" name to prove structural_usage_keys
    # redacts whatever a worker's structured output contains before surfacing it,
    # rather than trusting a provider payload to be safe by construction.
    log_text = (
        '{"modelUsage": {"claude-sonnet-5": '
        '{"inputTokens": 10, "api_key=sk-ABCDEFGHIJKLMNOP1234567890": 1}}}'
    )
    cell = structural_usage_keys(caps, log_text)
    assert cell["class"] == CLASS_MEASURED
    joined = " ".join(cell["value"])
    assert "sk-ABCDEFGHIJKLMNOP1234567890" not in joined
    assert "inputTokens" in joined


# --------------------------------------------------------------------------- RunResult


def test_run_result_from_record_rejects_mismatched_worker():
    caps = capabilities_for(REGISTRY.get("claude-code"))
    record = RunRecord(
        task="ENG-PC-11",
        role="focused-tests",
        worker="codex-build",
        planned_execution_system="Codex",
        planned_provider="OpenAI",
        planned_model="gpt-x",
        planned_intensity="low",
        requested_command=["codex"],
        result=RESULT_PASS,
    )
    with pytest.raises(ValueError, match="do not match"):
        run_result_from_record(record, capabilities=caps, evidence_paths={})


@pytest.mark.parametrize(
    "notes, exit_status, read_only_violation, boundary_evidence, expected",
    [
        ([], 1, True, None, FAILURE_READ_ONLY_VIOLATION),
        ([], 1, False, {"class": "MEASURED", "worker_action_denied": True}, FAILURE_PERMISSION_DENIED),
        ([], 124, False, None, FAILURE_TIMEOUT),
        (["some other worker failure"], 1, False, None, FAILURE_WORKER_ERROR),
        # A read-only violation or a permission denial must win over exit 124 (timeout's exit
        # status): a read-only worker that modified the tree, or a run denied by the read-only
        # permission boundary, is a contract violation and must never be reported as a timeout
        # merely because the process also exited 124.
        ([], 124, True, None, FAILURE_READ_ONLY_VIOLATION),
        ([], 124, False, {"class": "MEASURED", "worker_action_denied": True}, FAILURE_PERMISSION_DENIED),
        # Notes are diagnostic prose, not typed classification evidence.
        (
            ["read-only worker modified the working tree: x. Treat as a contract violation."],
            1,
            False,
            None,
            FAILURE_WORKER_ERROR,
        ),
        (
            ["worker tool action was denied by the configured read-only permission boundary"],
            1,
            False,
            None,
            FAILURE_WORKER_ERROR,
        ),
        (
            ["worker tool action was denied by the configured read-only permission boundary"],
            1,
            False,
            {"class": "UNKNOWN", "worker_action_denied": True},
            FAILURE_WORKER_ERROR,
        ),
    ],
)
def test_failure_category_classification(
    notes, exit_status, read_only_violation, boundary_evidence, expected
):
    caps = capabilities_for(REGISTRY.get("claude-code"))
    record = RunRecord(
        task="ENG-PC-11",
        role="focused-tests",
        worker="claude-code",
        planned_execution_system="Claude Code",
        planned_provider="Anthropic",
        planned_model="claude-sonnet-5",
        planned_intensity="low",
        requested_command=["claude"],
        result=RESULT_FAIL,
        exit_status=exit_status,
        notes=notes,
        read_only_violation=read_only_violation,
        boundary_evidence=boundary_evidence,
    )
    result = run_result_from_record(record, capabilities=caps, evidence_paths={}, log_text="")
    assert result.failure_category == expected


def test_failure_category_none_on_pass_and_not_run_when_blocked():
    caps = capabilities_for(REGISTRY.get("claude-code"))
    passed = RunRecord(
        task="ENG-PC-11", role="focused-tests", worker="claude-code", planned_execution_system="Claude Code",
        planned_provider="Anthropic", planned_model="m", planned_intensity="low", requested_command=["claude"],
        result=RESULT_PASS, exit_status=0,
    )
    assert run_result_from_record(passed, capabilities=caps, evidence_paths={}).failure_category == FAILURE_NONE

    from scripts.agents.manifest import RESULT_BLOCKED

    blocked = dataclasses.replace(passed, result=RESULT_BLOCKED, exit_status=None)
    assert run_result_from_record(blocked, capabilities=caps, evidence_paths={}).failure_category == FAILURE_NOT_RUN


def test_run_result_actual_model_class_reflects_whether_the_cli_actually_reported_it():
    """``actual_model_class`` must be MEASURED only when the CLI's own ``modelUsage`` block named
    the model, never for one of ``structured_actual_model_report``'s fallbacks to the requested
    model -- even though the fallback value can be identical to a genuinely-measured one."""

    caps = capabilities_for(REGISTRY.get("claude-code"))
    base = dict(
        task="ENG-PC-11", role="focused-tests", worker="claude-code", planned_execution_system="Claude Code",
        planned_provider="Anthropic", planned_model="claude-sonnet-5", planned_intensity="low",
        requested_command=["claude"], result=RESULT_PASS, exit_status=0,
    )

    measured = RunRecord(actual_model="claude-sonnet-5-20260101", actual_model_measured=True, **base)
    result = run_result_from_record(measured, capabilities=caps, evidence_paths={})
    assert result.actual_model == "claude-sonnet-5-20260101"
    assert result.actual_model_class == CLASS_MEASURED

    # structured_actual_model_report fell back to the requested model (e.g. output was not JSON,
    # had no modelUsage block, or reported an ambiguous set of names): the CLI never actually
    # confirmed this identifier, so it must not be asserted as MEASURED even though the value
    # is identical to what a genuine report could have said.
    fallback = dataclasses.replace(measured, actual_model="claude-sonnet-5", actual_model_measured=False)
    result = run_result_from_record(fallback, capabilities=caps, evidence_paths={})
    assert result.actual_model == "claude-sonnet-5"
    assert result.actual_model_class == CLASS_DERIVED

    # Never ran at all: falls back to the planned model, still not MEASURED.
    never_ran = dataclasses.replace(measured, actual_model="", actual_model_measured=False)
    result = run_result_from_record(never_ran, capabilities=caps, evidence_paths={})
    assert result.actual_model == "claude-sonnet-5"
    assert result.actual_model_class == CLASS_DERIVED

    # Nothing known at all: UNKNOWN.
    unknown = dataclasses.replace(measured, actual_model="", actual_model_measured=False, planned_model="")
    result = run_result_from_record(unknown, capabilities=caps, evidence_paths={})
    assert result.actual_model == ""
    assert result.actual_model_class == CLASS_UNKNOWN


# --------------------------------------------------------------------------- integration with a real dry run


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", "-b", "work", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    (tmp_path / "seed.txt").write_text("seed\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", "seed"], check=True)
    return tmp_path


def test_run_result_feeds_a_real_dry_run_manifest_without_a_second_evidence_store(git_repo):
    delegation = orchestrate.run_delegation(
        registry=REGISTRY,
        root=git_repo,
        task="ENG-PC-11",
        worker_name="claude-code",
        role="primary-implementation",
        model=None,
        intensity="low",
        why="test",
        prompt_args=["do the thing"],
        scope_paths=["seed.txt"],
        dry_run=True,
        allow_write=False,
        allow_overflow=False,
        timeout=30.0,
    )
    caps = capabilities_for(REGISTRY.get("claude-code"))
    result = run_result_from_record(
        delegation.record,
        capabilities=caps,
        evidence_paths=delegation.manifest["paths"],
        log_text="[dry run: worker not executed]\n",
    )
    assert result.failure_category == FAILURE_NOT_RUN
    assert result.evidence_paths == delegation.manifest["paths"]
    assert result.usage_categories["class"] == CLASS_UNKNOWN
    result.as_dict()
