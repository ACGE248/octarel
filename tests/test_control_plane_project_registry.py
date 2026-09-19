"""ENG-CP-03 (issue #165): project registry, selection, scoping and migration.

Follows `tests/test_control_plane_project_contract.py` (CPX-01) and
`tests/test_control_plane_dependency_isolation.py` (CPX-02): deterministic,
no network, no provider call, and every "two projects are isolated" claim
proven against a real second Git repository created in a temp directory rather
than a mock.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scripts.agents.control_plane import project_registry as pr
from scripts.agents.control_plane.commands import CommandContext
from scripts.agents.control_plane.dashboard_api import create_app
from scripts.agents.control_plane.models import Runbook, Task, WorktreeRecord
from scripts.agents.control_plane.octascene_project import OCTASCENE_PROJECT_ID
from scripts.agents.control_plane.project import ProjectRootError, cp_code_root
from scripts.agents.control_plane.quickstart import (
    VIDEO_EDITOR_COMMANDS_RELATIVE,
    VIDEO_EDITOR_LEDGER_RELATIVE,
)
from scripts.agents.control_plane.scheduler import Scheduler
from scripts.agents.control_plane.state import CURRENT_SCHEMA_VERSION, State
from scripts.agents.control_plane.supervisor import Supervisor
from scripts.agents.registry import load_registry


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=Test", *args],
        cwd=str(root),
        check=True,
        capture_output=True,
    )


def make_repo(root: Path, *, branch: str = "main", files: dict[str, str] | None = None) -> Path:
    """A real, minimal Git repository -- never a mock or a bare directory."""

    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", branch)
    for name, body in (files or {"README.md": "# repo\n"}).items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    return root


@pytest.fixture()
def project_a(tmp_path: Path) -> Path:
    return make_repo(
        tmp_path / "alpha",
        branch="main",
        files={"AGENTS.md": "# alpha policy\n", "docs/ROADMAP.md": "# alpha roadmap\n", "Makefile": "test:\n\ttrue\n"},
    )


@pytest.fixture()
def project_b(tmp_path: Path) -> Path:
    """A deliberately generic repository: no AGENTS.md, no OctaScene shape."""

    return make_repo(tmp_path / "beta", branch="trunk", files={"TASKS.md": "- [ ] one\n"})


@pytest.fixture()
def octascene_repo(tmp_path: Path) -> Path:
    """An OctaScene-shaped checkout. Never the Octarel code root."""

    return make_repo(
        tmp_path / "octascene",
        files={
            "AGENTS.md": "# OctaScene\n",
            "docs/PRODUCT_ROADMAP.md": "# roadmap\n",
            str(VIDEO_EDITOR_LEDGER_RELATIVE): "# ledger\n",
            VIDEO_EDITOR_COMMANDS_RELATIVE: "# commands\n",
        },
    )


@pytest.fixture()
def state(tmp_path: Path) -> State:
    return State(tmp_path / "state" / "cp.db")


def register(state: State, project_id: str, root: Path, **extra) -> object:
    payload = {"project_id": project_id, "display_name": project_id.title(), "local_repo_root": str(root)}
    payload.update(extra)
    return pr.register_project(state, payload)


# --------------------------------------------------------------- registration


def test_registers_a_project_and_reads_it_back(state: State, project_a: Path) -> None:
    contract = register(state, "alpha", project_a, github_remote="acme/alpha", default_branch="main")
    assert contract.project_id == "alpha"
    assert contract.local_repo_root.resolve() == project_a.resolve()

    stored = pr.get_project(state, "alpha")
    assert stored.github_remote == "acme/alpha"
    assert stored.local_repo_root.resolve() == project_a.resolve()


def test_generic_repository_with_only_git_metadata_is_registrable(state: State, project_b: Path) -> None:
    """Spec 6: "a generic repository with only Git metadata must still be registrable"."""

    contract = register(state, "beta", project_b)
    assert contract.policy_entrypoints == ()
    assert contract.roadmap_paths == ()
    assert contract.validation_command == ()
    assert pr.validate_project(contract)["ok"] is True


def test_duplicate_project_id_is_rejected(state: State, project_a: Path) -> None:
    register(state, "alpha", project_a)
    with pytest.raises(pr.DuplicateProjectError) as exc:
        register(state, "alpha", project_a)
    assert "already registered" in str(exc.value)


def test_duplicate_repository_root_under_a_new_id_is_rejected(state: State, project_a: Path) -> None:
    register(state, "alpha", project_a)
    with pytest.raises(pr.DuplicateProjectError) as exc:
        register(state, "alpha-again", project_a)
    # The error must name the project that already owns the repository so the
    # operator can act on it, not just say "duplicate".
    assert "alpha" in str(exc.value)


def test_nonexistent_and_non_git_directories_are_rejected(state: State, tmp_path: Path) -> None:
    with pytest.raises(ProjectRootError):
        register(state, "ghost", tmp_path / "does-not-exist")
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(ProjectRootError):
        register(state, "plain", plain)


def test_invalid_project_ids_are_rejected(state: State, project_a: Path) -> None:
    for bad in ("Has Spaces", "UPPER", "", "-leading", "x" * 65, "path/traversal"):
        with pytest.raises(pr.ProjectConfigError):
            register(state, bad, project_a)


@pytest.mark.parametrize("field", ["policy_entrypoints", "roadmap_paths", "task_sources"])
@pytest.mark.parametrize("bad", ["../escape.md", "/etc/passwd", "docs/../../escape.md"])
def test_paths_outside_the_repository_are_rejected(state: State, project_a: Path, field: str, bad: str) -> None:
    """Spec 7: policy paths must remain inside repository boundaries."""

    with pytest.raises(pr.ProjectConfigError) as exc:
        register(state, "alpha", project_a, **{field: [bad]})
    assert field in str(exc.value)


def test_symlink_escape_from_repository_root_is_rejected(state: State, project_a: Path, tmp_path: Path) -> None:
    secret_dir = tmp_path / "outside"
    secret_dir.mkdir()
    (secret_dir / "secret.md").write_text("top secret\n", encoding="utf-8")
    (project_a / "escape").symlink_to(secret_dir)
    with pytest.raises(pr.ProjectConfigError):
        register(state, "alpha", project_a, roadmap_paths=["escape/secret.md"])


def test_validation_command_must_be_argv_not_a_shell_string(state: State, project_a: Path) -> None:
    with pytest.raises(pr.ProjectConfigError) as exc:
        register(state, "alpha", project_a, validation_command="make test && rm -rf /")
    assert "argv" in str(exc.value)

    contract = register(state, "alpha", project_a, validation_command=["make", "test"])
    assert contract.validation_command == ("make", "test")


def test_invalid_github_remote_is_rejected_with_an_actionable_message(state: State, project_a: Path) -> None:
    with pytest.raises(pr.ProjectConfigError) as exc:
        register(state, "alpha", project_a, github_remote="https://github.com/acme/alpha.git")
    assert "owner/repo" in str(exc.value)


# ------------------------------------------------------------------ detection


def test_detection_reports_generic_git_facts_without_assuming_octascene(project_b: Path) -> None:
    detected = pr.detect_project(project_b)
    assert detected["default_branch"] == "trunk"
    assert detected["task_sources"] == ["TASKS.md"]
    assert detected["policy_entrypoints"] == []
    assert detected["github_remote"] is None
    assert detected["suggested_project_id"] == "beta"


def test_detection_finds_policy_roadmap_and_validation_candidates(project_a: Path) -> None:
    detected = pr.detect_project(project_a)
    assert "AGENTS.md" in detected["policy_entrypoints"]
    assert "docs/ROADMAP.md" in detected["roadmap_paths"]
    assert detected["validation_command"] == ["make", "test"]


def test_detection_rejects_a_non_git_directory(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(ProjectRootError):
        pr.detect_project(plain)


# ------------------------------------------------------------------ selection


def test_select_and_read_back_the_selected_project(state: State, project_a: Path, project_b: Path) -> None:
    register(state, "alpha", project_a)
    register(state, "beta", project_b)
    pr.select_project(state, "beta")
    assert pr.selected_project(state).project_id == "beta"
    pr.select_project(state, "alpha")
    assert pr.selected_project(state).project_id == "alpha"


def test_selecting_an_unknown_or_disabled_project_fails(state: State, project_a: Path) -> None:
    with pytest.raises(pr.UnknownProjectError):
        pr.select_project(state, "nope")
    register(state, "alpha", project_a)
    pr.set_project_enabled(state, "alpha", False)
    with pytest.raises(pr.ProjectConfigError) as exc:
        pr.select_project(state, "alpha")
    assert "disabled" in str(exc.value)


def test_selection_persists_across_a_restart(tmp_path: Path, project_a: Path, project_b: Path) -> None:
    """Spec 12: persistence across restart -- a new State over the same file."""

    db = tmp_path / "state" / "cp.db"
    first = State(db)
    register(first, "alpha", project_a)
    register(first, "beta", project_b)
    pr.select_project(first, "beta")
    first.close()

    second = State(db)
    assert pr.selected_project(second).project_id == "beta"
    assert {p["project_id"] for p in pr.list_projects(second)} == {"alpha", "beta"}
    second.close()


def test_selection_never_mutates_either_repository(state: State, project_a: Path, project_b: Path) -> None:
    """Spec 9: switching projects does not mutate either repository."""

    def snapshot(root: Path) -> tuple[str, str]:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(root), capture_output=True, text=True, check=True
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=str(root), capture_output=True, text=True, check=True
        ).stdout
        return head, status

    register(state, "alpha", project_a)
    register(state, "beta", project_b)
    before = (snapshot(project_a), snapshot(project_b))
    pr.select_project(state, "beta")
    pr.select_project(state, "alpha")
    pr.select_project(state, "beta")
    assert (snapshot(project_a), snapshot(project_b)) == before


# ----------------------------------------------------------------- edit/remove


def test_editing_configuration_keeps_identity_and_history(state: State, project_a: Path) -> None:
    register(state, "alpha", project_a)
    updated = pr.update_project(state, "alpha", {"display_name": "Alpha Renamed", "github_remote": "acme/alpha"})
    assert updated.project_id == "alpha"
    assert updated.display_name == "Alpha Renamed"
    assert pr.get_project(state, "alpha").github_remote == "acme/alpha"


def test_project_id_is_immutable(state: State, project_a: Path) -> None:
    register(state, "alpha", project_a)
    with pytest.raises(pr.ProjectConfigError) as exc:
        pr.update_project(state, "alpha", {"project_id": "renamed"})
    assert "immutable" in str(exc.value)


def test_removal_requires_explicit_confirmation(state: State, project_a: Path, project_b: Path) -> None:
    register(state, "alpha", project_a)
    register(state, "beta", project_b)
    with pytest.raises(pr.ProjectConfigError) as exc:
        pr.remove_project(state, "beta")
    assert "confirmation" in str(exc.value)
    assert pr.get_project(state, "beta") is not None


def test_removal_preserves_repository_and_control_plane_history(
    state: State, project_a: Path, project_b: Path
) -> None:
    """Spec 2: removal must not delete the repository or destroy CP evidence."""

    register(state, "alpha", project_a)
    register(state, "beta", project_b)
    state.upsert_task(Task(id="b1", task_ref="B-1", role="focused-tests", worker="w", project_id="beta"))
    state.record_event(category="test", message="beta history", project_id="beta")

    before_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(project_b), capture_output=True, text=True, check=True
    ).stdout
    result = pr.remove_project(state, "beta", confirm=True)

    assert project_b.exists() and (project_b / ".git").exists()
    after_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(project_b), capture_output=True, text=True, check=True
    ).stdout
    assert after_head == before_head
    assert (project_b / "TASKS.md").exists()

    # Historical CP evidence survives de-registration.
    assert result["preserved_records"]["tasks"] >= 1
    assert state.get_task("b1") is not None
    assert any(e.message == "beta history" for e in state.list_events(project_id="beta"))


def test_removing_the_only_selected_project_is_refused(state: State, project_a: Path) -> None:
    register(state, "alpha", project_a)
    pr.select_project(state, "alpha")
    with pytest.raises(pr.ProjectConfigError) as exc:
        pr.remove_project(state, "alpha", confirm=True)
    assert "only registered project" in str(exc.value)


def test_removing_the_selected_project_reselects_another(state: State, project_a: Path, project_b: Path) -> None:
    register(state, "alpha", project_a)
    register(state, "beta", project_b)
    pr.select_project(state, "beta")
    pr.remove_project(state, "beta", confirm=True)
    assert pr.selected_project(state).project_id == "alpha"


def test_disable_hides_a_project_from_selection_without_deleting_it(state: State, project_a: Path) -> None:
    register(state, "alpha", project_a)
    pr.set_project_enabled(state, "alpha", False)
    assert [p["project_id"] for p in pr.list_projects(state, enabled_only=True)] == []
    assert [p["project_id"] for p in pr.list_projects(state)] == ["alpha"]
    pr.set_project_enabled(state, "alpha", True)
    assert pr.selected_project(state).project_id == "alpha"


# ------------------------------------------------------------------ migration


def test_octascene_is_auto_registered_from_its_own_adapter(state: State, octascene_repo: Path) -> None:
    """Spec 3: an existing installation recognizes OctaScene without manual setup."""

    contract = pr.ensure_octascene_project(state, octascene_repo)
    assert contract is not None
    assert contract.project_id == OCTASCENE_PROJECT_ID
    assert contract.github_remote == "ACGE248/octages"
    # Declarations come from the CPX-01 adapter, not restated in the registry.
    assert "AGENTS.md" in contract.policy_entrypoints
    assert contract.validation_command[:2] == ("python3", "scripts/ci/local_gate.py")


def test_legacy_single_project_state_is_adopted_by_octascene(tmp_path: Path, octascene_repo: Path) -> None:
    """Spec 10: existing records are preserved and remain visible after migration."""

    db = tmp_path / "state" / "cp.db"
    legacy = State(db)
    legacy.upsert_task(Task(id="old1", task_ref="V1-07", role="primary-implementation", worker="claude-code"))
    legacy.record_event(category="command", message="legacy event")
    legacy.upsert_worktree(WorktreeRecord(path=str(tmp_path / "legacy-wt"), branch="legacy"))
    legacy.set_control_setting("repository_health_cache", json.dumps({"legacy": True}))
    legacy.close()

    reopened = State(db)
    result = pr.bootstrap_registry(reopened, octascene_repo)
    assert result["octascene_project_id"] == OCTASCENE_PROJECT_ID
    assert result["selected_project_id"] == OCTASCENE_PROJECT_ID

    # Every legacy record is now visible *under OctaScene*, not lost.
    assert [t.id for t in reopened.list_tasks(project_id=OCTASCENE_PROJECT_ID)] == ["old1"]
    assert any(e.message == "legacy event" for e in reopened.list_events(project_id=OCTASCENE_PROJECT_ID))
    assert [w.branch for w in reopened.list_worktrees(project_id=OCTASCENE_PROJECT_ID)] == ["legacy"]
    moved = reopened.get_project_setting("repository_health_cache", project_id=OCTASCENE_PROJECT_ID)
    assert json.loads(moved) == {"legacy": True}
    reopened.close()


def test_migration_is_idempotent_and_restart_safe(tmp_path: Path, octascene_repo: Path) -> None:
    db = tmp_path / "state" / "cp.db"
    first = State(db)
    first.upsert_task(Task(id="old1", task_ref="V1-07", role="r", worker="w"))
    first.close()

    results = []
    for _ in range(3):
        st = State(db)
        results.append(pr.bootstrap_registry(st, octascene_repo))
        st.close()

    final = State(db)
    # Exactly one OctaScene project, one copy of the task, deterministic outcome.
    assert [p["project_id"] for p in pr.list_projects(final)] == [OCTASCENE_PROJECT_ID]
    assert [t.id for t in final.list_tasks(project_id=OCTASCENE_PROJECT_ID)] == ["old1"]
    # The first run performs the one-time adoption; later runs skip migration
    # entirely (gated on the persisted schema version) rather than re-running it
    # and risking re-attributing rows written since.
    assert sum(results[0]["migration"]["adopted_rows"].values()) == 1
    assert results[1]["migration"] is None
    assert results[2]["migration"] is None
    # Selection is still resolved deterministically on every start.
    assert {r["selected_project_id"] for r in results} == {OCTASCENE_PROJECT_ID}
    assert final.schema_version() == CURRENT_SCHEMA_VERSION
    final.close()


def test_migration_does_not_overwrite_operator_edits_to_octascene(state: State, octascene_repo: Path) -> None:
    pr.ensure_octascene_project(state, octascene_repo)
    pr.update_project(state, OCTASCENE_PROJECT_ID, {"display_name": "OctaScene (edited)"})
    pr.ensure_octascene_project(state, octascene_repo)
    assert pr.get_project(state, OCTASCENE_PROJECT_ID).display_name == "OctaScene (edited)"


def test_schema_version_is_tracked(tmp_path: Path, octascene_repo: Path) -> None:
    db = tmp_path / "state" / "cp.db"
    fresh = State(db)
    assert fresh.schema_version() == 1  # implicit pre-ENG-CP-03 version
    pr.bootstrap_registry(fresh, octascene_repo)
    assert fresh.schema_version() == CURRENT_SCHEMA_VERSION
    fresh.close()


def test_a_failed_migration_does_not_partially_corrupt_state(tmp_path: Path, monkeypatch) -> None:
    """Spec 10: migration failure must not leave a half-migrated database."""

    db = tmp_path / "state" / "cp.db"
    seed = State(db)
    for i in range(3):
        seed.upsert_task(Task(id=f"t{i}", task_ref=f"T-{i}", role="r", worker="w"))
    seed.close()

    import sqlite3

    reopened = State(db)

    class FlakyConnection:
        """Delegates to the real connection but fails part-way through the migration."""

        def __init__(self, inner: sqlite3.Connection) -> None:
            self._inner = inner
            self._updates = 0

        def execute(self, sql, *args, **kwargs):
            if sql.startswith("UPDATE ") and " SET project_id" in sql:
                self._updates += 1
                if self._updates == 2:
                    raise sqlite3.OperationalError("simulated failure mid-migration")
            return self._inner.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    real_conn = reopened._conn
    reopened._conn = FlakyConnection(real_conn)
    with pytest.raises(sqlite3.OperationalError):
        reopened.adopt_unscoped_rows("octascene")
    reopened._conn = real_conn

    # All-or-nothing: no row was adopted, so nothing is half-migrated and a
    # later successful run adopts every row.
    assert reopened.list_tasks(project_id="octascene") == []
    adopted = reopened.adopt_unscoped_rows("octascene")
    assert adopted["tasks"] == 3
    reopened.close()


# ------------------------------------------------------------ project scoping


def test_history_is_filtered_per_project(state: State, project_a: Path, project_b: Path) -> None:
    register(state, "alpha", project_a)
    register(state, "beta", project_b)
    state.record_event(category="test", message="alpha only", project_id="alpha")
    state.record_event(category="test", message="beta only", project_id="beta")

    alpha_messages = [e.message for e in state.list_events(project_id="alpha")]
    beta_messages = [e.message for e in state.list_events(project_id="beta")]
    assert "alpha only" in alpha_messages and "beta only" not in alpha_messages
    assert "beta only" in beta_messages and "alpha only" not in beta_messages


def test_tasks_runbooks_and_worktrees_are_filtered_per_project(state: State) -> None:
    state.upsert_task(Task(id="a1", task_ref="A-1", role="r", worker="w", project_id="alpha"))
    state.upsert_task(Task(id="b1", task_ref="B-1", role="r", worker="w", project_id="beta"))
    state.upsert_worktree(WorktreeRecord(path="/tmp/alpha-wt", branch="a", project_id="alpha"))
    state.upsert_worktree(WorktreeRecord(path="/tmp/beta-wt", branch="b", project_id="beta"))

    assert [t.id for t in state.list_tasks(project_id="alpha")] == ["a1"]
    assert [t.id for t in state.list_tasks(project_id="beta")] == ["b1"]
    assert [w.branch for w in state.list_worktrees(project_id="alpha")] == ["a"]
    assert [w.branch for w in state.list_worktrees(project_id="beta")] == ["b"]


def test_acceptance_state_is_project_scoped(state: State) -> None:
    """Spec 4/12: acceptance state travels with its runbook's project."""

    state.upsert_runbook(
        Runbook(
            id="rb-a", name="Alpha run", preset="overnight-development", objective="o", source_ref="A-1",
            branch="a", worktree="/tmp/a", parent_worker="claude-code", max_duration_minutes=60,
            acceptance_stage="REVIEW", acceptance_evidence={"REVIEW": {"status": "PASS"}}, project_id="alpha",
        )
    )
    state.upsert_runbook(
        Runbook(
            id="rb-b", name="Beta run", preset="overnight-development", objective="o", source_ref="B-1",
            branch="b", worktree="/tmp/b", parent_worker="claude-code", max_duration_minutes=60,
            acceptance_stage="PENDING", project_id="beta",
        )
    )

    alpha = state.list_runbooks(project_id="alpha")
    assert [r.id for r in alpha] == ["rb-a"]
    assert alpha[0].acceptance_stage == "REVIEW"
    assert alpha[0].acceptance_evidence == {"REVIEW": {"status": "PASS"}}
    assert [r.id for r in state.list_runbooks(project_id="beta")] == ["rb-b"]


