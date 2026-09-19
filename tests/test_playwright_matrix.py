"""OCTAREL-TEST-01: isolated bounded-parallel Control Center Playwright matrix runner.

Deterministic: every lane runs a tiny fake command (never a browser, provider,
network service, or the live Octarel daemon), so lane isolation, bounded
concurrency, failure propagation and process cleanup are proven directly.
"""

from __future__ import annotations

import json
import os
import re
import signal
import socket
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from scripts.ci import playwright_matrix as pm
from scripts.ci.change_risk import classify
from scripts.ci.local_gate import phase_plan
from scripts.ci.test_impact import TestImpact
from tests.octarel_paths import OCTAREL_ROOT

FAKE_LANE = textwrap.dedent(
    '''
    import json, os, signal, socket, subprocess, sys, time
    from pathlib import Path

    cfg = json.loads(Path(sys.argv[1]).read_text())
    project = os.environ["OCTAREL_MATRIX_PROJECT"]
    mode = cfg.get("modes", {}).get(project, cfg.get("default", "pass"))
    port = int(os.environ["OCTAGES_CONTROL_CENTER_TEST_PORT"])
    fixture = Path(os.environ["OCTAREL_CONTROL_CENTER_FIXTURE_ROOT"])
    tmp = Path(os.environ["TMPDIR"])
    results = Path(os.environ["OCTAREL_PLAYWRIGHT_RESULTS_DIR"])
    rec = Path(cfg["records"]); rec.mkdir(parents=True, exist_ok=True)
    start = time.time()
    (fixture / "sentinel.txt").write_text(project)
    (tmp / "sentinel.txt").write_text(project)
    (results / "artifacts").mkdir(parents=True, exist_ok=True)
    (results / "artifacts" / "trace.zip").write_text("trace-" + project)
    print("STDOUT-MARK", project, flush=True)
    print("STDERR-MARK", project, file=sys.stderr, flush=True)

    def child(code, *args):
        # Detached like Playwright's webServer: its own session, so only the
        # runner's directory-based reaping can find it.
        return subprocess.Popen([sys.executable, "-c", code, *args], start_new_session=True)

    HOLD = ("import socket,sys,time,signal\\n"
            "if 'ignore-term' in sys.argv: signal.signal(signal.SIGTERM, signal.SIG_IGN)\\n"
            "s=socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\\n"
            "s.bind(('127.0.0.1', int(sys.argv[1]))); s.listen(); time.sleep(600)\\n")
    if mode in ("server", "server-ignore-term"):
        extra = ["ignore-term"] if mode.endswith("ignore-term") else []
        child(HOLD, str(port), "--root", str(fixture), *extra)
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close(); break
            except OSError:
                time.sleep(0.05)
    if mode == "browser":
        # Like chromium: a profile directory under the lane's TMPDIR in its argv, plus a grandchild.
        child("import subprocess,sys,time\\nsubprocess.Popen([sys.executable,'-c','import time;time.sleep(600)',sys.argv[1]]);time.sleep(600)",
              "--user-data-dir=" + str(tmp / "playwright_chromiumdev_profile-x"))
        time.sleep(0.3)
    if mode in ("sleep", "server", "browser", "server-ignore-term"):
        (rec / (project + ".started")).write_text(str(time.time()))
        if mode == "sleep" or cfg.get("hold", True):
            time.sleep(cfg.get("hold_seconds", 600) if mode != "pass" else 0)
    if mode == "pass" or mode == "slow":
        time.sleep(cfg.get("slow_seconds", 0.05) if mode == "pass" else cfg.get("slow_seconds", 0.4))
    (rec / (project + ".json")).write_text(json.dumps({
        "project": project, "port": port, "fixture": str(fixture), "tmp": str(tmp),
        "results": str(results), "start": start, "end": time.time(), "cwd": os.getcwd(),
        "env": dict(os.environ)}))
    if mode == "fail":
        print("boom in", project, file=sys.stderr); sys.exit(1)
    (results / "report.json").write_text(json.dumps({"stats": {"expected": 3, "unexpected": 0, "flaky": 0, "skipped": 1}}))
    sys.exit(0)
    '''
)


