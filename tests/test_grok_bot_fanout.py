"""ENG-AO-02: controlled, AO-orchestrated read-only Grok bot fan-out (deterministic, no live calls)."""

from __future__ import annotations

import dataclasses
import json
import os
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from scripts.agents import graph_context, orchestrate, subagents
from scripts.agents.control_plane import agent_activity
from scripts.agents.control_plane.models import Task
from scripts.agents.control_plane.supervisor import Supervisor
from scripts.agents.control_plane.usage_policy import TASK_CLASSES
from scripts.agents.registry import Registry, RegistryError, load_registry
from scripts.agents.validation import ValidationError

PRIMARY = "grok-build-bots"
BOT = "grok-build-bot"
ELIGIBLE = "serious-integration"


# --------------------------------------------------------------------------- fixtures / fakes

FAKE_GRAPHIFY = """#!{python}
import json, sys
from pathlib import Path
snapshot = Path(sys.argv[2])
graph = {{
    "nodes": [
        {{"id": "app", "label": "app.py", "source_file": "src/app.py"}},
        {{"id": "app.run", "label": "run()", "source_file": "src/app.py"}},
        {{"id": "util", "label": "util.py", "source_file": "src/util.py"}},
        {{"id": "t", "label": "test_app.py", "source_file": "tests/test_app.py"}},
        {{"id": "doc", "label": "guide.md", "source_file": "docs/guide.md"}},
        {{"id": "view", "label": "view.tsx", "source_file": "frontend/view.tsx"}},
        {{"id": "sec", "label": "secrets.py", "source_file": "config/secrets.py"}},
    ],
    "links": [
        {{"source": "app", "target": "util", "relation": "imports"}},
        {{"source": "t", "target": "app", "relation": "imports"}},
        {{"source": "doc", "target": "app", "relation": "imports"}},
        {{"source": "view", "target": "app", "relation": "imports"}},
        {{"source": "app", "target": "sec", "relation": "imports"}},
    ],
}}
out = snapshot / "graphify-out"
out.mkdir()
(out / "graph.json").write_text(json.dumps(graph))
"""


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "managed"
    root.mkdir()
    _git(root, "init", "-q", "-b", "work")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "Test")
    files = {
        "src/app.py": "import util\n",
        "src/util.py": "def helper(): pass\n",
        "tests/test_app.py": "import app\n",
        "docs/guide.md": "# guide\n",
        "frontend/view.tsx": "import app\n",
        "config/secrets.py": "TOKEN = 'do-not-leak'\n",
        ".gitignore": ".agent-output/\ngraphify-out/\n",
    }
    for name, body in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed")
    return root


