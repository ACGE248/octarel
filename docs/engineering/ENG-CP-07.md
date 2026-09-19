# ENG-CP-07 / CPX-07 — Public hardening and multi-repo acceptance

**Issue:** [#175](https://github.com/ACGE248/octages/issues/175)
**Parent:** [#165](https://github.com/ACGE248/octages/issues/165)
**Depends on:** CPX-06 merged (`ACGE248/octarel` canonical runtime).
**Publication:** the public-visible git history must be the CPX-07 public-safe
snapshot, not the private extraction history.

## Goal

Make Octarel genuinely public/open-source ready and prove it is a standalone
multi-repository orchestration system. Octarel owns generic orchestration
mechanisms. Managed repositories own their own truth.

## In this slice

- Public-safety scanner for the current tree and git history
- LICENSE / NOTICE / THIRD_PARTY attribution
- Clean-room clone/install/start
- Two-repository acceptance (OctaScene + generic)
- Provider/subscription documentation and local health
- Release/upgrade/migration procedure (no tag until gates pass)
- Canonical live state/service ownership
- Embedded Octages runtime stays **rollback-only**
- Control Center browser acceptance
- Documentation in both repositories

## Out of scope

- Starting CPX-08 or OctaScene product work
- Live/billable product-provider calls
- Paid CI/runners
- Weakening tests
- Making the GitHub repository public before every #175 gate is green
- Deleting OctaScene-owned policy, roadmap, ledgers, ADRs, or validation

## Embedded Octages retirement

The embedded Control Plane in `ACGE248/octages` remains the documented
rollback. It refuses to start as primary unless
`OCTAGES_EMBEDDED_ORCHESTRATOR_FALLBACK=1`.

Retirement condition for a later explicit issue: Octarel has been the only
primary for a complete development cycle, rollback has not been required, and
an operator-reviewed change removes the duplicated runtime without touching
OctaScene product truth.