def test_refreshing_one_project_does_not_erase_another_projects_worktrees(state: State) -> None:
    """A whole-table worktree snapshot replace would silently destroy Project B."""

    state.upsert_worktree(WorktreeRecord(path="/tmp/beta-wt", branch="b", project_id="beta"))
    state.replace_worktrees(
        [WorktreeRecord(path="/tmp/alpha-wt", branch="a")], project_id="alpha"
    )
    assert [w.path for w in state.list_worktrees(project_id="beta")] == ["/tmp/beta-wt"]
    assert [w.path for w in state.list_worktrees(project_id="alpha")] == ["/tmp/alpha-wt"]


# ------------------------------------- independent-review regression coverage
# Every case below covers a finding raised by the independent (non-Claude,
# xAI/Grok) review of this slice. They exist so those specific defects cannot
# silently return.


def test_startup_recovery_does_not_destroy_another_projects_worktrees(
    tmp_path: Path, project_a: Path, project_b: Path
) -> None:
    """Blocker (independent review): unscoped recovery wiped the whole table.

    ``run_recovery`` observes exactly one repository. Before this was scoped,
    every daemon start ran ``DELETE FROM worktrees`` and re-inserted the
    observed rows with a NULL project -- destroying a second project's rows and
    hiding the just-migrated ones from every project-scoped read.
    """

    from scripts.agents.control_plane.recovery import run_recovery

    st = State(tmp_path / "rec" / "cp.db")
    register(st, "alpha", project_a)
    register(st, "beta", project_b)
    st.upsert_worktree(WorktreeRecord(path=str(project_b), branch="trunk", project_id="beta"))

    run_recovery(st, project_a, project_id="alpha")

    # Project B's row survives a recovery pass aimed at Project A...
    assert [w.path for w in st.list_worktrees(project_id="beta")] == [str(project_b)]
    # ...and Project A's freshly observed rows are attributed to Project A,
    # not left NULL and therefore invisible.
    alpha_rows = st.list_worktrees(project_id="alpha")
    assert alpha_rows
    assert all(w.project_id == "alpha" for w in alpha_rows)
    assert Path(project_a).resolve() in {Path(w.path).resolve() for w in alpha_rows}
    st.close()


