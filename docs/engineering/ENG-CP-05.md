# ENG-CP-05 / CPX-05 — Extract Octarel into a standalone repository

**Issue:** [#171](https://github.com/ACGE248/octages/issues/171)
**Status:** implementation on the dedicated extraction branch / standalone checkout.
**Depends on:** CPX-01–CPX-04 merged (`bd9abcca` on `ACGE248/octages`).

## Goal

Create `ACGE248/octarel` as an independently installable orchestration control plane. OctaScene
(`ACGE248/octages`) remains a **managed project**. Octarel must not become a second source of OctaScene
product truth.

This slice does **not** delete the embedded Control Plane from Octages, does **not** switch
`dev.octascene.com`, and does **not** make Octarel public.

## Layout

- GitHub: `https://github.com/ACGE248/octarel` (private until audit + explicit publish).
- Local canonical checkout: an operator-chosen directory (do not commit machine-specific paths).
- Runtime package: copied `scripts/agents` plus vendored `scripts/ci` (the CPX-02 intra-repo dependency,
  now inside Octarel rather than imported from Octages).
- Public CLI: `python -m octarel`.

## State

Standalone state defaults to this checkout's `.orchestrator-state/`, overridable with `OCTAREL_STATE_DIR`.
`python -m octarel migrate --from <octages-state-dir>` uses SQLite backup, never deletes the source, and is
idempotent on the same source snapshot.

## Cutover

CPX-05 did not switch `dev.octascene.com`. Permanent cutover is CPX-06; see
`docs/engineering/ENG-CP-06.md` and `docs/engineering/CUTOVER.md`.
