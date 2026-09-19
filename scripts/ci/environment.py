"""Secret-safe, no-network development worktree readiness checks."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

try:
    from scripts.ci.runtime_paths import CP_RUNTIME_DIRNAMES, ensure_git_excludes
except ModuleNotFoundError:  # direct ``python scripts/ci/environment.py`` entry point
    from runtime_paths import CP_RUNTIME_DIRNAMES, ensure_git_excludes


def resolve_python(root: Path) -> str:
    local = root / ".venv" / "bin" / "python"
    return str(local) if local.is_file() and os.access(local, os.X_OK) else sys.executable


def _run(argv: list[str], root: Path, timeout: float = 10) -> tuple[bool, str]:
    try:
        result = subprocess.run(argv, cwd=root, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    value = (result.stdout or result.stderr).strip().splitlines()
    return result.returncode == 0, value[0][:200] if value else "UNKNOWN"


def _node_resolves(root: Path, package: str) -> bool:
    if not shutil.which("node"):
        return False
    ok, _ = _run(["node", "-e", f"require.resolve({package!r})"], root)
    return ok


def _browser_ready(root: Path) -> bool:
    if not _node_resolves(root, "@playwright/test"):
        return False
    script = "const {chromium}=require('@playwright/test');const fs=require('fs');process.exit(fs.existsSync(chromium.executablePath())?0:1)"
    ok, _ = _run(["node", "-e", script], root)
    return ok


def _dependency_roots(root: Path) -> list[Path]:
    """Return independently locked Node projects required by the frontend gate."""

    roots = [root]
    v2 = root / "frontend" / "v2"
    if (v2 / "package-lock.json").is_file():
        roots.append(v2)
    return roots


def environment_fingerprint(facts: dict[str, Any]) -> str:
    safe = {key: value for key, value in facts.items() if key not in {"failures", "repo_root"}}
    return hashlib.sha256(json.dumps(safe, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


DEPENDENCIES_REUSED = "DEPENDENCIES_REUSED"
DEPENDENCIES_INSTALLED = "DEPENDENCIES_INSTALLED"
DEPENDENCIES_FAILED = "DEPENDENCIES_FAILED"
BROWSER_REUSED = "BROWSER_REUSED"
BROWSER_DOWNLOADED = "BROWSER_DOWNLOADED"
BROWSER_FAILED = "BROWSER_FAILED"
BROWSER_NOT_REQUIRED = "BROWSER_NOT_REQUIRED"

_FINGERPRINT_MARKER = ".lockfile-fingerprint.json"


def _lockfile_fingerprint(dependency_root: Path) -> str | None:
    lockfile = dependency_root / "package-lock.json"
    if not lockfile.is_file():
        return None
    return hashlib.sha256(lockfile.read_bytes()).hexdigest()


def _stored_fingerprint(dependency_root: Path) -> str | None:
    marker = dependency_root / "node_modules" / _FINGERPRINT_MARKER
    if not marker.is_file():
        return None
    try:
        return json.loads(marker.read_text(encoding="utf-8")).get("package_lock_sha256")
    except (OSError, json.JSONDecodeError):
        return None


def _store_fingerprint(dependency_root: Path, fingerprint: str) -> None:
    marker = dependency_root / "node_modules" / _FINGERPRINT_MARKER
    try:
        marker.write_text(json.dumps({"package_lock_sha256": fingerprint}), encoding="utf-8")
    except OSError:  # pragma: no cover - best-effort bookkeeping only
        pass


def bootstrap_dependencies(root: Path, *, require_frontend: bool, require_browser: bool,
                            allow_network: bool = True, timeout: float = 300) -> dict[str, Any]:
    """Install Node/Playwright dependencies only when actually needed (ENG-AGENT-14, issue #140).

    Unlike :func:`prepare_worktree`'s ``hydrate_offline`` (which only checks whether
    ``node_modules`` exists at all), this fingerprints each dependency root's
    ``package-lock.json`` content and skips ``npm ci`` whenever an existing
    ``node_modules`` was already installed from that exact lockfile content --
    a stale-but-present ``node_modules`` from a different lockfile revision is
    correctly treated as needing reinstall, not silently reused. Playwright's
    browser cache (e.g. ``~/Library/Caches/ms-playwright``) is checked before
    ever invoking ``playwright install``; when the required browser is already
    present, install is never invoked at all -- Playwright's own installer is
    itself cache-aware, but this avoids even the subprocess call, and lets a
    caller running with ``allow_network=False`` (never redownload) fail
    closed instead of attempting a network fetch.
    """

    root = root.resolve()
    dependency_roots = _dependency_roots(root) if require_frontend else []
    projects: list[dict[str, Any]] = []
    for dependency_root in dependency_roots:
        rel = dependency_root.relative_to(root).as_posix() or "."
        current_fingerprint = _lockfile_fingerprint(dependency_root)
        node_modules_present = (dependency_root / "node_modules").is_dir()
        stored_fingerprint = _stored_fingerprint(dependency_root) if node_modules_present else None
        if node_modules_present and current_fingerprint is not None and stored_fingerprint == current_fingerprint:
            projects.append({
                "path": rel, "status": DEPENDENCIES_REUSED,
                "reason": f"node_modules already installed from the current package-lock.json (sha256 {current_fingerprint[:12]})",
                "fingerprint": current_fingerprint,
            })
            continue
        if current_fingerprint is None:
            projects.append({
                "path": rel, "status": DEPENDENCIES_REUSED,
                "reason": "no package-lock.json at this path; nothing to install", "fingerprint": None,
            })
            continue
        reason = (
            "no node_modules present" if not node_modules_present
            else f"node_modules fingerprint {stored_fingerprint!r} does not match the current lockfile"
        )
        argv = ["npm", "ci", "--ignore-scripts"] + ([] if allow_network else ["--offline"])
        ok, detail = _run(argv, dependency_root, timeout=timeout)
        if ok:
            _store_fingerprint(dependency_root, current_fingerprint)
        projects.append({
            "path": rel, "status": DEPENDENCIES_INSTALLED if ok else DEPENDENCIES_FAILED,
            "reason": reason, "detail": detail, "fingerprint": current_fingerprint,
        })

    browser_result: dict[str, Any]
    if not require_browser:
        browser_result = {"status": BROWSER_NOT_REQUIRED, "reason": "browser not required for this worktree"}
    elif _browser_ready(root):
        browser_result = {"status": BROWSER_REUSED, "reason": "required Chromium build already present in the local cache"}
    elif not allow_network:
        browser_result = {
            "status": BROWSER_FAILED,
            "reason": "Chromium is not cached locally and allow_network=False refuses to download it",
        }
    else:
        ok, detail = _run(["npx", "--no-install", "playwright", "install", "chromium"], root, timeout=timeout)
        if not ok:
            # --no-install requires the playwright CLI to already be resolvable
            # (i.e. node_modules already installed above); fall back to a
            # plain npx only if that specific resolution failed, never to
            # paper over a real download failure.
            ok, detail = _run(["npx", "playwright", "install", "chromium"], root, timeout=timeout)
        browser_result = {
            "status": BROWSER_DOWNLOADED if ok and _browser_ready(root) else BROWSER_FAILED,
            "reason": "downloaded the required Chromium build" if ok else f"playwright install failed: {detail}",
        }

    return {
        "dependencies": projects,
        "dependencies_status": (
            DEPENDENCIES_FAILED if any(p["status"] == DEPENDENCIES_FAILED for p in projects)
            else DEPENDENCIES_INSTALLED if any(p["status"] == DEPENDENCIES_INSTALLED for p in projects)
            else DEPENDENCIES_REUSED
        ),
        "browser": browser_result,
    }


def prepare_worktree(
    root: Path, *, require_python: bool = True, require_frontend: bool = False,
    require_browser: bool = False, require_ffmpeg: bool = False, create_local_dirs: bool = False,
    hydrate_offline: bool = False,
) -> dict[str, Any]:
    """Return READY/BLOCKED without network access or secret inspection."""

    root = root.resolve()
    # ENG-AGENT-16 (issue #146): idempotently register these directories in
    # this worktree's local (never-committed) git excludes on every
    # preparation, not only when freshly provisioned -- this is what makes a
    # *historical* worktree, whose already-committed .gitignore predates one
    # or more of these directories, self-heal the moment it is next prepared,
    # with no branch content edit required.
    ensure_git_excludes(root)
    if create_local_dirs:
        for name in CP_RUNTIME_DIRNAMES:
            (root / name).mkdir(exist_ok=True)
    python = resolve_python(root)
    py_ok, py_version = _run([python, "--version"], root)
    python_packages: dict[str, bool] = {}
    if require_python and py_ok:
        for package in ("pytest", "ruff"):
            ok, _ = _run([python, "-c", f"import {package}"], root)
            python_packages[package] = ok
    node_ok, node_version = _run(["node", "--version"], root) if shutil.which("node") else (False, "UNKNOWN")
    npm_ok, npm_version = _run(["npm", "--version"], root) if shutil.which("npm") else (False, "UNKNOWN")
    dependency_roots = _dependency_roots(root)
    missing_dependency_roots = [path for path in dependency_roots if not (path / "node_modules").is_dir()]
    node_dependencies = not missing_dependency_roots
    offline_prepare: dict[str, Any] = {"attempted": False, "result": "NOT_NEEDED"}
    needs_node_prepare = require_frontend and (
        not node_dependencies or (require_browser and not _node_resolves(root, "@playwright/test"))
    )
    if hydrate_offline and needs_node_prepare and npm_ok:
        offline_prepare["attempted"] = True
        prepared: list[dict[str, str]] = []
        for dependency_root in dependency_roots:
            if (dependency_root / "node_modules").is_dir() or not (dependency_root / "package-lock.json").is_file():
                continue
            ok, detail = _run(["npm", "ci", "--offline", "--ignore-scripts"], dependency_root, timeout=180)
            prepared.append({
                "path": dependency_root.relative_to(root).as_posix() or ".",
                "result": "PASS" if ok else "BLOCKED",
                "detail": detail,
            })
        offline_prepare.update({
            "result": "PASS" if prepared and all(item["result"] == "PASS" for item in prepared) else "BLOCKED",
            "projects": prepared,
        })
        missing_dependency_roots = [path for path in dependency_roots if not (path / "node_modules").is_dir()]
        node_dependencies = not missing_dependency_roots
    playwright = _node_resolves(root, "@playwright/test") if require_browser else None
    axe = _node_resolves(root, "@axe-core/playwright") if require_browser else None
    browser = _browser_ready(root) if require_browser else None
    ffmpeg = shutil.which("ffmpeg") if require_ffmpeg else None
    ffprobe = shutil.which("ffprobe") if require_ffmpeg else None
    failures: list[str] = []
    if require_python and not py_ok:
        failures.append(f"approved Python interpreter unavailable: {python}")
    for package, available in python_packages.items():
        if not available:
            failures.append(f"required Python package unavailable: {package}")
    if require_frontend and (not node_ok or not npm_ok):
        failures.append("Node/npm runtime unavailable")
    if require_frontend and not node_dependencies:
        missing = ", ".join(path.relative_to(root).as_posix() or "." for path in missing_dependency_roots)
        failures.append(f"node_modules unavailable for: {missing}; run offline worktree preparation before the final gate")
    if require_browser and not playwright:
        failures.append("@playwright/test unavailable")
    if require_browser and not axe:
        failures.append("@axe-core/playwright unavailable")
    if require_browser and not browser:
        failures.append("Playwright Chromium runtime unavailable; prepare it before the final gate")
    if require_ffmpeg and not ffmpeg:
        failures.append("ffmpeg unavailable")
    if require_ffmpeg and not ffprobe:
        failures.append("ffprobe unavailable")
    facts: dict[str, Any] = {
        "status": "READY" if not failures else "BLOCKED",
        "failures": failures,
        "python": python,
        "python_version": py_version,
        "python_packages": python_packages,
        "node_version": node_version if require_frontend else None,
        "npm_version": npm_version if require_frontend else None,
        "node_dependencies_ready": node_dependencies if require_frontend else None,
        "playwright_ready": playwright,
        "axe_ready": axe,
        "playwright_browser_ready": browser,
        "ffmpeg": bool(ffmpeg) if require_ffmpeg else None,
        "ffprobe": bool(ffprobe) if require_ffmpeg else None,
        "cache_roots": {
            "npm": str(Path(os.environ.get("npm_config_cache", Path.home() / ".npm")).name),
            "playwright": "configured" if os.environ.get("PLAYWRIGHT_BROWSERS_PATH") else "platform-default",
        },
        "network_install_performed": False,
        "offline_prepare": offline_prepare,
    }
    facts["fingerprint"] = environment_fingerprint(facts)
    return facts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--prepare", action="store_true", help="hydrate npm dependencies from the local cache only")
    parser.add_argument("--frontend", action="store_true")
    parser.add_argument("--browser", action="store_true")
    parser.add_argument("--ffmpeg", action="store_true")
    args = parser.parse_args()
    result = prepare_worktree(
        args.repo_root, require_frontend=args.frontend or args.browser, require_browser=args.browser,
        require_ffmpeg=args.ffmpeg, create_local_dirs=args.prepare, hydrate_offline=args.prepare,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "READY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