def test_recovery_leaves_migrated_records_visible_to_scoped_reads(tmp_path: Path, octascene_repo: Path) -> None:
    """Bootstrap-then-recover must not undo the migration it just performed."""

    from scripts.agents.control_plane.recovery import run_recovery

    db = tmp_path / "rec2" / "cp.db"
    legacy = State(db)
    legacy.upsert_worktree(WorktreeRecord(path=str(octascene_repo), branch="main"))
    legacy.close()

    st = State(db)
    pr.bootstrap_registry(st, octascene_repo)
    run_recovery(st, octascene_repo, project_id=OCTASCENE_PROJECT_ID)
    rows = st.list_worktrees(project_id=OCTASCENE_PROJECT_ID)
    assert rows, "recovery must not strip project_id off the migrated worktree rows"
    assert all(w.project_id == OCTASCENE_PROJECT_ID for w in rows)
    st.close()


def test_dispatch_decisions_and_intake_claims_are_project_stamped(state: State) -> None:
    """Important finding: these writers inserted without a project."""

    state.record_dispatch_decision(
        task_id="t1", stable_task_id="A-1", owner_ref="task:A-1", outcome="ADMITTED",
        reason="ok", project_id="alpha",
    )
    state.record_dispatch_decision(
        task_id="t2", stable_task_id="B-1", owner_ref="task:B-1", outcome="ADMITTED",
        reason="ok", project_id="beta",
    )
    assert [d["task_id"] for d in state.list_dispatch_decisions(project_id="alpha")] == ["t1"]
    assert [d["task_id"] for d in state.list_dispatch_decisions(project_id="beta")] == ["t2"]

    state.claim_task_identity(
        stable_task_id="A-1", owner_ref="task:A-1", source="enqueue:t1", project_id="alpha"
    )
    claims = {c["stable_task_id"]: c["project_id"] for c in state.list_task_identity_claims()}
    assert claims["A-1"] == "alpha"


