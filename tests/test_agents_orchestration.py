"""Focused unit tests for the ENG-AGENT-01 local delegation tooling.

Covers manifest/path validation, secret redaction, unsupported-worker and
disabled-overflow behaviour, git-worktree safety for write-capable workers,
read-only contract enforcement, preservation of the existing OpenCode2 + Gemini
route, and the guarantee that no GitHub workflow invokes an AI worker.
"""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
from pathlib import Path

import pytest

from scripts.agents import orchestrate
from scripts.agents.manifest import (
    RESULT_PASS,
    RunRecord,
    compact_pointer,
    write_artifacts,
)
from scripts.agents.redaction import PLACEHOLDER, redact_command, redact_text
from scripts.agents.registry import Registry, RegistryError, load_registry
from scripts.agents.runner import (
    run_worker_process,
    structured_actual_model,
    structured_failure,
)
from scripts.agents.validation import (
    ValidationError,
    validate_scope_path,
    validate_task_id,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- registry


def test_registry_is_well_formed():
    registry = load_registry()
    assert registry.workers
    for role, names in registry.routes.items():
        assert names, f"route {role} has no workers"
        for name in names:
            assert name in registry.workers, f"route {role} names unknown worker {name}"
    for name, worker in registry.workers.items():
        assert worker.cli_bin
        assert worker.capability in {"write", "focused-edit", "read-only"}
        assert worker.cost_class
        assert worker.default_intensity in registry.intensities
        assert worker.provider_policy
        assert worker.allowed_policy_roles
        assert worker.allow_api_billing is False
        if worker.is_write_capable:
            assert worker.requires_isolated_worktree is True
    assert registry.intensities == ("low", "medium", "high")


def test_opencode2_gemini_flash_lite_route_is_preserved():
    registry = load_registry()
    worker = registry.get("opencode2-gemini-flash-lite")
    assert worker.execution_system == "OpenCode2"
    assert worker.provider == "Google"
    assert "gemini-3.5-flash-lite" in worker.default_model
    assert worker.preserve is True
    # Still the first-choice mechanical/focused-test worker; Antigravity supplements it.
    assert registry.route("mechanical-testing")[0] == "opencode2-gemini-flash-lite"
    assert registry.route("focused-tests")[0] == "opencode2-gemini-flash-lite"
    assert "antigravity-focused-tests" in registry.route("focused-tests")
    assert worker.is_read_only


def test_every_route_member_declares_the_role():
    registry = load_registry()
    for role, names in registry.routes.items():
        for name in names:
            assert role in registry.get(name).roles


def test_antigravity_workers_are_read_only_plan_sandbox():
    registry = load_registry()
    antigravity = [w for w in registry.workers.values() if w.execution_system == "Antigravity"]
    assert len(antigravity) == 4
    for worker in antigravity:
        assert worker.is_read_only
        assert worker.cli_bin == "agy"
        assert worker.default_model == "gemini-3.8-flash-medium"
        assert "--mode" in worker.cli_template and "plan" in worker.cli_template
        assert "--sandbox" in worker.cli_template


def test_verified_cli_defaults_and_no_unsafe_approval_flags():
    registry = load_registry()
    assert registry.get("grok-build").default_model == "grok-4.6"
    assert registry.get("grok-build-review").is_read_only
    # ENG-AGENT-05 adds the free OpenCode2 reviewer before any metered or
    # Codex fallback while preserving every existing route.
    assert list(registry.route("diff-review"))[:2] == [
        "antigravity-diff-review", "opencode2-gemini-flash-lite-review",
    ]
    assert "grok-build-review" in registry.route("diff-review")
    assert registry.route("diff-review")[-1] == "codex-review"
    forbidden = {"--always-approve", "--auto", "--dangerously-skip-permissions", "bypassPermissions"}
    for worker in registry.workers.values():
        assert forbidden.isdisjoint(worker.cli_template)

    grok_command = registry.get("grok-build-review").build_command(
        model=None, intensity="medium", prompt="review this diff"
    )
    assert grok_command[grok_command.index("--single") + 1] == "review this diff"
    assert grok_command[grok_command.index("--sandbox") + 1] == "read-only"
    agy_command = registry.get("antigravity-focused-tests").build_command(
        model=None, intensity="medium", prompt="run one test"
    )
    assert agy_command[agy_command.index("--print") + 1] == "run one test"


# ------------------------------------------- Codex CLI worker (ENG-AGENT-02-S7, issue #97)


def test_codex_review_worker_is_read_only_subscription_authenticated_and_uses_sandbox():
    registry = load_registry()
    codex = registry.get("codex-review")
    assert codex.is_read_only
    assert codex.cost_class == "premium-subscription"  # never "paid-api"/metered
    command = codex.build_command(model=None, intensity="medium", prompt="review this diff")
    assert command[0] == "codex"
    assert "exec" in command
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert command[command.index("--model") + 1] == "gpt-5.6-sol"
    assert command[-1] == "review this diff"
    # No API-key-shaped flag anywhere in the template -- subscription auth only.
    assert not any("api-key" in tok.lower() or "apikey" in tok.lower() for tok in codex.cli_template)


def test_codex_review_auth_check_uses_the_local_cli_session_only():
    registry = load_registry()
    codex = registry.get("codex-review")
    assert codex.auth_check_args == ("login", "status")
    assert codex.auth_check_success_pattern == "Logged in using ChatGPT"


def test_claude_auth_check_requires_the_local_claude_ai_subscription_session():
    registry = load_registry()
    claude = registry.get("claude-code")
    assert claude.auth_check_args == ("auth", "status")
    assert claude.auth_check_success_pattern == '"authMethod": "claude.ai"'


def test_claude_auth_check_distinguishes_subscription_from_missing_or_api_auth(monkeypatch):
    registry = load_registry()
    claude = registry.get("claude-code")
    monkeypatch.setattr("scripts.agents.registry.shutil.which", lambda _bin: "/usr/local/bin/claude")

    class FakeStatus:
        returncode = 0
        stderr = ""

        def __init__(self, stdout):
            self.stdout = stdout

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: FakeStatus('{"loggedIn": true, "authMethod": "claude.ai"}'),
    )
    assert claude.check_auth() is True

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: FakeStatus('{"loggedIn": false, "authMethod": "none"}'),
    )
    assert claude.check_auth() is False

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: FakeStatus('{"loggedIn": true, "authMethod": "apiKey"}'),
    )
    assert claude.check_auth() is False


