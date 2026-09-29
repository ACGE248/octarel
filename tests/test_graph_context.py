"""ENG-AO-01: optional, provider-neutral Graphify context for AO workers."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from scripts.agents import graph_context, graph_lifecycle, orchestrate
from scripts.agents.graph_context import build_graph_context
from scripts.agents.policy import validate_policy_preservation
from scripts.agents.registry import load_registry

FAKE_GRAPHIFY = """#!{python}
import json, os, sys
from pathlib import Path
if sys.argv[1:] == ["--version"]:
    print("graphify " + os.environ.get("FAKE_GRAPHIFY_VERSION", "0.9.71"))
    sys.exit(0)
if sys.argv[1:] == ["--help"]:
    print("usage: graphify extract PATH --code-only")
    sys.exit(0)
snapshot = Path(sys.argv[2])
log = os.environ.get("FAKE_GRAPHIFY_LOG")
if log:
    with open(log, "a") as handle:
        handle.write(json.dumps({{"argv": sys.argv[1:], "cwd": os.getcwd(),
            "env_keys": sorted(k for k in os.environ if "KEY" in k or "TOKEN" in k),
            "files": sorted(str(p.relative_to(snapshot)) for p in snapshot.rglob("*") if p.is_file())}}) + "\\n")
if os.environ.get("FAKE_GRAPHIFY_FAIL"):
    sys.exit(3)
graph = {{
    "nodes": [
        {{"id": "app", "label": "app.py", "source_file": "src/app.py"}},
        {{"id": "app.run", "label": "run()", "source_file": "src/app.py"}},
        {{"id": "util", "label": "util.py", "source_file": "src/util.py"}},
        {{"id": "util.helper", "label": "helper()", "source_file": "src/util.py"}},
        {{"id": "t", "label": "test_app.py", "source_file": "tests/test_app.py"}},
        {{"id": "sec", "label": "secrets.py", "source_file": "config/secrets.py"}},
        {{"id": "ghost", "label": "ghost.py", "source_file": "src/ghost.py"}},
        {{"id": "abs", "label": "abs.py", "source_file": "/etc/passwd"}},
    ],
    "links": [
        {{"source": "app", "target": "util", "relation": "imports"}},
        {{"source": "app", "target": "sec", "relation": "imports"}},
        {{"source": "app", "target": "ghost", "relation": "imports"}},
        {{"source": "t", "target": "app", "relation": "imports"}},
        {{"source": "app.run", "target": "util.helper", "relation": "calls"}},
    ],
}}
for i in range(int(os.environ.get("FAKE_GRAPHIFY_EXTRA_NODES", "0"))):
    graph["nodes"].append({{"id": f"n{{i}}", "label": f"sym_{{i}}_" + "x" * 80, "source_file": "src/app.py"}})
out = snapshot / "graphify-out"
out.mkdir(exist_ok=True)
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
    for name, body in {
        "src/app.py": "import util\n",
        "src/util.py": "def helper(): pass\n",
        "tests/test_app.py": "import app\n",
        "config/secrets.py": "TOKEN = 'do-not-leak'\n",
        ".env": "SECRET=abc\n",
        "data/dump.txt": "private\n",
    }.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    (root / ".gitignore").write_text("graphify-out/\n.agent-output/\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed")
    return root


