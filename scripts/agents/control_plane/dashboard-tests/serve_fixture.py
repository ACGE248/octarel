#!/usr/bin/env python3
"""Serve the control-center dashboard with deterministic fixture data for Playwright.

Not part of the shipped product and never imported by app/tests — a
throwaway harness process for scripts/agents/control_plane/playwright.config.js
only. It builds a command context rooted at an isolated, git-ignored temp
directory (so it never touches a developer's real .orchestrator-state/), pre-
seeds a handful of tasks in different states (including two simultaneously
RUNNING, to exercise the "simultaneous workers" visualization requirement)
and a couple of attention-worthy conditions, then serves the real dashboard
FastAPI app unmodified.

Usage: run as a module so relative imports resolve, exactly like every other
scripts.agents entry point::

    python -m scripts.agents.control_plane.dashboard-tests.serve_fixture --port 8899

(Hyphens in the path segment mean this file is invoked directly with `python
<path>` by the Playwright config instead, using a small sys.path shim below,
since "dashboard-tests" is not a valid Python package identifier.)
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import signal
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import subprocess  # noqa: E402

from scripts.agents.control_plane.commands import CommandContext  # noqa: E402
from scripts.agents.control_plane.context_cursor import (  # noqa: E402
    AncestryRequest,
    ContextIdentity,
    build_incremental_context,
)
from scripts.agents.control_plane.dashboard_api import create_app  # noqa: E402
from scripts.agents.control_plane.models import (  # noqa: E402
    KIND_READ,
    Runbook,
    Task,
    WorktreeRecord,
    utc_now_iso,
)
from scripts.agents.control_plane.octascene_project import (  # noqa: E402
    OCTASCENE_DISPLAY_NAME,
    OCTASCENE_GITHUB_REMOTE,
    OCTASCENE_PROJECT_ID,
)
from scripts.agents.control_plane.project import ProjectContract  # noqa: E402
from scripts.agents.control_plane.project_registry import (  # noqa: E402
    contract_to_row,
    migrate_legacy_state_to_project,
    select_project,
)
from scripts.agents.control_plane.provider_state import (  # noqa: E402
    STATE_COST_BLOCKED,
    seed_provider_states,
)
from scripts.agents.control_plane.quickstart import (
    VIDEO_EDITOR_LEDGER_RELATIVE,  # noqa: E402
)
from scripts.agents.control_plane.remote_access import (  # noqa: E402
    AccessVerifier,
    RemoteAccessConfig,
    RemoteAccessState,
)
from scripts.agents.control_plane.run_events import RunEvent  # noqa: E402
from scripts.agents.control_plane.runbooks import PRESETS  # noqa: E402
from scripts.agents.control_plane.runtime_services import (
    RuntimeServiceManager,  # noqa: E402
)
from scripts.agents.control_plane.scheduler import (  # noqa: E402
    ConcurrencyPolicy,
    Scheduler,
)
from scripts.agents.control_plane.state import State, default_db_path  # noqa: E402
from scripts.agents.control_plane.supervisor import Supervisor  # noqa: E402
from scripts.agents.policy import PolicyBundle  # noqa: E402
from scripts.agents.registry import load_registry  # noqa: E402

FIXTURE_BRANCH = "fixture-main"
FIXTURE_SECOND_BRANCH = "fixture-second"
# ENG-CP-03 (issue #165): the second managed project's own default branch.
# Deliberately not "main" so a project-switch test proves the UI reads each
# project's own declared branch rather than assuming OctaScene's.
FIXTURE_PROJECT_B_BRANCH = "trunk"
FIXTURE_PROJECT_B_ID = "fixture-project-b"
FIXTURE_PROJECT_B_NAME = "Fixture Project B"
FIXTURE_PROJECT_B_REMOTE = "fixture-org/project-b"
FIXTURE_SECOND_WORKTREE_DIRNAME = "second-worktree"

# ENG-AGENT-14 (issue #140): a real, registered review checkout (a detached
# worktree carrying WorktreeRecord.review_pr/review_head_sha/
# review_repository) so Playwright coverage can assert the Worktrees card
# renders a review checkout's origin honestly. This is a real detached `git
# worktree add --detach`, exactly matching what
# provisioning.provision_review_worktree produces -- not a synthetic row
# with no backing checkout.
FIXTURE_REVIEW_WORKTREE_DIRNAME = "review-pr-fixture"
FIXTURE_REVIEW_PR_NUMBER = 999
FIXTURE_REVIEW_REPOSITORY = "ACGE248/octages"

# ENG-AGENT-02-S7 (issue #97): a minimal, real ledger so /api/quickstart's
# "Continue Video Editor" resolver has a genuine "pending" task to resolve in
# Playwright coverage, exactly like this repository's real
# docs/video-editor/IMPLEMENTATION_STATUS_V2.md.
FIXTURE_VIDEO_EDITOR_TASK_ID = "V1-01"
FIXTURE_VIDEO_EDITOR_BRANCH = "video-editor/v1-01-fixture-task"
FIXTURE_VIDEO_EDITOR_WORKTREE_DIRNAME = "video-editor-worktree"
FIXTURE_VIDEO_EDITOR_LEDGER = f"""\
# Fixture Video Editor ledger

## Status values

`pending | complete`