def test_workers_without_a_declared_auth_check_report_none_not_a_guess():
    """A worker that never declared ``cli.auth_check`` must not silently gain an

    opinion about authentication -- ``check_auth()`` returns ``None`` (not
    applicable) so ``availability_reason()`` falls back to the pre-existing
    CLI-presence-only behavior, unaffected by this slice.
    """

    registry = load_registry()
    assert registry.get("grok-build").check_auth() is None


def test_check_auth_reports_true_when_the_local_cli_session_is_authenticated(monkeypatch):
    registry = load_registry()
    codex = registry.get("codex-review")

    class FakeCompleted:
        returncode = 0
        stdout = "Logged in using ChatGPT\n"
        stderr = ""

    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return FakeCompleted()

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("scripts.agents.registry.shutil.which", lambda _bin: "/usr/local/bin/codex")
    assert codex.check_auth() is True
    assert calls == [["codex", "login", "status"]]


def test_check_auth_reports_false_when_the_local_cli_session_is_not_authenticated(monkeypatch):
    registry = load_registry()
    codex = registry.get("codex-review")

    class FakeCompleted:
        returncode = 1
        stdout = "Not logged in\n"
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: FakeCompleted())
    monkeypatch.setattr("scripts.agents.registry.shutil.which", lambda _bin: "/usr/local/bin/codex")
    assert codex.check_auth() is False


def test_check_auth_rejects_api_key_authenticated_codex_sessions(monkeypatch):
    """Independent review (Grok Build, issue #97) found the pattern that used

    to be configured here ("Logged in using") would also match a
    hypothetical "Logged in using API key" status line, which would let an
    API-key-authenticated Codex session (a real spend/policy concern this
    worker is explicitly documented to never fall back to) report as
    truthfully AVAILABLE as a genuine ChatGPT-subscription session. The
    configured pattern must require the literal "ChatGPT" auth method.
    """

    registry = load_registry()
    codex = registry.get("codex-review")

    class FakeApiKeyAuth:
        returncode = 0
        stdout = "Logged in using API key\n"
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: FakeApiKeyAuth())
    monkeypatch.setattr("scripts.agents.registry.shutil.which", lambda _bin: "/usr/local/bin/codex")
    assert codex.check_auth() is False


def test_check_auth_never_raises_when_the_cli_invocation_itself_fails(monkeypatch):
    registry = load_registry()
    codex = registry.get("codex-review")

    def raise_oserror(*_a, **_k):
        raise OSError("boom")

    monkeypatch.setattr(subprocess, "run", raise_oserror)
    monkeypatch.setattr("scripts.agents.registry.shutil.which", lambda _bin: "/usr/local/bin/codex")
    assert codex.check_auth() is False


# ----------------------------------------------------- ENG-AGENT-11 (issue #131)


def test_claude_declares_a_launch_probe():
    registry = load_registry()
    claude = registry.get("claude-code")
    assert claude.launch_probe_args == ("-p", "Reply with exactly: CLAUDE_OK")
    assert claude.launch_probe_success_pattern == "CLAUDE_OK"


def test_explicit_negative_auth_status_is_not_authenticated_not_a_launch_error(monkeypatch):
    """A real, understood negative answer must never be relabeled as an

    environment defect -- only the metadata-ambiguous case gets the
    authoritative-prompt fallback.
    """

    from scripts.agents.registry import REASON_NOT_AUTHENTICATED

    registry = load_registry()
    claude = registry.get("claude-code")
    monkeypatch.setattr("scripts.agents.registry.shutil.which", lambda _bin: "/usr/local/bin/claude")

    class FakeStatus:
        returncode = 0
        stdout = '{"loggedIn": false, "authMethod": "none"}'
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: FakeStatus())
    launch_probe_calls = []
    monkeypatch.setattr(
        "scripts.agents.registry.Worker.run_launch_probe",
        lambda self, **_k: launch_probe_calls.append(1) or True,
    )
    assert claude.availability_reason() == REASON_NOT_AUTHENTICATED
    assert launch_probe_calls == []


def test_sandbox_spawn_failure_is_launch_environment_error_when_no_authoritative_probe_succeeds(monkeypatch):
    from scripts.agents.registry import REASON_LAUNCH_ENVIRONMENT_ERROR

    registry = load_registry()
    claude = registry.get("claude-code")
    monkeypatch.setattr("scripts.agents.registry.shutil.which", lambda _bin: "/usr/local/bin/claude")

    def raise_oserror(*_a, **_k):
        raise OSError("Operation not permitted")

    monkeypatch.setattr(subprocess, "run", raise_oserror)
    assert claude.check_auth() is False  # unchanged boolean contract
    assert claude.availability_reason() == REASON_LAUNCH_ENVIRONMENT_ERROR


