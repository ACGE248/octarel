"""ENG-CP-04 (issue #169): generic task-source adapters.

Scheduler, orchestrator, and dashboard code discover the next eligible task
through this module, never by parsing an OctaScene Video Editor ledger
directly. The selected :class:`~.project.ProjectContract` names the adapter
(``capabilities["task_source_adapter"]``) and the repository-relative
``task_sources`` / ``github_remote`` the adapter reads.

Adapters supported here:

- ``file_ledger`` (alias ``markdown_checklist``) — markdown tables and
  checklists in the selected project's declared task-source files.
- ``github_issues`` — open GitHub issues for the selected project's
  ``github_remote``. Tests inject a fetcher; production uses ``gh``.
- ``video_editor_ledger`` — OctaScene compatibility. Delegates to
  :func:`quickstart.next_eligible_video_editor_task` so current next-eligible
  behavior stays equivalent.
- ``octascene_program_ledgers`` — OctaScene's maintained Sketch, Motion,
  Creator Beta, and Video Editor ledgers as one ordered repository-owned task
  source.  Human-readable status phrases are normalized without copying their
  product truth into Control Plane state.

Nothing here copies ledger/issue content into Control Plane state. Every
call re-reads the selected project's checkout (never process ``cwd``, never
the Control Plane code checkout).
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from .project import ProjectContract

ADAPTER_FILE_LEDGER = "file_ledger"
ADAPTER_MARKDOWN_CHECKLIST = "markdown_checklist"
ADAPTER_GITHUB_ISSUES = "github_issues"
ADAPTER_VIDEO_EDITOR_LEDGER = "video_editor_ledger"
ADAPTER_OCTASCENE_PROGRAM_LEDGERS = "octascene_program_ledgers"

ELIGIBLE_STATUSES = frozenset({"pending", "open", "todo"})

_TABLE_ROW_RE = re.compile(r"^\|\s*([A-Za-z0-9_.-]+)\s*\|\s*([A-Za-z0-9._-]+)\s*\|\s*(.*?)\s*\|$")
_CHECKLIST_RE = re.compile(r"^[-*]\s+\[([ xX])\]\s+(\S+)(?:\s+(.*))?$")
_PROGRAM_TASK_RE = re.compile(r"`?((?:SKETCH|MOTION|BETA)-[A-Z0-9-]+)`?", re.IGNORECASE)
_PROGRAM_DEPENDENCY_RE = re.compile(
    r"\b(?:after|depends(?:\s+on)?|requires|blocked\s+by)\s+`?((?:SKETCH|MOTION|BETA)-[A-Z0-9-]+)`?",
    re.IGNORECASE,
)
_PROGRAM_RANGE_RE = re.compile(
    r"`?((SKETCH|MOTION)-(\d+))`?\s*[–-]\s*`?(?:(?:SKETCH|MOTION)-)?(\d+)`?",
    re.IGNORECASE,
)
_PROGRAM_MERGED_RANGE_RE = re.compile(
    r"\b(Sketch|Motion)\s+`?((SKETCH|MOTION)-(\d+))`?\s*[–-]\s*`?(?:(?:SKETCH|MOTION)-)?(\d+)`?\s+merged\b",
    re.IGNORECASE,
)


class TaskSourceError(ValueError):
    """Task discovery failed closed for the selected project."""


@dataclass(frozen=True)
class DiscoveredTask:
    """One task observed in a selected project's declared task source."""

    task_id: str
    status: str
    title: str
    notes: str = ""
    source: str = ADAPTER_FILE_LEDGER
    source_ref: str = ""
    issue_number: int | None = None

    @property
    def eligible(self) -> bool:
        return self.status.lower() in ELIGIBLE_STATUSES

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "title": self.title,
            "notes": self.notes,
            "source": self.source,
            "source_ref": self.source_ref,
            "issue_number": self.issue_number,
            "eligible": self.eligible,
        }


GithubIssueFetcher = Callable[[ProjectContract], list[dict[str, Any]]]


class TaskSourceAdapter(Protocol):
    name: str

    def discover(self, project: ProjectContract) -> list[DiscoveredTask]:
        ...


