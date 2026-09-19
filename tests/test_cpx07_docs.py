"""ENG-CP-07 (issue #175): required public documentation is present."""

from __future__ import annotations

from tests.octarel_paths import OCTAREL_ROOT

REQUIRED = (
    "README.md",
    "LICENSE",
    "NOTICE",
    "THIRD_PARTY.md",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "docs/ARCHITECTURE.md",
    "docs/INSTALL.md",
    "docs/PROJECTS.md",
    "docs/PROVIDERS.md",
    "docs/WORKTREES.md",
    "docs/TESTING.md",
    "docs/REMOTE_ACCESS.md",
    "docs/STATE.md",
    "docs/RELEASE.md",
    "docs/PUBLICATION.md",
    "docs/engineering/ENG-CP-07.md",
)


def test_required_docs_exist_and_are_non_empty() -> None:
    for relative in REQUIRED:
        path = OCTAREL_ROOT / relative
        assert path.is_file(), relative
        assert path.read_text(encoding="utf-8").strip()