def test_successful_authoritative_prompt_overrides_an_ambiguous_metadata_probe(monkeypatch):
    """Issue #131's central acceptance criterion: a successful authoritative

    Claude prompt must make the route AVAILABLE even though the cheap
    metadata auth-check heuristic could not get a truthful answer.
    """

    from scripts.agents.registry import REASON_AVAILABLE

    registry = load_registry()
    claude = registry.get("claude-code")
    monkeypatch.setattr("scripts.agents.registry.shutil.which", lambda _bin: "/usr/local/bin/claude")

    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[1:] == ["auth", "status"]:
            raise OSError("sandbox: Operation not permitted")

        class FakeCompleted:
            returncode = 0
            stdout = "CLAUDE_OK\n"
            stderr = ""

        return FakeCompleted()

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert claude.availability_reason() == REASON_AVAILABLE
    assert calls == [
        ["claude", "auth", "status"],
        ["claude", "-p", "Reply with exactly: CLAUDE_OK"],
    ]


def test_empty_auth_status_output_is_launch_error_not_a_false_logout(monkeypatch):
    """A status invocation that ran (returncode 0) but produced no parseable

    answer at all -- exactly what a restricted launcher that cannot reach the
    real session looks like -- must not be read as an explicit "not logged
    in" negative.
    """

    from scripts.agents.registry import REASON_LAUNCH_ENVIRONMENT_ERROR

    registry = load_registry()
    claude = registry.get("claude-code")
    monkeypatch.setattr("scripts.agents.registry.shutil.which", lambda _bin: "/usr/local/bin/claude")

    class FakeEmptyStatus:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: FakeEmptyStatus())
    monkeypatch.setattr("scripts.agents.registry.Worker.run_launch_probe", lambda self, **_k: False)
    assert claude.availability_reason() == REASON_LAUNCH_ENVIRONMENT_ERROR


def test_run_launch_probe_returns_none_when_undeclared():
    registry = load_registry()
    assert registry.get("grok-build").run_launch_probe() is None


def test_availability_reason_covers_the_full_truthful_vocabulary(monkeypatch):
    from scripts.agents.registry import (
        REASON_AVAILABLE,
        REASON_CLI_MISSING,
        REASON_DISABLED,
        REASON_NOT_AUTHENTICATED,
    )

    disabled = _registry_with(name="antigravity-focused-tests", enabled=False)
    assert disabled.get("antigravity-focused-tests").availability_reason() == REASON_DISABLED

    cli_missing = _registry_with(name="antigravity-focused-tests", cli_bin="definitely-not-a-real-binary-xyz")
    assert cli_missing.get("antigravity-focused-tests").availability_reason() == REASON_CLI_MISSING

    monkeypatch.setattr("scripts.agents.registry.shutil.which", lambda _bin: "/usr/local/bin/fake")

    registry = load_registry()
    opencode = registry.get("opencode2-gemini-flash-lite")  # no auth_check declared
    assert opencode.availability_reason() == REASON_AVAILABLE

    codex = registry.get("codex-review")

    class NotAuthed:
        returncode = 1
        stdout = "not logged in"
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: NotAuthed())
    assert codex.availability_reason() == REASON_NOT_AUTHENTICATED


def test_antigravity_uses_scoped_prompt_when_repo_agent_discovery_is_unavailable():
    registry = load_registry()
    for worker in registry.workers.values():
        if worker.execution_system != "Antigravity":
            continue
        assert "--agent" not in worker.cli_template


def test_route_rejects_unknown_role():
    with pytest.raises(RegistryError):
        load_registry().route("does-not-exist")


# ------------------------------------------------- permission profiles (ENG-AGENT-02-S5/issue #94)


def test_repo_configured_auto_profile_is_explicit_for_supported_write_workers_only():
    """The unattended profile stays opt-in and unavailable to other workers."""

    registry = load_registry()
    for worker in registry.workers.values():
        assert "--dangerously-skip-permissions" not in worker.cli_template
        if worker.name not in {"claude-code", "codex-build"}:
            assert not worker.supports_permission_profile("repo_configured_auto")

    claude = registry.get("claude-code")
    assert claude.supports_permission_profile("repo_configured_auto")
    unattended_command = claude.build_command(
        model=None, intensity="medium", prompt="finish the runbook", permission_profile="repo_configured_auto"
    )
    assert "--dangerously-skip-permissions" in unattended_command
    assert "--permission-mode" not in unattended_command
    assert unattended_command[-1] == "finish the runbook" or "finish the runbook" in unattended_command

    codex = registry.get("codex-build")
    codex_unattended = codex.build_command(
        model=None, intensity="medium", prompt="finish the runbook", permission_profile="repo_configured_auto"
    )
    assert "workspace-write" in codex_unattended
    assert "--dangerously-bypass-approvals-and-sandbox" not in codex_unattended

    standard_command = claude.build_command(model=None, intensity="medium", prompt="finish the runbook")
    assert "--permission-mode" in standard_command
    assert standard_command[standard_command.index("--permission-mode") + 1] == "manual"
    assert "--dangerously-skip-permissions" not in standard_command


def test_build_command_rejects_an_unsupported_permission_profile():
    registry = load_registry()
    grok = registry.get("grok-build-review")
    assert grok.is_read_only
    with pytest.raises(RegistryError, match="does not support permission_profile"):
        grok.build_command(model=None, intensity="medium", prompt="x", permission_profile="repo_configured_auto")


def test_run_delegation_rejects_repo_configured_auto_for_a_read_only_worker(git_repo):
    """Defense in depth at the CLI-argument layer, not just the Runbook layer: a

    read-only worker can never gain the unattended write profile even when the
    orchestrator's lower-level ``run`` verb is invoked directly.
    """

    registry = load_registry()
    with pytest.raises(ValidationError, match="permission_profile"):
        _run(
            registry,
            git_repo,
            "antigravity-focused-tests",
            permission_profile="repo_configured_auto",
            dry_run=True,
        )