@pytest.fixture()
def harness(tmp_path: Path):
    script = tmp_path / "fake_lane.py"
    script.write_text(FAKE_LANE, encoding="utf-8")
    records = tmp_path / "records"

    class Harness:
        results_base = tmp_path / "results"
        command = [sys.executable, str(script), str(tmp_path / "cfg.json")]

        def configure(self, **cfg) -> None:
            cfg.setdefault("records", str(records))
            (tmp_path / "cfg.json").write_text(json.dumps(cfg), encoding="utf-8")

        def run(self, **kwargs):
            kwargs.setdefault("lane_timeout", 60)
            return pm.run_matrix(
                root=tmp_path, command_override=self.command, results_base=self.results_base,
                base_env=kwargs.pop("base_env", {"PATH": os.environ["PATH"]}), **kwargs,
            )

        def record(self, project: str) -> dict:
            return json.loads((records / f"{project}.json").read_text())

    h = Harness()
    h.configure()
    return h


def assert_nothing_left(result: pm.MatrixResult) -> None:
    assert pm.processes_under(result.run_dir) == []
    for lane in result.lanes:
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", lane.port), timeout=0.3).close()


# 1 ------------------------------------------------------------ port allocation


def test_each_lane_gets_a_unique_loopback_port(harness):
    result = harness.run(concurrency=3)
    ports = [lane.port for lane in result.lanes]
    assert len(ports) == 7 == len(set(ports)) and all(1024 < port < 65536 for port in ports)
    assert all(harness.record(lane.project)["port"] == lane.port for lane in result.lanes)


# 2, 3 ------------------------------------------------------ directory isolation


def test_lanes_have_isolated_artifact_fixture_and_tmp_directories(harness):
    harness.configure(default="fail")  # failures keep their scratch so isolation is inspectable
    result = harness.run(concurrency=3)
    roots = {lane.project: harness.record(lane.project) for lane in result.lanes}
    for key in ("fixture", "tmp", "results"):
        assert len({r[key] for r in roots.values()}) == 7
    for lane in result.lanes:
        rec = roots[lane.project]
        for key in ("fixture", "tmp", "results"):
            assert Path(rec[key]).is_relative_to(lane.directory)
        # each lane's fixture/tmp holds only its own sentinel: no shared mutable state
        assert (Path(rec["fixture"]) / "sentinel.txt").read_text() == lane.project
        assert (Path(rec["tmp"]) / "sentinel.txt").read_text() == lane.project
    assert len({lane.directory for lane in result.lanes}) == 7


# 4, 5 ------------------------------------------------------------- concurrency


def test_concurrency_one_runs_lanes_strictly_serially(harness):
    harness.configure(default="slow")
    result = harness.run(concurrency=1)
    assert result.ok and result.max_observed_concurrency == 1
    spans = sorted((harness.record(l.project)["start"], harness.record(l.project)["end"]) for l in result.lanes)
    assert all(a_end <= b_start + 0.01 for (_, a_end), (b_start, _) in zip(spans, spans[1:]))


def test_concurrency_three_is_bounded_and_actually_parallel(harness):
    harness.configure(default="slow", slow_seconds=0.6)
    result = harness.run(concurrency=3)
    assert result.ok
    assert result.max_observed_concurrency == 3
    events = sorted(
        [(harness.record(l.project)["start"], 1) for l in result.lanes]
        + [(harness.record(l.project)["end"], -1) for l in result.lanes],
        key=lambda item: (item[0], item[1]),
    )
    running = peak = 0
    for _, delta in events:
        running += delta
        peak = max(peak, running)
    assert 2 <= peak <= 3  # overlapping, but never beyond the bound
    assert result.serial_equivalent_seconds > result.wall_seconds