| ID | Status | Notes/evidence |
|---|---|---|
| {FIXTURE_VIDEO_EDITOR_TASK_ID} | pending | Fixture task for Playwright coverage. |
"""

# ENG-AGENT-02-S6 (issue #95): a fixed throwaway team domain/audience/allowlist
# for exercising the authenticated remote-mode UX in Playwright. No real
# Cloudflare account, credential, or network call is ever involved — the
# fixture mints and verifies its own tokens with an in-memory RSA keypair
# generated fresh each time this process starts (see `_build_remote_state`
# and the test-only `/test/mint-remote-token` route added in `main()`).
FIXTURE_TEAM_DOMAIN = "fixture-team.cloudflareaccess.com"
FIXTURE_AUDIENCE = "fixture-application-aud"
FIXTURE_ALLOWED_EMAIL = "maintainer@example.com"
FIXTURE_DENIED_EMAIL = "not-allowlisted@example.com"
FIXTURE_KID = "fixture-key-1"


def _parent_identity() -> tuple[int, float | None]:
    """PID/create-time proof for the Playwright process that owns this server."""

    pid = os.getppid()
    try:
        import psutil

        return pid, psutil.Process(pid).create_time()
    except (ImportError, OSError):
        return pid, None


def _parent_identity_alive(identity: tuple[int, float | None]) -> bool:
    pid, created_at = identity
    if os.getppid() != pid:
        return False
    try:
        import psutil

        process = psutil.Process(pid)
        return created_at is None or abs(process.create_time() - created_at) <= 1.0
    except ImportError:
        return pid > 1
    except (OSError, ValueError):
        return False


def _start_parent_watchdog(identity: tuple[int, float | None]) -> None:
    """Terminate this fixture if Playwright disappears without cleanup."""

    def watch() -> None:
        while _parent_identity_alive(identity):
            time.sleep(0.5)
        os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=watch, name="playwright-parent-watchdog", daemon=True).start()


def _git_init_fixture_root(root: Path) -> None:
    """A real (throwaway) git repo so runbook branch/worktree validation succeeds.

    Never touches the real developer checkout: this is an isolated temp dir,
    never the actual repository this dashboard is served from.
    """

    if (root / ".git").exists():
        return
    subprocess.run(["git", "init", "-q", "-b", FIXTURE_BRANCH, str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "fixture@example.com"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Fixture"], check=True)
    (root / "seed.txt").write_text("fixture seed\n", encoding="utf-8")
    ledger_path = root / "docs" / "video-editor" / "IMPLEMENTATION_STATUS_V2.md"
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(FIXTURE_VIDEO_EDITOR_LEDGER, encoding="utf-8")
    # CPX-06: Octarel has no OctaScene PRODUCT_ROADMAP. The harness owns a
    # throwaway program-index table so Settings still exercises derived labels.
    roadmap_path = root / "docs" / "PRODUCT_ROADMAP.md"
    roadmap_path.write_text(
        "# Fixture roadmap\n\n"
        "## Program index\n\n"
        "| Program / feature family | Product version | Status source | Implementation source |\n"
        "|---|---|---|---|\n"
        "| Fixture program | test | fixture | fixture |\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "fixture seed"], check=True)


def _git_init_second_fixture_project(root: Path) -> Path:
    """A separate, deliberately *generic* repository for the second project.

    ENG-CP-03 (issue #165): the project selector must be exercisable with more
    than one project, and the second one must not look like OctaScene -- no
    AGENTS.md, no docs/PRODUCT_ROADMAP.md, a non-``main`` default branch. This
    is a genuinely independent Git repository (not another worktree of the
    fixture root), so Playwright can prove two unrelated repositories coexist.
    """

    project = root.parent / f"{root.name}-project-b"
    if (project / ".git").exists():
        return project
    project.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", FIXTURE_PROJECT_B_BRANCH, str(project)], check=True)
    subprocess.run(["git", "-C", str(project), "config", "user.email", "fixture@example.com"], check=True)
    subprocess.run(["git", "-C", str(project), "config", "user.name", "Fixture"], check=True)
    (project / "TASKS.md").write_text("- [ ] generic task\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(project), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(project), "commit", "-q", "-m", "project b seed"], check=True)
    return project


def _git_add_second_fixture_worktree(root: Path) -> Path:
    """A second real worktree of the same throwaway repo.

    One write-capable agent per worktree is enforced even in fixtures: the
    `RUNNING`/`SUCCEEDED` runbook fixtures occupy the primary worktree, so a
    test that actually starts the `DRAFT` one needs its own distinct,
    genuinely registered worktree rather than colliding with them.
    """

    second = root / FIXTURE_SECOND_WORKTREE_DIRNAME
    if not second.exists():
        subprocess.run(
            ["git", "-C", str(root), "worktree", "add", "-b", FIXTURE_SECOND_BRANCH, str(second)], check=True
        )
    return second


def _git_add_video_editor_fixture_worktree(root: Path) -> Path:
    """A real worktree already checked out to the fixture ledger's "pending"

    task's branch, so /api/quickstart's "Continue Video Editor" option
    resolves `ready: true` in Playwright coverage without needing to
    exercise real worktree auto-provisioning here (that path has its own
    dedicated, non-Playwright Python coverage in
    tests/test_orchestrator_quickstart_start.py).
    """

    worktree = root / FIXTURE_VIDEO_EDITOR_WORKTREE_DIRNAME
    if not worktree.exists():
        subprocess.run(
            ["git", "-C", str(root), "worktree", "add", "-b", FIXTURE_VIDEO_EDITOR_BRANCH, str(worktree)],
            check=True,
        )
    return worktree


def _seed_configuration_revisions(state: State, created_at: str) -> None:
    """Seed valid current state plus an invalid historical rollback candidate.

    The invalid legacy snapshot lets the browser prove preview-time refusal and
    the distinct EMPTY versus UNKNOWN rendering without weakening Scope A's
    write validation. Direct inserts are confined to this throwaway fixture.
    """

    state._conn.executemany(
        "INSERT INTO configuration_revisions "
        "(actor, created_at, reason, changed_fields, predecessor_id, settings) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            (
                "fixture:migration",
                created_at,
                "legacy empty value retained for rollback validation coverage",
                json.dumps({"held_task_ids": {"before": None, "after": ""}}, sort_keys=True),
                0,
                json.dumps({"held_task_ids": ""}, sort_keys=True),
            ),
            (
                "fixture:operator",
                created_at,
                "restore valid runtime configuration",
                json.dumps(
                    {
                        "held_task_ids": {"before": "", "after": None},
                        "max_write_workers": {"before": None, "after": "4"},
                    },
                    sort_keys=True,
                ),
                1,
                json.dumps({"max_write_workers": "4"}, sort_keys=True),
            ),
        ],
    )
    state._conn.execute(
        "INSERT INTO control_settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        ("max_write_workers", "4"),
    )
    state._conn.commit()


def _git_add_review_fixture_worktree(root: Path) -> tuple[Path, str]:
    """A real detached worktree at the fixture repo's own HEAD, registered as

    an auto-provisioned review checkout -- see ``FIXTURE_REVIEW_*`` above.
    """

    worktree = root / FIXTURE_REVIEW_WORKTREE_DIRNAME
    head_sha = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, check=True,
    ).stdout.strip()
    if not worktree.exists():
        subprocess.run(["git", "-C", str(root), "worktree", "add", "--detach", str(worktree), head_sha], check=True)
    return worktree, head_sha


def build_fixture_context(root: Path, *, state_path: Path | None = None) -> CommandContext:
    # OCTAREL-TEST-02 (issue #45): stamp the whole seed from one instant so the
    # fixture's *relative* task recency is intentional instead of a function of
    # how long seeding took. ``dashboard_api.workflow()`` renders the single most
    # recently updated active task reference, and ``utc_now_iso()`` resolves to
    # whole seconds, so a seed that happened to cross a clock second between
    # ENG-AGENT-02's fx-* tasks and ENG-AGENT-07's fallback session silently
    # handed the Live Workflow card to ENG-AGENT-07 -- and every test locating
    # ``.workflow-stage-card[data-stage-id="fx-*"]`` then found no such card.
    # Under the concurrent viewport matrix that seed is slower, so the flip was
    # load-dependent. ENG-AGENT-02 is the reference the browser suite inspects,
    # so it keeps "now" and every other reference is stamped one second older.
    seeded_at = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    current_ref_at = seeded_at.isoformat(timespec="seconds")
    other_ref_at = (seeded_at - dt.timedelta(seconds=1)).isoformat(timespec="seconds")
    _git_init_fixture_root(root)
    second_worktree = _git_add_second_fixture_worktree(root)
    video_editor_worktree = _git_add_video_editor_fixture_worktree(root)
    review_worktree, review_head_sha = _git_add_review_fixture_worktree(root)
    registry = load_registry()
    state = State(state_path or default_db_path(root))
    _seed_configuration_revisions(state, current_ref_at)
    for provider in seed_provider_states(registry):
        state.upsert_provider_state(provider)
    state.upsert_worktree(WorktreeRecord(path=str(root), branch=FIXTURE_BRANCH, locked=False))
    state.upsert_worktree(WorktreeRecord(path=str(second_worktree), branch=FIXTURE_SECOND_BRANCH, locked=False))
    state.upsert_worktree(
        WorktreeRecord(path=str(video_editor_worktree), branch=FIXTURE_VIDEO_EDITOR_BRANCH, locked=False)
    )
    state.upsert_worktree(
        WorktreeRecord(
            path=str(review_worktree), branch=None, managed=True,
            review_repository=FIXTURE_REVIEW_REPOSITORY, review_pr=FIXTURE_REVIEW_PR_NUMBER,
            review_head_sha=review_head_sha,
        )
    )

    state.upsert_task(
        Task(
            id="fx-running-1",
            task_ref="ENG-AGENT-02",
            role="focused-tests",
            worker="grok-build",
            state="RUNNING",
            # These are test/verification tasks, not implementation writes;
            # leaving `kind` at its Task-model write default previously
            # overcounted the managed-dispatch write cap (ENG-AGENT-10)
            # against fixture data seeded before that cap existed.
            kind=KIND_READ,
        ),
        updated_at=current_ref_at,
    )
    state.upsert_task(
        Task(
            id="fx-running-2",
            task_ref="ENG-AGENT-02",
            role="mechanical-testing",
            worker="opencode2-gemini-flash-lite",
            state="RUNNING",
            kind=KIND_READ,
        ),
        updated_at=current_ref_at,
    )
    state.upsert_task(
        Task(id="fx-queued-1", task_ref="ENG-CI-04", role="focused-tests", worker="grok-build", state="QUEUED"),
        updated_at=other_ref_at,
    )
    state.upsert_task(
        Task(
            id="fx-blocked-1",
            task_ref="ENG-AGENT-02",
            role="diff-review",
            worker="grok-build-review",
            state="BLOCKED",
            dependencies=("fx-running-1",),
        ),
        updated_at=current_ref_at,
    )
    state.upsert_task(
        Task(
            id="fx-done-1",
            task_ref="ENG-AGENT-02",
            role="focused-tests",
            worker="opencode2-gemini-flash-lite",
            state="SUCCEEDED",
        ),
        updated_at=current_ref_at,
    )

    grok = state.get_provider_state("grok-build-review")
    if grok is not None:
        grok.state = STATE_COST_BLOCKED
        grok.last_error = "fixture: daily budget exceeded"
        state.upsert_provider_state(grok)

    state.record_event(category="command", message="fixture: enqueued fx-running-1")
    state.record_event(category="steering", message="fixture: parsed steering input (PARSED): '/pause fx-queued-1' -> pause")
    state.record_event(category="command", level="warning", message="fixture: fx-blocked-1 waiting on dependency")

    # Runbooks (ENG-AGENT-02-S5): one of each status so Playwright can exercise
    # every card action without ever spawning a real worker process. Only the
    # DRAFT one is ever started by a test, and always with dry_run=true.
    finish_pr_preset = PRESETS["finish-pr"]
    state.upsert_runbook(
        Runbook(
            id="fx-rb-draft",
            name="Fixture draft runbook",
            preset="finish-pr",
            objective=finish_pr_preset.objective_template.format(source_ref="PR #999"),
            source_ref="PR #999",
            branch=FIXTURE_SECOND_BRANCH,
            worktree=str(second_worktree),
            parent_worker="claude-code",
            max_duration_minutes=120,
            phases=finish_pr_preset.phases,
        )
    )
    state.upsert_task(
        Task(
            id="fx-rb-running-session",
            task_ref="ENG-AGENT-02-S5",
            role="primary-implementation",
            worker="claude-code",
            state="RUNNING",
            launch_mode="session",
            worktree=str(root),
        ),
        updated_at=other_ref_at,
    )
    overnight_preset = PRESETS["overnight-development"]
    running_rb = Runbook(
        id="fx-rb-running",
        name="Fixture overnight run",
        preset="overnight-development",
        objective=overnight_preset.objective_template.format(source_ref="ENG-AGENT-02-S5"),
        source_ref="ENG-AGENT-02-S5",
        branch=FIXTURE_BRANCH,
        worktree=str(root),
        parent_worker="claude-code",
        max_duration_minutes=480,
        phases=overnight_preset.phases,
        status="RUNNING",
        task_id="fx-rb-running-session",
    )
    running_rb.started_at = utc_now_iso()
    state.upsert_runbook(running_rb)
    state.upsert_usage_governance(
        {
            "runbook_id": running_rb.id,
            "task_id": running_rb.task_id,
            "classification": "routine",
            "codex_policy": "conserve",
            "codex_auto_eligible": False,
            "max_codex_invocations": 1,
            "codex_invocations": 0,
            "telemetry_quality": "unknown",
            "route_history": [{"role": "primary-implementation", "worker": "claude-code"}],
            "context_manifest": {"paths": ["AGENTS.md"]},
        }
    )

    state.upsert_task(
        Task(
            id="fx-rb-done-session",
            task_ref="ENG-AGENT-02-S5",
            role="primary-implementation",
            worker="claude-code",
            state="SUCCEEDED",
            result="PASS",
            launch_mode="session",
            worktree=str(root),
        ),
        updated_at=other_ref_at,
    )
    test_fix_preset = PRESETS["test-fix"]
    done_rb = Runbook(
        id="fx-rb-done",
        name="Fixture completed runbook",
        preset="test-fix",
        objective=test_fix_preset.objective_template.format(source_ref="ENG-AGENT-02"),
        source_ref="ENG-AGENT-02",
        branch=FIXTURE_BRANCH,
        worktree=str(root),
        parent_worker="claude-code",
        max_duration_minutes=120,
        phases=test_fix_preset.phases,
        status="SUCCEEDED",
        task_id="fx-rb-done-session",
        report_markdown="# Morning report — fixture\n\n- Final status: SUCCEEDED\n",
    )
    state.upsert_runbook(done_rb)

    state.upsert_task(
        Task(
            id="fx-rb-recoverable-session",
            task_ref="ENG-PC-07",
            role="primary-implementation",
            worker="claude-code",
            state="RECOVERABLE_ORPHAN",
            pid=1,
            ownership_evidence_class="PID_ONLY",
            recovery_attempts=0,
            result="PASS",
            launch_mode="session",
            runbook_id="fx-rb-recoverable",
            worktree=str(root),
        ),
        # A legacy epoch placeholder supplies no identity boundary. PID 1 is
        # observable in the fixture process, leaving a deliberate PID_ONLY
        # AMBIGUOUS proof for the disabled-control browser assertion. Keeping
        # the placeholder chronologically old also prevents this presentation
        # fixture from stealing the Live Workflow card from ENG-AGENT-02.
        updated_at="1970-01-01T00:00:00+00:00",
    )
    recoverable_rb = Runbook(
        id="fx-rb-recoverable",
        name="Fixture recoverable orphan run",
        preset="test-fix",
        objective=test_fix_preset.objective_template.format(source_ref="ENG-PC-07"),
        source_ref="ENG-PC-07",
        branch=FIXTURE_BRANCH,
        worktree=str(root),
        parent_worker="claude-code",
        max_duration_minutes=120,
        phases=test_fix_preset.phases,
        status="SUCCEEDED",
        task_id="fx-rb-recoverable-session",
        acceptance_stage="review",
        acceptance_evidence={
            "implementation": {"status": "PASS"},
            "review": {"status": "PASS", "tree_sha": "fixture-old-tree"},
            "test": {"status": "PASS", "tree_sha": "fixture-old-tree"},
        },
        report_markdown="# Recoverable orphan fixture\n",
    )
    state.upsert_runbook(recoverable_rb)

    state.upsert_task(
        Task(
            id="fx-rb-failed-session",
            task_ref="V1-01",
            role="primary-implementation",
            worker="claude-code",
            state="FAILED",
            result="FAIL",
            last_error="Claude subscription weekly limit reached; reset later",
            failed_worker_id="claude-code",
            failure_execution_system="Claude Code",
            failure_provider="Anthropic",
            failure_model=registry.get("claude-code").default_model,
            failure_category="QUOTA",
            failure_reason_sanitized="Claude subscription weekly limit reached; reset later",
            failure_reset="later",
            failure_reset_source="provider diagnostic",
            launch_mode="session",
            worktree=str(video_editor_worktree),
        ),
        updated_at=other_ref_at,
    )
    failed_rb = Runbook(
        id="fx-rb-failed",
        name="Fixture blocked V1-01 run",
        preset="overnight-development",
        objective=overnight_preset.objective_template.format(source_ref="V1-01"),
        source_ref="V1-01 (Local import.)",
        branch=FIXTURE_VIDEO_EDITOR_BRANCH,
        worktree=str(video_editor_worktree),
        parent_worker="claude-code",
        max_duration_minutes=120,
        permission_profile="repo_configured_auto",
        codex_policy="unrestricted",
        codex_auto_eligible=True,
        max_codex_invocations=1,
        phases=overnight_preset.phases,
        status="FAILED",
        task_id="fx-rb-failed-session",
    )
    state.upsert_runbook(failed_rb)

    state.upsert_task(
        Task(
            id="fx-rb-fallback-session",
            task_ref="ENG-AGENT-07",
            role="primary-implementation",
            worker="codex-build",
            state="RUNNING",
            pid=4242,
            failed_worker_id="claude-code",
            failure_execution_system="Claude Code",
            failure_provider="Anthropic",
            failure_model=registry.get("claude-code").default_model,
            failure_category="QUOTA",
            failure_reason_sanitized="Claude subscription weekly limit reached; reset later",
            fallback_selected_worker="codex-build",
            fallback_automatic=True,
            launch_mode="session",
            runbook_id="fx-rb-fallback",
            worktree=str(root),
        ),
        updated_at=other_ref_at,
    )
    fallback_rb = Runbook(
        id="fx-rb-fallback",
        name="Fixture automatic fallback run",
        preset="overnight-development",
        objective=overnight_preset.objective_template.format(source_ref="ENG-AGENT-07"),
        source_ref="ENG-AGENT-07",
        branch=FIXTURE_BRANCH,
        worktree=str(root),
        parent_worker="codex-build",
        max_duration_minutes=120,
        permission_profile="repo_configured_auto",
        codex_policy="unrestricted",
        codex_auto_eligible=True,
        max_codex_invocations=1,
        phases=overnight_preset.phases,
        status="RUNNING",
        task_id="fx-rb-fallback-session",
    )
    state.upsert_runbook(fallback_rb)
    state.upsert_usage_governance(
        {
            "runbook_id": fallback_rb.id,
            "task_id": fallback_rb.task_id,
            "classification": "routine",
            "codex_policy": "unrestricted",
            "codex_auto_eligible": True,
            "max_codex_invocations": 1,
            "codex_invocations": 1,
            "route_history": [
                {
                    "worker": "claude-code",
                    "provider": "Anthropic",
                    "status": "FAILED",
                    "failure_category": "QUOTA",
                    "failure_reason": "Claude subscription weekly limit reached; reset later",
                    "automatic": False,
                },
                {
                    "worker": "codex-build",
                    "provider": "OpenAI",
                    "status": "RUNNING",
                    "automatic": True,
                    "from_worker": "claude-code",
                    "failure_category": "QUOTA",
                },
            ],
            # OCTAREL-UI-06 (issue #25): exact CLI-reported counts on this
            # record so the usage panel's MEASURED path is exercised alongside
            # the UNKNOWN one on the other fixture runbook.
            "telemetry_quality": "exact",
            "input_tokens": 142800,
            "output_tokens": 9310,
            "context_manifest": {},
        }
    )

    # OCTAREL-UI-07 (issue #42): one usage record per billing class, so the
    # browser suite can assert that a subscription-included run, an API-billed
    # run, a free-route run and an unclassified one are each presented as what
    # they are. The route history records cost_class the way a real run now
    # does, which is also what makes these rows independent of whatever
    # workers.json happens to say today.
    for runbook_id, history, tokens, reported in (
        (
            "fx-usage-api-billed",
            [{"worker": "grok-build", "provider": "xAI", "model": "grok-4.6", "cost_class": "metered-configured"}],
            (80_000, 4_000),
            None,
        ),
        (
            # OCTAREL-UI-08 (issue #44): an API-billed run whose CLI reported
            # its own cost. Deliberately a figure the derived path could not
            # produce, so a row showing the derived number here would be
            # visible as a bug rather than a coincidence.
            "fx-usage-reported-cost",
            [{"worker": "grok-build", "provider": "xAI", "model": "grok-4.6", "cost_class": "metered-configured"}],
            (61_000, 3_100),
            (0.69791188, "worker CLI structured total_cost_usd"),
        ),
        (
            # A subscription-backed run whose CLI also reports a cost. The
            # Claude CLI emits total_cost_usd meaning "what this would have
            # cost on the API", not "what you were charged". It must stay
            # $0.00 incremental: the route decides, never the monetary field.
            "fx-usage-subscription-reported",
            [{
                "worker": "claude-code", "provider": "Anthropic",
                "model": "claude-sonnet-5", "cost_class": "premium-subscription",
            }],
            (44_000, 2_200),
            (0.51234567, "worker CLI structured total_cost_usd"),
        ),
        (
            "fx-usage-free-route",
            [
                {
                    "worker": "opencode2-gemini-flash-lite",
                    "provider": "Google",
                    "model": "google/gemini-3.5-flash-lite",
                    "cost_class": "free-verified",
                }
            ],
            (51_000, 2_400),
            None,
        ),
        (
            # No cost_class anywhere, so billing cannot be established. This
            # row exists to prove the panel says so instead of showing $0.00.
            "fx-usage-unclassified",
            [{"worker": "retired-worker", "provider": "Somebody", "model": "mystery-model"}],
            (12_000, 800),
            None,
        ),
    ):
        state.upsert_usage_governance(
            {
                "runbook_id": runbook_id,
                "task_id": f"{runbook_id}-task",
                "classification": "routine",
                "codex_policy": "conserve",
                "telemetry_quality": "exact",
                "input_tokens": tokens[0],
                "output_tokens": tokens[1],
                # OCTAREL-UI-08 (issue #44): a reported cost belongs to the
                # attempt that produced it, not to the record.
                "route_history": [{
                    **history[0],
                    "status": "SUCCEEDED",
                    "ended_at": utc_now_iso(),
                    **({"reported_cost_usd": reported[0], "reported_cost_source": reported[1]}
                       if reported else {}),
                }],
                "context_manifest": {},
            }
        )

    # ENG-PC-05 scope C: exercise every truthful budget presentation path.
    # The run-scoped token budget contains only the successful attempt with
    # trustworthy counts, so it honestly demonstrates WARNING.  The wider
    # task-scoped token budget also contains the legacy failed attempt whose
    # tokens are UNKNOWN; it must therefore stay UNKNOWN rather than present
    # the successful attempt's partial sum as the task total.
    for budget in (
        {
            "id": "fx-budget-token-warning", "scope_type": "run",
            "scope_key": "fx-rb-fallback:attempt:2", "constraint_type": "tokens",
            "limit_value": 200_000, "warning_fraction": 0.75,
        },
        {
            "id": "fx-budget-token-unknown", "scope_type": "task",
            "scope_key": "fx-rb-fallback-session", "constraint_type": "tokens",
            "limit_value": 200_000, "warning_fraction": 0.75,
        },
        {
            "id": "fx-budget-attempt-block", "scope_type": "task",
            "scope_key": "fx-rb-fallback-session", "constraint_type": "attempts",
            "limit_value": 0, "warning_fraction": 0.8,
        },
        {
            "id": "fx-budget-zero-cash", "scope_type": "task",
            "scope_key": "fx-rb-fallback-session", "constraint_type": "metered_cash_usd",
            "limit_value": 1, "warning_fraction": 0.8,
        },
        {
            "id": "fx-budget-quota-unknown", "scope_type": "provider",
            # Presentation-only provider scope: no configured route uses this
            # fixture identity, so UNKNOWN progress cannot veto an unrelated
            # dry-run admission while still exercising truthful UNKNOWN UI.
            "scope_key": "Fixture Provider", "constraint_type": "provider_quota_reserve",
            "limit_value": 10, "warning_fraction": 0.8,
        },
    ):
        state.upsert_usage_budget(budget)

    # Keep scope C's synthetic budget evidence off the ENG-PC-04 run whose
    # event ordering and content are pinned independently.  This runbook is a
    # presentation fixture only: it shares the budgeted task because the
    # events describe that task, but owns its own run/event sequence.
    budget_rb = Runbook(
        id="fx-rb-budget",
        name="Fixture budget evidence run",
        preset="test-fix",
        objective=test_fix_preset.objective_template.format(source_ref="ENG-PC-05"),
        source_ref="ENG-PC-05",
        branch=FIXTURE_BRANCH,
        worktree=str(root),
        parent_worker="codex-build",
        max_duration_minutes=120,
        phases=test_fix_preset.phases,
        status="SUCCEEDED",
        task_id="fx-rb-fallback-session",
        report_markdown="# Budget evidence fixture\n",
    )
    state.upsert_runbook(budget_rb)

    for status, event_type, level, budget_id, scope in (
        ("WARNING", "usage.budget_warning", "warning", "fx-budget-token-warning", "run:fx-rb-fallback:attempt:2"),
        ("BLOCKED", "usage.budget_blocked", "error", "fx-budget-attempt-block", "task:fx-rb-fallback-session"),
    ):
        state.record_run_event(
            RunEvent(
                run_id=budget_rb.id,
                event_class="usage",
                event_type=event_type,
                source="control_plane.usage_budgets",
                provenance="MEASURED",
                message=f"usage budget {budget_id} at {scope} is {status}",
                task_id=budget_rb.task_id,
                level=level,
                data={
                    "status": status,
                    "budget_id": budget_id,
                    "bounding_scope": scope,
                    "evidence_class": "MEASURED",
                },
            )
        )

    # ENG-PC-04: a real typed event plus an existing .agent-output pointer for
    # the chronological timeline and Run Detail browser coverage.
    event_run_dir = root / ".agent-output" / "ENG-AGENT-02-S5" / "claude-code" / "fixture-event"
    event_run_dir.mkdir(parents=True, exist_ok=True)
    event_summary = event_run_dir / "summary.md"
    event_summary.write_text("Fixture measured lease evidence.\n", encoding="utf-8")
    state.record_run_event(
        RunEvent(
            run_id="fx-rb-running",
            event_class="lease",
            event_type="lease.acquired",
            source="control_plane.execution_lease",
            provenance="MEASURED",
            message="Execution lease acquired",
            task_id="fx-rb-running-session",
            data={"generation": 1, "worker": "claude-code"},
            evidence={"summary": event_summary.relative_to(root).as_posix()},
        ),
        repo_root=root,
    )

    # This fixture deliberately seeds more concurrently RUNNING work (two
    # plain tasks plus two write session runbooks) than production's default
    # caps (ENG-AGENT-10) allow, specifically to make the dashboard's
    # simultaneous-execution views exercisable. Give the harness headroom
    # above those already-saturated fixture defaults for the dry-run tests
    # that admit one additional real write task through the managed path,
    # without changing the shipped production ConcurrencyPolicy defaults.
    scheduler = Scheduler(ConcurrencyPolicy(max_global_workers=6, max_write_workers=4, max_extra_read_workers=2))
    supervisor = Supervisor(registry=registry, repo_root=root, state=state)

    # ENG-CP-03 (issue #165): register the fixture root as the OctaScene
    # project and adopt every record seeded above into it through the *real*
    # migration path -- the same `migrate_legacy_state_to_project` an existing
    # installation runs on first open. That keeps every pre-existing panel
    # populated and simultaneously exercises the migration in browser coverage.
    # A second, unrelated repository is registered so the selector genuinely has
    # two projects to switch between.
    project_b_root = _git_init_second_fixture_project(root)
    state.upsert_project(
        contract_to_row(
            ProjectContract(
                project_id=OCTASCENE_PROJECT_ID,
                display_name=OCTASCENE_DISPLAY_NAME,
                local_repo_root=root,
                default_branch=FIXTURE_BRANCH,
                github_remote=OCTASCENE_GITHUB_REMOTE,
                policy_entrypoints=("AGENTS.md",),
                roadmap_paths=("docs/PRODUCT_ROADMAP.md",),
                task_sources=(str(VIDEO_EDITOR_LEDGER_RELATIVE),),
                validation_command=("python3", "scripts/ci/local_gate.py", "--docs-reviewed"),
                capabilities={
                    "task_source_adapter": "video_editor_ledger",
                    "validation_adapter": "exact_tree_local_gate",
                    "validation_authority": "repository-owned-exact-tree-local-gate",
                    "app_lifecycle_command": "make run",
                    "app_lifecycle_port": "8765",
                    "runtime_service_argv": '["make", "run"]',
                    "runtime_service_port": "18765",
                    "runtime_service_host": "127.0.0.1",
                    "runtime_service_name": "Fixture development app",
                },
            )
        )
    )
    state.upsert_project(
        contract_to_row(
            ProjectContract(
                project_id=FIXTURE_PROJECT_B_ID,
                display_name=FIXTURE_PROJECT_B_NAME,
                local_repo_root=project_b_root,
                default_branch=FIXTURE_PROJECT_B_BRANCH,
                github_remote=FIXTURE_PROJECT_B_REMOTE,
                task_sources=("TASKS.md",),
                capabilities={"task_source_adapter": "file_ledger"},
            )
        )
    )
    migrate_legacy_state_to_project(state, OCTASCENE_PROJECT_ID)
    select_project(state, OCTASCENE_PROJECT_ID)

    # ENG-PC-06: two real deliveries make the Run Detail context inspector
    # exercise a measured zero delta, a positive DERIVED saving, bounded
    # ancestry with visible truncation, and advisory Graphify status. Prompt
    # and Graphify text are intentionally distinctive so browser/API tests can
    # prove neither secret-bearing body is exposed by the inspector.
    context_identity = ContextIdentity(
        project_id=OCTASCENE_PROJECT_ID,
        task_id="fx-rb-running-session",
        consumer_id="claude-code",
        tree_sha="a" * 40,
        policy_digest="fixture-policy-v1",
        task_contract_digest="fixture-contract-v1",
    )
    context_policy = PolicyBundle(
        prompt="FIXTURE_RAW_COMPOSED_PROMPT_MUST_NOT_APPEAR\n",
        manifest={
            "preserved_policy_identity": {
                "universal_policy": "AGENTS.md",
                "role": "IMPLEMENTER",
                "workflow": "IMPLEMENT",
                "provider": "OpenAI",
            }
        },
    )
    context_ancestry = AncestryRequest(
        parent_program="ENG-PC",
        parent_task="ENG-PC-04",
        source_references=(
            "https://github.com/ACGE248/octarel/issues/34",
            "docs/ROADMAP.md#ENG-PC-06",
        ),
        limit=3,
    )
    for _ in range(2):
        build_incremental_context(
            state,
            identity=context_identity,
            policy_bundle=context_policy,
            ancestry=context_ancestry,
            graph_context_supplier=lambda: {
                "text": "FIXTURE_GRAPH_TEXT_MUST_NOT_APPEAR",
                "evidence": {"status": "READY"},
            },
        )

    ctx = CommandContext(state=state, registry=registry, scheduler=scheduler, supervisor=supervisor, repo_root=root)
    # ENG-PC-09: a durable external/unowned service row proves both inspectors
    # render operational metadata while withholding every destructive control.
    runtime = RuntimeServiceManager(ctx)
    external = runtime._declared_row(str(root), "fx-rb-running")
    external.update(
        health="EXTERNAL",
        ownership="EXTERNAL_UNOWNED",
        owner=None,
        status_reason="fixture external listener is informational and cannot be signalled",
    )
    state.upsert_runtime_service(external)
    return ctx


def reset_fixture_context(ctx: CommandContext, root: Path) -> CommandContext:
    """Re-seed the Playwright fixture database in place.

    The Control Center Playwright matrix shares one ``serve_fixture`` process
    across every viewport project. Mutating tests (start/pause/stop runbooks)
    otherwise leak buttons and statuses into later projects. This rebuilds the
    same deterministic seed ``build_fixture_context`` produced at startup.
    """

    # Build the replacement completely before swapping it into the live app.
    # Closing/deleting the current SQLite connection first races the browser's
    # concurrent dashboard reads and produced "closed database" errors during
    # viewport resets. Old connections remain valid for already-started reads
    # and disappear with this throwaway fixture process.
    reset_db = default_db_path(root).with_name(f"reset-{uuid.uuid4().hex}.db")
    fresh = build_fixture_context(root, state_path=reset_db)
    ctx.state = fresh.state
    ctx.registry = fresh.registry
    ctx.scheduler = fresh.scheduler
    ctx.supervisor = fresh.supervisor
    ctx.repo_root = fresh.repo_root
    from scripts.agents.control_plane.operations import refresh_worktree_statuses

    refresh_worktree_statuses(ctx)
    return ctx


def _build_remote_state(hostname: str) -> tuple[RemoteAccessState, object]:
    """A fully local, no-network Cloudflare-Access-shaped remote state for tests.

    Returns the :class:`RemoteAccessState` to pass into ``create_app`` and the
    freshly generated RSA private key, so ``main()`` can also mount a
    test-only token-minting route signing with that same key.
    """

    from cryptography.hazmat.primitives.asymmetric import rsa
    from jwt.algorithms import RSAAlgorithm

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(RSAAlgorithm.to_jwk(private_key.public_key()))
    jwk["kid"] = FIXTURE_KID
    jwk["alg"] = "RS256"
    jwk["use"] = "sig"
    jwks = {"keys": [jwk]}

    config = RemoteAccessConfig(
        enabled=True,
        hostname=hostname,
        team_domain=FIXTURE_TEAM_DOMAIN,
        audience=FIXTURE_AUDIENCE,
        allowed_emails=frozenset({FIXTURE_ALLOWED_EMAIL}),
    )
    verifier = AccessVerifier(config, jwks_fetcher=lambda url: jwks)
    return RemoteAccessState(config=config, verifier=verifier), private_key


def _mount_test_token_route(app, private_key) -> None:
    """Test-only route: mint a Cloudflare-Access-shaped JWT signed with the

    fixture's own throwaway key. Exists only on this Playwright fixture
    process, never on the real ``create_app`` used by the product dashboard —
    a Playwright test has no other way to produce a validly-signed token
    without either a real Cloudflare account or a JS-side JWT library, and
    this keeps every signing/verification code path exercised in Python,
    where it is already unit-tested.
    """

    import time

    import jwt as pyjwt

    @app.get("/test/mint-remote-token")
    def mint_remote_token(email: str = FIXTURE_ALLOWED_EMAIL) -> dict[str, str]:
        now = int(time.time())
        claims = {
            "email": email,
            "aud": FIXTURE_AUDIENCE,
            "iss": f"https://{FIXTURE_TEAM_DOMAIN}",
            "iat": now,
            "exp": now + 3600,
            "sub": "fixture-user",
        }
        token = pyjwt.encode(claims, private_key, algorithm="RS256", headers={"kid": FIXTURE_KID})
        return {"token": token}


def _fixture_manager_invoker(argv, timeout):
    """Deterministic stand-in for a natural-language interpreter.

    Recognizes a couple of fixture phrases so the Manager Chat UI can be driven
    end to end, and returns "no single matching command" for anything else.
    Never spawns a process and never contacts a provider.
    """

    # Not argv[-1]: a worker's CLI template may place {prompt} anywhere, so
    # scan the whole command rather than assuming it is the final argument.
    lowered = " ".join(str(part) for part in (argv or [])).lower()
    if "wind things down" in lowered:
        # Maps to a destructive verb on purpose: the browser suite asserts that
        # a natural-language destructive request still has to be confirmed.
        reply = {"verb": "stop", "args": {"task_id": "fx-running-1"}, "summary": "Stop task fx-running-1"}
    elif "take a break" in lowered:
        reply = {"verb": "pause", "args": {"task_id": "fx-running-1"}, "summary": "Pause task fx-running-1"}
    else:
        reply = {"verb": None, "summary": "no single matching command"}
    return 0, json.dumps(reply), ""


def _fixture_pricing_book():
    """A fixed API-equivalent price list (OCTAREL-UI-07, issue #42).

    The real loader reads whatever OpenCode catalog snapshot happens to be
    cached on the machine, which would make every money assertion in the
    browser suite depend on a developer's local state. This book is pinned
    instead, and deliberately omits Anthropic so the "model the catalog does
    not carry" path stays exercised alongside the priced ones.

    Rates are the catalog's own units: US dollars per million tokens.
    """

    from scripts.agents.control_plane import pricing as _pricing
    from scripts.agents.model_catalog import Catalog, CatalogModel

    def model(model_id: str, cost: dict) -> CatalogModel:
        provider_id, _, bare = model_id.partition("/")
        return CatalogModel(
            id=model_id, provider_id=provider_id, model_id=bare, display_name=bare, family="",
            status="active", api_url="", context_limit=200000, output_limit=8192, cost=cost,
            credential="api", cost_class="metered", cost_reason="", capable=True, capability_reasons=(),
        )

    return _pricing.book_from_catalog(
        Catalog(
            status="ok",
            reason="fixture",
            opencode_version="fixture",
            refreshed_at=utc_now_iso(),
            credentials_status="ok",
            models=(
                model("xai/grok-4.6", {"input": 3.0, "output": 15.0}),
                model("openai/gpt-5.6-sol", {"input": 1.25, "output": 10.0}),
                model("google/gemini-3.5-flash-lite", {"input": 0.1, "output": 0.4}),
            ),
        )
    )


def _mount_test_repo_root_route(app, ctx) -> None:
    """Test-only route: expose this fixture process's isolated repo_root so a
    Playwright spec running in the same host process's filesystem (see the
    existing ``realWorktreePath`` comment in control-center.spec.js -- "the
    fixture server and this test process share one host") can write a real
    ``.agent-output/<task_ref>/<worker>/<run_id>/`` fixture tree for
    OCTAREL-UI-01's Agent Activity viewer before opening it, the same way
    other fixtures here seed real on-disk state rather than mocking the API.
    Never mounted on the real product dashboard's ``create_app``.
    """

    @app.get("/test/repo-root")
    def repo_root_probe() -> dict[str, str]:
        return {"root": str(ctx.repo_root)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8899)
    parser.add_argument("--root", default=None, help="isolated root dir; defaults to a fresh temp dir")
    args = parser.parse_args()
    _start_parent_watchdog(_parent_identity())

    root = Path(args.root) if args.root else Path(tempfile.mkdtemp(prefix="octages-control-center-fixture-"))
    root.mkdir(parents=True, exist_ok=True)
    ctx = build_fixture_context(root)
    remote_state, private_key = _build_remote_state(f"{args.host}:{args.port}")
    app = create_app(
        ctx,
        roadmap_path=root / "docs" / "PRODUCT_ROADMAP.md",
        remote=remote_state,
        # OCTAREL-UI-05 (issue #24): Manager Chat's natural-language interpreter
        # is a deterministic fake in the fixture. The browser suite must never
        # make a live or billable provider call, and a fixed reply also keeps
        # the assertions deterministic.
        manager_invoker=_fixture_manager_invoker,
        # OCTAREL-UI-07 (issue #42): a pinned price list, so money figures in
        # the browser suite never depend on the host's cached catalog.
        pricing_loader=_fixture_pricing_book,
    )
    _mount_test_token_route(app, private_key)
    _mount_test_repo_root_route(app, ctx)

    @app.post("/__fixture__/reset")
    def reset_fixture() -> dict[str, bool]:
        """Test-harness only: never mounted on the production dashboard."""

        reset_fixture_context(ctx, root)
        return {"ok": True}

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