def test_run_session_rejects_repo_configured_auto_for_a_read_only_worker(git_repo):
    registry = load_registry()
    with pytest.raises(ValidationError, match="permission_profile"):
        orchestrate.run_session(
            registry=registry,
            root=git_repo,
            task="ENG-AGENT-02-S5",
            worker_name="antigravity-focused-tests",
            role="focused-tests",
            model=None,
            intensity="low",
            why="test",
            prompt="do it",
            dry_run=True,
            timeout=30.0,
            permission_profile="repo_configured_auto",
        )


# --------------------------------------------------------------------------- validation


@pytest.mark.parametrize("good", ["ENG-AGENT-01", "FND-08", "LFD-028", "ENG-CI-02", "VE-DIR-05"])
def test_validate_task_id_accepts_references(good):
    assert validate_task_id(good) == good


@pytest.mark.parametrize(
    "bad", ["", "eng-agent-01", "ENGAGENT01", "ENG AGENT 01", "../ENG", "ENG/01", "ENG-AGENT-01;rm"]
)
def test_validate_task_id_rejects_unsafe(bad):
    with pytest.raises(ValidationError):
        validate_task_id(bad)


def test_validate_scope_path_accepts_repo_file():
    resolved = validate_scope_path("scripts/agents/orchestrate.py", REPO_ROOT)
    assert resolved == (REPO_ROOT / "scripts/agents/orchestrate.py").resolve()


@pytest.mark.parametrize(
    "bad",
    [
        "../outside.txt",
        "data/projects/x.json",
        ".env",
        "data/.secrets.json",
        "data/.cloud_gpu.secrets.json",
        ".claude/launch.json",
        ".npmrc",
        ".ssh/id_ed25519",
        "/etc/passwd",
        "does/not/exist.py",
    ],
)
def test_validate_scope_path_rejects_unsafe(bad):
    with pytest.raises(ValidationError):
        validate_scope_path(bad, REPO_ROOT)


def test_validate_scope_path_require_exists_false_accepts_a_deleted_path(tmp_path):
    # issue #144: a diff-review scope built from a candidate's changed-path
    # list must still resolve for a file the candidate deleted -- it is only
    # ever used to build a git-diff pathspec, never a filesystem read.
    resolved = validate_scope_path("removed/gone.py", tmp_path, require_exists=False)
    assert resolved == (tmp_path / "removed/gone.py").resolve()


def test_validate_scope_path_require_exists_true_still_rejects_missing_by_default(tmp_path):
    with pytest.raises(ValidationError):
        validate_scope_path("removed/gone.py", tmp_path)


@pytest.mark.parametrize("bad", ["../outside.txt", "/etc/passwd", "data/.secrets.json", ".env"])
def test_validate_scope_path_require_exists_false_still_rejects_traversal_and_forbidden(tmp_path, bad):
    # Relaxing existence must never relax the repo-boundary/forbidden-name/
    # secret-marker checks -- those apply unconditionally.
    with pytest.raises(ValidationError):
        validate_scope_path(bad, tmp_path, require_exists=False)


# --------------------------------------------------------------------------- redaction


def test_redact_command_masks_secret_flags_and_values():
    command = [
        "agy",
        "--model",
        "gemini-3.5-flash",
        "--api-key",
        "sk-ABCDEFGHIJKLMNOP1234567890",
        "--effort",
        "low",
        "GEMINI_API_KEY=AIzaSyA1234567890abcdefgh_ABCDEFGHIJKL",
        "run the token suite",
    ]
    redacted = redact_command(command)
    joined = " ".join(redacted)
    assert "sk-ABCDEFGHIJKLMNOP1234567890" not in joined
    assert "AIzaSyA1234567890abcdefgh_ABCDEFGHIJKL" not in joined
    assert PLACEHOLDER in joined
    # Non-secret arguments and the prompt text survive.
    assert "--model" in redacted and "gemini-3.5-flash" in redacted
    assert "run the token suite" in redacted


def test_redact_text_masks_value_shaped_tokens():
    text = "authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payloadpart.signature99\nghp_ABCDEFGHIJKLMNOPQRSTUVWX0123456789"
    redacted = redact_text(text)
    assert "ghp_ABCDEFGHIJKLMNOPQRSTUVWX0123456789" not in redacted
    assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in redacted
    assert PLACEHOLDER in redacted


def test_redact_text_masks_entire_quoted_multiword_assignment():
    text = 'API_TOKEN="first second third" safe=value'
    redacted = redact_text(text)
    assert redacted == f'API_TOKEN="{PLACEHOLDER}" safe=value'


# --------------------------------------------------------------------------- manifest


def test_manifest_and_pointer_roundtrip(tmp_path):
    record = RunRecord(
        task="ENG-AGENT-01",
        role="focused-tests",
        worker="opencode2-gemini-flash-lite",
        planned_execution_system="OpenCode2",
        planned_provider="Google",
        planned_model="google/gemini-3.5-flash-lite",
        planned_intensity="low",
        requested_command=["opencode", "run", "--api-key", "sk-ABCDEFGHIJKLMNOP1234567890"],
        result=RESULT_PASS,
        exit_status=0,
        files_changed=[],
        tests_or_checks=["pytest tests/test_agents_orchestration.py"],
        notes=["API_TOKEN=do-not-store"],
    )
    worker_dir = tmp_path / ".agent-output" / "ENG-AGENT-01" / "opencode2-gemini-flash-lite"
    worker_dir.mkdir(parents=True)
    paths = write_artifacts(
        worker_dir, record, log_text="secret sk-ABCDEFGHIJKLMNOP1234567890 here", repo_root=tmp_path
    )

    manifest = json.loads((worker_dir / "manifest.json").read_text())
    assert manifest["task"] == "ENG-AGENT-01"
    assert manifest["result"] == RESULT_PASS
    assert "sk-ABCDEFGHIJKLMNOP1234567890" not in json.dumps(manifest)
    assert PLACEHOLDER in " ".join(manifest["requested_command"])
    assert manifest["ci_invocation_allowed"] is False
    assert manifest["notes"] == [f"API_TOKEN={PLACEHOLDER}"]

    # Detailed log is redacted on disk.
    assert "sk-ABCDEFGHIJKLMNOP1234567890" not in (worker_dir / "logs" / "run.log").read_text()

    pointer = compact_pointer(manifest)
    assert "TASK: ENG-AGENT-01" in pointer
    assert "STATUS: PASS" in pointer
    assert paths["summary"].endswith("summary.md")
    assert (worker_dir / "summary.md").exists()


