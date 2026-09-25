"""ENG-AO-08 (issue #19): dependency reuse fails closed on an incomplete install.

The real VE-BRIDGE-01 recovery reported ``DEPENDENCIES_REUSED`` for a ``node_modules`` whose lockfile
fingerprint matched but whose ``.bin``/``tsc``/``vite`` were gone. Deterministic and offline: ``npm ci`` is
replaced by a fake installer that lays down (or deliberately breaks) a tree derived from a fixture
``package.json``; no real ``node_modules`` is ever touched.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from scripts.ci import environment as env
from scripts.ci import local_gate
from scripts.ci.change_risk import ChangeRisk
from scripts.ci.test_impact import TestImpact

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGES = {"typescript": {"tsc": "bin/tsc"}, "vite": {"vite": "bin/vite.js"}, "react": {}}


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=Test", *args], cwd=root, check=True,
        capture_output=True,
    )


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    """A managed-project checkout with a frontend/v2-style locked Node project (tracked, committed)."""

    root = tmp_path / "managed"
    v2 = root / "frontend" / "v2"
    v2.mkdir(parents=True)
    _git(root, "init", "-q", "-b", "main")
    (v2 / "package.json").write_text(
        json.dumps({"name": "v2", "devDependencies": {"typescript": "1", "vite": "1"}, "dependencies": {"react": "1"}}),
        encoding="utf-8",
    )
    (v2 / "package-lock.json").write_text('{"lockfileVersion": 3}\n', encoding="utf-8")
    (root / "package.json").write_text('{"name": "root"}\n', encoding="utf-8")
    (root / "package-lock.json").write_text('{"lockfileVersion": 3}\n', encoding="utf-8")
    (root / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed")
    return root


def lay_down_install(dependency_root: Path) -> None:
    """What a complete ``npm ci`` leaves: packages, their linked bins, and npm's completed-install record."""

    modules = dependency_root / "node_modules"
    shutil.rmtree(modules, ignore_errors=True)
    (modules / ".bin").mkdir(parents=True)
    try:
        declared = json.loads((dependency_root / "package.json").read_text(encoding="utf-8"))
    except OSError:
        declared = {}
    names = {*declared.get("dependencies", {}), *declared.get("devDependencies", {})}
    for name in names:
        package = modules / name
        package.mkdir(parents=True)
        bins = PACKAGES.get(name, {})
        (package / "package.json").write_text(json.dumps({"name": name, "bin": bins or None}), encoding="utf-8")
        for exe, target in bins.items():
            (package / target).parent.mkdir(parents=True, exist_ok=True)
            (package / target).write_text("#!/usr/bin/env node\n", encoding="utf-8")
            (modules / ".bin" / exe).symlink_to(Path("..") / name / target)
    (modules / ".package-lock.json").write_text("{}", encoding="utf-8")


class FakeNpm:
    """Replaces ``environment._run``: counts ``npm ci`` calls and can misbehave on demand."""

    def __init__(self) -> None:
        self.installs = 0
        self.delay = 0.0
        self.interrupt = False  # leave a half-written tree and fail
        self.touch_source: Path | None = None
        self._guard = threading.Lock()
        self.active = 0
        self.max_active = 0

    def __call__(self, argv, root, timeout=10):
        if argv[:2] != ["npm", "ci"]:
            return True, "ok"
        with self._guard:
            self.installs += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(self.delay)
            if self.interrupt:
                shutil.rmtree(root / "node_modules", ignore_errors=True)
                (root / "node_modules" / "react").mkdir(parents=True)  # no .bin, no tsc/vite, no record
                return False, "npm ci interrupted"
            lay_down_install(root)
            if self.touch_source is not None and root == self.touch_source.parent:
                self.touch_source.write_text("mutated by install\n", encoding="utf-8")
            return True, "ok"
        finally:
            with self._guard:
                self.active -= 1


@pytest.fixture()
def npm(monkeypatch) -> FakeNpm:
    fake = FakeNpm()
    monkeypatch.setattr(env, "_run", fake)
    return fake


def v2_of(result: dict) -> dict:
    return next(p for p in result["dependencies"] if p["path"] == "frontend/v2")


def bootstrap(project: Path) -> dict:
    return env.bootstrap_dependencies(project, require_frontend=True, require_browser=False)


def seed_healthy(project: Path) -> None:
    for root in (project, project / "frontend" / "v2"):
        lay_down_install(root)
        env._store_fingerprint(root, env._lockfile_fingerprint(root))


# 1 + 10 -------------------------------------------------------------------- healthy cache is reused, fast


