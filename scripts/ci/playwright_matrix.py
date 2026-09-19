#!/usr/bin/env python3
"""Bounded-parallel Control Center Playwright viewport matrix (OCTAREL-TEST-01).

Each viewport project runs as its own *lane*: exactly one Playwright worker,
one project, one unique loopback port, one fixture-server process (started by
Playwright's ``webServer`` from that lane's own environment), one fixture/Git/
SQLite root, one result/artifact directory, and its own stdout/stderr files.
Lanes never share mutable state; they only run concurrently, up to a bounded
concurrency (default 3, ``OCTAREL_PLAYWRIGHT_MATRIX_CONCURRENCY``).

Any lane that does not positively PASS fails the aggregate; a lane that could
not launch, timed out, was cancelled, or produced no report is never PASS.
Every process a lane spawned (fixture server, browsers) is reaped when the lane
ends and on interruption, identified by the lane's unique directory appearing in
its command line (the lane's ``TMPDIR`` is inside it, so browser profiles match).

Test scope selection stays with ``scripts/ci/test_impact.py``; this module only
executes the Control Center responsive matrix when a caller requires it.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.ci.ports import PortLeases  # noqa: E402

VIEWPORT_PROJECTS: tuple[str, ...] = (
    "desktop-1920",
    "desktop-1440",
    "desktop-1280",
    "tablet-768",
    "android-430",
    "iphone-390",
    "iphone-16-pro-393",
)
PLAYWRIGHT_CONFIG = "scripts/agents/control_plane/playwright.config.js"
CONCURRENCY_ENV = "OCTAREL_PLAYWRIGHT_MATRIX_CONCURRENCY"
DEFAULT_CONCURRENCY = 3
MIN_CONCURRENCY = 1
MAX_CONCURRENCY = len(VIEWPORT_PROJECTS)
DEFAULT_LANE_TIMEOUT_SECONDS = 900.0
RESULTS_BASE = Path("test-results") / "control-center-matrix"
TERMINATE_GRACE_SECONDS = 5.0

PASS, FAIL, TIMEOUT, LAUNCH_FAILED, CANCELLED = "PASS", "FAIL", "TIMEOUT", "LAUNCH_FAILED", "CANCELLED"

# Child environments are built from this allowlist, so no secret or provider
# credential in the caller's environment ever reaches a lane.
ENV_ALLOWLIST = (
    "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "SHELL",
    "PLAYWRIGHT_BROWSERS_PATH", "XDG_CACHE_HOME", "SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS",
)


class MatrixConfigError(ValueError):
    """Invalid concurrency or viewport selection (exit status 2)."""


@dataclass
class Lane:
    project: str
    port: int
    directory: Path
    status: str = CANCELLED
    returncode: int | None = None
    started_at: float | None = None
    ended_at: float | None = None
    counts: dict[str, int] = field(default_factory=dict)
    detail: str = ""
    pid: int | None = None
    leftover_processes_reaped: int = 0

    @property
    def fixture_root(self) -> Path:
        return self.directory / "fixture"

    @property
    def tmp_dir(self) -> Path:
        return self.directory / "tmp"

    @property
    def results_dir(self) -> Path:
        return self.directory / "results"

    @property
    def stdout_path(self) -> Path:
        return self.directory / "stdout.log"

    @property
    def stderr_path(self) -> Path:
        return self.directory / "stderr.log"

    @property
    def report_path(self) -> Path:
        return self.results_dir / "report.json"

    @property
    def duration(self) -> float:
        if self.started_at is None or self.ended_at is None:
            return 0.0
        return max(0.0, self.ended_at - self.started_at)

    def as_dict(self, root: Path) -> dict[str, Any]:
        def rel(path: Path) -> str:
            try:
                return str(path.relative_to(root))
            except ValueError:
                return str(path)

        return {
            "project": self.project,
            "result": self.status,
            "port": self.port,
            "duration_seconds": round(self.duration, 3),
            "returncode": self.returncode,
            "counts": self.counts,
            "detail": self.detail,
            "artifact_dir": rel(self.directory),
            "stdout": rel(self.stdout_path),
            "stderr": rel(self.stderr_path),
            "leftover_processes_reaped": self.leftover_processes_reaped,
        }


# ------------------------------------------------------------------ selection


def parse_concurrency(value: str | int | None) -> int:
    if value is None or (isinstance(value, str) and not value.strip()):
        return DEFAULT_CONCURRENCY
    try:
        number = int(str(value).strip())
    except ValueError:
        raise MatrixConfigError(f"concurrency must be an integer, got {value!r}") from None
    if not MIN_CONCURRENCY <= number <= MAX_CONCURRENCY:
        raise MatrixConfigError(
            f"concurrency must be between {MIN_CONCURRENCY} and {MAX_CONCURRENCY}, got {number}"
        )
    return number


def resolve_projects(spec: str | Sequence[str] | None) -> tuple[str, ...]:
    """Requested viewport projects in canonical matrix order; unknown/duplicate names fail."""

    if spec is None or spec == "" or spec == [] or spec == ():
        return VIEWPORT_PROJECTS
    names = [p.strip() for p in (spec.split(",") if isinstance(spec, str) else spec)]
    if any(not name for name in names):
        raise MatrixConfigError("empty viewport project name in selection")
    unknown = [name for name in names if name not in VIEWPORT_PROJECTS]
    if unknown:
        raise MatrixConfigError(f"unknown viewport project(s): {', '.join(unknown)}; valid: {', '.join(VIEWPORT_PROJECTS)}")
    duplicated = sorted({name for name in names if names.count(name) > 1})
    if duplicated:
        raise MatrixConfigError(f"duplicate viewport project(s): {', '.join(duplicated)}")
    return tuple(name for name in VIEWPORT_PROJECTS if name in names)


# ------------------------------------------------------------ lane environment


def lane_environment(lane: Lane, *, python: str, base_env: dict[str, str] | None = None) -> dict[str, str]:
    source = os.environ if base_env is None else base_env
    env = {key: source[key] for key in ENV_ALLOWLIST if key in source}
    env.update(
        {
            "OCTAGES_CONTROL_CENTER_TEST_PORT": str(lane.port),
            "OCTAREL_CONTROL_CENTER_FIXTURE_ROOT": str(lane.fixture_root),
            "OCTAREL_PLAYWRIGHT_RESULTS_DIR": str(lane.results_dir),
            "OCTAREL_MATRIX_PROJECT": lane.project,
            "OCTAGES_PYTHON_BIN": python,
            "OCTAREL_PYTHON_BIN": python,
            "TMPDIR": str(lane.tmp_dir),
            "FORCE_COLOR": "0",
        }
    )
    return env


def default_command(project: str, extra: Sequence[str] = ()) -> list[str]:
    return [
        "npx", "--no-install", "playwright", "test", "--config", PLAYWRIGHT_CONFIG,
        f"--project={project}", "--workers=1", *extra,
    ]


# ---------------------------------------------------------- process management


def _process_table() -> list[tuple[int, int, str]]:
    try:
        out = subprocess.run(
            ["ps", "-axww", "-o", "pid=,pgid=,command="], capture_output=True, text=True, timeout=10, check=False
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    rows: list[tuple[int, int, str]] = []
    for line in out.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            rows.append((int(parts[0]), int(parts[1]), parts[2]))
    return rows


def processes_under(directory: Path) -> list[int]:
    """PIDs (excluding this process) whose command line references the lane directory."""

    needle = str(directory)
    me = os.getpid()
    return [pid for pid, _pgid, command in _process_table() if pid != me and needle in command]


def _signal_pid(pid: int, sig: int) -> None:
    try:
        os.kill(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _signal_group(pgid: int, sig: int) -> None:
    if pgid == os.getpgrp():
        return
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def terminate_lane_processes(lane: Lane, leader: subprocess.Popen[bytes] | None = None) -> int:
    """Terminate the lane's process group and every stray process tied to its directory.

    Returns how many processes besides the lane leader were still alive and had to be reaped.
    """

    lingering: set[int] = set()
    if leader is not None and leader.poll() is None:
        _signal_group(leader.pid, signal.SIGTERM)
    deadline = time.monotonic() + TERMINATE_GRACE_SECONDS
    while time.monotonic() < deadline:
        alive = set(processes_under(lane.directory))
        if (leader is None or leader.poll() is not None) and not alive:
            break
        for pid in alive:
            if leader is None or pid != leader.pid:
                lingering.add(pid)
            _signal_pid(pid, signal.SIGTERM)
        time.sleep(0.1)
    if leader is not None and leader.poll() is None:
        _signal_group(leader.pid, signal.SIGKILL)
    for pid in processes_under(lane.directory):
        if leader is None or pid != leader.pid:
            lingering.add(pid)
        _signal_pid(pid, signal.SIGKILL)
    if leader is not None:
        try:
            leader.wait(timeout=TERMINATE_GRACE_SECONDS)
        except subprocess.TimeoutExpired:  # pragma: no cover - SIGKILL was sent
            pass
    for _ in range(50):  # confirm the SIGKILLs landed
        if not processes_under(lane.directory):
            break
        time.sleep(0.1)
    return len(lingering)


# --------------------------------------------------------------------- a lane


def _read_counts(report: Path) -> dict[str, int] | None:
    try:
        stats = json.loads(report.read_text(encoding="utf-8")).get("stats", {})
    except (OSError, ValueError):
        return None
    return {
        "passed": int(stats.get("expected", 0)),
        "failed": int(stats.get("unexpected", 0)),
        "flaky": int(stats.get("flaky", 0)),
        "skipped": int(stats.get("skipped", 0)),
    }


def run_lane(
    lane: Lane,
    *,
    command: Sequence[str],
    python: str,
    timeout: float,
    cancel: threading.Event,
    require_report: bool,
    base_env: dict[str, str] | None = None,
) -> Lane:
    if cancel.is_set():
        lane.status, lane.detail = CANCELLED, "matrix was cancelled before this lane started"
        return lane
    for directory in (lane.fixture_root, lane.tmp_dir, lane.results_dir):
        directory.mkdir(parents=True, exist_ok=True)
    env = lane_environment(lane, python=python, base_env=base_env)
    lane.status = "RUNNING"
    lane.started_at = time.monotonic()
    proc: subprocess.Popen[bytes] | None = None
    try:
        with lane.stdout_path.open("wb") as out, lane.stderr_path.open("wb") as err:
            try:
                proc = subprocess.Popen(
                    list(command), cwd=str(REPO_ROOT), env=env, stdout=out, stderr=err,
                    stdin=subprocess.DEVNULL, start_new_session=True,
                )
            except OSError as exc:
                lane.status, lane.detail = LAUNCH_FAILED, f"could not launch lane command: {exc}"
                return lane
            lane.pid = proc.pid
            deadline = time.monotonic() + timeout
            while proc.poll() is None:
                if cancel.is_set():
                    lane.status, lane.detail = CANCELLED, "matrix interrupted while this lane was running"
                    break
                if time.monotonic() > deadline:
                    lane.status, lane.detail = TIMEOUT, f"lane exceeded {timeout:g}s and was terminated"
                    break
                time.sleep(0.05)
    finally:
        if lane.started_at is not None:
            lane.ended_at = time.monotonic()
        lane.leftover_processes_reaped = terminate_lane_processes(lane, proc)
    if lane.status in (TIMEOUT, CANCELLED, LAUNCH_FAILED):
        lane.returncode = proc.returncode if proc else None
        return lane
    lane.returncode = proc.returncode if proc else None
    counts = _read_counts(lane.report_path)
    lane.counts = counts or {}
    if lane.returncode != 0:
        lane.status, lane.detail = FAIL, f"lane exited with status {lane.returncode}"
    elif require_report and (counts is None or counts["passed"] < 1):
        lane.status, lane.detail = FAIL, "lane exited 0 but produced no Playwright report with passing tests"
    elif counts is not None and (counts["failed"] or counts["flaky"]):
        lane.status, lane.detail = FAIL, "report contains failed or flaky tests"
    else:
        lane.status = PASS
    return lane


# ---------------------------------------------------------------------- matrix


@dataclass
class MatrixResult:
    lanes: list[Lane]
    concurrency: int
    wall_seconds: float
    max_observed_concurrency: int
    run_dir: Path
    interrupted: bool = False

    @property
    def ok(self) -> bool:
        return not self.interrupted and bool(self.lanes) and all(lane.status == PASS for lane in self.lanes)

    @property
    def serial_equivalent_seconds(self) -> float:
        return sum(lane.duration for lane in self.lanes)


def format_summary(result: MatrixResult) -> str:
    rows = [("Project", "Result", "Port", "Duration", "Pass/Fail/Skip")]
    for lane in result.lanes:
        c = lane.counts
        rows.append(
            (lane.project, lane.status, str(lane.port), f"{lane.duration:.1f}s",
             f"{c.get('passed', '-')}/{c.get('failed', '-')}/{c.get('skipped', '-')}" if c else "-")
        )
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    lines = ["  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip() for row in rows]
    lines += [
        "",
        f"Aggregate: {'PASS' if result.ok else 'FAIL'}" + (" (interrupted)" if result.interrupted else ""),
        f"Total wall time: {result.wall_seconds:.1f}s",
        f"Serial-equivalent lane time: {result.serial_equivalent_seconds:.1f}s",
        f"Concurrency: {result.concurrency} (max observed {result.max_observed_concurrency})",
        f"Artifacts: {result.run_dir}",
    ]
    for lane in result.lanes:
        if lane.status != PASS:
            lines.append(f"  {lane.project}: {lane.status} - {lane.detail} (see {lane.directory})")
    return "\n".join(lines)


def _write_evidence(result: MatrixResult, root: Path) -> None:
    document = {
        "schema": "octarel.control-center-matrix/1",
        "aggregate": "PASS" if result.ok else "FAIL",
        "interrupted": result.interrupted,
        "concurrency": result.concurrency,
        "max_observed_concurrency": result.max_observed_concurrency,
        "wall_seconds": round(result.wall_seconds, 3),
        "serial_equivalent_seconds": round(result.serial_equivalent_seconds, 3),
        "finished_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "lanes": [lane.as_dict(root) for lane in result.lanes],
    }
    (result.run_dir / "summary.json").write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (result.run_dir / "summary.txt").write_text(format_summary(result) + "\n", encoding="utf-8")


def run_matrix(
    *,
    root: Path = REPO_ROOT,
    projects: Sequence[str] = VIEWPORT_PROJECTS,
    concurrency: int = DEFAULT_CONCURRENCY,
    python: str | None = None,
    extra_args: Sequence[str] = (),
    command_override: Sequence[str] | None = None,
    lane_timeout: float = DEFAULT_LANE_TIMEOUT_SECONDS,
    results_base: Path | None = None,
    base_env: dict[str, str] | None = None,
    cancel: threading.Event | None = None,
    keep_success_scratch: bool = False,
) -> MatrixResult:
    concurrency = parse_concurrency(concurrency)
    projects = resolve_projects(list(projects))
    python = python or sys.executable
    cancel = cancel or threading.Event()
    base = results_base or (root / RESULTS_BASE)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S")
    run_dir = base / f"{stamp}-{os.getpid()}"
    run_dir.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    observed = {"now": 0, "max": 0}
    lock = threading.Lock()

    with PortLeases(root) as leases:
        lanes = [Lane(project=name, port=leases.allocate(f"cc-matrix-{name}"), directory=run_dir / "lanes" / name)
                 for name in projects]

        def work(lane: Lane) -> Lane:
            with lock:
                observed["now"] += 1
                observed["max"] = max(observed["max"], observed["now"])
            try:
                command = list(command_override) if command_override else default_command(lane.project, extra_args)
                run_lane(
                    lane, command=command, python=python, timeout=lane_timeout, cancel=cancel,
                    require_report=command_override is None, base_env=base_env,
                )
            except BaseException as exc:  # noqa: BLE001 - a crashed lane must never read as PASS
                lane.status, lane.detail = FAIL, f"runner error: {exc!r}"
            finally:
                with lock:
                    observed["now"] -= 1
            return lane

        with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="cc-lane") as pool:
            for lane in lanes:
                pool.submit(work, lane)

    result = MatrixResult(
        lanes=lanes, concurrency=concurrency, wall_seconds=time.monotonic() - started,
        max_observed_concurrency=observed["max"], run_dir=run_dir, interrupted=cancel.is_set(),
    )
    for lane in lanes:
        # Deterministic cleanup: scratch is removed only for a PASS lane; a
        # failing lane keeps fixture state, traces and screenshots as evidence.
        if lane.status == PASS and not keep_success_scratch:
            shutil.rmtree(lane.fixture_root, ignore_errors=True)
            shutil.rmtree(lane.tmp_dir, ignore_errors=True)
    _write_evidence(result, root)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Control Center viewport matrix in isolated bounded-parallel lanes.")
    parser.add_argument("--projects", default=None, help="comma-separated viewport projects (default: all seven)")
    parser.add_argument("--concurrency", default=None, help=f"1-{MAX_CONCURRENCY}; default env {CONCURRENCY_ENV} or {DEFAULT_CONCURRENCY}")
    parser.add_argument("--python", default=None, help="python used by each lane's fixture server")
    parser.add_argument("--lane-timeout", type=float, default=DEFAULT_LANE_TIMEOUT_SECONDS)
    parser.add_argument("--results-base", default=None)
    parser.add_argument("--command", default=None, help="test hook: replace the Playwright command (shlex string)")
    parser.add_argument("extra", nargs="*", help="extra arguments after -- are passed to `playwright test`")
    args = parser.parse_args(argv)

    try:
        concurrency = parse_concurrency(args.concurrency if args.concurrency is not None else os.environ.get(CONCURRENCY_ENV))
        projects = resolve_projects(args.projects)
    except MatrixConfigError as exc:
        print(f"playwright-matrix: {exc}", file=sys.stderr)
        return 2
    if args.lane_timeout <= 0:
        print("playwright-matrix: --lane-timeout must be positive", file=sys.stderr)
        return 2

    cancel = threading.Event()

    def on_signal(signum: int, _frame: Any) -> None:
        cancel.set()

    previous = {sig: signal.signal(sig, on_signal) for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    try:
        result = run_matrix(
            projects=projects, concurrency=concurrency, python=args.python, extra_args=args.extra,
            command_override=shlex.split(args.command) if args.command else None,
            lane_timeout=args.lane_timeout,
            results_base=Path(args.results_base) if args.results_base else None,
            cancel=cancel,
        )
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    print(format_summary(result))
    if result.interrupted:
        return 130
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
