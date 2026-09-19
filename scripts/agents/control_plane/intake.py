"""Stable task-intake collision checks for managed dispatch (ENG-AGENT-10).

The SQLite claim is the durable serialization point. Maintained current docs
provide an additional fail-closed check for an issue-number collision; no
historical document is rewritten or treated as a mutable claim.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ..validation import validate_task_id
from .state import State

_ISSUE_RE = re.compile(r"(?:issue\s*:?\s*#|github\.com/[^\s)]+/issues/)(\d+)", re.I)


class IntakeCollision(ValueError):
    pass


@dataclass(frozen=True)
class IntakeResult:
    stable_task_id: str
    owner_ref: str
    source: str


def stable_task_id(value: str) -> str:
    candidate = str(value or "").strip().split(maxsplit=1)[0].rstrip(":;,.()")
    return validate_task_id(candidate)


def normalize_owner(owner_ref: str | None, stable_id: str) -> str:
    value = str(owner_ref or "").strip()
    if not value:
        return f"task:{stable_id}"
    match = _ISSUE_RE.search(value)
    return f"issue:#{match.group(1)}" if match else value


def _maintained_issue_owners(repo_root: Path, stable_id: str) -> list[tuple[str, str]]:
    """Return issue owners near ``stable_id`` in maintained current docs only."""

    candidates = [repo_root / "docs" / "PRODUCT_ROADMAP.md", repo_root / "docs" / "engineering" / f"{stable_id}.md"]
    candidates.extend(sorted((repo_root / "docs").glob("*/IMPLEMENTATION_STATUS*.md")))
    found: set[tuple[str, str]] = set()
    token = re.compile(rf"(?<![A-Za-z0-9-]){re.escape(stable_id)}(?![A-Za-z0-9-])")
    for path in candidates:
        if not path.is_file():
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            if not token.search(line):
                continue
            if path.name == f"{stable_id}.md":
                window = "\n".join(lines)
            elif path.name == "PRODUCT_ROADMAP.md":
                start = index
                while start > 0 and not lines[start].startswith("### "):
                    start -= 1
                end = index + 1
                while end < len(lines) and not lines[end].startswith("### "):
                    end += 1
                window = "\n".join(lines[start:end])
            else:
                # Maintained ledgers own tasks row-by-row; an issue reference
                # on another row belongs to another task.
                window = line
            for issue in _ISSUE_RE.findall(window):
                found.add((f"issue:#{issue}", str(path.relative_to(repo_root))))
    return sorted(found)


def check_and_claim(
    *, state: State, repo_root: Path, task_ref: str, owner_ref: str | None, source: str,
    project_id: str | None = None,
) -> IntakeResult:
    stable_id = stable_task_id(task_ref)
    owner = normalize_owner(owner_ref, stable_id)

    if owner.startswith("issue:#"):
        conflicting_docs = [item for item in _maintained_issue_owners(repo_root, stable_id) if item[0] != owner]
        if conflicting_docs:
            details = ", ".join(f"{claim} in {path}" for claim, path in conflicting_docs)
            raise IntakeCollision(
                f"stable task id {stable_id} is already owned by different maintained issue evidence: {details}"
            )

    claim = state.claim_task_identity(
        stable_task_id=stable_id, owner_ref=owner, source=source, project_id=project_id
    )
    existing_owner = str(claim.get("owner_ref") or "")
    if existing_owner != owner:
        raise IntakeCollision(
            f"stable task id {stable_id} is already owned by {existing_owner} "
            f"from {claim.get('source')}; requested owner {owner} is blocked"
        )
    return IntakeResult(stable_task_id=stable_id, owner_ref=owner, source=source)