# --------------------------------------------------------------------------- git fixture


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", "-b", "work", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    (tmp_path / "seed.txt").write_text("seed\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", "seed"], check=True)
    return tmp_path


def _registry_with(**overrides) -> Registry:
    """Clone the real registry, replacing one worker's fields for a test."""

    base = load_registry()
    name = overrides.pop("name")
    worker = dataclasses.replace(base.get(name), **overrides)
    workers = dict(base.workers)
    workers[name] = worker
    return Registry(workers=workers, routes=base.routes, intensities=base.intensities)


def _run(registry, root, worker, **kw):
    defaults = dict(
        registry=registry,
        root=root,
        task="ENG-AGENT-01",
        worker_name=worker,
        role="focused-tests",
        model=None,
        intensity="low",
        why="test",
        prompt_args=["do the thing"],
        scope_paths=["seed.txt"],
        dry_run=False,
        allow_write=False,
        allow_overflow=False,
        timeout=30.0,
    )
    defaults.update(kw)
    return orchestrate.run_delegation(**defaults)


# --------------------------------------------------------------------------- behaviour


def test_unsupported_worker_records_and_does_not_fall_back(git_repo):
    registry = _registry_with(name="antigravity-focused-tests", cli_bin="definitely-not-a-real-binary-xyz")
    result = _run(registry, git_repo, "antigravity-focused-tests")

    assert result.record.result == "UNSUPPORTED"
    assert result.exit_code == orchestrate.EXIT_UNSUPPORTED_OR_BLOCKED
    assert "not installed" in " ".join(result.record.notes)
    # Exactly one worker directory was created; no other worker was tried.
    task_dir = git_repo / ".agent-output" / "ENG-AGENT-01"
    assert [p.name for p in task_dir.iterdir()] == ["antigravity-focused-tests"]


def test_disabled_overflow_worker_is_blocked(git_repo):
    result = _run(load_registry(), git_repo, "deepseek-overflow", role="overflow")
    assert result.record.result == "BLOCKED"
    assert result.exit_code == orchestrate.EXIT_UNSUPPORTED_OR_BLOCKED
    assert "disabled" in " ".join(result.record.notes)
    assert "No automatic paid fallback" in " ".join(result.record.notes)


def test_dry_run_builds_command_without_executing(git_repo):
    registry = _registry_with(name="antigravity-focused-tests", cli_bin="still-not-real-binary")
    result = _run(registry, git_repo, "antigravity-focused-tests", dry_run=True)
    assert result.record.result == "DRY_RUN"
    assert result.exit_code == orchestrate.EXIT_OK
    manifest = result.manifest
    assert manifest["requested_command"][0] == "still-not-real-binary"
    assert "--sandbox" in manifest["requested_command"]
    assert any("do the thing" in argument for argument in manifest["requested_command"])
    assert manifest["actual"] == {"execution_system": "", "provider": "", "model": "", "intensity": ""}


def test_worker_failure_is_recorded_as_fail(git_repo):
    registry = _registry_with(
        name="antigravity-focused-tests",
        cli_bin="sh",
        cli_template=("-c", "echo boom >&2; exit 5", "--"),
    )
    result = _run(registry, git_repo, "antigravity-focused-tests")
    assert result.record.result == "FAIL"
    assert result.record.exit_status == 5
    assert result.exit_code == orchestrate.EXIT_WORKER_FAILED


def test_structured_worker_cancellation_is_recorded_as_fail(git_repo):
    registry = _registry_with(
        name="antigravity-focused-tests",
        cli_bin="sh",
        cli_template=("-c", 'printf \'{"stopReason":"cancelled"}\'', "--"),
    )
    result = _run(registry, git_repo, "antigravity-focused-tests")
    assert result.record.result == "FAIL"
    assert "stopReason=cancelled" in " ".join(result.record.notes)


def test_read_only_review_can_receive_redacted_scoped_diff(git_repo):
    (git_repo / "seed.txt").write_text("changed sk-ABCDEFGHIJKLMNOP1234567890\n")
    subprocess.run(["git", "-C", str(git_repo), "add", "seed.txt"], check=True)
    registry = _registry_with(
        name="antigravity-focused-tests",
        cli_bin="sh",
        cli_template=("-c", "true", "--"),
    )
    result = _run(registry, git_repo, "antigravity-focused-tests", include_diff=True)
    command = " ".join(result.record.requested_command)
    assert "scoped diff below" in command
    assert "sk-ABCDEFGHIJKLMNOP1234567890" not in command
    assert PLACEHOLDER in command


def test_diff_review_scope_accepts_a_path_the_candidate_deleted(git_repo):
    # issue #144: the acceptance-pipeline review dispatch scopes real changed
    # paths, including ones the candidate deleted -- a deleted path no longer
    # exists on disk, but --include-diff only needs it as a git pathspec.
    subprocess.run(["git", "-C", str(git_repo), "rm", "-q", "seed.txt"], check=True)
    result = _run(
        load_registry(), git_repo, "opencode2-gemini-flash-lite-review", role="diff-review",
        scope_paths=["seed.txt"], include_diff=True, dry_run=True,
    )
    assert result.record.result == "DRY_RUN"
    assert any("-seed" in argument for argument in result.record.requested_command)