def test_operation_project_id_survives_a_later_update(state: State) -> None:
    """Important finding: the ON CONFLICT clause omitted project_id."""

    state.upsert_operation(
        "op1", kind="git", action="merge", target="x", state="RUNNING", stage="start",
        message="m", project_id="alpha",
    )
    state.upsert_operation(
        "op1", kind="git", action="merge", target="x", state="DONE", stage="end", message="m2",
    )
    assert [o["id"] for o in state.list_operations(project_id="alpha")] == ["op1"]


def test_legacy_adoption_runs_once_and_does_not_reattribute_later_rows(tmp_path: Path, octascene_repo: Path) -> None:
    """Important finding: re-running adoption would capture other projects' rows.

    A row written with no project *after* the one-time migration must not be
    silently relabelled as OctaScene's on the next start.
    """

    db = tmp_path / "once" / "cp.db"
    first = State(db)
    first.upsert_task(Task(id="legacy", task_ref="V1-07", role="r", worker="w"))
    pr.bootstrap_registry(first, octascene_repo)
    assert [t.id for t in first.list_tasks(project_id=OCTASCENE_PROJECT_ID)] == ["legacy"]
    # A later unscoped row (e.g. written while a different project was active).
    first.upsert_task(Task(id="later", task_ref="X-1", role="r", worker="w"))
    first.close()

    second = State(db)
    pr.bootstrap_registry(second, octascene_repo)
    adopted = [t.id for t in second.list_tasks(project_id=OCTASCENE_PROJECT_ID)]
    assert "legacy" in adopted
    assert "later" not in adopted, "post-migration NULL rows must not be re-attributed to OctaScene"
    second.close()


def test_replace_worktrees_is_atomic(state: State) -> None:
    """Minor finding: the delete was committed before the re-inserts."""

    import sqlite3

    state.upsert_worktree(WorktreeRecord(path="/tmp/alpha-wt", branch="a", project_id="alpha"))

    class FailingConnection:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, *args, **kwargs):
            if sql.startswith("INSERT INTO worktrees"):
                raise sqlite3.OperationalError("simulated failure mid-replace")
            return self._inner.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    real = state._conn
    state._conn = FailingConnection(real)
    with pytest.raises(sqlite3.OperationalError):
        state.replace_worktrees([WorktreeRecord(path="/tmp/new-wt", branch="n")], project_id="alpha")
    state._conn = real

    # The original row is still there: the failed replace rolled back whole.
    assert [w.path for w in state.list_worktrees(project_id="alpha")] == ["/tmp/alpha-wt"]