@pytest.fixture(autouse=True)
def _graphify_absent_by_default(monkeypatch, tmp_path):
    monkeypatch.setenv("OCTAREL_GRAPHIFY_BIN", str(tmp_path / "missing" / "graphify"))
    monkeypatch.setenv("OCTAREL_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("OCTAREL_GRAPHIFY", raising=False)


@pytest.fixture
def graphify(tmp_path, monkeypatch) -> Path:
    binary = tmp_path / "bin" / "graphify"
    binary.parent.mkdir()
    binary.write_text(FAKE_GRAPHIFY.format(python=sys.executable))
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("OCTAREL_GRAPHIFY_BIN", str(binary))
    return binary


class Fakes:
    """sh-based stand-ins for the grok primary and bot CLIs; they record every invocation."""

    def __init__(self, base: Path):
        self.dir = base / "fake"
        self.dir.mkdir()
        self.bot_calls = self.dir / "bot-calls.txt"
        self.primary_calls = self.dir / "primary-calls.txt"
        self.primary_prompt = self.dir / "primary-prompt.txt"
        self.prompts = self.dir / "prompts"
        self.prompts.mkdir()
        self.lock_seen = self.dir / "lock-seen.txt"
        self.env_seen = self.dir / "env-seen.txt"

    def bot_script(self, *, fail: tuple[str, ...] = (), quota: tuple[str, ...] = (), sleep: tuple[str, ...] = (),
                   write: tuple[str, ...] = ()) -> str:
        lines = [
            r"""id=$(printf '%s' "$1" | sed -n 's/.*Bot: \(bot-[0-9]-[a-z]*\).*/\1/p' | head -1)""",
            f'echo "$id" >> {self.bot_calls}',
            f'printf "%s" "$1" > {self.prompts}/"$id".txt',
            f'if test -f "$PWD/.agent-output/.write-lock"; then echo "$id" >> {self.lock_seen}; fi',
            f'env | grep -c "API_KEY" >> {self.env_seen} || true',
            'case "$id" in',
        ]
        for key in write:
            lines.append(f'  *{key}*) echo bot-write > bot-was-here.txt;;')
        for key in fail:
            lines.append(f'  *{key}*) echo "bot exploded" >&2; exit 3;;')
        for key in quota:
            lines.append(f'  *{key}*) echo \'{{"is_error":true,"result":"usage limit reached: quota exhausted"}}\'; exit 0;;')
        for key in sleep:
            lines.append(f'  *{key}*) sleep 5;;')
        lines += ["esac", 'printf \'{"result":"finding from %s","usage":{"input_tokens":11,"output_tokens":3}}\' "$id"']
        return "\n".join(lines)

    def primary_script(self) -> str:
        return "\n".join([
            f'echo run >> {self.primary_calls}',
            f'printf "%s" "$1" > {self.primary_prompt}',
            "echo change > primary-out.txt",
            'printf \'{"result":"primary done"}\'',
        ])


@pytest.fixture
def fakes(tmp_path) -> Fakes:
    return Fakes(tmp_path)


def _registry(fakes: Fakes, *, bot_script: str | None = None, **config) -> Registry:
    base = load_registry()
    primary = base.get(PRIMARY)
    settings = {**primary.subagents, **config}
    workers = dict(base.workers)
    workers[PRIMARY] = dataclasses.replace(
        primary, cli_bin="sh", cli_template=("-c", fakes.primary_script(), "--"), subagents=settings
    )
    workers[BOT] = dataclasses.replace(
        base.get(BOT), cli_bin="sh", cli_template=("-c", bot_script or fakes.bot_script(), "--")
    )
    return Registry(workers=workers, routes=base.routes, intensities=base.intensities)


def _run(registry: Registry, root: Path, **overrides):
    kwargs = dict(
        registry=registry, root=root, task="ENG-AO-02", worker_name=PRIMARY, role="bot-implementation", model=None,
        intensity="low", why="test", prompt_args=["integrate the change"],
        scope_paths=["src/app.py", "docs/guide.md"], dry_run=False, allow_write=True, allow_overflow=False,
        timeout=60.0, bot_task_class=ELIGIBLE, project_id="octascene",
    )
    kwargs.update(overrides)
    return orchestrate.run_delegation(**kwargs)


def _fanout(result) -> dict:
    return result.manifest["policy_manifest"]["bot_fanout"]


def _lines(path: Path) -> list[str]:
    return path.read_text().splitlines() if path.exists() else []


# --------------------------------------------------------------------------- default routes unchanged


def test_normal_grok_routes_keep_no_subagents_and_have_no_bot_mode():
    registry = load_registry()
    for name in ("grok-build", "grok-build-review"):
        worker = registry.get(name)
        assert "--no-subagents" in worker.cli_template
        assert worker.subagents == {}
    grok = registry.get("grok-build")
    assert grok.is_write_capable and grok.roles == ("primary-implementation", "secondary-implementation")
    # Routing preference is untouched: the bot workers are on no pre-existing route.
    assert registry.route("primary-implementation") == ("claude-code", "codex-build", "grok-build")
    assert registry.route("secondary-implementation") == ("grok-build", "claude-code", "codex-build")
    assert registry.route("diff-review") == (
        "antigravity-diff-review", "opencode2-gemini-flash-lite-review", "grok-build-review", "codex-review",
    )
    for role, names in registry.routes.items():
        if role not in {"bot-implementation", "bot-investigation"}:
            assert PRIMARY not in names and BOT not in names


def test_bot_mode_is_a_separate_explicit_route():
    registry = load_registry()
    assert registry.route("bot-implementation") == (PRIMARY,)
    assert registry.route("bot-investigation") == (BOT,)
    primary = registry.get(PRIMARY)
    assert primary.roles == ("bot-implementation",) and primary.is_write_capable
    assert primary.allow_api_billing is False and primary.requires_isolated_worktree is True
    assert primary.auth_mode == "configured-cli-session"
    # the primary keeps --no-subagents: AO, not the model, owns the fan-out
    assert "--no-subagents" in primary.cli_template
    assert "--always-approve" not in primary.cli_template


def test_bot_worker_is_read_only_non_recursive_and_sandboxed():
    registry = load_registry()
    bot = registry.get(BOT)
    assert bot.is_read_only and not bot.is_write_capable and bot.subagents == {}
    assert bot.allow_api_billing is False and bot.allowed_policy_roles == ("RESEARCHER",)
    command = subagents.TRANSPORTS["grok-cli"].build_command(bot, model=None, intensity="low", prompt="p")
    assert "--no-subagents" in command and "--agents" not in command
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert command[command.index("--permission-mode") + 1] == "plan"
    assert "--disable-web-search" in command
    assert not {"--always-approve", "--allow-write", "--dangerously-skip-permissions"} & set(command)
    # never a reviewer route: bot output is not independent review
    assert "REVIEWER" not in bot.allowed_policy_roles
    assert BOT not in registry.route("diff-review") and PRIMARY not in registry.route("diff-review")


def test_eligible_classes_are_the_existing_usage_policy_classes():
    assert subagents.ELIGIBLE_TASK_CLASSES <= set(TASK_CLASSES)
    assert subagents.KNOWN_TASK_CLASSES == set(TASK_CLASSES)
    assert {"mechanical", "routine"}.isdisjoint(subagents.ELIGIBLE_TASK_CLASSES)


# --------------------------------------------------------------------------- registry validation


def _broken(**changes) -> dict:
    workers = dict(load_registry().workers)
    primary = workers[PRIMARY]
    workers[PRIMARY] = dataclasses.replace(primary, subagents={**primary.subagents, **changes})
    return workers


@pytest.mark.parametrize(
    "changes",
    [
        {"max_bots": 4},
        {"max_bots": 0},
        {"max_bots": True},
        {"levels": 2},
        {"mode": "native-unbounded"},
        {"eligible_task_classes": ["mechanical"]},
        {"eligible_task_classes": []},
        {"transport": "unknown"},
        {"bot_worker": "nope"},
        {"bot_worker": "grok-build"},  # write-capable, not read-only
        {"bot_timeout_seconds": 0},
    ],
)
def test_registry_rejects_unbounded_or_unsafe_bot_config(changes):
    with pytest.raises(RegistryError):
        subagents.validate_subagent_configs(_broken(**changes))


def test_registry_rejects_recursive_writable_or_billing_bots():
    workers = dict(load_registry().workers)
    workers[BOT] = dataclasses.replace(workers[BOT], subagents=dict(workers[PRIMARY].subagents))
    with pytest.raises(RegistryError, match="recursive"):
        subagents.validate_subagent_configs(workers)
    workers = dict(load_registry().workers)
    workers[BOT] = dataclasses.replace(workers[BOT], allow_api_billing=True)
    with pytest.raises(RegistryError, match="API billing"):
        subagents.validate_subagent_configs(workers)
    workers = dict(load_registry().workers)
    template = tuple(t for t in workers[BOT].cli_template if t != "--no-subagents")
    workers[BOT] = dataclasses.replace(workers[BOT], cli_template=template)
    with pytest.raises(RegistryError, match="no-subagents"):
        subagents.validate_subagent_configs(workers)
    workers = dict(load_registry().workers)
    template = tuple(t for t in workers[BOT].cli_template if t != "--disable-web-search")
    workers[BOT] = dataclasses.replace(workers[BOT], cli_template=template)
    with pytest.raises(RegistryError, match="disable-web-search"):
        subagents.validate_subagent_configs(workers)
    workers = dict(load_registry().workers)
    template = tuple("default" if t == "plan" else t for t in workers[BOT].cli_template)
    workers[BOT] = dataclasses.replace(workers[BOT], cli_template=template)
    with pytest.raises(RegistryError, match="plan"):
        subagents.validate_subagent_configs(workers)
    workers = dict(load_registry().workers)
    template = tuple(t for t in workers[PRIMARY].cli_template if t != "--no-subagents")
    workers[PRIMARY] = dataclasses.replace(workers[PRIMARY], cli_template=template)
    with pytest.raises(RegistryError, match="no-subagents"):
        subagents.validate_subagent_configs(workers)


def test_only_a_write_capable_primary_may_own_bots():
    workers = dict(load_registry().workers)
    review = workers["grok-build-review"]
    workers["grok-build-review"] = dataclasses.replace(review, subagents=dict(workers[PRIMARY].subagents))
    with pytest.raises(RegistryError, match="write-capable"):
        subagents.validate_subagent_configs(workers)


def test_hard_cap_is_two_to_three_and_registry_default_is_within_it():
    assert 2 <= subagents.BOT_HARD_CAP <= 3
    assert load_registry().get(PRIMARY).subagents["max_bots"] <= subagents.BOT_HARD_CAP


# --------------------------------------------------------------------------- planning / gating


@pytest.mark.parametrize("task_class", [None, "mechanical", "routine"])
def test_small_or_unclassified_work_declines_fanout_by_default(task_class):
    plan = subagents.plan_fanout(load_registry().get(PRIMARY), task_class, ["src/app.py"])
    assert plan is not None and plan.enabled is False and plan.roles == ()
    assert plan.evidence()["decision"] == "declined" and plan.evidence()["bot_count"] == 0


@pytest.mark.parametrize("task_class", sorted(subagents.ELIGIBLE_TASK_CLASSES))
def test_complex_task_classes_are_eligible(task_class):
    plan = subagents.plan_fanout(load_registry().get(PRIMARY), task_class, ["src/app.py"])
    assert plan.enabled and [role.key for role in plan.roles] == ["dependency", "tests"]
    assert "impact: no UI/API/documentation surface in scope" in plan.skipped_roles


def test_impact_bot_only_when_ui_api_or_docs_are_in_scope_and_count_is_bounded():
    worker = load_registry().get(PRIMARY)
    plan = subagents.plan_fanout(worker, ELIGIBLE, ["src/app.py", "docs/guide.md"])
    assert [role.key for role in plan.roles] == ["dependency", "tests", "impact"]
    tight = dataclasses.replace(worker, subagents={**worker.subagents, "max_bots": 2})
    bounded = subagents.plan_fanout(tight, ELIGIBLE, ["src/app.py", "docs/guide.md"])
    assert len(bounded.roles) == 2 and any("over the 2-bot bound" in s for s in bounded.skipped_roles)
    # a config can never exceed the hard cap even if it bypassed validation
    huge = dataclasses.replace(worker, subagents={**worker.subagents, "max_bots": 99})
    assert len(subagents.plan_fanout(huge, ELIGIBLE, ["docs/guide.md"]).roles) <= subagents.BOT_HARD_CAP


def test_no_bot_mode_worker_has_no_plan_and_rejects_an_explicit_class(project):
    registry = load_registry()
    assert subagents.plan_fanout(registry.get("grok-build"), ELIGIBLE, ["src/app.py"]) is None
    with pytest.raises(ValidationError, match="bot-enabled worker"):
        _run(registry, project, worker_name="grok-build", role="primary-implementation", dry_run=True)
    with pytest.raises(ValidationError, match="invalid bot task class"):
        _run(registry, project, dry_run=True, bot_task_class="whenever")


def test_bot_worker_cannot_be_selected_for_a_review_role(project):
    with pytest.raises(ValidationError, match="does not declare role"):
        _run(load_registry(), project, worker_name=PRIMARY, role="diff-review", dry_run=True)


# --------------------------------------------------------------------------- dry run / declined


def test_dry_run_records_planned_fanout_without_running_bots(project, fakes):
    result = _run(_registry(fakes), project, dry_run=True)
    assert result.record.result == "DRY_RUN"
    evidence = _fanout(result)
    assert evidence["decision"] == "planned" and evidence["used"] is False and evidence["bots"] == []
    assert evidence["planned_roles"] == ["dependency", "tests", "impact"]
    assert not fakes.bot_calls.exists() and not fakes.primary_calls.exists()


def test_declined_task_runs_single_agent_primary_without_bots(project, fakes):
    result = _run(_registry(fakes), project, bot_task_class="mechanical")
    assert result.record.result == "PASS"
    assert _fanout(result)["decision"] == "declined" and _fanout(result)["bot_count"] == 0
    assert not fakes.bot_calls.exists()
    assert _lines(fakes.primary_calls) == ["run"]
    assert "BOT FINDINGS" not in fakes.primary_prompt.read_text()


def test_no_class_supplied_is_never_automatic(project, fakes):
    result = _run(_registry(fakes), project, bot_task_class=None)
    assert result.record.result == "PASS" and not fakes.bot_calls.exists()
    assert "never automatic" in _fanout(result)["reason"]


# --------------------------------------------------------------------------- end-to-end fan-out


def test_fanout_runs_bounded_read_only_bots_then_the_primary_synthesizes(project, fakes):
    result = _run(_registry(fakes), project)
    assert result.record.result == "PASS" and result.exit_code == 0
    evidence = _fanout(result)
    assert evidence["decision"] == "fanned-out" and evidence["used"] is True
    assert evidence["bot_count"] == 3 <= subagents.BOT_HARD_CAP and evidence["levels"] == 1
    assert evidence["bots_read_only"] is True and evidence["primary_only_writer"] is True
    assert evidence["independent_review"] is False and evidence["api_billing"] is False
    assert evidence["retries"] == 0 and evidence["model_escalation"] is False
    assert sorted(_lines(fakes.bot_calls)) == ["bot-1-dependency", "bot-2-tests", "bot-3-impact"]
    # each bot: exactly one attempt, provider/model/timing/result/usage recorded
    for bot in evidence["bots"]:
        assert bot["attempts"] == 1 and bot["retries"] == 0 and bot["read_only"] is True
        assert bot["provider"] == "xAI" and bot["model"] == "grok-4.6" and bot["worker"] == BOT
        assert bot["result"] == "PASS" and bot["started_at"] and bot["finished_at"]
        assert bot["usage"] == {"input_tokens": 11, "output_tokens": 3}
    # findings reach the primary, labelled as advisory and not independent review
    prompt = fakes.primary_prompt.read_text()
    assert "BOT FINDINGS" in prompt and "NOT independent review" in prompt
    for bot_id in ("bot-1-dependency", "bot-2-tests", "bot-3-impact"):
        assert f"finding from {bot_id}" in prompt
    # only the primary wrote, and bot logs live under the primary's own evidence directory
    assert (project / "primary-out.txt").exists() and not (project / "bot-was-here.txt").exists()
    assert "primary-out.txt" in result.manifest["files_changed"]
    run_dir = project / Path(result.manifest["paths"]["manifest"]).parent
    assert sorted(p.name for p in (run_dir / "bots").iterdir()) == ["bot-1-dependency", "bot-2-tests", "bot-3-impact"]
    assert all((run_dir / "bots" / d / "run.log").is_file() for d in os.listdir(run_dir / "bots"))
    assert any("bot fan-out fanned-out: 3/3" in note for note in result.manifest["notes"])


def test_bots_run_inside_the_writer_lock_and_get_no_api_keys(project, fakes, monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "xai-should-never-reach-a-bot")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-never-reach-a-bot")
    result = _run(_registry(fakes), project)
    assert result.record.result == "PASS"
    assert sorted(_lines(fakes.lock_seen)) == ["bot-1-dependency", "bot-2-tests", "bot-3-impact"]
    assert set(_lines(fakes.env_seen)) == {"0"}
    assert not (project / ".agent-output" / ".write-lock").exists()  # released after the primary


def test_bots_cannot_start_while_another_writer_holds_the_checkout(project, fakes):
    lock = project / ".agent-output" / ".write-lock"
    lock.parent.mkdir(parents=True)
    lock.write_text(f"grok-build pid={os.getpid()} at=1")
    result = _run(_registry(fakes), project)
    assert result.record.result == "BLOCKED"
    assert not fakes.bot_calls.exists() and not fakes.primary_calls.exists()


def test_fanout_needs_the_explicit_write_authorisation_like_any_primary(project, fakes):
    result = _run(_registry(fakes), project, allow_write=False)
    assert result.record.result == "BLOCKED" and not fakes.bot_calls.exists()


def test_bot_that_writes_is_a_contract_violation_that_blocks_the_primary(project, fakes):
    registry = _registry(fakes, bot_script=fakes.bot_script(write=("tests",)))
    result = _run(registry, project)
    assert result.record.result == "BLOCKED"
    evidence = _fanout(result)
    assert evidence["decision"] == "blocked-contract-violation" and evidence["used"] is False
    assert "bot-was-here.txt" in evidence["contract_violation"]
    assert not fakes.primary_calls.exists()  # primary never launched on a mutated checkout
    assert "contract violation" in " ".join(result.manifest["notes"])


def test_individual_bot_failure_is_recorded_without_retry_and_primary_continues(project, fakes):
    registry = _registry(fakes, bot_script=fakes.bot_script(fail=("tests",)))
    result = _run(registry, project)
    assert result.record.result == "PASS"
    evidence = _fanout(result)
    assert evidence["partial_failure"] is True and evidence["succeeded"] == 2 and evidence["failed"] == 1
    failed = next(bot for bot in evidence["bots"] if bot["role"] == "tests")
    assert failed["result"] == "FAIL" and failed["exit_status"] == 3 and failed["attempts"] == 1
    assert _lines(fakes.bot_calls).count("bot-2-tests") == 1  # no retry storm
    assert len(_lines(fakes.bot_calls)) == 3 and _lines(fakes.primary_calls) == ["run"]
    prompt = fakes.primary_prompt.read_text()
    assert "finding from bot-1-dependency" in prompt and "finding from bot-2-tests" not in prompt
    assert "bot-2-tests: FAIL" in prompt and "not retried" in prompt


def test_quota_or_context_failure_never_escalates_the_model_or_retries(project, fakes):
    registry = _registry(fakes, bot_script=fakes.bot_script(quota=("dependency", "tests", "impact")))
    result = _run(registry, project)
    evidence = _fanout(result)
    assert evidence["decision"] == "no-usable-findings" and evidence["used"] is False
    assert evidence["model_escalation"] is False and evidence["retries"] == 0
    for bot in evidence["bots"]:
        assert bot["failure_category"] == "quota" and bot["attempts"] == 1 and bot["model"] == "grok-4.6"
    assert len(_lines(fakes.bot_calls)) == 3  # one attempt each, no stronger model, no retry
    # the primary still runs as the normal single agent, on its own configured model
    assert result.record.result == "PASS" and _lines(fakes.primary_calls) == ["run"]
    assert "BOT FINDINGS" not in fakes.primary_prompt.read_text()
    assert result.manifest["actual"]["model"] == "grok-4.6"


def test_bot_timeout_is_bounded_and_recorded(project, fakes):
    registry = _registry(fakes, bot_script=fakes.bot_script(sleep=("impact",)), bot_timeout_seconds=1)
    result = _run(registry, project)
    impact = next(bot for bot in _fanout(result)["bots"] if bot["role"] == "impact")
    assert impact["result"] == "TIMEOUT" and "timed out" in impact["failure_reason"]
    assert _fanout(result)["partial_failure"] is True and result.record.result == "PASS"


def test_bots_may_use_at_most_a_third_of_the_run_budget(project, fakes, monkeypatch):
    seen = []
    real = subagents.run_worker_process_group

    def spy(command, root, *, timeout, on_group=None):
        seen.append(timeout)
        return real(command, root, timeout=timeout, on_group=on_group)

    monkeypatch.setattr(subagents, "run_worker_process_group", spy)
    _run(_registry(fakes), project, timeout=30.0)  # configured bot timeout is 600s
    assert seen == [10.0, 10.0, 10.0]


def test_a_hung_bot_is_reaped_and_fails_closed_without_launching_the_primary(project, fakes, monkeypatch):
    release = threading.Event()
    killed = []

    def hang(command, root, *, timeout, on_group=None):
        on_group(4242)  # a live bot process group that never returns
        release.wait(30)
        return 0, ""

    monkeypatch.setattr(subagents, "BOT_JOIN_GRACE_SECONDS", 0.2)
    monkeypatch.setattr(subagents, "run_worker_process_group", hang)
    monkeypatch.setattr(subagents, "kill_process_group", killed.append)
    try:
        result = _run(_registry(fakes, bot_timeout_seconds=0.1), project)
    finally:
        release.set()
    evidence = _fanout(result)
    assert result.record.result == "BLOCKED" and evidence["decision"] == "blocked-contract-violation"
    assert "still running after their bound" in evidence["reason"] and evidence["failed"] == 3
    assert {bot["result"] for bot in evidence["bots"]} == {"TIMEOUT"}
    assert all("did not finish" in bot["failure_reason"] for bot in evidence["bots"])
    assert killed and set(killed) == {4242}  # the live bot groups were reaped before giving up
    assert not fakes.primary_calls.exists()  # the writer never starts beside a possibly-live bot


def test_a_timed_out_bot_takes_its_child_processes_with_it(project, fakes, tmp_path):
    pidfile = tmp_path / "grandchild.pid"
    script = f"sleep 60 &\necho $! > {pidfile}\nsleep 60"
    registry = _registry(fakes, bot_script=script, bot_timeout_seconds=1)
    result = _run(registry, project)
    assert {bot["result"] for bot in _fanout(result)["bots"]} == {"TIMEOUT"}
    grandchild = int(pidfile.read_text().split()[0])
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(grandchild, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    with pytest.raises(ProcessLookupError):
        os.kill(grandchild, 0)
    assert result.record.result == "PASS" and not (project / ".agent-output" / ".write-lock").exists()


def test_primary_timeout_budget_accounts_for_fanout(project, fakes, monkeypatch):
    seen = {}
    real = orchestrate.run_worker_process

    def spy(command, root, *, timeout):
        seen.setdefault("timeouts", []).append(timeout)
        return real(command, root, timeout=timeout)

    monkeypatch.setattr(orchestrate, "run_worker_process", spy)
    _run(_registry(fakes), project, timeout=120.0)
    assert seen["timeouts"][-1] < 120.0  # bot time is deducted from the primary budget


def test_independent_review_remains_a_separate_provider_diverse_stage(project, fakes):
    result = _run(_registry(fakes), project)
    evidence = _fanout(result)
    assert evidence["independent_review"] is False and "not independent review" in evidence["independent_review_note"]
    registry = load_registry()
    # diff-review still resolves through non-bot workers, first the Google reviewers, Codex last
    assert registry.route("diff-review")[0] == "antigravity-diff-review" and registry.route("diff-review")[-1] == "codex-review"
    policy = result.manifest["policy_manifest"]
    assert policy["role"] == "IMPLEMENTER" and policy["read_write_mode"] == "write"
    assert evidence["bot_policy_identity"]["role"] == "RESEARCHER"
    assert evidence["bot_policy_identity"]["read_write_mode"] == "read-only"


# --------------------------------------------------------------------------- Graphify scoping / secrets


def test_each_bot_gets_only_its_scoped_graph_slice(project, fakes, graphify):
    result = _run(_registry(fakes), project)
    bots = {bot["role"]: bot for bot in _fanout(result)["bots"]}
    assert _fanout(result)["graph_context_supplied"] is True
    for role, bot in bots.items():
        assert bot["graph_context"]["supplied"] is True and bot["graph_context"]["focus"] == role
        assert bot["graph_context"]["status"] in {"used", "refreshed"}
    dependency = (fakes.prompts / "bot-1-dependency.txt").read_text()
    tests = (fakes.prompts / "bot-2-tests.txt").read_text()
    impact = (fakes.prompts / "bot-3-impact.txt").read_text()
    assert "Scope focus: dependency" in dependency and "Imports / dependencies: src/util.py" in dependency
    assert "Likely affected tests" not in dependency
    assert "Scope focus: tests" in tests and "tests/test_app.py" in tests and "Imports / dependencies" not in tests
    assert "Scope focus: impact" in impact and "Dependents (blast radius): frontend/view.tsx" in impact
    assert "src/util.py" not in impact and "tests/test_app.py" not in impact
    # the same slices are advisory only
    for text in (dependency, tests, impact):
        assert "NOT authoritative" in text


def test_secret_paths_are_excluded_from_every_bot_context(project, fakes, graphify):
    (project / "config" / "notes.txt").write_text("wip\n")
    (project / ".env").write_text("SECRET=abc\n")
    (project / "data").mkdir()
    (project / "data" / "dump.txt").write_text("private\n")
    result = _run(_registry(fakes), project)
    assert result.record.result == "PASS"
    for prompt_file in fakes.prompts.iterdir():
        text = prompt_file.read_text()
        envelope = text[text.index("--- BOT TASK ENVELOPE ---") :]
        graph_part = text[text.index("--- ADVISORY GRAPH CONTEXT") : text.index("--- BOT TASK ENVELOPE ---")]
        for forbidden in ("secrets.py", "do-not-leak", "SECRET=abc", "data/dump", "dump.txt"):
            assert forbidden not in graph_part and forbidden not in envelope
    for bot in _fanout(result)["bots"]:
        assert not any(graph_context.is_sensitive_path(path) for path in bot["scope"])


def test_sensitive_scope_paths_are_rejected_before_any_bot_starts(project, fakes):
    with pytest.raises(ValidationError):
        _run(_registry(fakes), project, scope_paths=["config/secrets.py"])
    assert not fakes.bot_calls.exists()


def test_graphify_unavailable_falls_back_to_bounded_scope_only_bots(project, fakes):
    result = _run(_registry(fakes), project)
    evidence = _fanout(result)
    assert result.record.result == "PASS" and evidence["decision"] == "fanned-out"
    assert evidence["graph_context_supplied"] is False and evidence["bot_count"] <= subagents.BOT_HARD_CAP
    for bot in evidence["bots"]:
        assert bot["graph_context"]["supplied"] is False and bot["graph_context"]["status"] == "unavailable"
        text = (fakes.prompts / f"{bot['id']}.txt").read_text()
        assert "ADVISORY GRAPH CONTEXT" not in text and "src/app.py" in text  # scope paths only


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("docs/guide.md", True), ("README.md", True), ("frontend/view.tsx", True), ("src/api/routes.py", True),
        ("src/dashboard.py", True), ("src/capital.py", False), ("src/mapping.py", False), ("src/app.py", False),
        ("tests/test_app.py", False),
    ],
)
def test_impact_surface_matches_path_segments_not_substrings(path, expected):
    assert graph_context.is_impact_path(path) is expected


