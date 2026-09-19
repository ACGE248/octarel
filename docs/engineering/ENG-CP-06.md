# ENG-CP-06 / CPX-06 — Cut over OctaScene orchestration to Octarel

**Issue:** [#173](https://github.com/ACGE248/octages/issues/173)
**Depends on:** CPX-01–CPX-05 (`ACGE248/octarel` extracted and private).
**Status:** complete on `main` (PR #1). Public hardening is CPX-07.

## Goal

Make Octarel the canonical running orchestration system for OctaScene
development. Switch `dev.octascene.com` to standalone Octarel after local
acceptance. Reduce Octages to a managed project. Do not start CPX-07.

## Ownership

Octarel owns orchestration runtime, dashboard, state, providers/workers,
generic orchestration policies, project registry, runbooks, orchestration
history/evidence, and service lifecycle.

Octages (`ACGE248/octages`) owns OctaScene product truth: `AGENTS.md`,
`docs/PRODUCT_ROADMAP.md`, program/task ledgers, ADRs, repository
validation, and OctaScene-specific compatibility/configuration.

Octarel never copies OctaScene product truth into its tree.

## State

1. Snapshot both embedded Octages `.orchestrator-state/` and current
   Octarel state with `python -m octarel cutover snapshot`.
2. Import the latest pre-cutover embedded database with
   `python -m octarel cutover adopt --from <octages-state>` (SQLite backup;
   source is never deleted).
3. A second adopt of the same snapshot is a no-op (`already-imported`).
4. Restart must not duplicate tasks/runbooks/events.

Operator runbook and rollback: [`CUTOVER.md`](CUTOVER.md).

## Remote access

The tunnel still terminates on `127.0.0.1:8877`. Cutover changes which
local process owns that port. Cloudflare Access is unchanged and remains
the public boundary. Do not bind Octarel on a non-loopback address.

## Service persistence

`scripts/octarel-dashboard-service.sh` plus `python -m octarel service`
install/status/stop own the macOS launchd dashboard. Install copies the
wrapper to `~/Library/Application Support/Octarel/` so launchd does not
depend on an external volume at login. The pid file is written next to that
wrapper (launchd cannot write onto an external-volume state dir). The wrapper
execs a Unix CLI interpreter (never `Python.app`), refuses a foreign 8877
listener, and no-ops when Octarel is already healthy. Cloudflare Access
identifiers stay in the operator's local plist, never in git.

## Embedded Octages daemon

The embedded implementation stays in Octages as rollback. After CPX-06 it
refuses to start as a primary dashboard/daemon unless
`OCTAGES_EMBEDDED_ORCHESTRATOR_FALLBACK=1`.

## Out of scope

- Making `ACGE248/octarel` public (CPX-07)
- Mass-deleting the embedded Control Plane
- Deleting historical Octages orchestration state
- Starting the next OctaScene product task