def test_unscoped_worktree_replace_never_touches_a_projects_rows(state: State) -> None:
    """Second-round blocker: every fallback path still reached a whole-table wipe.

    ``replace_worktrees`` with no project must replace only the unscoped
    (``project_id IS NULL``) rows -- otherwise "all projects disabled", a
    registry-listing failure, or a bootstrap that registered nothing each wiped
    every project's worktree rows.
    """

    state.upsert_worktree(WorktreeRecord(path="/tmp/alpha-wt", branch="a", project_id="alpha"))
    state.upsert_worktree(WorktreeRecord(path="/tmp/legacy-wt", branch="legacy"))

    state.replace_worktrees([WorktreeRecord(path="/tmp/new-legacy-wt", branch="n")])

    assert [w.path for w in state.list_worktrees(project_id="alpha")] == ["/tmp/alpha-wt"]
    unscoped = [w.path for w in state.list_worktrees() if w.project_id is None]
    assert unscoped == ["/tmp/new-legacy-wt"]


def test_startup_recovery_fallback_paths_do_not_wipe_project_worktrees(
    tmp_path: Path, project_a: Path, project_b: Path
) -> None:
    """Every enabled project disabled must not destroy their worktree rows."""

    from scripts.agents.control_plane.recovery import run_recovery

    st = State(tmp_path / "fb" / "cp.db")
    register(st, "alpha", project_a)
    register(st, "beta", project_b)
    st.upsert_worktree(WorktreeRecord(path=str(project_a), branch="main", project_id="alpha"))
    st.upsert_worktree(WorktreeRecord(path=str(project_b), branch="trunk", project_id="beta"))
    pr.set_project_enabled(st, "alpha", False)
    pr.set_project_enabled(st, "beta", False)
    assert pr.list_projects(st, enabled_only=True) == []

    # The "no enabled projects" fallback the orchestrator takes.
    run_recovery(st, project_a)

    assert [w.path for w in st.list_worktrees(project_id="alpha")] == [str(project_a)]
    assert [w.path for w in st.list_worktrees(project_id="beta")] == [str(project_b)]
    st.close()


def test_schema_version_is_not_bumped_when_adoption_could_not_run(
    tmp_path: Path, monkeypatch, octascene_repo: Path
) -> None:
    """Second-round blocker: a premature bump stranded legacy rows forever."""

    db = tmp_path / "strand" / "cp.db"
    first = State(db)
    first.upsert_task(Task(id="legacy", task_ref="V1-07", role="r", worker="w"))

    # Simulate a start where the OctaScene checkout cannot be resolved.
    monkeypatch.setattr(pr, "ensure_octascene_project", lambda *a, **k: None)
    result = pr.bootstrap_registry(first, octascene_repo)
    assert result["octascene_project_id"] is None
    assert first.schema_version() == 1, "must stay unmigrated so a later start can adopt"
    first.close()
    monkeypatch.undo()

    # A later start that *can* resolve OctaScene completes the migration.
    second = State(db)
    pr.bootstrap_registry(second, octascene_repo)
    assert second.schema_version() == CURRENT_SCHEMA_VERSION
    assert [t.id for t in second.list_tasks(project_id=OCTASCENE_PROJECT_ID)] == ["legacy"]
    second.close()


def test_operations_and_adopted_worktrees_are_stamped_by_production_writers(
    tmp_path: Path, project_a: Path
) -> None:
    """Second-round finding: the State-layer fixes were inert without callers."""

    from scripts.agents.control_plane.operations import _record_operation

    registry = load_registry()
    st = State(tmp_path / "ops" / "cp.db")
    ctx = CommandContext(
        state=st, registry=registry, scheduler=Scheduler(),
        supervisor=Supervisor(registry=registry, repo_root=project_a, state=st), repo_root=project_a,
    )
    register(st, "alpha", project_a)
    pr.select_project(st, "alpha")

    _record_operation(ctx, "op1", "git", "fetch", str(project_a), "RUNNING", "Fetching", "fetching origin")
    assert [o["id"] for o in st.list_operations(project_id="alpha")] == ["op1"]
    # Its event is stamped too, so it stays in project-scoped history.
    assert any(e.message.startswith("fetch RUNNING") for e in st.list_events(project_id="alpha"))
    st.close()


def test_failed_git_observation_never_deletes_persisted_worktrees(
    tmp_path: Path, project_a: Path, monkeypatch
) -> None:
    """Third-round blocker: an empty snapshot means "could not observe".

    ``discover_git_worktrees`` returns ``[]`` when the Git observation failed
    (a real repository always reports at least its main worktree). Replacing
    from it deleted the persisted rows and inserted nothing.
    """

    from scripts.agents.control_plane import recovery as recovery_mod

    st = State(tmp_path / "obs" / "cp.db")
    register(st, "alpha", project_a)
    st.upsert_worktree(WorktreeRecord(path=str(project_a), branch="main", project_id="alpha"))
    st.upsert_worktree(WorktreeRecord(path=str(tmp_path / "legacy-wt"), branch="legacy"))

    monkeypatch.setattr(recovery_mod, "discover_git_worktrees", lambda _root: [])
    recovery_mod.run_recovery(st, project_a, project_id="alpha")
    assert [w.path for w in st.list_worktrees(project_id="alpha")] == [str(project_a)]

    # The unscoped fallback must likewise preserve still-unadopted legacy rows.
    recovery_mod.run_recovery(st, project_a)
    assert any(w.branch == "legacy" for w in st.list_worktrees())
    st.close()


def test_scoped_replace_never_steals_a_path_owned_by_another_project(state: State) -> None:
    """Third-round finding: two roots can be worktrees of the same repository."""

    shared = "/tmp/shared-wt"
    state.upsert_worktree(
        WorktreeRecord(path=shared, branch="b", managed=True, review_pr=7, project_id="beta")
    )
    state.replace_worktrees([WorktreeRecord(path=shared, branch="observed")], project_id="alpha")

    still_beta = state.list_worktrees(project_id="beta")
    assert [w.path for w in still_beta] == [shared]
    assert still_beta[0].managed is True
    assert still_beta[0].review_pr == 7
    assert state.list_worktrees(project_id="alpha") == []


def test_unstamped_upsert_cannot_null_a_stored_project(state: State) -> None:
    state.upsert_worktree(WorktreeRecord(path="/tmp/wt", branch="a", project_id="alpha"))
    # A freshly discovered record carries no project of its own.
    state.upsert_worktree(WorktreeRecord(path="/tmp/wt", branch="a-updated"))
    rows = state.list_worktrees(project_id="alpha")
    assert [w.branch for w in rows] == ["a-updated"]


def test_clearing_terminal_history_without_a_project_spares_other_projects(state: State) -> None:
    state.record_terminal_command(
        actor="me", session_id="s", cwd="/tmp", branch="a", command="ls", project_id="alpha"
    )
    state.record_terminal_command(actor="me", session_id="s", cwd="/tmp", branch=None, command="pwd")
    state.clear_terminal_commands()
    assert [c["command"] for c in state.list_terminal_commands(project_id="alpha")] == ["ls"]


def test_claim_task_identity_returns_the_backfilled_project(state: State) -> None:
    state.claim_task_identity(stable_task_id="A-1", owner_ref="task:A-1", source="legacy")
    claim = state.claim_task_identity(
        stable_task_id="A-1", owner_ref="task:A-1", source="enqueue:t1", project_id="alpha"
    )
    assert claim["project_id"] == "alpha"


