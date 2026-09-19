# Architecture

Octarel is a standalone orchestration control plane. It is not a product
monorepo and it is not a second copy of any managed project's truth.

## Ownership

| Octarel owns | Each managed repository owns |
|---|---|
| scheduler, runbooks, durable orchestration | `AGENTS.md` / repository policy |
| provider and worker adapters | roadmap, ledgers, ADRs |
| worktree locks and provisioning | repository-owned validation command |
| process supervision | application source and tests |
| review/test dispatch (generic) | product/provider/spend rules |
| SQLite orchestration state | GitHub issues/PRs for that repo |
| Control Center dashboard | |
| generic Git/GitHub integration | |

The process runs from the Octarel checkout (`cp_code_root()`). Process cwd is
never implicit project truth. A selected project's root is always an explicit
registry row.

## Layout

- `octarel/` — public CLI (`python -m octarel`)
- `scripts/agents/` — orchestrator, workers, Control Plane
- `scripts/ci/` — exact-tree local gate, public-safety, clean-room
- `scripts/agents/control_plane/dashboard/` — Control Center static UI
- `.agents/` — Octarel's own agent policy (not a managed project's policy)
- `.orchestrator-state/` — gitignored SQLite (or `OCTAREL_STATE_DIR`)

## Projects

`scripts/agents/control_plane/project.py` defines a generic
`ProjectContract`. OctaScene-specific path declarations live only in
`octascene_project.py`. Every other repository uses the generic registry,
file-ledger / GitHub-issues task adapters, and the declared validation
command.

## Runtime identity

`python -m octarel health` and `/api/cp-status` report:

- `code_root` / `cp_code_root` — Octarel checkout
- `selected_project_root` — managed repository
- `state_db` — orchestration SQLite
- `runtime=octarel`

`code_root_equals_selected_project` must be false in normal operation.