@pytest.fixture
def graphify(tmp_path: Path, monkeypatch) -> Path:
    binary = tmp_path / "bin" / "graphify"
    binary.parent.mkdir()
    binary.write_text(FAKE_GRAPHIFY.format(python=sys.executable))
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "graphify.log"
    monkeypatch.setenv("OCTAREL_GRAPHIFY_BIN", str(binary))
    monkeypatch.setenv("OCTAREL_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("FAKE_GRAPHIFY_LOG", str(log))
    monkeypatch.delenv("OCTAREL_GRAPHIFY", raising=False)
    return log


def _calls(log: Path) -> list[dict]:
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def test_absent_graphify_is_unavailable_and_changes_nothing(project, monkeypatch, tmp_path):
    monkeypatch.delenv("OCTAREL_GRAPHIFY_BIN", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setenv("OCTAREL_STATE_DIR", str(tmp_path / "state"))
    context = build_graph_context(project, ["src"])
    assert context.text == ""
    assert context.evidence["status"] == "unavailable"
    assert not (tmp_path / "state").exists()


def test_disabled_by_operator_is_skipped(project, graphify, monkeypatch):
    monkeypatch.setenv("OCTAREL_GRAPHIFY", "off")
    assert build_graph_context(project, ["src"]).evidence["status"] == "skipped"
    assert _calls(graphify) == []


def test_selected_project_is_indexed_not_octarel_cwd(project, graphify, tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    context = build_graph_context(project, ["src/app.py"], project_id="octascene")
    assert context.evidence["status"] == "used"
    call = _calls(graphify)[0]
    assert Path(call["argv"][1]).name == "snapshot" and Path(call["cwd"]).name == "snapshot"
    assert not Path(call["argv"][1]).is_relative_to(project)
    assert "src/app.py" in call["files"]
    # generated data lives in Octarel state, never in the managed repository
    assert not (project / "graphify-out").exists()
    assert (tmp_path / "state" / "graph-context").is_dir()
    assert graph_lifecycle.cached_status(project, project_id="octascene", verify_tree=False)["status"] == "READY"
    assert subprocess.run(
        ["git", "-C", str(project), "status", "--porcelain"], capture_output=True, text=True, check=False
    ).stdout == ""


def test_context_is_derived_bounded_and_excludes_secrets(project, graphify, monkeypatch):
    monkeypatch.setenv("FAKE_GRAPHIFY_EXTRA_NODES", "300")
    context = build_graph_context(project, ["src/app.py"])
    text = context.text
    assert "src/util.py" in text and "tests/test_app.py" in text and "helper()" in text
    assert "ADVISORY GRAPH CONTEXT" in text and "NOT authoritative" in text
    for forbidden in ("secrets.py", "do-not-leak", ".env", "data/dump", "/etc/passwd", "ghost.py"):
        assert forbidden not in text
    call = _calls(graphify)[0]
    assert not any(p.startswith(("data/", ".env")) or "secrets" in p for p in call["files"])
    assert len(text) <= graph_context.MAX_CONTEXT_CHARS + 600
    assert context.evidence["truncated"] is True


def test_secret_shaped_content_is_redacted(project, graphify, monkeypatch):
    monkeypatch.setenv("FAKE_GRAPHIFY_EXTRA_NODES", "0")
    original = graph_context._summarize

    def leaky(*args, **kwargs):
        summary = original(*args, **kwargs)
        summary["calls"].append("leak = sk-abcdefghijklmnopqrstuvwx")
        return summary

    monkeypatch.setattr(graph_context, "_summarize", leaky)
    assert "sk-abcdefghijklmnop" not in build_graph_context(project, ["src/app.py"]).text


def test_graphify_never_receives_provider_credentials(project, graphify, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("XAI_API_KEY", "x")
    build_graph_context(project, ["src"])
    assert _calls(graphify)[0]["env_keys"] == []


# ---------------------------------------------------------- ENG-AO-10 lifecycle


def test_lifecycle_missing_reports_exact_bootstrap_and_never_installs(project, monkeypatch, tmp_path):
    monkeypatch.delenv("OCTAREL_GRAPHIFY_BIN", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    status = graph_lifecycle.capability_status()
    assert status["status"] == "MISSING"
    assert status["install_commands"] == ["uv tool install graphifyy", "pipx install graphifyy"]
    assert not (project / "graphify-out").exists()


def test_incompatible_graphify_is_not_used_for_refresh(project, graphify, monkeypatch):
    monkeypatch.setenv("FAKE_GRAPHIFY_VERSION", "1.0.0")
    result = graph_lifecycle.refresh_graph(project, project_id="demo", trigger="manual")
    assert result["status"] == "FAILED_SAFE"
    assert "incompatible" in result["reason"]
    assert all(call["argv"][:1] != ["extract"] for call in _calls(graphify))


def test_operator_can_pin_a_separately_verified_exact_version(graphify, monkeypatch):
    monkeypatch.setenv("FAKE_GRAPHIFY_VERSION", "1.0.0")
    monkeypatch.setenv(graph_lifecycle.VERIFIED_VERSION_ENV, "1.0.0")

    result = graph_lifecycle.capability_status()

    assert result["status"] == "READY"
    assert result["supported_version"] == "1.0.0"
    assert result["supported_version_source"] == "operator-verified-environment"


def test_operator_verified_version_must_be_an_exact_semantic_version(graphify, monkeypatch):
    monkeypatch.setenv(graph_lifecycle.VERIFIED_VERSION_ENV, ">=1")

    result = graph_lifecycle.capability_status()

    assert result["status"] == "OUTDATED"
    assert "exact semantic version" in result["reason"]


def test_incompatible_graphify_is_not_used_by_worker_context(project, graphify, monkeypatch):
    monkeypatch.setenv("FAKE_GRAPHIFY_VERSION", "1.0.0")
    context = build_graph_context(project, ["src"], project_id="demo")
    assert context.text == ""
    assert context.evidence["status"] == "failed-safe"
    assert "incompatible" in context.evidence["reason"]
    assert _calls(graphify) == []


def test_explicit_refresh_records_tree_matched_health_and_never_mutates_project(project, graphify):
    before = subprocess.run(
        ["git", "-C", str(project), "status", "--porcelain"], capture_output=True, text=True, check=False
    ).stdout
    result = graph_lifecycle.refresh_graph(project, project_id="demo", trigger="manual")
    health = graph_lifecycle.cached_status(project, project_id="demo")
    after = subprocess.run(
        ["git", "-C", str(project), "status", "--porcelain"], capture_output=True, text=True, check=False
    ).stdout
    assert result["status"] == health["status"] == "READY"
    assert health["tree_id"] == health["current_tree_id"]
    assert health["nodes"] and health["edges"]
    assert health["api_llm_disabled"] is True
    assert before == after == ""
    assert all(call["argv"][0] != "hook" for call in _calls(graphify))


def test_cached_status_marks_changed_tree_stale_without_running_graphify(project, graphify):
    graph_lifecycle.refresh_graph(project, project_id="demo", trigger="manual")
    calls = len(_calls(graphify))
    (project / "src/app.py").write_text("changed\n")
    status = graph_lifecycle.cached_status(project, project_id="demo")
    assert status["status"] == "STALE"
    assert len(_calls(graphify)) == calls


def test_refresh_coordinator_coalesces_duplicates(project, monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def fake(root, *, project_id, trigger, event_recorder=None):
        calls.append((root, project_id, trigger))
        entered.set()
        release.wait(2)
        return {"status": "READY", "trigger": trigger}

    monkeypatch.setattr(graph_lifecycle, "refresh_graph", fake)
    queue = graph_lifecycle.RefreshCoordinator()
    try:
        first = queue.request(project, project_id="demo", trigger="manual")
        assert entered.wait(1)
        second = queue.request(project, project_id="demo", trigger="pre-review")
        assert first is second
        release.set()
        assert first.result(2)["status"] == "READY"
        assert len(calls) == 1
    finally:
        queue.shutdown()


def test_refresh_coordinator_newer_tree_supersedes_queued_older(project, monkeypatch):
    entered = threading.Event()
    release = threading.Event()

    def fake(root, *, project_id, trigger, event_recorder=None):
        entered.set()
        release.wait(2)
        return {"status": "READY", "trigger": trigger}

    monkeypatch.setattr(graph_lifecycle, "refresh_graph", fake)
    queue = graph_lifecycle.RefreshCoordinator()
    try:
        active = queue.request(project, project_id="demo", trigger="manual")
        assert entered.wait(1)
        (project / "src/app.py").write_text("tree two\n")
        older = queue.request(project, project_id="demo", trigger="pre-review")
        (project / "src/app.py").write_text("tree three\n")
        newer = queue.request(project, project_id="demo", trigger="implementation-checkpoint")
        assert older.result(1)["reason"] == "superseded by a newer tree refresh"
        release.set()
        assert active.result(2)["status"] == "READY"
        assert newer.result(2)["status"] == "READY"
    finally:
        queue.shutdown()


def test_two_worktrees_keep_separate_lifecycle_identity(project, graphify, tmp_path):
    second = tmp_path / "second"
    _git(project, "worktree", "add", "-q", "-b", "second", str(second))
    first = graph_lifecycle.refresh_graph(project, project_id="demo", trigger="manual")
    other = graph_lifecycle.refresh_graph(second, project_id="demo", trigger="manual")
    assert first["worktree_id"] != other["worktree_id"]
    assert first["cache_key"] != other["cache_key"]


def test_tree_matched_cache_is_reused_and_stale_tree_refreshed(project, graphify):
    first = build_graph_context(project, ["src"])
    second = build_graph_context(project, ["src"])
    assert first.evidence["status"] == "used" and second.evidence["status"] == "used"
    assert len(_calls(graphify)) == 1
    (project / "src/util.py").write_text("def helper(): return 1\n")
    third = build_graph_context(project, ["src"])
    assert third.evidence["status"] == "refreshed"
    assert third.evidence["tree_id"] != first.evidence["tree_id"]
    assert len(_calls(graphify)) == 2
    assert _calls(graphify)[0]["argv"][0] == "extract"
    assert _calls(graphify)[1]["argv"][0] == "update"


def test_simultaneous_context_requests_build_one_exact_tree(project, graphify):
    barrier = threading.Barrier(2)

    def build():
        barrier.wait()
        return build_graph_context(project, ["src"]).evidence["tree_id"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        trees = list(pool.map(lambda _item: build(), range(2)))
    assert trees[0] == trees[1]
    assert len(_calls(graphify)) == 1


def test_pruning_preserves_current_graph_and_active_build(tmp_path):
    worktree = tmp_path / "worktree-cache"
    active = worktree / ".build-active"
    active.mkdir(parents=True)
    trees = [worktree / name for name in ("old", "middle", "current")]
    for index, tree in enumerate(trees):
        tree.mkdir()
        os.utime(tree, (index + 1, index + 1))
    graph_context._prune(worktree, trees[-1])
    assert active.is_dir()
    assert trees[-1].is_dir()
    assert len([path for path in worktree.iterdir() if path.is_dir() and not path.name.startswith(".")]) == 2


def test_stale_graph_that_cannot_refresh_is_skipped_with_reason(project, graphify, monkeypatch):
    build_graph_context(project, ["src"])
    (project / "src/util.py").write_text("changed\n")
    monkeypatch.setenv("FAKE_GRAPHIFY_FAIL", "1")
    context = build_graph_context(project, ["src"])
    assert context.text == ""
    assert context.evidence["status"] == "stale"
    assert "refresh failed" in context.evidence["reason"]


def test_build_failure_is_failed_safe(project, graphify, monkeypatch):
    monkeypatch.setenv("FAKE_GRAPHIFY_FAIL", "1")
    context = build_graph_context(project, ["src"])
    assert context.text == "" and context.evidence["status"] == "failed-safe"


def test_wrong_project_graph_is_rejected(project, graphify, tmp_path):
    first = build_graph_context(project, ["src"], project_id="alpha")
    cache = next((tmp_path / "state" / "graph-context").rglob("meta.json")).parent
    meta = json.loads((cache / "meta.json").read_text())
    meta["repo_id"] = "someone-elses-repository"  # graph copied in from another repository
    (cache / "meta.json").write_text(json.dumps(meta))
    again = build_graph_context(project, ["src"], project_id="alpha")
    assert first.evidence["status"] == "used"
    assert again.evidence["status"] == "refreshed"  # rejected, then rebuilt for this repository
    assert len(_calls(graphify)) == 2
    # a different project id / worktree never shares cache entries
    other = build_graph_context(project, ["src"], project_id="beta")
    assert other.evidence["project_key"] != first.evidence["project_key"]


def test_graph_naming_files_missing_from_tree_is_never_trusted(project, graphify):
    text = build_graph_context(project, ["src/app.py"]).text
    assert "ghost.py" not in text


def test_non_git_path_and_no_scope_are_skipped(tmp_path, graphify):
    assert build_graph_context(tmp_path, ["src"]).evidence["status"] == "skipped"


def test_unexpected_error_is_contained(project, graphify, monkeypatch):
    monkeypatch.setattr(graph_context, "_identity", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert build_graph_context(project, ["src"]).evidence["status"] == "failed-safe"


# ---------------------------------------------------------------- provider-neutral delivery

CASES = [
    ("claude-code", "primary-implementation", None),
    ("codex-build", "primary-implementation", None),
    ("grok-build", "primary-implementation", None),  # native Grok CLI
    ("grok-build-review", "diff-review", None),
    ("opencode2-gemini-flash-lite", "focused-tests", None),  # Gemini through OpenCode
    ("opencode2-gemini-flash-lite", "focused-tests", "xai/grok-4.6"),  # Grok/xAI through OpenCode
    ("antigravity-impact-search", "impact-search", None),
]


def _dry_run(project, worker, role, model):
    kwargs = dict(
        registry=load_registry(), root=project, task="ENG-AO-01", worker_name=worker, role=role, model=model,
        intensity="low", why="test", prompt_args=["scope it"], scope_paths=["src/app.py"], dry_run=True,
        allow_write=False, allow_overflow=True, timeout=5.0, project_id="octascene",
    )
    if role == "diff-review":
        (project / "src/app.py").write_text("import util\n# edit\n")
        _git(project, "add", "-A")
        kwargs["include_diff"] = True
    return orchestrate.run_delegation(**kwargs)


def _graph_section(command: list[str]) -> str:
    joined = "\n".join(command)
    start = joined.index("--- ADVISORY GRAPH CONTEXT")
    return joined[start : joined.index("--- TASK ENVELOPE", start)]


@pytest.mark.parametrize(("worker", "role", "model"), CASES)
def test_every_transport_receives_the_same_generic_graph_context(project, graphify, worker, role, model):
    result = _dry_run(project, worker, role, model)
    assert result.record.result == "DRY_RUN"
    evidence = result.manifest["policy_manifest"]["graph_context"]
    assert evidence["status"] in {"used", "refreshed"} and evidence["injected"] is True
    assert evidence["authoritative"] is False and evidence["llm_enrichment"] is False and evidence["api_billing"] is False
    section = _graph_section(result.record.requested_command)
    assert "src/util.py" in section
    if model:
        assert any(model in arg for arg in result.record.requested_command)


def test_graph_section_is_identical_across_transports(project, graphify):
    sections = {
        _graph_section(_dry_run(project, w, r, m).record.requested_command)
        for w, r, m in CASES
        if r != "diff-review"
    }
    assert len(sections) == 1


def test_graphify_absent_leaves_delegation_unchanged(project, monkeypatch, tmp_path):
    monkeypatch.delenv("OCTAREL_GRAPHIFY_BIN", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    result = _dry_run(project, "claude-code", "primary-implementation", None)
    assert result.record.result == "DRY_RUN"
    assert result.manifest["policy_manifest"]["graph_context"]["status"] == "unavailable"
    assert "ADVISORY GRAPH CONTEXT" not in "\n".join(result.record.requested_command)


def test_run_session_receives_graph_context_from_changed_files(project, graphify):
    (project / "src/app.py").write_text("import util\n# wip\n")
    result = orchestrate.run_session(
        registry=load_registry(), root=project, task="ENG-AO-01", worker_name="claude-code",
        role="primary-implementation", model=None, intensity="low", why="test", prompt="finish", dry_run=True,
        timeout=5.0,
    )
    assert result.manifest["policy_manifest"]["graph_context"]["status"] in {"used", "refreshed"}
    assert "src/util.py" in _graph_section(["\n".join(result.record.requested_command) + "\n--- TASK ENVELOPE"])


def test_graph_context_never_changes_policy_identity_or_rewrites_policy_files(project, graphify):
    (project / "AGENTS.md").write_text("# managed project policy\n")
    (project / "CLAUDE.md").write_text("# managed bootstrap\n")
    _git(project, "add", "-A")
    _git(project, "commit", "-q", "-m", "policy")
    before = {p: (project / p).read_bytes() for p in ("AGENTS.md", "CLAUDE.md")}
    with_graph = _dry_run(project, "claude-code", "primary-implementation", None).manifest["policy_manifest"]
    assert {p: (project / p).read_bytes() for p in before} == before
    assert not (project / ".opencode").exists() and not (project / ".claude").exists()
    os.environ["OCTAREL_GRAPHIFY"] = "off"
    try:
        without = _dry_run(project, "claude-code", "primary-implementation", None).manifest["policy_manifest"]
    finally:
        del os.environ["OCTAREL_GRAPHIFY"]
    validate_policy_preservation(without, with_graph)  # identical role/workflow/core/task identity
    assert with_graph["preserved_policy_identity"] == without["preserved_policy_identity"]


def test_exact_tree_gate_never_consumes_graph_data():
    gate = Path(__file__).resolve().parents[1] / "scripts" / "ci"
    for source in gate.rglob("*.py"):
        assert "graph_context" not in source.read_text(encoding="utf-8"), source
    assert build_graph_context.__module__ == "scripts.agents.graph_context"
    evidence = graph_context._result(graph_context.STATUS_USED, "x", text="t").evidence
    assert evidence["authoritative"] is False