def test_adopt_worktree_stamps_the_selected_project(tmp_path: Path, project_a: Path) -> None:
    from scripts.agents.control_plane.operations import adopt_worktree

    registry = load_registry()
    st = State(tmp_path / "adopt" / "cp.db")
    ctx = CommandContext(
        state=st, registry=registry, scheduler=Scheduler(),
        supervisor=Supervisor(registry=registry, repo_root=project_a, state=st), repo_root=project_a,
    )
    register(st, "alpha", project_a)
    pr.select_project(st, "alpha")

    adopt_worktree(ctx, path=str(project_a))
    rows = st.list_worktrees(project_id="alpha")
    assert [Path(w.path).resolve() for w in rows] == [project_a.resolve()]
    assert rows[0].managed is True
    st.close()


def test_adopt_worktree_never_steals_a_path_owned_by_another_project(
    tmp_path: Path, project_a: Path
) -> None:
    """Fourth-round finding: adopt looked only at the selected project's rows."""

    from scripts.agents.control_plane.operations import OperationError, adopt_worktree

    registry = load_registry()
    st = State(tmp_path / "steal" / "cp.db")
    ctx = CommandContext(
        state=st, registry=registry, scheduler=Scheduler(),
        supervisor=Supervisor(registry=registry, repo_root=project_a, state=st), repo_root=project_a,
    )
    register(st, "alpha", project_a)
    pr.select_project(st, "alpha")
    owned_path = str(project_a.resolve())
    st.upsert_worktree(
        WorktreeRecord(
            path=owned_path,
            branch="main",
            managed=True,
            review_pr=14,
            review_head_sha="abc123",
            project_id="beta",
        )
    )

    with pytest.raises(OperationError, match="already owned by project 'beta'"):
        adopt_worktree(ctx, path=owned_path)

    still_beta = st.list_worktrees(project_id="beta")
    assert [w.path for w in still_beta] == [owned_path]
    assert still_beta[0].managed is True
    assert still_beta[0].review_pr == 14
    assert still_beta[0].review_head_sha == "abc123"
    assert st.list_worktrees(project_id="alpha") == []
    st.close()


def test_upsert_worktree_never_relabels_a_foreign_stamp(state: State) -> None:
    """Defense in depth for the adopt-steal path: the persistence primitive refuses."""

    shared = "/tmp/shared-owned"
    state.upsert_worktree(
        WorktreeRecord(path=shared, branch="b", managed=True, review_pr=7, project_id="beta")
    )
    with pytest.raises(ValueError, match="already owned by project 'beta'"):
        state.upsert_worktree(
            WorktreeRecord(path=shared, branch="stolen", managed=True, project_id="alpha")
        )
    still = state.list_worktrees(project_id="beta")
    assert [w.branch for w in still] == ["b"]
    assert still[0].review_pr == 7
    assert still[0].managed is True
    assert state.list_worktrees(project_id="alpha") == []


def test_cleanup_runs_in_the_selected_project_root_and_stamps_its_event(
    tmp_path: Path, project_a: Path, project_b: Path, monkeypatch
) -> None:
    """Fourth-round minor finding + test gap: cleanup used CP checkout and was untested."""

    from scripts.agents.control_plane import operations as ops

    registry = load_registry()
    st = State(tmp_path / "cleanup" / "cp.db")
    ctx = CommandContext(
        state=st, registry=registry, scheduler=Scheduler(),
        supervisor=Supervisor(registry=registry, repo_root=project_a, state=st), repo_root=project_a,
    )
    register(st, "alpha", project_a)
    register(st, "beta", project_b)
    pr.select_project(st, "beta")

    finished = project_b / "finished-wt"
    observed = [
        {
            "path": str(finished),
            "cleanup_eligible": True,
            "canonical_checkout": False,
            "classification": "FINISHED_CLEAN",
        }
    ]
    monkeypatch.setattr(ops, "refresh_worktree_statuses", lambda _ctx: observed)
    monkeypatch.setattr(ops, "refresh_repository_health", lambda _ctx: {})
    captured: list[tuple[list[str], Path]] = []

    def fake_run(argv: list[str], cwd: Path, *, timeout: int = 30):
        captured.append((list(argv), Path(cwd)))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(ops, "_run", fake_run)

    result = ops.cleanup_worktrees(ctx, confirm=True)
    assert result["removed"] == [str(finished.resolve())]
    assert all(cwd == project_b.resolve() for _argv, cwd in captured)
    assert any(e.project_id == "beta" and "safely removed" in e.message for e in st.list_events(project_id="beta"))
    assert not any("safely removed" in e.message for e in st.list_events(project_id="alpha"))
    st.close()


def test_derived_caches_never_leak_across_projects(state: State) -> None:
    state.set_project_setting("repository_health_cache", json.dumps({"p": "alpha"}), "alpha")
    assert state.get_project_setting("repository_health_cache", project_id="alpha")
    # Project B has no cache yet -- it must read as absent, never as A's data.
    assert state.get_project_setting("repository_health_cache", project_id="beta") is None


def test_global_settings_stay_global(state: State) -> None:
    """Spec 4: do not blindly add project_id to everything."""

    state.set_control_setting("max_write_workers", "3")
    assert state.get_control_setting("max_write_workers") == "3"
    # Provider states are global CP configuration, with no project column.
    columns = {row[1] for row in state._conn.execute("PRAGMA table_info(provider_states)").fetchall()}
    assert "project_id" not in columns


# ---------------------------------------------------- CP root vs project root


def test_cp_code_root_is_never_mistaken_for_a_project_root(state: State, project_b: Path) -> None:
    contract = register(state, "beta", project_b)
    assert contract.local_repo_root.resolve() != cp_code_root().resolve()
    report = pr.validate_project(contract)
    assert report["is_cp_code_root"] is False
    assert report["cp_code_root"] == str(cp_code_root())


def test_registry_resolution_is_independent_of_process_cwd(
    state: State, project_a: Path, project_b: Path, monkeypatch, tmp_path: Path
) -> None:
    register(state, "alpha", project_a)
    pr.select_project(state, "alpha")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert pr.selected_project(state).local_repo_root.resolve() == project_a.resolve()
    monkeypatch.chdir(project_b)
    assert pr.selected_project(state).local_repo_root.resolve() == project_a.resolve()


# ------------------------------------------------------------------- HTTP API


@pytest.fixture()
def api(tmp_path: Path, project_a: Path, project_b: Path):
    registry = load_registry()
    st = State(tmp_path / "api" / "cp.db")
    ctx = CommandContext(
        state=st,
        registry=registry,
        scheduler=Scheduler(),
        supervisor=Supervisor(registry=registry, repo_root=project_a, state=st),
        repo_root=project_a,
    )
    register(st, "alpha", project_a)
    register(st, "beta", project_b)
    pr.select_project(st, "alpha")
    roadmap = tmp_path / "ROADMAP.md"
    roadmap.write_text("# Roadmap\n\n## Program index\n\n| a | b | c | d |\n|---|---|---|---|\n", encoding="utf-8")
    return ctx, TestClient(create_app(ctx, roadmap_path=roadmap))