def task_source_adapter_name(project: ProjectContract) -> str:
    """The adapter the selected project declared, with generic (not OctaScene) fallbacks.

    Missing capability is not inferred from an OctaScene ledger path. A project
    with declared ``task_sources`` uses the file/ledger adapter; a project with
    only a GitHub remote uses GitHub Issues. Anything else fails clearly.
    """

    named = (project.capabilities.get("task_source_adapter") or "").strip()
    if named:
        if named == ADAPTER_MARKDOWN_CHECKLIST:
            return ADAPTER_FILE_LEDGER
        return named
    if project.task_sources:
        return ADAPTER_FILE_LEDGER
    if project.github_remote:
        return ADAPTER_GITHUB_ISSUES
    raise TaskSourceError(
        f"project {project.project_id!r} declares no task_source_adapter, "
        "no task_sources, and no github_remote"
    )


def _require_project_root(project: ProjectContract) -> Path:
    root = project.local_repo_root
    if not root.exists() or not root.is_dir():
        raise TaskSourceError(
            f"project {project.project_id!r} local_repo_root does not exist or is not a directory: {root!r}"
        )
    return root


def parse_file_ledger(text: str, *, source_ref: str) -> list[DiscoveredTask]:
    """Parse markdown tables and checklists, in document order.

    Table rows follow ``| ID | status | notes |``. Checklist rows follow
    ``- [ ] ID title`` (unchecked → ``pending``) and ``- [x] ID title``
    (checked → ``complete``). Prose, headings, and separator rows are skipped.
    """

    tasks: list[DiscoveredTask] = []
    seen: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        table = _TABLE_ROW_RE.match(stripped)
        if table:
            task_id, status, notes = table.groups()
            if not re.search(r"[A-Za-z]", task_id) or task_id == "ID":
                continue
            if task_id in seen:
                continue
            seen.add(task_id)
            tasks.append(
                DiscoveredTask(
                    task_id=task_id,
                    status=status,
                    title=notes.rstrip(".") or task_id,
                    notes=notes,
                    source=ADAPTER_FILE_LEDGER,
                    source_ref=source_ref,
                )
            )
            continue
        check = _CHECKLIST_RE.match(stripped)
        if not check:
            continue
        mark, task_id, rest = check.groups()
        if task_id in seen:
            continue
        seen.add(task_id)
        title = (rest or "").strip() or task_id
        status = "complete" if mark.lower() == "x" else "pending"
        tasks.append(
            DiscoveredTask(
                task_id=task_id,
                status=status,
                title=title,
                notes=title,
                source=ADAPTER_FILE_LEDGER,
                source_ref=source_ref,
            )
        )
    return tasks


class FileLedgerAdapter:
    name = ADAPTER_FILE_LEDGER

    def discover(self, project: ProjectContract) -> list[DiscoveredTask]:
        root = _require_project_root(project)
        if not project.task_sources:
            raise TaskSourceError(
                f"project {project.project_id!r} file_ledger adapter requires declared task_sources"
            )
        discovered: list[DiscoveredTask] = []
        seen: set[str] = set()
        missing: list[str] = []
        for relative in project.task_sources:
            path = root / relative
            if not path.is_file():
                missing.append(relative)
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as exc:
                raise TaskSourceError(
                    f"project {project.project_id!r} could not read task source {relative}: {exc}"
                ) from exc
            for task in parse_file_ledger(text, source_ref=relative):
                if task.task_id in seen:
                    continue
                seen.add(task.task_id)
                discovered.append(task)
        if missing and not discovered:
            raise TaskSourceError(
                f"project {project.project_id!r} declared task_sources are missing: {', '.join(missing)}"
            )
        return discovered