def test_matching_fingerprint_and_healthy_install_is_reused_without_installing(project, npm):
    seed_healthy(project)
    result = bootstrap(project)

    assert result["dependencies_status"] == env.DEPENDENCIES_REUSED
    assert npm.installs == 0  # the optimization is unchanged: no npm at all
    entry = v2_of(result)
    assert entry["install_state"] == "reused_healthy" and entry["health"] == {"ok": True, "problems": []}


# 2 ------------------------------------------------------------------------- missing node_modules/.bin


def test_matching_fingerprint_with_missing_bin_dir_is_not_reused(project, npm):
    seed_healthy(project)
    shutil.rmtree(project / "frontend" / "v2" / "node_modules" / ".bin")

    result = bootstrap(project)

    entry = v2_of(result)
    assert entry["status"] == env.DEPENDENCIES_INSTALLED and npm.installs == 1
    assert entry["reuse_rejected"] is True and entry["install_state"] == "installed"
    assert "node_modules/.bin is missing" in entry["reason"]
    assert env.dependency_health(project / "frontend" / "v2") == []


# 3 ------------------------------------------------------------- required tool/package missing (tsc, vite)


@pytest.mark.parametrize("package", ["typescript", "vite"])
def test_matching_fingerprint_with_missing_declared_package_is_not_reused(project, npm, package):
    seed_healthy(project)
    shutil.rmtree(project / "frontend" / "v2" / "node_modules" / package)  # leaves a dangling .bin link

    result = bootstrap(project)

    entry = v2_of(result)
    assert entry["status"] == env.DEPENDENCIES_INSTALLED and npm.installs == 1
    assert f"declared package {package} is not installed" in entry["reason"]


def test_dangling_bin_link_alone_is_detected(project):
    seed_healthy(project)
    v2 = project / "frontend" / "v2"
    (v2 / "node_modules" / "vite" / "bin" / "vite.js").unlink()

    assert env.dependency_health(v2) == ["executable vite declared by vite is missing from node_modules/.bin"]


# 4 ------------------------------------------------------------------------ interrupted / partial install


def test_marker_present_but_install_record_missing_is_treated_as_interrupted(project, npm):
    seed_healthy(project)
    (project / "frontend" / "v2" / "node_modules" / ".package-lock.json").unlink()

    entry = v2_of(bootstrap(project))

    assert entry["status"] == env.DEPENDENCIES_INSTALLED
    assert "interrupted install" in entry["reason"]


def test_interrupted_reinstall_fails_closed_and_never_leaves_a_fingerprint(project, npm):
    seed_healthy(project)
    shutil.rmtree(project / "frontend" / "v2" / "node_modules" / ".bin")
    npm.interrupt = True

    result = bootstrap(project)

    entry = v2_of(result)
    assert result["dependencies_status"] == env.DEPENDENCIES_FAILED
    assert entry["status"] == env.DEPENDENCIES_FAILED and entry["install_state"] == "install_failed"
    assert env._stored_fingerprint(project / "frontend" / "v2") is None
    npm.interrupt = False
    assert bootstrap(project)["dependencies_status"] == env.DEPENDENCIES_INSTALLED  # never mis-reused


def test_install_that_still_leaves_an_incomplete_tree_is_a_failure(project, monkeypatch):
    def broken_installer(argv, root, timeout=10):
        if argv[:2] == ["npm", "ci"]:
            lay_down_install(root)
            if root.name == "v2":
                shutil.rmtree(root / "node_modules" / "typescript")
        return True, "ok"

    monkeypatch.setattr(env, "_run", broken_installer)
    entry = v2_of(bootstrap(project))

    assert entry["status"] == env.DEPENDENCIES_FAILED
    assert "still incomplete" in entry["detail"] and entry["health"]["problems_after"]


# 5 + 6 --------------------------------------------------------- reason recorded; repair is reusable after


def test_rejection_reason_is_recorded_and_the_repair_is_reused_next_time(project, npm):
    seed_healthy(project)
    shutil.rmtree(project / "frontend" / "v2" / "node_modules" / "vite")

    first = v2_of(bootstrap(project))
    assert first["reason"].startswith("reuse rejected: lockfile fingerprint matched but the install is incomplete")
    assert first["health"]["problems_before"] and first["health"]["problems_after"] == []
    assert npm.installs == 1

    second = bootstrap(project)
    assert second["dependencies_status"] == env.DEPENDENCIES_REUSED and npm.installs == 1


def test_missing_and_stale_fingerprint_states_are_distinguished(project, npm):
    assert v2_of(bootstrap(project))["reason"] == "no node_modules present"
    env._store_fingerprint(project / "frontend" / "v2", "0" * 64)
    reason = v2_of(bootstrap(project))["reason"]
    assert reason.startswith("node_modules fingerprint '000") and "does not match" in reason