def test_focused_tests_scope_still_rejects_a_nonexistent_path(git_repo):
    # require_exists=False is scoped to diff-review only -- every other role
    # keeps the original existing-path requirement.
    with pytest.raises(ValidationError):
        _run(load_registry(), git_repo, "antigravity-focused-tests", scope_paths=["never-existed.txt"])


def test_read_only_review_diff_includes_staged_changes(git_repo):
    (git_repo / "seed.txt").write_text("staged change\n")
    subprocess.run(["git", "-C", str(git_repo), "add", "seed.txt"], check=True)
    registry = _registry_with(
        name="antigravity-focused-tests",
        cli_bin="sh",
        cli_template=("-c", "true", "--"),
    )
    result = _run(registry, git_repo, "antigravity-focused-tests", include_diff=True)
    assert any("+staged change" in argument for argument in result.record.requested_command)


def test_worker_success_and_output_is_redacted_on_disk(git_repo):
    registry = _registry_with(
        name="antigravity-focused-tests",
        cli_bin="sh",
        cli_template=("-c", "echo leaked sk-ABCDEFGHIJKLMNOP1234567890; exit 0", "--"),
    )
    result = _run(registry, git_repo, "antigravity-focused-tests")
    assert result.record.result == "PASS"
    log = (git_repo / result.manifest["paths"]["log"]).read_text()
    assert "sk-ABCDEFGHIJKLMNOP1234567890" not in log
    assert PLACEHOLDER in log


def test_worker_environment_uses_allowlist(monkeypatch, git_repo):
    monkeypatch.setenv("GH_TOKEN", "ghp_ABCDEFGHIJKLMNOPQRSTUVWX0123456789")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "do-not-forward")
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "do-not-forward")
    exit_code, output = run_worker_process(["/usr/bin/env"], git_repo, timeout=10)
    assert exit_code == 0
    assert "GH_TOKEN=" not in output
    assert "AWS_SECRET_ACCESS_KEY=" not in output
    assert "OPENCODE_SERVER_PASSWORD=" not in output


def test_reviewer_preset_self_heal_is_consulted_before_launching_any_worker(monkeypatch, git_repo):
    """ENG-AGENT-17 (issue #148): every worker launch consults the reviewer-

    preset self-heal, and it is consulted before the worker actually runs
    (not merely logged after the fact).
    """

    calls = []

    def fake_ensure(root, command):
        calls.append((root, tuple(command)))
        return True

    monkeypatch.setattr(orchestrate, "ensure_opencode_agent_preset_for_command", fake_ensure)
    registry = _registry_with(name="antigravity-focused-tests", cli_bin="sh", cli_template=("-c", "true", "--"))

    result = _run(registry, git_repo, "antigravity-focused-tests")

    assert result.record.result == "PASS"
    assert len(calls) == 1
    root, command = calls[0]
    assert root == git_repo
    assert command[0] == "sh"


def test_reviewer_preset_self_heal_failure_blocks_without_launching_the_worker(monkeypatch, git_repo):
    """A worker whose agent preset cannot be resolved/self-healed must never

    launch at all -- not merely report a bad result after silently running
    under a substituted default agent.
    """

    marker = git_repo / "worker-ran.marker"
    monkeypatch.setattr(orchestrate, "ensure_opencode_agent_preset_for_command", lambda root, command: False)
    registry = _registry_with(
        name="antigravity-focused-tests", cli_bin="sh", cli_template=("-c", f"touch {marker}", "--"),
    )

    result = _run(registry, git_repo, "antigravity-focused-tests")

    assert result.record.result == "BLOCKED"
    assert any("agent preset" in note for note in result.record.notes)
    assert not marker.exists()


def test_repeated_runs_keep_unique_artifact_directories(git_repo):
    registry = _registry_with(name="antigravity-focused-tests", cli_bin="sh", cli_template=("-c", "true", "--"))
    first = _run(registry, git_repo, "antigravity-focused-tests")
    second = _run(registry, git_repo, "antigravity-focused-tests")
    assert first.manifest["paths"]["manifest"] != second.manifest["paths"]["manifest"]
    assert (git_repo / first.manifest["paths"]["manifest"]).exists()
    assert (git_repo / second.manifest["paths"]["manifest"]).exists()


def test_structured_cancelled_result_is_failure():
    assert structured_failure('{"stopReason":"cancelled"}') == (
        "structured worker result reported stopReason=cancelled"
    )
    assert structured_failure('{"stopReason":"complete"}') is None
    assert structured_failure("plain output") is None


def test_structured_denied_tool_after_json_result_is_failure():
    output = (
        '{"status":"SUCCESS","response":""}\n'
        "Headless mode: tool permission requests are auto-denied. RunCommand was denied."
    )
    assert structured_failure(output) == (
        "worker tool action was denied by the configured read-only permission boundary"
    )


def test_structured_empty_response_is_failure():
    assert structured_failure('{"status":"SUCCESS","response":""}') == (
        "structured worker result contained an empty response"
    )


def test_structured_actual_model_prefers_reported_identifier():
    output = '{"modelUsage":{"grok-4.6-build":{"modelCalls":1}}}'
    assert structured_actual_model(output, "grok-4.6") == "grok-4.6-build"
    assert structured_actual_model("plain output", "requested-model") == "requested-model"


def test_read_only_worker_flagged_when_it_modifies_the_tree(git_repo):
    registry = _registry_with(
        name="antigravity-focused-tests",
        cli_bin="sh",
        cli_template=("-c", "echo touching; : > sneaky_edit.txt", "--"),
    )
    result = _run(registry, git_repo, "antigravity-focused-tests")
    assert result.record.result == "FAIL"
    assert "sneaky_edit.txt" in result.record.files_changed
    assert "modified the working tree" in " ".join(result.record.notes)