def _default_github_issue_fetcher(project: ProjectContract) -> list[dict[str, Any]]:
    if not project.github_remote:
        raise TaskSourceError(
            f"project {project.project_id!r} github_issues adapter requires github_remote"
        )
    argv = [
        "gh",
        "issue",
        "list",
        "--repo",
        project.github_remote,
        "--state",
        "open",
        "--limit",
        "100",
        "--json",
        "number,title,state,labels,body",
    ]
    label = (project.capabilities.get("github_issue_label") or "").strip()
    if label:
        argv.extend(["--label", label])
    try:
        result = subprocess.run(
            argv,
            cwd=str(project.local_repo_root),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise TaskSourceError(
            f"project {project.project_id!r} could not list GitHub issues for {project.github_remote}: {exc}"
        ) from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "gh issue list failed").strip()[:400]
        raise TaskSourceError(
            f"project {project.project_id!r} GitHub issue list failed for {project.github_remote}: {detail}"
        )
    try:
        payload = json.loads(result.stdout or "[]")
    except ValueError as exc:
        raise TaskSourceError(
            f"project {project.project_id!r} GitHub issue list returned invalid JSON"
        ) from exc
    if not isinstance(payload, list):
        raise TaskSourceError(
            f"project {project.project_id!r} GitHub issue list returned a non-list payload"
        )
    return payload


class GitHubIssuesAdapter:
    name = ADAPTER_GITHUB_ISSUES

    def __init__(self, fetcher: GithubIssueFetcher | None = None) -> None:
        self._fetcher = fetcher or _default_github_issue_fetcher

    def discover(self, project: ProjectContract) -> list[DiscoveredTask]:
        _require_project_root(project)
        if not project.github_remote:
            raise TaskSourceError(
                f"project {project.project_id!r} github_issues adapter requires github_remote"
            )
        issues = self._fetcher(project)
        tasks: list[DiscoveredTask] = []
        for raw in issues:
            number = raw.get("number")
            title = str(raw.get("title") or "").strip()
            state = str(raw.get("state") or "open").strip().lower()
            if number is None or not title:
                continue
            task_id = f"#{int(number)}"
            body = str(raw.get("body") or "").strip()
            tasks.append(
                DiscoveredTask(
                    task_id=task_id,
                    status="pending" if state == "open" else state,
                    title=title,
                    notes=body,
                    source=ADAPTER_GITHUB_ISSUES,
                    source_ref=f"{project.github_remote}#{int(number)}",
                    issue_number=int(number),
                )
            )
        return tasks


class VideoEditorLedgerAdapter:
    """OctaScene compatibility adapter. Parses only through quickstart.py."""

    name = ADAPTER_VIDEO_EDITOR_LEDGER

    def discover(self, project: ProjectContract) -> list[DiscoveredTask]:
        root = _require_project_root(project)
        from .quickstart import (
            VIDEO_EDITOR_LEDGER_RELATIVE,
            next_eligible_video_editor_task,
            parse_video_editor_ledger,
        )

        ledger_path = root / VIDEO_EDITOR_LEDGER_RELATIVE
        try:
            text = ledger_path.read_text(encoding="utf-8")
        except OSError:
            return []
        source_ref = str(VIDEO_EDITOR_LEDGER_RELATIVE)
        tasks = [
            DiscoveredTask(
                task_id=item.task_id,
                status=item.status,
                title=item.notes.rstrip(".") or item.task_id,
                notes=item.notes,
                source=ADAPTER_VIDEO_EDITOR_LEDGER,
                source_ref=source_ref,
            )
            for item in parse_video_editor_ledger(text)
        ]
        # Defensive: next-eligible must match the dedicated helper byte-for-byte.
        expected = next_eligible_video_editor_task(root)
        actual = next((task for task in tasks if task.status == "pending"), None)
        if (expected is None) != (actual is None):
            raise TaskSourceError("OctaScene video-editor ledger adapter diverged from next_eligible_video_editor_task")
        if expected is not None and actual is not None and expected.task_id != actual.task_id:
            raise TaskSourceError(
                "OctaScene video-editor ledger adapter next-eligible "
                f"{actual.task_id!r} != {expected.task_id!r}"
            )
        return tasks


def _markdown_cells(line: str) -> list[str]:
    stripped = line.strip()
    if not (stripped.startswith("|") and stripped.endswith("|")):
        return []
    return [cell.strip() for cell in stripped[1:-1].split("|")]


