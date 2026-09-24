"""ENG-AO-01: optional, provider-neutral Graphify context for AO workers."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.agents import graph_context, orchestrate
from scripts.agents.graph_context import build_graph_context
from scripts.agents.policy import validate_policy_preservation
from scripts.agents.registry import load_registry

FAKE_GRAPHIFY = """#!{python}
import json, os, sys
from pathlib import Path
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
    assert subprocess.run(["git", "-C", str(project), "status", "--porcelain"], capture_output=True, text=True).stdout == ""


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