def test_impact_caller_rows_are_filtered_on_their_file_path():
    summary = {
        "symbols": {}, "imports": [], "dependents": ["src/capital.py", "docs/a.md"], "calls": [], "tests": [],
        "called_by": ["run() (src/capital.py) -> helper", "render() (frontend/view.tsx) -> helper"],
    }
    focused = graph_context._focused_summary(summary, graph_context.FOCUS_IMPACT)
    assert focused["dependents"] == ["docs/a.md"]
    assert focused["called_by"] == ["render() (frontend/view.tsx) -> helper"]


def test_graph_focus_slices_are_pure_subsets_and_unfocused_output_is_unchanged(project, graphify):
    contexts = graph_context.build_graph_contexts(project, ["src/app.py", "docs/guide.md"], [None, *graph_context.FOCUSES])
    full = contexts[None].text
    assert "Scope focus" not in full
    assert "src/util.py" in full and "tests/test_app.py" in full and "docs/guide.md" in full
    assert graph_context.build_graph_context(project, ["src/app.py", "docs/guide.md"]).text == full
    for focus in graph_context.FOCUSES:
        assert len(contexts[focus].text) <= len(full) + 200
        assert contexts[focus].evidence["focus"] == focus
    with pytest.raises(ValueError):
        graph_context._focused_summary({}, "nonsense")


