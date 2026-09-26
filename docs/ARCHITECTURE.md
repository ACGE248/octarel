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

The Control Center's Agent Activity viewer surfaces the status Graphify recorded for an attempt (used,
refreshed, stale, skipped, unavailable, failed-safe, or not-recorded) with its reason, and labels it
advisory. Only that status record is exposed -- never the derived context text, its node list, or any
path -- and reading it never runs Graphify.

## Manager Chat

Manager Chat is an orchestration interface, not a model client. An operator message is first parsed by the
deterministic steering grammar; a recognized slash command or bounded intent is handled there and costs no
AI call. Only text that grammar cannot recognize is routed to an interpreter.

Interpretation uses the normal machinery: candidates come from the configured route for a read-only role, in
that order, skipping any worker that is disabled, whose CLI is unavailable, or whose provider state is not
routable -- each skip recorded with its reason. A worker that allows API billing is refused outright, so
Manager Chat can never be the path that enables paid billing. Model selection is cached-only.

The interpreter's reply is untrusted input. It may only name a verb already in the command vocabulary with a
bounded argument shape, and the verb, argument keys and value types are revalidated before display. A reply
can never introduce a new verb or reach the command layer directly.

The result is always a proposal. Execution still goes through the normal steering path, which re-derives
destructiveness itself and requires explicit confirmation, so a natural-language request for a destructive
action cannot bypass a confirmation gate. The selected worker/provider/model, and any fallback, are shown in
the conversation and recorded as events.

## Continuous overnight advancement

An overnight session (`control_plane/overnight.py`, table `overnight_sessions`) is a durable record the daemon advances
once per poll: refresh the project's repository truth, resolve the current next eligible task through
`advance_after_success` semantics, start it through Quick Start, wait for normal acceptance, verify the merge, repeat.
It is bounded by duration and accepted-task count, allows one write-capable task at a time, never names a provider or
enables paid/API fallback, and merges only with explicit per-session authorization plus the project's `overnight_merge`
capability. The dashboard/API/CLI only create and control it. See `scripts/agents/README.md`.

## Single-writer runbook advancement

Exactly one Octarel process may advance a runbook at a time (ENG-AO-07). `control_plane/advancement_lease.py` holds a
per-runbook OS advisory `flock` under `<state dir>/advancement-leases/`; `reconcile_runbooks` takes it non-blocking for
each runbook's whole reconcile step (acceptance/gate, fallback, finalization, successor advancement), re-reads the runbook
after winning it, and a process that loses only observes and skips. The kernel drops the lock when its owner exits or is
killed, so a crashed owner never needs stale-lease cleanup. `POST /api/runbooks/{id}/advance` uses the same lease and
returns 409 with the owner record instead of advancing; the overnight tick's successor advancement withholds and retries next tick when another process owns the runbook. Lease file names carry a hash of the runbook id so distinct ids never share a lease. The daemon additionally holds `daemon-authority.lock` for its
lifetime; while another process holds it the dashboard loop only polls its own subprocesses and never reconciles, so the
dashboard stays a monitoring surface. Operator pause/resume/stop remain plain state writes that the next owner honours.

## Runtime polling and process ownership

The Control Center keeps active task/run state live while caching expensive
Git-derived worktree/checkpoint facts for short, labelled TTLs. Its browser
refresh resolves project selection first, then fetches independent read models
concurrently; polling and monitoring never probe or invoke an AI provider.

The one place the Control Center can reach a provider is Manager Chat, and only
in response to an operator message it could not parse deterministically -- see
Manager Chat below. Every other surface, including every periodic refresh,
remains provider-free.

Delegated workers, terminal shells, test lanes, and managed development apps
start in owned process sessions. Cancellation and shutdown terminate only
process groups whose PID/session handle or durable PID/create-time/cwd/argv
metadata proves Octarel ownership. Octarel never kills by executable name.
