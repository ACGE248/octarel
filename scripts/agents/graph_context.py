"""Optional, provider-neutral Graphify repository-intelligence context (ENG-AO-01).

Graphify is *derived* code intelligence only.  Truth precedence is always:

1. the current managed-project source tree;
2. managed-project AGENTS/policy and maintained documentation;
3. current task/program/ADR contracts;
4. Graphify-derived context (this module);
5. agent inference.

This module produces one bounded, redacted, advisory text section that the AO prompt
builders append after the composed policy bundle.  Because the section is plain prompt
text, every transport (Claude Code, Codex, Gemini or any other model through OpenCode
or Antigravity, native Grok, Grok/xAI through OpenCode) receives byte-identical context;
there is deliberately no per-provider implementation.

Safety properties:

* Graphify is never installed, and never given API credentials (provider key variables are
  scrubbed from its environment) -- only the local, deterministic AST ``update`` verb is run.
* Graphify runs on a *snapshot* of the selected checkout's non-sensitive files kept under
  Octarel state, so it never sees secret paths and never writes into the managed repository.
* A cached graph is used only when project, repository, worktree and content-tree identity
  all match; otherwise it is refreshed deterministically or skipped with a recorded reason.
* Graph nodes naming files absent from the current tree, or sensitive paths, are dropped.
* Every failure is contained: :func:`build_graph_context` never raises.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

from .redaction import redact_text
from .validation import _FORBIDDEN_NAMES, _FORBIDDEN_PARTS, _SECRET_MARKERS

CACHE_SCHEMA = 1
GRAPH_DIRNAME = "graph-context"

STATUS_USED = "used"
STATUS_REFRESHED = "refreshed"
STATUS_UNAVAILABLE = "unavailable"
STATUS_STALE = "stale"
STATUS_SKIPPED = "skipped"
STATUS_FAILED_SAFE = "failed-safe"
_INJECTED = frozenset({STATUS_USED, STATUS_REFRESHED})

ENV_MODE = "OCTAREL_GRAPHIFY"  # "off" disables; anything else is auto-detect
ENV_BIN = "OCTAREL_GRAPHIFY_BIN"

MAX_FILES = 20_000
MAX_FILE_BYTES = 1_000_000
MAX_SEED_FILES = 25
MAX_ITEMS = 12
MAX_CONTEXT_CHARS = 6_000
BUILD_TIMEOUT_SECONDS = 120.0
KEEP_TREES_PER_WORKTREE = 2

_SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".keystore", ".jks", ".kdbx", ".db", ".sqlite", ".sqlite3")
# Provider credentials must never reach Graphify: it then has no paid/LLM path to take.
_SCRUBBED_ENV_MARKERS = ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
_SCRUBBED_ENV_NAMES = frozenset({"ANTHROPIC_AUTH_TOKEN", "GOOGLE_APPLICATION_CREDENTIALS"})

# ENG-AO-02: per-bot scoped slices of the same graph.  ``None`` keeps the unfocused, full section.
FOCUS_DEPENDENCY = "dependency"
FOCUS_TESTS = "tests"
FOCUS_IMPACT = "impact"
FOCUSES = (FOCUS_DEPENDENCY, FOCUS_TESTS, FOCUS_IMPACT)
_IMPACT_DIRS = frozenset({"docs", "doc", "frontend", "ui", "web", "api", "dashboard", "static", "templates", "routes"})
_IMPACT_STEMS = frozenset({"api", "routes", "dashboard"})
_IMPACT_SUFFIXES = (".md", ".html", ".css", ".tsx", ".jsx", ".vue", ".svelte")
_LABELLED_PATH = re.compile(r"\(([^()]+)\)")

PRECEDENCE_NOTE = (
    "Advisory only. Truth precedence: 1) current source tree, 2) project AGENTS/policy and maintained docs, "
    "3) task/program/ADR contracts, 4) this graph context, 5) agent inference. "
    "Verify against the actual files before relying on anything below."
)


@dataclass(frozen=True)
class GraphContext:
    """Prompt text (empty unless graph context was used) plus the evidence record."""

    text: str
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def injected(self) -> bool:
        return bool(self.text)


def is_sensitive_path(relative: str) -> bool:
    """Existing scope/secret rules applied to a repository-relative path."""

    parts = PurePosixPath(relative.replace("\\", "/")).parts
    if not parts:
        return True
    lowered_parts = [part.lower() for part in parts]
    if any(part in _FORBIDDEN_PARTS or part in _FORBIDDEN_NAMES for part in parts):
        return True
    if any(part.startswith(".env") for part in lowered_parts) and not lowered_parts[-1].endswith(".example"):
        return True
    lowered = "/".join(lowered_parts)
    if any(marker in lowered for marker in _SECRET_MARKERS):
        return True
    return lowered.endswith(_SECRET_SUFFIXES) or lowered_parts[-1].startswith("id_rsa")


def state_root() -> Path:
    """Octarel-owned state root; never inside a managed project unless it is ignored state."""

    env_dir = os.environ.get("OCTAREL_STATE_DIR")
    if env_dir:
        path = Path(env_dir)
        base = path.parent if path.is_file() else path
    elif os.environ.get("OCTAREL_CODE_ROOT"):
        base = Path(os.environ["OCTAREL_CODE_ROOT"]) / ".orchestrator-state"
    else:
        base = Path(__file__).resolve().parents[2] / ".orchestrator-state"
    return base


def graph_state_root() -> Path:
    return state_root() / GRAPH_DIRNAME


def _result(status: str, reason: str, *, text: str = "", **extra: Any) -> GraphContext:
    evidence: dict[str, Any] = {
        "status": status,
        "reason": reason,
        "source": "graphify",
        "authoritative": False,
        "llm_enrichment": False,
        "api_billing": False,
        "precedence": "advisory-below-source-tree-policy-and-task-contracts",
        "injected": status in _INJECTED and bool(text),
    }
    evidence.update(extra)
    return GraphContext(text=text, evidence=evidence)


def _sha(*parts: str) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")
    return digest.hexdigest()


def _git(root: Path, *args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, check=True, timeout=60).stdout


def _tracked_tree(root: Path) -> tuple[dict[str, str], int]:
    """``{relative path: content sha256}`` for non-sensitive regular files, plus skipped count."""

    listing = _git(root, "ls-files", "-co", "--exclude-standard", "-z")
    files: dict[str, str] = {}
    skipped = 0
    for raw in listing.split(b"\0"):
        if not raw:
            continue
        relative = os.fsdecode(raw)
        path = root / relative
        if is_sensitive_path(relative) or path.is_symlink() or not path.is_file():
            skipped += 1
            continue
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                skipped += 1
                continue
            files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            skipped += 1
            continue
        if len(files) > MAX_FILES:
            raise OverflowError(f"more than {MAX_FILES} files")
    return files, skipped


def _identity(root: Path, project_id: str | None) -> dict[str, str]:
    common = _git(root, "rev-parse", "--path-format=absolute", "--git-common-dir").decode().strip()
    repo_id = _sha("repo", os.path.realpath(common))
    worktree_id = _sha("worktree", os.path.realpath(root))
    return {
        "project_key": _sha("project", project_id)[:16] if project_id else repo_id[:16],
        "project_id": project_id or "",
        "repo_id": repo_id,
        "worktree_id": worktree_id,
    }


def _scrubbed_env(base: Mapping[str, str]) -> dict[str, str]:
    env = {
        key: value
        for key, value in base.items()
        if key not in _SCRUBBED_ENV_NAMES and not any(marker in key.upper() for marker in _SCRUBBED_ENV_MARKERS)
    }
    env["GRAPHIFY_NO_LLM"] = "1"
    env["NO_COLOR"] = "1"
    return env


def _load_valid_cache(cache_dir: Path, expected: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    meta_path = cache_dir / "meta.json"
    graph_path = cache_dir / "snapshot" / "graphify-out" / "graph.json"
    if not meta_path.is_file() or not graph_path.is_file():
        return None, "no cached graph for this tree"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        graph = json.loads(graph_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, "cached graph unreadable"
    for key, value in expected.items():
        if meta.get(key) != value:
            return None, f"cached graph {key} does not match the selected project/checkout/tree"
    if not isinstance(graph, dict):
        return None, "cached graph malformed"
    return graph, ""


def _write_snapshot(root: Path, files: Iterable[str], destination: Path) -> None:
    for relative in files:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / relative, target)


def _build_graph(binary: str, root: Path, files: dict[str, str], cache_dir: Path, meta: dict[str, Any]) -> str:
    """Build into ``cache_dir``; returns ``""`` on success or a failure reason."""

    cache_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".build-", dir=cache_dir.parent))
    try:
        snapshot = staging / "snapshot"
        snapshot.mkdir()
        _write_snapshot(root, files, snapshot)
        completed = subprocess.run(
            [binary, "update", str(snapshot)],
            cwd=snapshot,
            capture_output=True,
            text=True,
            timeout=BUILD_TIMEOUT_SECONDS,
            env=_scrubbed_env(os.environ),
            stdin=subprocess.DEVNULL,
        )
        if completed.returncode != 0:
            return f"graphify exited {completed.returncode}"
        graph_path = snapshot / "graphify-out" / "graph.json"
        if not graph_path.is_file():
            return "graphify produced no graph.json"
        json.loads(graph_path.read_text(encoding="utf-8"))
        (staging / "meta.json").write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        if cache_dir.exists():
            shutil.rmtree(cache_dir)
        staging.rename(cache_dir)
        return ""
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return f"graphify build failed: {type(exc).__name__}"
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def _prune(worktree_dir: Path, keep: Path) -> None:
    trees = sorted((p for p in worktree_dir.iterdir() if p.is_dir() and not p.name.startswith(".")), key=lambda p: p.stat().st_mtime)
    for old in trees[: max(0, len(trees) - KEEP_TREES_PER_WORKTREE)]:
        if old != keep:
            shutil.rmtree(old, ignore_errors=True)


def _normalize_path(raw: Any, snapshot_paths: set[str]) -> str | None:
    if not isinstance(raw, str) or not raw:
        return None
    normalized = PurePosixPath(raw.replace("\\", "/")).as_posix()
    if normalized.startswith("./"):
        normalized = normalized[2:]
    # Only files that exist in the current, filtered tree count; anything else the graph names
    # (absolute paths, stale files, secret paths) is dropped because the source tree is authoritative.
    if normalized in snapshot_paths and not is_sensitive_path(normalized):
        return normalized
    return None


def _is_test_path(relative: str) -> bool:
    lowered = relative.lower()
    name = PurePosixPath(lowered).name
    return "/tests/" in "/" + lowered or name.startswith("test_") or ".test." in name or ".spec." in name or name.endswith("_test.py")


def _seed_files(seeds: Iterable[str], snapshot_paths: set[str]) -> list[str]:
    selected: set[str] = set()
    for seed in seeds:
        prefix = PurePosixPath(seed.replace("\\", "/")).as_posix().strip("/")
        if not prefix or prefix.startswith(".."):
            continue
        for path in snapshot_paths:
            if path == prefix or path.startswith(prefix + "/"):
                selected.add(path)
    return sorted(selected)[:MAX_SEED_FILES]


def _summarize(graph: dict[str, Any], seeds: list[str], snapshot_paths: set[str]) -> dict[str, Any]:
    nodes = [n for n in graph.get("nodes", []) if isinstance(n, dict)]
    edges = [e for e in (graph.get("links") or graph.get("edges") or []) if isinstance(e, dict)]
    node_file: dict[Any, str] = {}
    node_label: dict[Any, str] = {}
    for node in nodes:
        path = _normalize_path(node.get("source_file") or node.get("file") or node.get("path"), snapshot_paths)
        if path is not None:
            node_file[node.get("id")] = path
            node_label[node.get("id")] = str(node.get("label") or node.get("name") or node.get("id"))
    seed_set = set(seeds)
    symbols: dict[str, list[str]] = {}
    for node_id, path in node_file.items():
        label = node_label[node_id]
        if path in seed_set and label != PurePosixPath(path).name and label != path:
            symbols.setdefault(path, []).append(label)
    imports: set[str] = set()
    dependents: set[str] = set()
    calls: set[str] = set()
    called_by: set[str] = set()
    for edge in edges:
        src, dst = edge.get("source"), edge.get("target")
        if src not in node_file or dst not in node_file:
            continue
        relation = str(edge.get("relation") or edge.get("type") or "").lower()
        src_file, dst_file = node_file[src], node_file[dst]
        src_seed, dst_seed = src_file in seed_set, dst_file in seed_set
        if src_file == dst_file:
            continue
        if "import" in relation or "depend" in relation or "use" in relation:
            if src_seed:
                imports.add(dst_file)
            if dst_seed:
                dependents.add(src_file)
        elif "call" in relation:
            if src_seed:
                calls.add(f"{node_label[src]} -> {node_label[dst]} ({dst_file})")
            if dst_seed:
                called_by.add(f"{node_label[src]} ({src_file}) -> {node_label[dst]}")
    neighbors = (imports | dependents) - seed_set
    seed_stems = {PurePosixPath(p).stem.removeprefix("test_") for p in seeds}
    tests = {p for p in neighbors if _is_test_path(p)} | {
        p
        for p in snapshot_paths
        if _is_test_path(p) and p not in seed_set and any(stem and stem in PurePosixPath(p).stem for stem in seed_stems)
    }
    return {
        "symbols": {path: sorted(set(labels)) for path, labels in sorted(symbols.items())},
        "imports": sorted(imports - seed_set),
        "dependents": sorted(dependents - seed_set),
        "calls": sorted(calls),
        "called_by": sorted(called_by),
        "tests": sorted(tests),
        "graph_nodes": len(nodes),
        "graph_edges": len(edges),
    }


def is_impact_path(relative: str) -> bool:
    """UI, API, or documentation surface (used to scope the impact-focused bot slice).

    Matches whole path segments, never substrings, so ``src/capital.py`` is not an "api" file.
    """

    path = PurePosixPath(relative.replace("\\", "/").lower())
    return (
        path.suffix in _IMPACT_SUFFIXES
        or path.stem in _IMPACT_STEMS
        or any(part in _IMPACT_DIRS for part in path.parts[:-1])
    )


def _labelled_path_is_impact(item: str) -> bool:
    """``label (path) -> label`` caller/callee rows are matched on their file path, not the whole label."""

    return any(is_impact_path(path) for path in _LABELLED_PATH.findall(item))


def _focused_summary(summary: dict[str, Any], focus: str | None) -> dict[str, Any]:
    if focus is None:
        return summary
    empty: dict[str, Any] = {**summary, "symbols": {}, "imports": [], "dependents": [], "calls": [], "called_by": [], "tests": []}
    if focus == FOCUS_DEPENDENCY:
        keep = ("symbols", "imports", "dependents", "calls", "called_by")
        return {**empty, **{key: summary[key] for key in keep}}
    if focus == FOCUS_TESTS:
        return {**empty, "symbols": summary["symbols"], "tests": summary["tests"]}
    if focus == FOCUS_IMPACT:
        return {
            **empty,
            "dependents": [path for path in summary["dependents"] if is_impact_path(path)],
            "called_by": [item for item in summary["called_by"] if _labelled_path_is_impact(item)],
        }
    raise ValueError(f"unknown graph focus {focus!r}")


def _render(summary: dict[str, Any], seeds: list[str], focus: str | None = None) -> tuple[str, bool]:
    summary = _focused_summary(summary, focus)
    lines = ["Seed files: " + ", ".join(seeds)]
    if focus is not None:
        lines.insert(0, f"Scope focus: {focus} (a bounded slice; do not assume it is the whole picture)")
    truncated = False

    def add(title: str, items: list[str]) -> None:
        nonlocal truncated
        if not items:
            return
        if len(items) > MAX_ITEMS:
            truncated = True
        lines.append(f"{title}: " + "; ".join(items[:MAX_ITEMS]))

    for path, labels in list(summary["symbols"].items())[:MAX_ITEMS]:
        add(f"Symbols in {path}", labels)
    add("Imports / dependencies", summary["imports"])
    add("Dependents (blast radius)", summary["dependents"])
    add("Callees", summary["calls"])
    add("Callers", summary["called_by"])
    add("Likely affected tests", summary["tests"])
    body = redact_text("\n".join(lines))
    if len(body) > MAX_CONTEXT_CHARS:
        body, truncated = body[:MAX_CONTEXT_CHARS].rstrip() + "\n[truncated]", True
    return body, truncated


def build_graph_context(
    root: Path,
    seed_paths: Iterable[str],
    *,
    project_id: str | None = None,
    state_root: Path | None = None,
) -> GraphContext:
    """Return advisory graph context for the selected checkout ``root``; never raises."""

    return build_graph_contexts(root, seed_paths, (None,), project_id=project_id, state_root=state_root)[None]


def build_graph_contexts(
    root: Path,
    seed_paths: Iterable[str],
    focuses: Iterable[str | None],
    *,
    project_id: str | None = None,
    state_root: Path | None = None,
) -> dict[str | None, GraphContext]:
    """One graph preparation, one bounded advisory section per requested focus; never raises.

    ENG-AO-02 gives each read-only bot a small, differently-scoped slice of the same tree-matched
    graph instead of the broad section.  ``None`` is the ordinary full section.
    """

    wanted = list(dict.fromkeys(focuses))
    try:
        return _build(Path(root), list(seed_paths), project_id, state_root, wanted)
    except Exception as exc:  # noqa: BLE001 - graph context must never break a worker launch
        failed = _result(STATUS_FAILED_SAFE, f"graph context failed safely: {type(exc).__name__}")
        return {focus: failed for focus in wanted}


def _build(
    root: Path, seed_paths: list[str], project_id: str | None, state_root: Path | None, focuses: list[str | None]
) -> dict[str | None, GraphContext]:
    prepared = _prepare(root, seed_paths, project_id, state_root)
    if isinstance(prepared, GraphContext):
        return {focus: prepared for focus in focuses}
    graph, seeds, snapshot_paths, status, reason, base = prepared
    summary = _summarize(graph, seeds, snapshot_paths)
    results: dict[str | None, GraphContext] = {}
    for focus in focuses:
        body, truncated = _render(summary, seeds, focus)
        text = (
            "\n--- ADVISORY GRAPH CONTEXT (Graphify-derived; NOT authoritative) ---\n"
            f"{PRECEDENCE_NOTE}\n{body}\n"
        )
        extra = {"focus": focus} if focus is not None else {}
        results[focus] = _result(
            status,
            reason,
            text=text,
            seed_files=seeds,
            graph_nodes=summary["graph_nodes"],
            graph_edges=summary["graph_edges"],
            context_characters=len(text),
            truncated=truncated,
            **base,
            **extra,
        )
    return results


def _prepare(root: Path, seed_paths: list[str], project_id: str | None, state_root: Path | None):
    """A validated ``(graph, seeds, snapshot_paths, status, reason, base)`` or a terminal :class:`GraphContext`."""

    if os.environ.get(ENV_MODE, "").strip().lower() in {"0", "off", "false", "disabled"}:
        return _result(STATUS_SKIPPED, f"disabled by {ENV_MODE}")
    binary = os.environ.get(ENV_BIN) or shutil.which("graphify")
    if not binary or not (shutil.which(binary) or Path(binary).is_file()):
        return _result(STATUS_UNAVAILABLE, "graphify is not installed; ordinary repository inspection applies")
    if not seed_paths:
        return _result(STATUS_SKIPPED, "no task scope or changed files to seed graph context")
    try:
        identity = _identity(root, project_id)
        files, skipped = _tracked_tree(root)
    except OverflowError:
        return _result(STATUS_SKIPPED, f"repository exceeds the {MAX_FILES}-file graph bound")
    except (subprocess.SubprocessError, OSError):
        return _result(STATUS_SKIPPED, "selected path is not a readable Git checkout")

    tree_id = _sha(*(f"{path}:{digest}" for path, digest in sorted(files.items())))
    expected = {"schema": CACHE_SCHEMA, **identity, "tree_id": tree_id, "llm_enrichment": False}
    worktree_dir = (state_root or graph_state_root()) / identity["project_key"] / identity["worktree_id"][:16]
    cache_dir = worktree_dir / tree_id[:24]
    base = {
        "project_key": identity["project_key"],
        "worktree_id": identity["worktree_id"][:16],
        "tree_id": tree_id[:24],
        "files_indexed": len(files),
        "files_excluded": skipped,
        "graphify_bin": Path(binary).name,
    }

    graph, why = _load_valid_cache(cache_dir, expected)
    status, reason = STATUS_USED, "tree-matched cached graph"
    if graph is None:
        had_prior = worktree_dir.is_dir() and any(worktree_dir.iterdir())
        failure = _build_graph(binary, root, files, cache_dir, {**expected, "files": len(files)})
        if failure and _load_valid_cache(cache_dir, expected)[0] is not None:
            failure = ""  # a concurrent worker built the identical tree-matched graph first
        if failure:
            stale = STATUS_STALE if had_prior else STATUS_FAILED_SAFE
            return _result(stale, f"{why}; refresh failed ({failure}); falling back to repository inspection", **base)
        graph, why = _load_valid_cache(cache_dir, expected)
        if graph is None:
            return _result(STATUS_FAILED_SAFE, f"refreshed graph did not validate: {why}", **base)
        status = STATUS_REFRESHED if had_prior else STATUS_USED
        reason = "stale graph refreshed for the current tree" if had_prior else "graph built for the current tree"
        _prune(worktree_dir, cache_dir)

    snapshot_paths = set(files)
    seeds = _seed_files(seed_paths, snapshot_paths)
    if not seeds:
        return _result(STATUS_SKIPPED, "no indexed (non-sensitive) files within the task scope", **base)
    return graph, seeds, snapshot_paths, status, reason, base