# --------------------------------------------------------------------------- sessions, supervisor, dashboard


def test_session_fanout_uses_explicit_bot_scopes(project, fakes):
    result = orchestrate.run_session(
        registry=_registry(fakes), root=project, task="ENG-AO-02", worker_name=PRIMARY, role="bot-implementation",
        model=None, intensity="low", why="test", prompt="finish the integration", dry_run=False, timeout=60.0,
        bot_task_class="hard-debugging", bot_scope_paths=["src/app.py"],
    )
    assert result.record.result == "PASS"
    evidence = result.manifest["policy_manifest"]["bot_fanout"]
    assert evidence["decision"] == "fanned-out" and evidence["bot_count"] == 2
    assert all(bot["scope"] == ["src/app.py"] for bot in evidence["bots"])
    assert "BOT FINDINGS" in fakes.primary_prompt.read_text()


def test_primary_workflow_override_never_reaches_the_read_only_bots(project, fakes):
    result = _run(_registry(fakes), project, workflow="TEST_AND_FIX")
    assert result.manifest["policy_manifest"]["workflow"] == "TEST_AND_FIX"
    bot_prompt = (fakes.prompts / "bot-1-dependency.txt").read_text()
    assert "ROLE: RESEARCHER" in bot_prompt and "WORKFLOW: none" in bot_prompt
    assert "POLICY: .agents/workflows/" not in bot_prompt


