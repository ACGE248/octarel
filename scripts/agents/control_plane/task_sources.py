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

ELIGIBLE_STATUSES = frozenset({"pending", "open", "todo"})

_TABLE_ROW_RE = re.compile(r"^\|\s*([A-Za-z0-9_.-]+)\s*\|\s*([A-Za-z0-9._-]+)\s*\|\s*(.*?)\s*\|$")
_CHECKLIST_RE = re.compile(r"^[-*]\s+\[([ xX])\]\s+(\S+)(?:\s+(.*))?$")


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


_FILE_LEDGER = FileLedgerAdapter()
_GITHUB_ISSUES = GitHubIssuesAdapter()
_VIDEO_EDITOR = VideoEditorLedgerAdapter()


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
