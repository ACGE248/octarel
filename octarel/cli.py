"""User-facing Octarel CLI.

Delegates to ``scripts.agents.orchestrator`` for daemon/dashboard/status
commands and adds standalone migrate/health/cutover entrypoints.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _octarel_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _ensure_sys_path() -> None:
    root = str(_octarel_root())
    if root not in sys.path:
        sys.path.insert(0, root)


def _default_state_dir() -> Path:
    env = os.environ.get("OCTAREL_STATE_DIR")
    if env:
        return Path(env)
    return _octarel_root() / ".orchestrator-state"


def main(argv: list[str] | None = None) -> int:
    _ensure_sys_path()
    parser = argparse.ArgumentParser(prog="octarel", description="Standalone Octarel orchestration control plane")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("version", help="print Octarel version")
    p_dash = sub.add_parser("dashboard", help="serve the Control Center dashboard")
    p_dash.add_argument("--host", default="127.0.0.1")
    p_dash.add_argument("--port", type=int, default=8877)
    p_run = sub.add_parser("run", help="run the orchestrator daemon")
    p_run.add_argument("--poll-interval", type=float, default=2.0)
    p_run.add_argument("--dry-run", action="store_true")
    sub.add_parser("status", help="print daemon/status summary")
    sub.add_parser("health", help="print code-root, state path, and selected project")
    p_mig = sub.add_parser("migrate", help="import Octages-hosted orchestration SQLite into Octarel state")
    p_mig.add_argument("--from", dest="source", required=True, help="source .orchestrator-state directory or orchestrator.db")
    p_mig.add_argument("--to", dest="destination", default=None, help="destination state directory (default: Octarel .orchestrator-state)")
    p_mig.add_argument("--dry-run", action="store_true")
    p_mig.add_argument(
        "--replace-unmigrated",
        action="store_true",
        help="replace a CPX-05 bootstrap destination that has no migration SHA (never deletes the Octages source)",
    )

    p_cut = sub.add_parser("cutover", help="CPX-06 snapshot, adopt, and rollback helpers")
    cut_sub = p_cut.add_subparsers(dest="cutover_cmd", required=True)
    p_snap = cut_sub.add_parser("snapshot", help="write a deterministic pre-cutover checkpoint")
    p_snap.add_argument("--octages-state", required=True, help="embedded Octages .orchestrator-state directory")
    p_snap.add_argument("--octarel-state", default=None, help="current Octarel .orchestrator-state directory")
    p_snap.add_argument("--out", dest="out", default=None, help="checkpoint parent directory")
    p_adopt = cut_sub.add_parser("adopt", help="import latest Octages state into Octarel (idempotent)")
    p_adopt.add_argument("--from", dest="source", required=True)
    p_adopt.add_argument("--to", dest="destination", default=None)
    p_adopt.add_argument("--dry-run", action="store_true")
    cut_sub.add_parser("rollback-plan", help="print the documented rollback procedure")

    p_svc = sub.add_parser("service", help="dashboard persistence status/start/stop/install")
    svc_sub = p_svc.add_subparsers(dest="service_cmd", required=True)
    svc_sub.add_parser("status", help="print whether Octarel owns 127.0.0.1:8877")
    svc_sub.add_parser("stop", help="stop the Octarel dashboard listener if we own it")
    p_inst = svc_sub.add_parser("install-launchd", help="write a local launchd plist (never committed)")
    p_inst.add_argument("--dest", default=None, help="plist path (default: ~/Library/LaunchAgents/com.octascene.orchestrator-dashboard.plist)")
    p_inst.add_argument("--octascene-root", default=None)
    p_canon = svc_sub.add_parser("canonicalize-state", help="copy live state onto the canonical Octarel checkout (never deletes the source)")
    p_canon.add_argument("--from", dest="source", required=True)
    p_canon.add_argument("--to", dest="destination", default=None)
    p_canon.add_argument("--dry-run", action="store_true")

    p_proj = sub.add_parser("project", help="list/add/select/remove managed repositories")
    proj_sub = p_proj.add_subparsers(dest="project_cmd", required=True)
    proj_sub.add_parser("list", help="print registered projects")
    p_detect = proj_sub.add_parser("detect", help="inspect a local folder without registering it")
    p_detect.add_argument("path")
    p_add = proj_sub.add_parser("add", help="register a managed repository")
    p_add.add_argument("--id", dest="project_id", required=True)
    p_add.add_argument("--path", required=True)
    p_add.add_argument("--name", default=None)
    p_add.add_argument("--remote", default=None)
    p_add.add_argument("--branch", default=None)
    p_add.add_argument("--policy", action="append", default=[])
    p_add.add_argument("--tasks", action="append", default=[])
    p_add.add_argument("--validate", nargs="+", default=[])
    p_sel = proj_sub.add_parser("select", help="select a registered project")
    p_sel.add_argument("project_id")
    p_rm = proj_sub.add_parser("remove", help="unregister a project (repository is untouched)")
    p_rm.add_argument("project_id")
    p_rm.add_argument("--confirm", action="store_true")

    p_prov = sub.add_parser("providers", help="local non-billable worker health/status")
    p_prov.add_argument("--probe", action="store_true", help="run local CLI auth checks (never API billing)")

    p_pub = sub.add_parser("public-safety", help="scan the tree (and history) for secrets/private material")
    p_pub.add_argument("--tree-only", action="store_true")
    p_pub.add_argument("--publication-gate", action="store_true")

    args = parser.parse_args(argv)
    if args.cmd == "version":
        from octarel import __version__

        print(__version__)
        return 0
    if args.cmd == "health":
        return _cmd_health()
    if args.cmd == "service":
        return _cmd_service(args)
    if args.cmd == "project":
        return _cmd_project(args)
    if args.cmd == "providers":
        return _cmd_providers(probe=args.probe)
    if args.cmd == "public-safety":
        from scripts.ci.public_safety import main as public_safety_main

        argv = ["--root", str(_octarel_root())]
        if args.tree_only:
            argv.append("--tree-only")
        if args.publication_gate:
            argv.append("--publication-gate")
        return public_safety_main(argv)
    if args.cmd == "cutover":
        return _cmd_cutover(args)
    if args.cmd == "migrate":
        from scripts.agents.control_plane.state_migration import (
            migrate_orchestrator_state,
        )

        dest = Path(args.destination) if args.destination else _default_state_dir()
        report = migrate_orchestrator_state(
            source=Path(args.source),
            destination=dest,
            dry_run=args.dry_run,
            replace_unmigrated_destination=args.replace_unmigrated,
        )
        print(report.as_text())
        return 0 if report.ok else 1
    if args.cmd in {"dashboard", "run", "status"}:
        os.environ.setdefault("OCTAREL_CODE_ROOT", str(_octarel_root()))
        from scripts.agents import orchestrator

        orch_argv = [args.cmd]
        if args.cmd == "dashboard":
            orch_argv.extend(["--host", args.host, "--port", str(args.port)])
        elif args.cmd == "run":
            orch_argv.extend(["--poll-interval", str(args.poll_interval)])
            if args.dry_run:
                orch_argv.append("--dry-run")
        return orchestrator.main(orch_argv)
    return 2


def _cmd_cutover(args: argparse.Namespace) -> int:
    from scripts.agents.control_plane.cutover import (
        adopt_octages_state,
        default_checkpoint_root,
        rollback_procedure_text,
        write_cutover_checkpoint,
    )

    if args.cutover_cmd == "rollback-plan":
        print(rollback_procedure_text())
        return 0
    if args.cutover_cmd == "snapshot":
        out = Path(args.out) if args.out else default_checkpoint_root(_octarel_root())
        octarel_state = Path(args.octarel_state) if args.octarel_state else _default_state_dir()
        report = write_cutover_checkpoint(
            destination=out,
            octages_state=Path(args.octages_state),
            octarel_state=octarel_state,
        )
        print(report.as_text())
        return 0 if report.ok else 1
    if args.cutover_cmd == "adopt":
        dest = Path(args.destination) if args.destination else _default_state_dir()
        report = adopt_octages_state(source=Path(args.source), destination=dest, dry_run=args.dry_run)
        print(report.as_text())
        return 0 if report.ok else 1
    return 2


def _cmd_service(args: argparse.Namespace) -> int:
    from scripts.agents.control_plane.service import (
        DEFAULT_PORT,
        LABEL,
        fetch_health,
        install_user_wrapper,
        is_octarel_listener,
        listener_pid,
        render_launchd_plist,
        stop_pid,
    )

    port = int(os.environ.get("OCTAREL_DASHBOARD_PORT") or DEFAULT_PORT)
    root = _octarel_root()
    if args.service_cmd == "status":
        pid = listener_pid(port)
        body = fetch_health(port)
        print(f"port={port}")
        print(f"pid={pid or ''}")
        print(f"runtime={(body or {}).get('runtime', '')}")
        print(f"cp_code_root={(body or {}).get('cp_code_root', '')}")
        print(f"octarel_listener={str(is_octarel_listener(port, code_root=root)).lower()}")
        return 0 if body and body.get("runtime") == "octarel" else 1
    if args.service_cmd == "stop":
        if not is_octarel_listener(port, code_root=root):
            print("error: no Octarel dashboard listener to stop", file=sys.stderr)
            return 1
        pid = listener_pid(port)
        if pid:
            stop_pid(pid)
        print(f"stopped pid={pid}")
        return 0
    if args.service_cmd == "canonicalize-state":
        from scripts.agents.control_plane.service import (
            canonical_state_dir,
            canonicalize_state_dir,
        )

        dest = Path(args.destination) if args.destination else canonical_state_dir(root)
        report = canonicalize_state_dir(source=Path(args.source), destination=dest, dry_run=args.dry_run)
        print(report.as_text())
        print("source_preserved=true")
        return 0 if report.ok else 1
    if args.service_cmd == "install-launchd":
        dest = Path(args.dest) if args.dest else (Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist")
        extra: dict[str, str] = {}
        if dest.is_file():
            import plistlib

            existing = plistlib.loads(dest.read_bytes())
            extra = {str(k): str(v) for k, v in (existing.get("EnvironmentVariables") or {}).items()}
        octascene = args.octascene_root or os.environ.get("OCTAREL_OCTASCENE_ROOT") or extra.get("OCTAREL_OCTASCENE_ROOT")
        wrapper = install_user_wrapper(root)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(
            render_launchd_plist(
                code_root=root,
                octascene_root=Path(octascene) if octascene else None,
                extra_env=extra,
                wrapper_path=wrapper,
            )
        )
        print(f"wrote {dest}")
        print(f"wrapper {wrapper}")
        print(f"bootstrap: launchctl bootstrap gui/$(id -u) {dest}")
        return 0
    return 2


def _cmd_health() -> int:
    from scripts.agents.control_plane.project import cp_code_root
    from scripts.agents.control_plane.project_registry import selected_project
    from scripts.agents.control_plane.state import (
        DB_FILENAME,
        State,
        default_db_path,
        resolve_standalone_db_path,
    )

    root = cp_code_root()
    state_dir = _default_state_dir()
    resolved = resolve_standalone_db_path()
    db = resolved if resolved is not None else (state_dir / DB_FILENAME if (state_dir / DB_FILENAME).is_file() else default_db_path(root))
    cwd = Path.cwd().resolve()
    print(f"runtime=octarel")
    print(f"code_root={root}")
    print(f"process_cwd={cwd}")
    print(f"state_dir={state_dir}")
    print(f"state_db={db}")
    print(f"state_exists={db.exists()}")
    selected_id = ""
    selected_root = ""
    if db.exists():
        state = State(db)
        try:
            from scripts.agents.control_plane.project_registry import bootstrap_registry

            bootstrap_registry(state, os.environ.get("OCTAREL_OCTASCENE_ROOT"))
        except Exception:
            pass
        project = selected_project(state)
        if project is not None:
            selected_id = project.project_id
            selected_root = str(project.local_repo_root)
    print(f"selected_project_id={selected_id}")
    print(f"selected_project_root={selected_root}")
    cwd_is_project = bool(selected_root) and cwd == Path(selected_root).resolve()
    print(f"cwd_equals_selected_project={str(cwd_is_project).lower()}")
    print(f"code_root_equals_selected_project={str(bool(selected_root) and root.resolve() == Path(selected_root).resolve()).lower()}")
    return 0


def _open_state():
    from scripts.agents.control_plane.state import (
        State,
        default_db_path,
        resolve_standalone_db_path,
    )

    resolved = resolve_standalone_db_path()
    db = resolved if resolved is not None else default_db_path(_octarel_root())
    return State(db)


def _cmd_project(args: argparse.Namespace) -> int:
    from scripts.agents.control_plane.project_registry import (
        detect_project,
        list_projects,
        register_project,
        remove_project,
        select_project,
    )

    state = _open_state()
    if args.project_cmd == "list":
        rows = list_projects(state)
        if not rows:
            print("projects=")
            return 0
        for row in rows:
            print(
                f"{row['project_id']}\t{row['display_name']}\t{row['local_repo_root']}\t{row.get('github_remote') or ''}"
            )
        return 0
    if args.project_cmd == "detect":
        detected = detect_project(args.path)
        for key, value in detected.items():
            print(f"{key}={value}")
        return 0
    if args.project_cmd == "add":
        payload = {
            "project_id": args.project_id,
            "display_name": args.name or args.project_id,
            "local_repo_root": args.path,
        }
        if args.remote:
            payload["github_remote"] = args.remote
        if args.branch:
            payload["default_branch"] = args.branch
        if args.policy:
            payload["policy_entrypoints"] = args.policy
        if args.tasks:
            payload["task_sources"] = args.tasks
            payload["capabilities"] = {"task_source_adapter": "file_ledger"}
        if args.validate:
            payload["validation_command"] = args.validate
            payload.setdefault("capabilities", {})["validation_adapter"] = "declared_command"
        contract = register_project(state, payload)
        print(f"registered={contract.project_id}")
        print(f"local_repo_root={contract.local_repo_root}")
        return 0
    if args.project_cmd == "select":
        contract = select_project(state, args.project_id)
        print(f"selected={contract.project_id}")
        print(f"local_repo_root={contract.local_repo_root}")
        return 0
    if args.project_cmd == "remove":
        result = remove_project(state, args.project_id, confirm=args.confirm)
        print(f"removed={result['removed']}")
        print(f"repository_untouched={result['repository_untouched']}")
        return 0
    return 2


def _cmd_providers(*, probe: bool) -> int:
    from scripts.agents.registry import load_registry

    registry = load_registry(_octarel_root() / "scripts/agents/workers.json")
    for name, worker in registry.workers.items():
        reason = worker.availability_reason(probe=probe)
        print(
            f"{name}\tprovider={worker.provider}\tcapability={worker.capability}\t"
            f"auth_mode={worker.auth_mode}\tallow_api_billing={str(worker.allow_api_billing).lower()}\t"
            f"availability={reason}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