def _program_status(raw: str) -> str:
    """Normalize maintained OctaScene ledger prose to scheduler states.

    The full original row remains in ``notes``.  Unknown prose is deliberately
    non-eligible rather than guessed as pending.
    """

    value = re.sub(r"[`*_]", "", raw).strip().lower()
    if any(token in value for token in ("owner decision", "owner-decision", "needs owner")):
        return "owner-decision"
    if any(token in value for token in ("candidate", "active", "undergoing", "in progress")):
        return "in-progress"
    if "pending" in value or "not started" in value:
        return "pending"
    if any(token in value for token in ("blocked", "depends on", "waits on", "last;")):
        return "blocked"
    if "not accepted" not in value and any(
        token in value for token in ("merged", "accepted", "complete", "closed", "superseded")
    ):
        return "merged"
    if "audit written" in value:
        return "accepted"
    return "unknown"


def _program_dependencies(raw: str) -> tuple[str, ...]:
    found: list[str] = []
    for match in _PROGRAM_DEPENDENCY_RE.finditer(raw):
        task_id = match.group(1).upper()
        if task_id not in found:
            found.append(task_id)
    return tuple(found)


def _program_task_ids(raw: str) -> tuple[str, ...]:
    range_match = _PROGRAM_RANGE_RE.search(raw)
    if range_match:
        prefix = range_match.group(2).upper()
        start = int(range_match.group(3))
        end = int(range_match.group(4))
        if start <= end and end - start <= 50:
            return tuple(f"{prefix}-{number:02d}" for number in range(start, end + 1))
    match = _PROGRAM_TASK_RE.search(raw)
    return (match.group(1).upper(),) if match else ()


def _program_sequence_dependency(task_id: str) -> str | None:
    numbered = re.fullmatch(r"(SKETCH|MOTION)-(\d+)", task_id)
    if numbered and int(numbered.group(2)) > 0:
        return f"{numbered.group(1)}-{int(numbered.group(2)) - 1:02d}"
    gate = re.fullmatch(r"(SKETCH|MOTION)-GATE-V2", task_id)
    if gate:
        return f"{gate.group(1)}-08"
    return None


def parse_octascene_program_ledger(text: str, *, source_ref: str) -> list[DiscoveredTask]:
    """Parse maintained Sketch/Motion/Beta task tables without inventing truth.

    Supported tables use either ``Task | Status`` or
    ``Reference | ... | Readiness``.  Rows without a stable task ID or a
    recognized status remain invisible/non-startable instead of being guessed.
    """

    tasks: list[DiscoveredTask] = []
    seen: set[str] = set()
    for match in _PROGRAM_MERGED_RANGE_RE.finditer(text):
        prefix = match.group(3).upper()
        start = int(match.group(4))
        end = int(match.group(5))
        if start > end or end - start > 50:
            continue
        evidence = match.group(0)
        for number in range(start, end + 1):
            task_id = f"{prefix}-{number:02d}"
            if task_id in seen:
                continue
            seen.add(task_id)
            tasks.append(
                DiscoveredTask(
                    task_id=task_id,
                    status="merged",
                    title=task_id,
                    notes=evidence,
                    source=ADAPTER_OCTASCENE_PROGRAM_LEDGERS,
                    source_ref=source_ref,
                )
            )
    header: list[str] | None = None
    for line in text.splitlines():
        cells = _markdown_cells(line)
        if not cells:
            header = None
            continue
        lowered = [re.sub(r"[`*_]", "", cell).strip().lower() for cell in cells]
        if "task" in lowered or "reference" in lowered:
            header = lowered
            continue
        if header is None or all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
            continue
        id_index = header.index("task") if "task" in header else header.index("reference")
        status_index = header.index("status") if "status" in header else header.index("readiness") if "readiness" in header else -1
        if id_index >= len(cells) or status_index < 0 or status_index >= len(cells):
            continue
        task_ids = _program_task_ids(cells[id_index])
        if not task_ids:
            continue
        status_text = cells[status_index]
        status = _program_status(status_text)
        title = re.sub(r"`", "", cells[id_index]).strip()
        row_notes = " | ".join(cell for cell in cells if cell).strip()
        declared_deps = list(_program_dependencies(status_text))
        for task_id in task_ids:
            if task_id in seen:
                continue
            seen.add(task_id)
            deps = list(declared_deps)
            sequence_dep = _program_sequence_dependency(task_id)
            if status in ELIGIBLE_STATUSES | {"blocked"} and sequence_dep and sequence_dep not in deps:
                deps.append(sequence_dep)
            notes = f"{row_notes}; depends on {', '.join(deps)}" if deps else row_notes
            tasks.append(
                DiscoveredTask(
                    task_id=task_id,
                    status=status,
                    title=title or task_id,
                    notes=notes,
                    source=ADAPTER_OCTASCENE_PROGRAM_LEDGERS,
                    source_ref=source_ref,
                )
            )
    return tasks