# 6, 7, 8 --------------------------------------------------------- aggregation


def test_aggregate_success_writes_machine_readable_evidence(harness):
    result = harness.run(concurrency=3)
    assert result.ok and [l.status for l in result.lanes] == [pm.PASS] * 7
    doc = json.loads((result.run_dir / "summary.json").read_text())
    assert doc["aggregate"] == "PASS" and doc["concurrency"] == 3 and len(doc["lanes"]) == 7
    assert all({"project", "result", "port", "duration_seconds", "counts", "artifact_dir"} <= set(l) for l in doc["lanes"])
    assert doc["lanes"][0]["counts"] == {"passed": 3, "failed": 0, "flaky": 0, "skipped": 1}
    text = (result.run_dir / "summary.txt").read_text()
    assert "Total wall time" in text and "Serial-equivalent lane time" in text and "Concurrency: 3" in text


def test_one_failing_lane_fails_the_aggregate_and_others_still_report(harness):
    harness.configure(modes={"tablet-768": "fail"})
    result = harness.run(concurrency=3)
    by = {l.project: l for l in result.lanes}
    assert not result.ok and by["tablet-768"].status == pm.FAIL and by["tablet-768"].returncode == 1
    assert all(l.status == pm.PASS for name, l in by.items() if name != "tablet-768")
    assert json.loads((result.run_dir / "summary.json").read_text())["aggregate"] == "FAIL"


def test_lane_launch_failure_is_never_a_pass(harness):
    result = pm.run_matrix(
        root=harness.results_base.parent, command_override=["/nonexistent/octarel-playwright"],
        results_base=harness.results_base, base_env={"PATH": os.environ["PATH"]}, concurrency=2,
    )
    assert not result.ok and {l.status for l in result.lanes} == {pm.LAUNCH_FAILED}
    assert all("could not launch" in l.detail for l in result.lanes)


def test_exit_zero_without_a_report_is_not_a_pass_for_the_real_command(tmp_path):
    lane = pm.Lane(project="desktop-1440", port=1, directory=tmp_path / "lane")
    done = pm.run_lane(
        lane, command=[sys.executable, "-c", "pass"], python=sys.executable, timeout=30,
        cancel=threading.Event(), require_report=True, base_env={"PATH": os.environ["PATH"]},
    )
    assert done.status == pm.FAIL and "no Playwright report" in done.detail


# 9 ------------------------------------------------------------------ timeout


def test_lane_timeout_terminates_the_lane_and_its_children(harness):
    harness.configure(modes={"desktop-1440": "server"}, hold_seconds=600)
    result = harness.run(projects=["desktop-1440", "tablet-768"], concurrency=2, lane_timeout=2.5)
    by = {l.project: l for l in result.lanes}
    assert by["desktop-1440"].status == pm.TIMEOUT and "exceeded" in by["desktop-1440"].detail
    assert by["tablet-768"].status == pm.PASS  # a slow neighbour does not poison the others
    assert not result.ok
    assert_nothing_left(result)


# 10 ---------------------------------------------------------- interruption


def test_cancellation_stops_running_lanes_and_marks_unstarted_lanes_cancelled(harness):
    harness.configure(default="sleep", hold_seconds=600)
    cancel = threading.Event()
    threading.Timer(1.5, cancel.set).start()
    result = harness.run(concurrency=2, cancel=cancel)
    assert result.interrupted and not result.ok
    assert {l.status for l in result.lanes} == {pm.CANCELLED}
    assert PASS_NOT_PRESENT(result)
    assert_nothing_left(result)


def PASS_NOT_PRESENT(result: pm.MatrixResult) -> bool:
    return all(l.status != pm.PASS for l in result.lanes)