def test_read_only_worker_detects_edit_to_already_dirty_file(git_repo):
    (git_repo / "seed.txt").write_text("dirty before\n")
    registry = _registry_with(
        name="antigravity-focused-tests",
        cli_bin="sh",
        cli_template=("-c", "echo changed-again > seed.txt", "--"),
    )
    result = _run(registry, git_repo, "antigravity-focused-tests")
    assert result.record.result == "FAIL"
    assert result.record.files_changed == ["seed.txt"]


def test_write_worker_blocked_without_allow_write(git_repo):
    registry = _registry_with(name="claude-code", cli_bin="sh", cli_template=("-c", "true", "--"))
    result = _run(registry, git_repo, "claude-code", role="primary-implementation")
    assert result.record.result == "BLOCKED"
    assert "write-capable" in " ".join(result.record.notes)


def test_allow_write_cannot_upgrade_read_only_worker(git_repo):
    with pytest.raises(ValidationError, match="cannot upgrade read-only"):
        _run(load_registry(), git_repo, "opencode2-gemini-flash-lite", allow_write=True)


def test_diff_review_requires_embedded_scoped_diff(git_repo):
    with pytest.raises(ValidationError, match="requires --include-diff"):
        _run(load_registry(), git_repo, "grok-build-review", role="diff-review")


# ------------------------------------------------------- ENG-AGENT-18 (issue #155)
#
# The acceptance pipeline's Review stage dispatches diff-review with
# --include-diff. Before this fix, the scoped diff was always built as
# `git diff HEAD <write-tree>` -- the working-tree/index diff -- so a clean,
# already-committed feature candidate (whose merge-base with the configured
# base ref is behind HEAD, but whose index equals HEAD) always produced an
# empty diff and failed with "found no scoped changes", even though the
# candidate carries real, reviewable committed changes.