class OctaSceneProgramLedgersAdapter:
    """Combine OctaScene's declared program ledgers in repository order."""

    name = ADAPTER_OCTASCENE_PROGRAM_LEDGERS

    def discover(self, project: ProjectContract) -> list[DiscoveredTask]:
        root = _require_project_root(project)
        if not project.task_sources:
            raise TaskSourceError(
                f"project {project.project_id!r} octascene_program_ledgers adapter requires declared task_sources"
            )
        discovered: list[DiscoveredTask] = []
        seen: set[str] = set()
        missing: list[str] = []
        for relative in project.task_sources:
            path = root / relative
            if not path.is_file():
                missing.append(relative)
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as exc:
                raise TaskSourceError(
                    f"project {project.project_id!r} could not read task source {relative}: {exc}"
                ) from exc
            if relative.endswith("video-editor/IMPLEMENTATION_STATUS_V2.md"):
                tasks = VideoEditorLedgerAdapter().discover(project)
            else:
                tasks = parse_octascene_program_ledger(text, source_ref=relative)
            for task in tasks:
                if task.task_id in seen:
                    continue
                seen.add(task.task_id)
                discovered.append(task)
        if missing:
            raise TaskSourceError(
                f"project {project.project_id!r} declared task_sources are missing: {', '.join(missing)}"
            )
        return discovered


_FILE_LEDGER = FileLedgerAdapter()
_GITHUB_ISSUES = GitHubIssuesAdapter()
_VIDEO_EDITOR = VideoEditorLedgerAdapter()
_OCTASCENE_PROGRAMS = OctaSceneProgramLedgersAdapter()


def adapter_for(
    project: ProjectContract,
    *,
    github_issue_fetcher: GithubIssueFetcher | None = None,
) -> TaskSourceAdapter:
    name = task_source_adapter_name(project)
    if name == ADAPTER_FILE_LEDGER:
        return _FILE_LEDGER
    if name == ADAPTER_GITHUB_ISSUES:
        return GitHubIssuesAdapter(github_issue_fetcher) if github_issue_fetcher is not None else _GITHUB_ISSUES
    if name == ADAPTER_VIDEO_EDITOR_LEDGER:
        return _VIDEO_EDITOR
    if name == ADAPTER_OCTASCENE_PROGRAM_LEDGERS:
        return _OCTASCENE_PROGRAMS
    raise TaskSourceError(
        f"project {project.project_id!r} declares unknown task_source_adapter {name!r}"
    )


def discover_tasks(
    project: ProjectContract,
    *,
    github_issue_fetcher: GithubIssueFetcher | None = None,
) -> list[DiscoveredTask]:
    """Every task currently visible in the selected project's declared source."""

    return adapter_for(project, github_issue_fetcher=github_issue_fetcher).discover(project)


def next_eligible_task(
    project: ProjectContract,
    *,
    github_issue_fetcher: GithubIssueFetcher | None = None,
) -> DiscoveredTask | None:
    """First eligible task in source document / listing order, or ``None``.

    ``None`` means the selected project currently has no startable task —
    callers must show that truthfully rather than guessing or reading another
    project's ledger.
    """

    for task in discover_tasks(project, github_issue_fetcher=github_issue_fetcher):
        if task.eligible:
            return task
    return None