def test_session_without_a_class_never_fans_out(project, fakes):
    result = orchestrate.run_session(
        registry=_registry(fakes), root=project, task="ENG-AO-02", worker_name=PRIMARY, role="bot-implementation",
        model=None, intensity="low", why="test", prompt="finish", dry_run=False, timeout=60.0,
    )
    assert result.record.result == "PASS" and not fakes.bot_calls.exists()


def test_cli_accepts_the_explicit_bot_task_class():
    parser = orchestrate.build_parser()
    args = parser.parse_args(
        ["run", "--task", "ENG-AO-02", "--worker", PRIMARY, "--role", "bot-implementation", "--why", "x",
         "--bot-task-class", "hard-debugging", "--scope", "src", "--", "go"]
    )
    assert args.bot_task_class == "hard-debugging"
    with pytest.raises(SystemExit):
        parser.parse_args(["run", "--task", "ENG-AO-02", "--worker", PRIMARY, "--role", "r", "--why", "x",
                           "--bot-task-class", "bogus", "--", "go"])


def _supervisor_argv(command: list[str], *, launch_mode: str | None = None) -> list[str]:
    supervisor = Supervisor.__new__(Supervisor)
    supervisor.repo_root = Path("/tmp/repo")
    task = Task(id="t1", task_ref="ENG-AO-02", role="bot-implementation", worker=PRIMARY, command=command)
    if launch_mode:
        task.launch_mode = launch_mode
    return supervisor._build_argv(task, dry_run=False)


