#!/usr/bin/env python3
"""CPX-07 public-safety scanner for the Octarel tree and intended publication history.

Scans the current Git tree (and optionally every reachable commit) for secrets,
private emails, operator-specific filesystem paths, committed databases, and
Cloudflare/operator configuration that must not become public.

This module never prints secret *values*; findings name a path, a line number
or commit, and a category. Historical findings are publication blockers even
when the current tree is clean.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

from scripts.agents.redaction import _VALUE_PATTERNS

PRIVATE_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
ABSOLUTE_PATH_RE = re.compile(
    r"(?:/Users/[A-Za-z0-9._-]+|/home/[A-Za-z0-9._-]+|/Volumes/[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*)"
)
CF_ACCESS_AUD_RE = re.compile(r"\b[a-f0-9]{64}\b")

ALLOWED_EMAIL_SUFFIXES = (
    "@example.com",
    "@example.org",
    "@example.net",
    "@example.invalid",
    "@example.test",
    "@users.noreply.github.com",
    "@localhost",
)
ALLOWED_EMAILS = {
    "octarel@example.com",
    "test@example.com",
    "noreply@github.com",
    "git@github.com",
}
ALLOWED_PATH_PREFIXES = (
    "/usr/",
    "/bin/",
    "/sbin/",
    "/opt/homebrew/",
    "/usr/local/",
    "/tmp/",
    "/private/tmp/",
    "/path/to/",
    "/Volumes/path",
)
PLACEHOLDER_PATH_MARKERS = ("/path/to/", "/Volumes/path", "YOUR_", "<you>", "<user>", "placeholder")

SKIP_DIR_NAMES = {
    ".git",
    ".venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".local-gate",
    ".orchestrator-state",
    ".agent-output",
    "test-results",
    "dist",
    ".npm",
}

TEXT_SUFFIXES = {
    ".py",
    ".md",
    ".txt",
    ".toml",
    ".yml",
    ".yaml",
    ".json",
    ".js",
    ".css",
    ".html",
    ".sh",
    ".example",
    ".toml",
    ".plist",
    ".svg",
    ".cfg",
    ".ini",
    ".rst",
}

COMMITTED_DB_SUFFIXES = {".db", ".sqlite", ".sqlite3"}

# Test fixtures use obviously fake credential-shaped strings. They are allowed
# only when the surrounding text is a test or the value is the documented fake.
ALLOWED_SECRET_SUBSTRINGS = (
    "sk-ABCDEFGHIJKLMNOP1234567890",
    "sk-1234567890abcdef",
    "YOUR_ACCESS",
    "YOUR_TEAM",
    "not-a-committed-secret-test-tag",
)


@dataclass
class Finding:
    category: str
    location: str
    detail: str
    publication_blocker: bool = True


@dataclass
class SafetyReport:
    ok: bool
    tree_ok: bool
    history_ok: bool
    findings: list[Finding] = field(default_factory=list)
    scanned_files: int = 0
    scanned_commits: int = 0

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "tree_ok": self.tree_ok,
            "history_ok": self.history_ok,
            "scanned_files": self.scanned_files,
            "scanned_commits": self.scanned_commits,
            "findings": [asdict(item) for item in self.findings],
            "publication_blockers": [asdict(item) for item in self.findings if item.publication_blocker],
        }

    def as_text(self) -> str:
        lines = [
            f"ok={str(self.ok).lower()}",
            f"tree_ok={str(self.tree_ok).lower()}",
            f"history_ok={str(self.history_ok).lower()}",
            f"scanned_files={self.scanned_files}",
            f"scanned_commits={self.scanned_commits}",
            f"findings={len(self.findings)}",
        ]
        for item in self.findings:
            flag = "BLOCKER" if item.publication_blocker else "note"
            lines.append(f"{flag}\t{item.category}\t{item.location}\t{item.detail}")
        return "\n".join(lines)


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )


def _is_allowed_email(value: str) -> bool:
    lowered = value.lower()
    if lowered in ALLOWED_EMAILS:
        return True
    if lowered.endswith(".invalid"):
        return True
    return any(lowered.endswith(suffix) for suffix in ALLOWED_EMAIL_SUFFIXES)


def _is_allowed_path(value: str) -> bool:
    if any(marker in value for marker in PLACEHOLDER_PATH_MARKERS):
        return True
    return any(value.startswith(prefix) for prefix in ALLOWED_PATH_PREFIXES)


def _looks_like_allowed_secret(text: str) -> bool:
    return any(token in text for token in ALLOWED_SECRET_SUBSTRINGS)


def _iter_text_files(root: Path) -> Iterable[Path]:
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIR_NAMES for part in path.parts):
            continue
        if path.suffix.lower() in TEXT_SUFFIXES or path.name in {
            "LICENSE",
            "NOTICE",
            "CHANGELOG",
            ".gitignore",
            ".env.example",
        }:
            yield path


def scan_file_text(relative: str, text: str) -> list[Finding]:
    findings: list[Finding] = []
    if relative.endswith(tuple(COMMITTED_DB_SUFFIXES)):
        findings.append(
            Finding("committed-database", relative, "database artifact must not be committed", True)
        )
    for lineno, line in enumerate(text.splitlines(), start=1):
        loc = f"{relative}:{lineno}"
        # Test fixtures exercise redaction with obviously fake credential-shaped
        # strings. Flag them only outside tests/.
        in_tests = relative.startswith("tests/")
        if not in_tests and not _looks_like_allowed_secret(line):
            for pattern in _VALUE_PATTERNS:
                if pattern.search(line):
                    findings.append(Finding("secret-pattern", loc, pattern.pattern, True))
                    break
        for email in PRIVATE_EMAIL_RE.findall(line):
            if "git@github.com:" in line or f"{email}:" in line and "github.com" in email:
                continue
            if not _is_allowed_email(email):
                findings.append(Finding("private-email", loc, "non-public email address", True))
        for path in ABSOLUTE_PATH_RE.findall(line):
            if not _is_allowed_path(path):
                findings.append(Finding("operator-path", loc, "operator-specific filesystem path", True))
        lowered = line.lower()
        if "octages_orch_remote_aud" in lowered and re.search(r"[a-f0-9]{32,}", line, re.I):
            if "YOUR_" not in line and "example" not in lowered:
                findings.append(Finding("cloudflare-config", loc, "Access audience looks real", True))
        if "allowed_emails" in lowered and "@" in line and "example.com" not in lowered and "YOUR_" not in line:
            if any(not _is_allowed_email(item) for item in PRIVATE_EMAIL_RE.findall(line)):
                findings.append(Finding("private-email", loc, "Access allow-list email", True))
    return findings


def scan_tree(root: Path) -> tuple[list[Finding], int]:
    findings: list[Finding] = []
    listed = _git(root, "ls-files")
    files = [line for line in listed.stdout.splitlines() if line]
    count = 0
    for relative in files:
        path = root / relative
        count += 1
        if path.suffix.lower() in COMMITTED_DB_SUFFIXES:
            findings.append(
                Finding("committed-database", relative, "database artifact is tracked", True)
            )
            continue
        if relative in {".env"} or relative.startswith(".env.") and relative != ".env.example":
            findings.append(Finding("env-file", relative, "tracked env file", True))
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if b"\0" in data[:1024]:
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        findings.extend(scan_file_text(relative, text))
    return findings, count


def scan_history(root: Path) -> tuple[list[Finding], int]:
    findings: list[Finding] = []
    log = _git(root, "log", "--all", "--format=%H%x09%an <%ae>%x09%cn <%ce>%x09%s")
    commits = [line for line in log.stdout.splitlines() if line]
    for line in commits:
        sha, author, committer, subject = (line.split("\t", 3) + [""] * 4)[:4]
        for blob in (author, committer, subject):
            for email in PRIVATE_EMAIL_RE.findall(blob):
                if not _is_allowed_email(email):
                    findings.append(
                        Finding(
                            "history-email",
                            sha,
                            "commit metadata contains a private email",
                            True,
                        )
                    )
            for path in ABSOLUTE_PATH_RE.findall(blob):
                if not _is_allowed_path(path):
                    findings.append(
                        Finding("history-path", sha, "commit subject contains an operator path", True)
                    )
    # Historical file contents: only look for the known unsafe classes.
    content = _git(root, "grep", "-n", "-I", "-E", r"/Users/|/Volumes/|@[A-Za-z0-9.-]+\.(com|org)", *(
        _git(root, "rev-list", "--all").stdout.splitlines()
    ))
    # `git grep <revs>` with many SHAs can fail if grep finds nothing.
    if content.returncode == 0:
        for line in content.stdout.splitlines():
            if "/path/to/" in line or "@example.com" in line or "users.noreply.github.com" in line:
                continue
            if ABSOLUTE_PATH_RE.search(line) and not any(
                marker in line for marker in PLACEHOLDER_PATH_MARKERS
            ):
                # git grep -n with revs prints `sha:path:lineno:text`
                loc = ":".join(line.split(":", 3)[:3])
                findings.append(Finding("history-path", loc, "historical blob contains an operator path", True))
            for email in PRIVATE_EMAIL_RE.findall(line):
                if not _is_allowed_email(email):
                    loc = ":".join(line.split(":", 3)[:3])
                    findings.append(Finding("history-email", loc, "historical blob contains a private email", True))
    return findings, len(commits)


def scan_repository(root: Path, *, history: bool = True) -> SafetyReport:
    tree_findings, file_count = scan_tree(root)
    history_findings: list[Finding] = []
    commit_count = 0
    if history:
        history_findings, commit_count = scan_history(root)
    findings = tree_findings + history_findings
    tree_ok = not tree_findings
    history_ok = not history_findings
    return SafetyReport(
        ok=tree_ok and history_ok,
        tree_ok=tree_ok,
        history_ok=history_ok,
        findings=findings,
        scanned_files=file_count,
        scanned_commits=commit_count,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Octarel public-safety scanner")
    parser.add_argument("--root", default=".", help="repository root")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--tree-only", action="store_true", help="skip git history")
    parser.add_argument(
        "--publication-gate",
        action="store_true",
        help="exit 1 unless both the current tree and history are public-safe",
    )
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    report = scan_repository(root, history=not args.tree_only)
    if args.json:
        print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    else:
        print(report.as_text())
    if args.publication_gate:
        return 0 if report.ok else 1
    return 0 if report.tree_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
