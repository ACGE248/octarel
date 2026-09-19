"""ENG-CP-07 (issue #175): public-safety scan of the current tree and history."""

from __future__ import annotations

from pathlib import Path

from scripts.ci.public_safety import scan_repository
from tests.octarel_paths import OCTAREL_ROOT


def test_current_tree_has_no_publication_secrets() -> None:
    report = scan_repository(OCTAREL_ROOT, history=False)
    assert report.tree_ok, report.as_text()
    assert report.scanned_files > 50


def test_history_detects_known_pre_public_blockers() -> None:
    report = scan_repository(OCTAREL_ROOT, history=True)
    categories = {item.category for item in report.findings}
    # Private merge-commit email and/or historical operator path.
    assert "history-email" in categories or "history-path" in categories
    assert report.tree_ok
    assert not report.history_ok
    assert not report.ok


def test_scanner_flags_a_private_email(tmp_path: Path) -> None:
    from scripts.ci.public_safety import scan_file_text

    leaked = "contact " + "@".join(["owner", "gmail.com"]) + " please\n"
    findings = scan_file_text("docs/leak.md", leaked)
    assert any(item.category == "private-email" for item in findings)
    allowed = scan_file_text("docs/ok.md", "author octarel@example.com\n")
    assert allowed == []