def test_supervisor_passes_the_explicit_class_and_keeps_it_out_of_the_prompt():
    argv = _supervisor_argv(["scope:src/app.py", "bot-class:serious-integration", "integrate"])
    assert argv[argv.index("--bot-task-class") + 1] == "serious-integration"
    assert argv[argv.index("--") + 1 :] == ["integrate"]
    assert "--bot-task-class" not in _supervisor_argv(["scope:src/app.py", "integrate"])


def test_run_history_exposes_parent_and_bot_activity_through_existing_subagents_shape(project, fakes):
    result = _run(_registry(fakes), project, bot_task_class="serious-integration")
    rows = agent_activity.latest_subagents(project, "ENG-AO-02", PRIMARY)
    assert [row["role"] for row in rows] == ["dependency", "tests", "impact"]
    assert all(row["state"] == "COMPLETED" and row["read_only"] is True for row in rows)
    run_id = Path(result.manifest["paths"]["manifest"]).parent.name
    details = agent_activity.read_attempt(project, "ENG-AO-02", PRIMARY, run_id)["details"]
    assert details["worker"] == PRIMARY and len(details["subagents"]) == 3
    assert agent_activity.latest_subagents(project, "ENG-AO-02", "grok-build") == []
    manifest = json.loads((project / result.manifest["paths"]["manifest"]).read_text())
    assert manifest["policy_manifest"]["bot_fanout"]["bot_count"] == 3


def test_transport_seam_accepts_a_future_bot_capable_transport():
    class OpenCodeXaiTransport(subagents.GrokCliTransport):
        name = "opencode-xai-test"

    subagents.register_transport(OpenCodeXaiTransport())
    try:
        assert "opencode-xai-test" in subagents.TRANSPORTS
        workers = _broken(transport="opencode-xai-test")
        subagents.validate_subagent_configs(workers)  # same bounded/read-only validation applies
    finally:
        del subagents.TRANSPORTS["opencode-xai-test"]
