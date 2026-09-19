#!/usr/bin/env python3
"""CPX-07 clean-room install/start acceptance.

Clones the candidate tree into a fresh directory, creates a new Python venv
and a new Node install, then proves the CLI, dashboard, and state initialize
without the development checkout's ``.venv``, ``node_modules``, or operator
paths. Never imports the OctaScene application.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class CleanRoomReport:
    ok: bool
    checkout: str
    steps: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    def as_text(self) -> str:
        lines = [f"ok={str(self.ok).lower()}", f"checkout={self.checkout}"]
        lines.extend(f"step={item}" for item in self.steps)
        lines.extend(f"failure={item}" for item in self.failures)
        return "\n".join(lines)


def _run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


def _assert_no_octages_app_imports(root: Path, report: CleanRoomReport) -> None:
    for path in (root / "scripts").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                if name == "app" or name.startswith("app."):
                    report.failures.append(f"{path.relative_to(root)} imports {name}")


def run_clean_room(*, source: Path, destination: Path | None = None, with_node: bool = True) -> CleanRoomReport:
    source = source.resolve()
    cleanup = False
    if destination is None:
        destination = Path(tempfile.mkdtemp(prefix="octarel-clean-room-"))
        cleanup = False  # caller may inspect; tests delete
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    report = CleanRoomReport(ok=False, checkout=str(destination))

    clone = _run(["git", "clone", "--quiet", str(source), str(destination / "octarel")], cwd=source)
    if clone.returncode:
        report.failures.append(clone.stderr.strip() or "git clone failed")
        return report
    checkout = destination / "octarel"
    # Overlay the source working tree so an uncommitted candidate can be proven
    # without depending on the development venv/node_modules.
    listed = _run(["git", "ls-files"], cwd=source)
    extra = _run(["git", "ls-files", "--others", "--exclude-standard"], cwd=source)
    for relative in [*(listed.stdout.splitlines()), *(extra.stdout.splitlines())]:
        if not relative or relative.endswith("/"):
            continue
        src_file = source / relative
        dest_file = checkout / relative
        if not src_file.is_file():
            continue
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_file, dest_file)
    report.steps.append("clone")
    if (checkout / ".venv").exists() or (checkout / "node_modules").exists():
        report.failures.append("clone reused development venv or node_modules")
        return report

    _assert_no_octages_app_imports(checkout, report)
    report.steps.append("no-octages-app-imports")

    venv = checkout / ".venv"
    created = _run([sys.executable, "-m", "venv", str(venv)], cwd=checkout)
    if created.returncode:
        report.failures.append(created.stderr.strip() or "venv failed")
        return report
    report.steps.append("venv")
    python = venv / "bin" / "python"
    pip = _run(
        [str(python), "-m", "pip", "install", "-q", "-r", "requirements-dev.txt"],
        cwd=checkout,
        timeout=600,
    )
    if pip.returncode:
        report.failures.append(pip.stderr.strip() or "pip install failed")
        return report
    report.steps.append("pip")

    env = os.environ.copy()
    env["PATH"] = f"{venv / 'bin'}{os.pathsep}{env.get('PATH', '')}"
    env.pop("OCTAREL_STATE_DIR", None)
    env.pop("OCTAREL_CODE_ROOT", None)
    env.pop("OCTAREL_OCTASCENE_ROOT", None)
    env.pop("OCTAGES_ORCH_CANONICAL_REPO_ROOT", None)
    env["VIRTUAL_ENV"] = str(venv)

    version = _run([str(python), "-m", "octarel", "version"], cwd=checkout, env=env)
    if version.returncode or not version.stdout.strip():
        report.failures.append("octarel version failed")
        return report
    report.steps.append(f"version={version.stdout.strip()}")

    health = _run([str(python), "-m", "octarel", "health"], cwd=checkout, env=env)
    if health.returncode:
        report.failures.append(health.stderr.strip() or "octarel health failed")
        return report
    health_text = health.stdout
    if f"code_root={checkout}" not in health_text.replace("\\", "/"):
        # Path equality after resolve.
        if str(checkout) not in health_text:
            report.failures.append("health did not report the clean-room code root")
            return report
    if "code_root_equals_selected_project=true" in health_text:
        report.failures.append("clean-room health treated Octarel as the selected project")
        return report
    report.steps.append("health")

    state_dir = checkout / ".orchestrator-state"
    db = state_dir / "orchestrator.db"
    if not db.is_file():
        # health may not create the db until a State is opened; initialize via migrate-less State.
        init = _run(
            [str(python), "-c", "from pathlib import Path; from scripts.agents.control_plane.state import State, default_db_path; State(default_db_path(Path('.')))"],
            cwd=checkout,
            env=env,
        )
        if init.returncode:
            report.failures.append(init.stderr.strip() or "state init failed")
            return report
    if not db.is_file():
        report.failures.append("state database was not created")
        return report
    report.steps.append("state")

    generic = destination / "generic-project"
    generic.mkdir()
    for cmd in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "config", "user.name", "Test"],
    ):
        _run(cmd, cwd=generic)
    (generic / "POLICY.md").write_text("# Generic policy\n", encoding="utf-8")
    (generic / "TASKS.md").write_text("| ID | status | notes |\n|---|---|---|\n| G-01 | pending | next |\n", encoding="utf-8")
    (generic / "validate.sh").write_text("#!/bin/sh\nprintf 'ok\\n'\n", encoding="utf-8")
    (generic / "validate.sh").chmod(0o755)
    _run(["git", "add", "POLICY.md", "TASKS.md", "validate.sh"], cwd=generic)
    _run(["git", "commit", "-q", "-m", "init"], cwd=generic)

    add = _run(
        [
            str(python),
            "-m",
            "octarel",
            "project",
            "add",
            "--id",
            "generic",
            "--path",
            str(generic),
            "--name",
            "Generic",
            "--policy",
            "POLICY.md",
            "--tasks",
            "TASKS.md",
            "--validate",
            "sh",
            "validate.sh",
        ],
        cwd=checkout,
        env=env,
    )
    if add.returncode:
        report.failures.append(add.stderr.strip() or add.stdout.strip() or "project add failed")
        return report
    select = _run([str(python), "-m", "octarel", "project", "select", "generic"], cwd=checkout, env=env)
    if select.returncode:
        report.failures.append(select.stderr.strip() or "project select failed")
        return report
    report.steps.append("project-add-select")

    if with_node:
        npm = _run(["npm", "install", "--ignore-scripts"], cwd=checkout, timeout=600)
        if npm.returncode:
            report.failures.append(npm.stderr.strip() or "npm install failed")
            return report
        report.steps.append("npm")

    port = _free_port()
    dash_env = env.copy()
    proc = subprocess.Popen(
        [str(python), "-m", "octarel", "dashboard", "--host", "127.0.0.1", "--port", str(port)],
        cwd=checkout,
        env=dash_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        body = None
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/cp-status", timeout=1) as response:
                    body = json.loads(response.read().decode("utf-8"))
                    break
            except (OSError, TimeoutError, json.JSONDecodeError):
                if proc.poll() is not None:
                    stderr = proc.stderr.read() if proc.stderr else ""
                    report.failures.append(f"dashboard exited: {stderr[-400:]}")
                    return report
                time.sleep(0.2)
        if not body or body.get("runtime") != "octarel":
            report.failures.append("dashboard did not report runtime=octarel")
            return report
        if Path(str(body.get("cp_code_root") or "")).resolve() != checkout.resolve():
            report.failures.append("dashboard cp_code_root is not the clean-room checkout")
            return report
        report.steps.append(f"dashboard:{port}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    if not report.failures:
        report.ok = True
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Octarel clean-room acceptance")
    parser.add_argument("--source", default=".", help="candidate git checkout to clone")
    parser.add_argument("--destination", default=None)
    parser.add_argument("--skip-node", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    dest = Path(args.destination) if args.destination else None
    report = run_clean_room(source=Path(args.source), destination=dest, with_node=not args.skip_node)
    if args.json:
        print(json.dumps(asdict(report), indent=2))
    else:
        print(report.as_text())
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