def test_projects_endpoint_reports_registry_and_selection(api) -> None:
    _ctx, client = api
    body = client.get("/api/projects").json()
    assert {p["project_id"] for p in body["projects"]} == {"alpha", "beta"}
    assert body["selected_project_id"] == "alpha"
    assert body["cp_code_root"] == str(cp_code_root())


def test_select_endpoint_switches_the_selected_project(api) -> None:
    _ctx, client = api
    res = client.post("/api/projects/select", json={"project_id": "beta"})
    assert res.status_code == 200
    assert res.json()["selected_project_id"] == "beta"
    assert client.get("/api/projects").json()["selected_project_id"] == "beta"


def test_select_endpoint_rejects_an_unknown_project(api) -> None:
    _ctx, client = api
    res = client.post("/api/projects/select", json={"project_id": "ghost"})
    assert res.status_code == 400
    assert "ghost" in res.json()["detail"]


def test_api_tasks_and_history_follow_the_selected_project(api) -> None:
    ctx, client = api
    ctx.state.upsert_task(Task(id="a1", task_ref="A-1", role="r", worker="w", project_id="alpha"))
    ctx.state.upsert_task(Task(id="b1", task_ref="B-1", role="r", worker="w", project_id="beta"))
    ctx.state.record_event(category="test", message="alpha only", project_id="alpha")
    ctx.state.record_event(category="test", message="beta only", project_id="beta")

    assert [t["id"] for t in client.get("/api/tasks").json()] == ["a1"]
    messages = [e["message"] for e in client.get("/api/events").json()]
    assert "alpha only" in messages and "beta only" not in messages

    client.post("/api/projects/select", json={"project_id": "beta"})
    assert [t["id"] for t in client.get("/api/tasks").json()] == ["b1"]
    messages = [e["message"] for e in client.get("/api/events").json()]
    assert "beta only" in messages and "alpha only" not in messages


def test_no_stale_project_a_data_remains_after_switching(api) -> None:
    """Spec 5: no Project A data may remain displayed as if it belonged to B."""

    ctx, client = api
    ctx.state.upsert_task(Task(id="a1", task_ref="A-1", role="r", worker="w", project_id="alpha"))
    ctx.state.upsert_runbook(
        Runbook(
            id="rb-a", name="Alpha", preset="overnight-development", objective="o", source_ref="A-1",
            branch="a", worktree="/tmp/a", parent_worker="claude-code", max_duration_minutes=60,
            project_id="alpha",
        )
    )
    ctx.state.upsert_worktree(WorktreeRecord(path="/tmp/alpha-wt", branch="a", project_id="alpha"))
    assert client.get("/api/tasks").json()
    assert client.get("/api/runbooks").json()

    client.post("/api/projects/select", json={"project_id": "beta"})
    assert client.get("/api/tasks").json() == []
    assert client.get("/api/runbooks").json() == []
    assert [w for w in client.get("/api/worktrees").json() if w["path"] == "/tmp/alpha-wt"] == []
    assert client.get("/api/overview").json()["task_counts"]["queued"] == 0


def test_detect_endpoint_reports_before_saving(api, tmp_path: Path) -> None:
    _ctx, client = api
    new_repo = make_repo(tmp_path / "gamma", branch="release", files={"TODO.md": "x\n"})
    body = client.post("/api/projects/detect", json={"path": str(new_repo)}).json()
    assert body["default_branch"] == "release"
    assert body["task_sources"] == ["TODO.md"]
    assert body["suggested_project_id"] == "gamma"


def test_create_update_and_delete_project_through_the_api(api, tmp_path: Path) -> None:
    _ctx, client = api
    new_repo = make_repo(tmp_path / "gamma")
    created = client.post(
        "/api/projects", json={"project_id": "gamma", "display_name": "Gamma", "local_repo_root": str(new_repo)}
    )
    assert created.status_code == 200
    assert "gamma" in {p["project_id"] for p in created.json()["projects"]}

    patched = client.patch("/api/projects/gamma", json={"display_name": "Gamma Renamed"})
    assert patched.status_code == 200
    assert next(p for p in patched.json()["projects"] if p["project_id"] == "gamma")["display_name"] == "Gamma Renamed"

    # Destructive CP-state change requires explicit confirmation.
    assert client.delete("/api/projects/gamma").status_code == 400
    removed = client.delete("/api/projects/gamma?confirm=true")
    assert removed.status_code == 200
    assert "gamma" not in {p["project_id"] for p in removed.json()["projects"]}
    assert new_repo.exists()


def test_create_project_rejects_an_invalid_repository_with_an_actionable_error(api, tmp_path: Path) -> None:
    _ctx, client = api
    plain = tmp_path / "plain"
    plain.mkdir()
    res = client.post("/api/projects", json={"project_id": "plain", "local_repo_root": str(plain)})
    assert res.status_code == 400
    assert "Git" in res.json()["detail"]


def test_validate_and_refresh_endpoints_report_repository_truth(api) -> None:
    _ctx, client = api
    report = client.get("/api/projects/alpha/validate").json()
    assert report["ok"] is True
    assert report["is_cp_code_root"] is False

    refreshed = client.post("/api/projects/alpha/refresh").json()
    assert refreshed["observed_default_branch"] == "main"
    assert refreshed["project_id"] == "alpha"

    assert client.get("/api/projects/ghost/validate").status_code == 404


def test_validate_reports_a_moved_checkout_without_failing_the_registry(api, tmp_path: Path, project_b: Path) -> None:
    ctx, client = api
    import shutil

    shutil.rmtree(project_b)
    report = client.get("/api/projects/beta/validate").json()
    assert report["ok"] is False
    assert report["problems"]
    # The registry itself still lists both projects: one broken checkout must
    # not take down project listing/selection for everything else.
    assert len(client.get("/api/projects").json()["projects"]) == 2


def test_roadmap_follows_the_selected_project(tmp_path: Path, project_a: Path, project_b: Path) -> None:
    registry = load_registry()
    st = State(tmp_path / "rm" / "cp.db")
    ctx = CommandContext(
        state=st, registry=registry, scheduler=Scheduler(),
        supervisor=Supervisor(registry=registry, repo_root=project_a, state=st), repo_root=project_a,
    )
    register(st, "alpha", project_a, roadmap_paths=["docs/ROADMAP.md"])
    register(st, "beta", project_b)  # declares no roadmap at all
    pr.select_project(st, "alpha")
    client = TestClient(create_app(ctx))

    assert isinstance(client.get("/api/roadmap").json(), list)
    client.post("/api/projects/select", json={"project_id": "beta"})
    # A project declaring no roadmap shows nothing -- never the other project's.
    assert client.get("/api/roadmap").json() == []


# ------------------------------------------------- remote authorization bound


# Exercised through the real Cloudflare Access verification path (the same
# throwaway-RSA-keypair / fake-JWKS technique tests/test_orchestrator_remote_
# dashboard.py already uses for ENG-AGENT-02-S6), not a stubbed identity, so
# these prove the actual deployed boundary rather than a test-only shortcut.
_TEAM_DOMAIN = "testteam.cloudflareaccess.com"
_AUDIENCE = "test-application-aud-tag"
_HOSTNAME = "dev.octascene.com"
_KID = "test-key-1"
_REMOTE_EMAIL = "maintainer@example.com"