def _base_and_feature_branch(git_repo: Path, *, feature_changes) -> None:
    """Tag ``git_repo``'s current commit as ``main``, then apply ``feature_changes``
    (a callable that mutates the tree) as one additional commit on ``work`` --
    a clean, fully committed feature branch ahead of ``main``.
    """

    subprocess.run(["git", "-C", str(git_repo), "branch", "main"], check=True)
    feature_changes(git_repo)
    subprocess.run(["git", "-C", str(git_repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(git_repo), "commit", "-q", "-m", "feature work"], check=True)


def test_clean_committed_candidate_ahead_of_base_produces_scoped_diff(git_repo):
    _base_and_feature_branch(
        git_repo, feature_changes=lambda repo: (repo / "seed.txt").write_text("committed change\n")
    )
    registry = _registry_with(name="antigravity-focused-tests", cli_bin="sh", cli_template=("-c", "true", "--"))

    result = _run(registry, git_repo, "antigravity-focused-tests", include_diff=True, diff_base="main")

    command = " ".join(result.record.requested_command)
    assert "+committed change" in command
    assert "scoped diff below" in command


def test_clean_committed_candidate_scope_filtering_still_applies(git_repo):
    def _changes(repo: Path) -> None:
        (repo / "seed.txt").write_text("in scope\n")
        (repo / "other.txt").write_text("out of scope\n")

    _base_and_feature_branch(git_repo, feature_changes=_changes)
    registry = _registry_with(name="antigravity-focused-tests", cli_bin="sh", cli_template=("-c", "true", "--"))

    result = _run(
        registry, git_repo, "antigravity-focused-tests",
        include_diff=True, diff_base="main", scope_paths=["seed.txt"],
    )

    command = " ".join(result.record.requested_command)
    assert "+in scope" in command
    assert "out of scope" not in command


def test_clean_committed_candidate_with_deleted_and_renamed_files(git_repo):
    def _changes(repo: Path) -> None:
        (repo / "seed.txt").unlink()
        (repo / "renamed.txt").write_text("moved\n")

    _base_and_feature_branch(git_repo, feature_changes=_changes)
    registry = _registry_with(name="antigravity-diff-review", cli_bin="sh", cli_template=("-c", "true", "--"))

    result = _run(
        registry, git_repo, "antigravity-diff-review", role="diff-review",
        include_diff=True, diff_base="main", scope_paths=["seed.txt", "renamed.txt"],
    )

    command = " ".join(result.record.requested_command)
    assert "-seed" in command
    assert "+moved" in command


def test_clean_committed_candidate_binds_the_exact_candidate_tree_sha(git_repo):
    _base_and_feature_branch(
        git_repo, feature_changes=lambda repo: (repo / "seed.txt").write_text("committed change\n")
    )
    expected_tree = subprocess.run(
        ["git", "-C", str(git_repo), "rev-parse", "HEAD^{tree}"], capture_output=True, text=True, check=True
    ).stdout.strip()
    registry = _registry_with(name="antigravity-focused-tests", cli_bin="sh", cli_template=("-c", "true", "--"))

    result = _run(registry, git_repo, "antigravity-focused-tests", include_diff=True, diff_base="main")

    assert result.record.candidate_tree_sha == expected_tree


def test_genuinely_no_change_candidate_still_fails_closed(git_repo):
    subprocess.run(["git", "-C", str(git_repo), "branch", "main"], check=True)
    registry = _registry_with(name="antigravity-focused-tests", cli_bin="sh", cli_template=("-c", "true", "--"))

    with pytest.raises(ValidationError, match="found no scoped changes"):
        _run(registry, git_repo, "antigravity-focused-tests", include_diff=True, diff_base="main")


def test_dirty_worktree_review_ignores_diff_base_and_uses_current_diff(git_repo):
    # dirty/uncommitted candidate -> preserve current (working-tree/index)
    # behavior even when a --diff-base is configured and would itself resolve
    # to a non-empty committed diff.
    _base_and_feature_branch(
        git_repo, feature_changes=lambda repo: (repo / "seed.txt").write_text("prior committed line\n")
    )
    (git_repo / "seed.txt").write_text("freshly staged line\n")
    subprocess.run(["git", "-C", str(git_repo), "add", "seed.txt"], check=True)
    registry = _registry_with(name="antigravity-focused-tests", cli_bin="sh", cli_template=("-c", "true", "--"))

    result = _run(registry, git_repo, "antigravity-focused-tests", include_diff=True, diff_base="main")

    command = " ".join(result.record.requested_command)
    # A dirty candidate's scoped diff is HEAD -> index: exactly the staged
    # delta on top of the already-committed "prior committed line", never the
    # wider base..HEAD history the clean-candidate fallback would produce.
    assert "+freshly staged line" in command
    assert "+prior committed line" not in command


def test_write_worker_blocked_on_protected_branch(git_repo):
    subprocess.run(["git", "-C", str(git_repo), "checkout", "-q", "-b", "main"], check=True)
    registry = _registry_with(name="claude-code", cli_bin="sh", cli_template=("-c", "true", "--"))
    result = _run(registry, git_repo, "claude-code", role="primary-implementation", allow_write=True)
    assert result.record.result == "BLOCKED"
    assert "protected branch" in " ".join(result.record.notes)


def test_write_worker_blocked_on_detached_head(git_repo):
    subprocess.run(["git", "-C", str(git_repo), "checkout", "-q", "--detach", "HEAD"], check=True)
    registry = _registry_with(name="claude-code", cli_bin="sh", cli_template=("-c", "true", "--"))
    result = _run(registry, git_repo, "claude-code", role="primary-implementation", allow_write=True)
    assert result.record.result == "BLOCKED"
    assert "detached HEAD" in " ".join(result.record.notes)


def test_write_worker_blocked_when_lock_is_held(git_repo):
    registry = _registry_with(name="claude-code", cli_bin="sh", cli_template=("-c", "true", "--"))
    lock_dir = git_repo / ".agent-output"
    lock_dir.mkdir(parents=True)
    (lock_dir / ".write-lock").write_text(f"grok-build pid={os.getpid()} at=1")
    result = _run(registry, git_repo, "claude-code", role="primary-implementation", allow_write=True)
    assert result.record.result == "BLOCKED"
    assert "holds the checkout lock" in " ".join(result.record.notes)


def test_write_worker_recovers_verifiably_stale_lock(git_repo):
    registry = _registry_with(name="claude-code", cli_bin="sh", cli_template=("-c", "true", "--"))
    lock_dir = git_repo / ".agent-output"
    lock_dir.mkdir(parents=True)
    (lock_dir / ".write-lock").write_text("grok-build pid=99999999 at=1")
    result = _run(registry, git_repo, "claude-code", role="primary-implementation", allow_write=True)
    assert result.record.result == "PASS"
    assert not (lock_dir / ".write-lock").exists()


def test_invalid_task_id_is_a_usage_error(git_repo):
    with pytest.raises(ValidationError):
        _run(load_registry(), git_repo, "opencode2-gemini-flash-lite", task="not a task id")


# --------------------------------------------------------------------------- CI safety


def test_no_github_workflow_invokes_an_ai_worker():
    workflows = list((REPO_ROOT / ".github" / "workflows").glob("*.yml"))
    banned = (
        "agy ",
        "agy\n",
        " grok",
        "opencode ",
        "opencode\n",
        "claude ",
        "anthropic",
        "scripts/agents",
        "scripts.agents",
        "orchestrate",
    )
    for path in workflows:
        text = path.read_text()
        lowered = text.lower()
        for token in banned:
            assert token.lower() not in lowered, f"{path.name} references {token!r}"
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.endswith("_API_KEY: ''") or "_API_KEY:" not in stripped:
                continue
            assert stripped.split(":", 1)[1].strip() in {"''", '""'}, f"{path.name}: non-empty API key {stripped!r}"


def test_agent_output_tree_is_git_ignored():
    gitignore = (REPO_ROOT / ".gitignore").read_text()
    assert "/.agent-output/" in gitignore
    check = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "check-ignore", "-q", ".agent-output/ENG-AGENT-01/x/manifest.json"],
    )
    assert check.returncode == 0


# --------------------------------------------------------------------------- CLI smoke


def test_cli_list_workers_and_route(capsys):
    assert orchestrate.main(["list-workers"]) == 0
    out = capsys.readouterr().out
    assert "opencode2-gemini-flash-lite" in out
    assert orchestrate.main(["route", "--role", "focused-tests"]) == 0
    out = capsys.readouterr().out
    assert "first available" in out


def test_cli_run_dry_run(monkeypatch, git_repo, capsys):
    monkeypatch.chdir(git_repo)
    code = orchestrate.main(
        [
            "run",
            "--task",
            "ENG-AGENT-01",
            "--worker",
            "opencode2-gemini-flash-lite",
            "--role",
            "focused-tests",
            "--why",
            "focused non-billable test verification",
            "--scope",
            "seed.txt",
            "--check",
            "tests/test_agents_orchestration.py",
            "--dry-run",
            "--",
            "run tests/test_agents_orchestration.py",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "STATUS: DRY_RUN" in out
    assert "MANIFEST: .agent-output/ENG-AGENT-01/opencode2-gemini-flash-lite/" in out
    manifests = list(
        (git_repo / ".agent-output" / "ENG-AGENT-01" / "opencode2-gemini-flash-lite").glob("*/manifest.json")
    )
    assert len(manifests) == 1