def test_real_sigint_terminates_everything_and_exits_130(tmp_path, harness):
    harness.configure(default="server", hold_seconds=600)
    base = tmp_path / "sigint-results"
    proc = subprocess.Popen(
        [sys.executable, str(OCTAREL_ROOT / "scripts/ci/playwright_matrix.py"), "--concurrency", "3",
         "--results-base", str(base), "--command", " ".join(harness.command)],
        cwd=str(OCTAREL_ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
    )
    records = tmp_path / "records"
    deadline = time.time() + 30
    while time.time() < deadline and len(list(records.glob("*.started"))) < 3:
        time.sleep(0.1)
    assert len(list(records.glob("*.started"))) >= 3, "lanes never started"
    run_dir = next(base.iterdir())
    assert pm.processes_under(run_dir), "fixture-like children should be alive before the interrupt"
    os.kill(proc.pid, signal.SIGINT)
    out, _ = proc.communicate(timeout=60)
    assert proc.returncode == 130 and "Aggregate: FAIL (interrupted)" in out
    assert pm.processes_under(run_dir) == []
    summary = json.loads((run_dir / "summary.json").read_text())
    assert summary["interrupted"] and all(l["result"] == pm.CANCELLED for l in summary["lanes"])


# 11, 12 -------------------------------------------------------------- cleanup


def test_fixture_server_left_listening_is_killed_after_a_passing_lane(harness):
    harness.configure(default="server", hold=False)
    result = harness.run(projects=["desktop-1440", "iphone-390"], concurrency=2)
    assert result.ok
    assert all(l.leftover_processes_reaped >= 1 for l in result.lanes)  # the orphan was found and reaped
    assert_nothing_left(result)


def test_browser_and_grandchild_processes_are_reaped_via_lane_tmpdir(harness):
    harness.configure(default="browser", hold=False)
    result = harness.run(projects=["android-430"], concurrency=1)
    assert result.lanes[0].leftover_processes_reaped >= 2  # browser + its grandchild
    assert_nothing_left(result)


def test_processes_ignoring_sigterm_are_escalated_to_sigkill(harness, monkeypatch):
    monkeypatch.setattr(pm, "TERMINATE_GRACE_SECONDS", 0.5)
    harness.configure(default="server-ignore-term", hold=False)
    result = harness.run(projects=["desktop-1280"], concurrency=1)
    assert_nothing_left(result)


# 13 ---------------------------------------------------- stdout/stderr isolation


def test_stdout_and_stderr_are_captured_per_lane(harness):
    harness.configure(modes={"iphone-390": "fail"})
    result = harness.run(concurrency=3)
    for lane in result.lanes:
        out, err = lane.stdout_path.read_text(), lane.stderr_path.read_text()
        assert f"STDOUT-MARK {lane.project}" in out and f"STDERR-MARK {lane.project}" in err
        for other in pm.VIEWPORT_PROJECTS:
            if other != lane.project:
                assert other not in out and other not in err
    assert "boom in iphone-390" in next(l for l in result.lanes if l.project == "iphone-390").stderr_path.read_text()


# 14 --------------------------------------------------- artifact preservation


def test_failed_lane_preserves_evidence_and_passing_lane_drops_scratch_only(harness):
    harness.configure(modes={"desktop-1920": "fail"})
    result = harness.run(projects=["desktop-1920", "desktop-1440"], concurrency=2)
    bad, good = result.lanes
    assert (bad.fixture_root / "sentinel.txt").exists() and (bad.tmp_dir / "sentinel.txt").exists()
    assert (bad.results_dir / "artifacts" / "trace.zip").read_text() == "trace-desktop-1920"
    assert not good.fixture_root.exists() and not good.tmp_dir.exists()
    assert good.stdout_path.exists() and good.report_path.exists()  # logs + report always retained


# 15, 16 ------------------------------------------------------------ validation


@pytest.mark.parametrize("value", ["0", "8", "-1", "abc", "2.5", "99"])
def test_invalid_concurrency_values_are_rejected(value):
    with pytest.raises(pm.MatrixConfigError):
        pm.parse_concurrency(value)
    assert pm.main(["--concurrency", value, "--projects", "desktop-1440"]) == 2


def test_invalid_concurrency_from_environment_is_rejected(monkeypatch):
    monkeypatch.setenv(pm.CONCURRENCY_ENV, "12")
    assert pm.main(["--projects", "desktop-1440"]) == 2


def test_default_and_environment_concurrency(monkeypatch):
    assert pm.DEFAULT_CONCURRENCY == 3 and pm.parse_concurrency(None) == 3
    assert [pm.parse_concurrency(str(n)) for n in (1, 2, 4, 7)] == [1, 2, 4, 7]


def test_duplicate_unknown_and_empty_viewport_selection_fail_and_order_is_canonical():
    with pytest.raises(pm.MatrixConfigError, match="duplicate"):
        pm.resolve_projects("desktop-1440,desktop-1440")
    with pytest.raises(pm.MatrixConfigError, match="unknown"):
        pm.resolve_projects("desktop-1440,watch-100")
    with pytest.raises(pm.MatrixConfigError, match="empty"):
        pm.resolve_projects("desktop-1440,,tablet-768")
    assert pm.resolve_projects(None) == pm.VIEWPORT_PROJECTS
    assert pm.resolve_projects("tablet-768,desktop-1920") == ("desktop-1920", "tablet-768")
    assert pm.main(["--projects", "desktop-1440,desktop-1440"]) == 2


def test_matrix_projects_match_the_playwright_config():
    config = (OCTAREL_ROOT / pm.PLAYWRIGHT_CONFIG).read_text(encoding="utf-8")
    assert tuple(re.findall(r"name: '([a-z0-9-]+)', use:", config)) == pm.VIEWPORT_PROJECTS


# 17, 18 -------------------------------------------- daemon state / no provider


def test_lanes_never_touch_live_state_and_receive_no_secrets_or_provider_env(harness, tmp_path):
    state_dir = OCTAREL_ROOT / ".orchestrator-state"
    before = sorted(p.name for p in state_dir.glob("*")) if state_dir.exists() else None
    secrets = {"ANTHROPIC_API_KEY": "sk-secret", "OPENAI_API_KEY": "sk-secret", "GITHUB_TOKEN": "ghp_secret",
               "GEMINI_API_KEY": "g", "OCTAREL_STATE_ROOT": "/live/state", "PATH": os.environ["PATH"]}
    result = harness.run(projects=["desktop-1440", "tablet-768"], concurrency=2, base_env=secrets)
    for lane in result.lanes:
        env = harness.record(lane.project)["env"]
        assert not [k for k in env if k.endswith(("_API_KEY", "_TOKEN")) or k == "OCTAREL_STATE_ROOT"]
        assert "/live/state" not in json.dumps(env)
        assert env["OCTAREL_CONTROL_CENTER_FIXTURE_ROOT"].startswith(str(result.run_dir))
        assert env["TMPDIR"].startswith(str(result.run_dir))
        assert harness.record(lane.project)["cwd"] == str(OCTAREL_ROOT)
    after = sorted(p.name for p in state_dir.glob("*")) if state_dir.exists() else None
    assert before == after


# lane command shape / gate integration ----------------------------------------


def test_default_lane_command_is_one_project_one_worker():
    argv = pm.default_command("iphone-390", ["control-center"])
    assert argv.count("--workers=1") == 1 and [a for a in argv if a.startswith("--project=")] == ["--project=iphone-390"]
    assert argv[-1] == "control-center"


def test_local_gate_runs_the_full_control_center_matrix_through_the_runner():
    risk = classify(["scripts/agents/control_plane/dashboard/app.js"])
    impact = TestImpact("T2", control_center_ui=True)
    phase = next(p for p in phase_plan(risk, impact, "python3", root=OCTAREL_ROOT) if p.name == "control-center-ui-audit")
    assert phase.argv[:2] == ("python3", "scripts/ci/playwright_matrix.py")
    assert "playwright" not in phase.argv and "--project" not in " ".join(phase.argv)
