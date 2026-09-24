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

## Controlled Grok bot fan-out

For complex work AO can explicitly select `grok-build-bots`: AO runs a bounded (max 3), one-level, read-only,
parallel set of `grok-build-bot` workers inside the primary's checkout write lock, each with a scoped Graphify
slice, then starts the single write-capable primary with their findings. Evidence lives in the ordinary run
manifest (`policy_manifest.bot_fanout`) and the existing dashboard `subagents` field. See
`scripts/agents/README.md` (Controlled Grok bot fan-out).

## Graphify repository intelligence

Octarel can optionally pass bounded, tree-matched Graphify code-graph context (symbols, imports,
dependents, callers/callees, likely tests) to workers as an advisory section of the normal context bundle.
Graphify is derived data below the managed project's source tree, policy/docs, and task contracts; it is
cached under Octarel state keyed by project + worktree + content tree, never inside the managed repository,
and never used by the exact-tree gate. Absent or stale Graphify degrades to normal repository inspection.
See `scripts/agents/README.md` (Graphify repository intelligence) for the contract.

## Runtime polling and process ownership

The Control Center keeps active task/run state live while caching expensive
Git-derived worktree/checkpoint facts for short, labelled TTLs. Its browser
refresh resolves project selection first, then fetches independent read models
concurrently; normal monitoring never probes or invokes an AI provider.

Delegated workers, terminal shells, test lanes, and managed development apps
start in owned process sessions. Cancellation and shutdown terminate only
process groups whose PID/session handle or durable PID/create-time/cwd/argv
metadata proves Octarel ownership. Octarel never kills by executable name.