# 7 ---------------------------------------------------------------------- concurrent checks never race


def test_concurrent_bootstraps_perform_exactly_one_install(project, npm):
    seed_healthy(project)
    shutil.rmtree(project / "frontend" / "v2" / "node_modules" / ".bin")
    npm.delay = 0.4
    barrier = threading.Barrier(3)
    results: list[dict] = []

    def run() -> None:
        barrier.wait()
        results.append(bootstrap(project))

    threads = [threading.Thread(target=run) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert npm.installs == 1 and npm.max_active == 1
    states = sorted(v2_of(r)["install_state"] for r in results)
    assert states == ["installed", "reused_after_concurrent_repair", "reused_after_concurrent_repair"]
    assert all(r["dependencies_status"] != env.DEPENDENCIES_FAILED for r in results)


def test_install_lock_is_released_after_a_crashed_holder(project):
    holder = subprocess.Popen(
        ["python3", "-c",
         "import sys,time\n"
         "from pathlib import Path\n"
         "from scripts.ci.environment import _install_lock\n"
         "with _install_lock(Path(sys.argv[1])):\n"
         "    print('READY', flush=True); time.sleep(600)\n", str(project / "frontend" / "v2")],
        cwd=str(REPO_ROOT), stdout=subprocess.PIPE, text=True,
    )
    assert holder.stdout.readline().strip() == "READY"
    holder.kill()  # SIGKILL: the kernel drops the flock
    holder.wait()
    with env._install_lock(project / "frontend" / "v2"):
        pass


# 8 -------------------------------------------------------------------------- source tree is preserved


def test_repair_leaves_the_tracked_source_tree_unchanged(project, npm):
    seed_healthy(project)
    shutil.rmtree(project / "frontend" / "v2" / "node_modules" / ".bin")
    (project / "frontend" / "v2" / "package.json").write_text(  # an uncommitted edit must survive too
        (project / "frontend" / "v2" / "package.json").read_text(encoding="utf-8").replace("v2", "v2 "), encoding="utf-8",
    )
    before = subprocess.run(["git", "diff", "HEAD", "--binary"], cwd=project, capture_output=True).stdout

    entry = v2_of(bootstrap(project))

    assert entry["status"] == env.DEPENDENCIES_INSTALLED and entry["source_tree_unchanged"] is True
    assert subprocess.run(["git", "diff", "HEAD", "--binary"], cwd=project, capture_output=True).stdout == before


def test_install_that_mutates_tracked_source_is_a_failure(project, npm):
    npm.touch_source = project / "frontend" / "v2" / "package-lock.json"

    entry = v2_of(bootstrap(project))

    assert entry["status"] == env.DEPENDENCIES_FAILED and entry["source_tree_unchanged"] is False
    assert "changed tracked managed-project source files" in entry["detail"]


# 9 -------------------------------------------------------- no gate is skipped because reuse was claimed


def _risk() -> ChangeRisk:
    return ChangeRisk("product", False, True, True, False, test_tier="T1", review_level="low")


def _preflight(project: Path, bootstrap_result: dict) -> dict:
    impact = TestImpact(tier="T1", product_ui=True)
    phase = local_gate.GatePhase("tree-whitespace", ("git", "diff", "--check", "--cached"))
    return local_gate.preflight(
        project, _risk(), ["frontend/v2/x.ts"], "python3", impact=impact, phases=[phase], registry_root=REPO_ROOT,
    )


def test_failed_dependency_repair_fails_gate_preflight_instead_of_being_skipped(project, monkeypatch):
    failed = {"dependencies_status": env.DEPENDENCIES_FAILED, "browser": {}, "dependencies": [
        {"path": "frontend/v2", "status": env.DEPENDENCIES_FAILED, "detail": "npm ci interrupted"}]}
    monkeypatch.setattr(local_gate, "bootstrap_dependencies", lambda *a, **k: failed)

    result = _preflight(project, failed)

    assert result["result"] == "fail"
    assert any("dependency install for frontend/v2 failed: npm ci interrupted" in f for f in result["failures"])
    assert result["environment"]["bootstrap"] is failed


def test_reuse_never_alters_the_selected_gate_phase_plan(project, npm):
    risk, impact = _risk(), TestImpact(tier="T1", product_ui=True)
    plan = [p.name for p in local_gate.phase_plan(risk, impact, "python3", root=project)]
    seed_healthy(project)
    assert bootstrap(project)["dependencies_status"] == env.DEPENDENCIES_REUSED
    assert "frontend-build" in plan
    assert [p.name for p in local_gate.phase_plan(risk, impact, "python3", root=project)] == plan