@pytest.fixture()
def remote_api(tmp_path: Path, project_a: Path, project_b: Path):
    import json as _json
    import time

    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    from jwt.algorithms import RSAAlgorithm

    from scripts.agents.control_plane.remote_access import (
        ACCESS_JWT_HEADER,
        AccessVerifier,
        RemoteAccessConfig,
        RemoteAccessState,
    )

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = _json.loads(RSAAlgorithm.to_jwk(private_key.public_key()))
    jwk.update({"kid": _KID, "alg": "RS256", "use": "sig"})

    config = RemoteAccessConfig(
        enabled=True, hostname=_HOSTNAME, team_domain=_TEAM_DOMAIN, audience=_AUDIENCE,
        allowed_emails=frozenset({_REMOTE_EMAIL}),
    )
    remote = RemoteAccessState(
        config=config, verifier=AccessVerifier(config, jwks_fetcher=lambda _url: {"keys": [jwk]})
    )

    now = int(time.time())
    token = jwt.encode(
        {"email": _REMOTE_EMAIL, "aud": _AUDIENCE, "iss": f"https://{_TEAM_DOMAIN}",
         "iat": now, "exp": now + 300, "sub": "user-123"},
        private_key, algorithm="RS256", headers={"kid": _KID},
    )

    registry = load_registry()
    st = State(tmp_path / "remote" / "cp.db")
    ctx = CommandContext(
        state=st, registry=registry, scheduler=Scheduler(),
        supervisor=Supervisor(registry=registry, repo_root=project_a, state=st), repo_root=project_a,
    )
    register(st, "alpha", project_a)
    register(st, "beta", project_b)
    pr.select_project(st, "alpha")
    client = TestClient(create_app(ctx, remote=remote))
    headers = {ACCESS_JWT_HEADER: token, "origin": f"https://{_HOSTNAME}"}
    return client, headers


def test_remote_identity_may_not_supply_arbitrary_filesystem_paths(remote_api, tmp_path: Path) -> None:
    """Spec 11: remote users cannot provide arbitrary filesystem paths."""

    client, headers = remote_api
    new_repo = make_repo(tmp_path / "delta")

    created = client.post(
        "/api/projects", json={"project_id": "delta", "local_repo_root": str(new_repo)}, headers=headers
    )
    assert created.status_code == 403
    assert "not authorized" in created.json()["detail"]

    detected = client.post("/api/projects/detect", json={"path": str(new_repo)}, headers=headers)
    assert detected.status_code == 403

    moved = client.patch("/api/projects/alpha", json={"local_repo_root": str(new_repo)}, headers=headers)
    assert moved.status_code == 403

    # The refusal is durable, not advisory: nothing was registered.
    assert "delta" not in {p["project_id"] for p in client.get("/api/projects", headers=headers).json()["projects"]}


def test_remote_identity_may_still_select_an_already_registered_project(remote_api) -> None:
    """The boundary is about *filesystem paths*, not about using the CP remotely."""

    client, headers = remote_api
    res = client.post("/api/projects/select", json={"project_id": "beta"}, headers=headers)
    assert res.status_code == 200
    assert res.json()["selected_project_id"] == "beta"
    assert client.get("/api/projects", headers=headers).status_code == 200


def test_remote_project_edits_that_touch_no_path_are_still_allowed(remote_api) -> None:
    client, headers = remote_api
    res = client.patch("/api/projects/beta", json={"display_name": "Beta Remote"}, headers=headers)
    assert res.status_code == 200


def test_existing_remote_authentication_is_not_weakened(remote_api) -> None:
    """An unauthenticated remote-shaped request is still rejected as before."""

    client, headers = remote_api
    from scripts.agents.control_plane.remote_access import ACCESS_JWT_HEADER

    bad = dict(headers)
    bad[ACCESS_JWT_HEADER] = "not-a-valid-token"
    assert client.get("/api/projects", headers=bad).status_code == 401


def test_local_requests_keep_full_filesystem_authority(api, tmp_path: Path) -> None:
    _ctx, client = api
    new_repo = make_repo(tmp_path / "epsilon")
    assert client.post("/api/projects/detect", json={"path": str(new_repo)}).status_code == 200


# ------------------------------------------------------- two-project isolation


def test_two_projects_coexist_with_fully_isolated_state(tmp_path: Path, project_a: Path, project_b: Path) -> None:
    """Spec 9: the end-to-end multi-project isolation proof."""

    registry = load_registry()
    st = State(tmp_path / "iso" / "cp.db")
    ctx = CommandContext(
        state=st, registry=registry, scheduler=Scheduler(),
        supervisor=Supervisor(registry=registry, repo_root=project_a, state=st), repo_root=project_a,
    )
    register(st, "alpha", project_a, validation_command=["make", "test"])
    register(st, "beta", project_b)

    for pid, root in (("alpha", project_a), ("beta", project_b)):
        pr.select_project(st, pid)
        # Each project's runbook records the project's own repository root.
        st.upsert_runbook(
            Runbook(
                id=f"rb-{pid}", name=pid, preset="overnight-development", objective="o", source_ref=f"{pid}-1",
                branch="main", worktree=str(root), parent_worker="claude-code", max_duration_minutes=60,
                project_id=pid,
            )
        )
        st.upsert_task(Task(id=f"t-{pid}", task_ref=f"{pid.upper()}-1", role="r", worker="w", project_id=pid))
        st.upsert_worktree(WorktreeRecord(path=str(root), branch="main", project_id=pid))

    client = TestClient(create_app(ctx))

    for pid, root in (("alpha", project_a), ("beta", project_b)):
        client.post("/api/projects/select", json={"project_id": pid})
        assert [t["id"] for t in client.get("/api/tasks").json()] == [f"t-{pid}"]
        assert [r["id"] for r in client.get("/api/runbooks").json()] == [f"rb-{pid}"]
        # Worktrees are associated with the correct repository.
        paths = {Path(w["path"]).resolve() for w in client.get("/api/worktrees").json()}
        assert Path(root).resolve() in paths
        other = project_b if pid == "alpha" else project_a
        assert Path(other).resolve() not in paths
        # The selected project's contract points at its own checkout.
        assert Path(ctx.project_root).resolve() == Path(root).resolve()

    # Neither repository was mutated by any of the above.
    for root in (project_a, project_b):
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=str(root), capture_output=True, text=True, check=True
        ).stdout
        assert status == ""


def test_runbook_validation_runs_against_the_selected_projects_root(
    state: State, project_a: Path, project_b: Path
) -> None:
    """CPX-02's run_project_validation executes in the project's own root."""

    from scripts.agents.control_plane.project import run_project_validation

    marker = project_b / "where.txt"
    contract = register(
        state, "beta", project_b, validation_command=["sh", "-c", f"pwd > {marker.name}"]
    )
    code, _out = run_project_validation(contract, timeout=30)
    assert code == 0
    assert Path(marker.read_text().strip()).resolve() == project_b.resolve()
    marker.unlink()
