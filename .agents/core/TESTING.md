# Deterministic testing

Risk selects the evidence; opening a PR does not automatically require the full suite.

- **T0 static:** prose, metadata, or tiny configuration. Run whitespace/parse/reference/targeted lint
  checks that can validate the change.
- **T1 focused:** ordinary bounded implementation. Run exact affected tests plus relevant
  build/lint/compile checks.
- **T2 subsystem:** a stabilized shared feature or subsystem. Run its related domain and contract
  tests plus applicable build/lint/compile checks.
- **T3 full:** releases, migrations, broad persistence/core contracts, test/build/gate changes,
  large cross-cutting refactors, explicit program gates, or unclassifiable high-risk changes.

Use the existing deterministic impact selector and exact-tree local gate when validation is required. Never
create a competing classifier or acceptance system. Never delete, skip, `xfail`, relax, or rewrite a legitimate
test merely to turn a gate green. Diagnose first, fix the root cause, and rerun only evidence invalidated by the
fix; rerun T3 only when the T3 evidence itself became stale.

`scripts/ci/test_impact.py` is the executable-selection seam beneath the risk authority: T1 resolves exact
owned tests plus smoke/contract anchors, T2 resolves subsystem/domain/contract groups, and T3 retains the
complete applicable suites. Unknown executable paths fail closed to T3. The local gate records its phase DAG,
isolated ports, environment fingerprint, concrete selectors, and deterministic evidence-reuse proof. Reuse is
allowed only when command, toolchain/environment, and all phase-relevant input fingerprints are identical.

UI audit scope is path/risk based: none for backend/prose; focused product or Control Center audit for an
ordinary scoped UI change; full relevant matrix for shared shell/design-system/navigation/accessibility or
release/high-risk UI. Do not run smoke plus full when full already supplies the evidence.

## Owner waiver

The repository owner may explicitly waive any Octarel test, browser run, UI audit, independent review, or
local-gate requirement for a bounded change.

A waiver changes the required process; it does not create passing evidence:
- record skipped items as `WAIVED_BY_OWNER`;
- do not claim skipped checks passed;
- do not fabricate manifests, reports, counts, or reviewer verdicts;
- do not weaken/delete/xfail legitimate tests to implement the waiver;
- run whatever narrower checks the owner still requests;
- if all validation is waived, report that the change was integrated without validation by explicit owner
  instruction.

High/critical risk normally binds read-only independent-review evidence to the exact candidate tree, but the
owner may explicitly waive that review for Octarel. The same applies to exact-tree local-gate execution.
GitHub Actions is not acceptance evidence.

Managed projects keep their own testing/review policy; an Octarel waiver does not automatically waive
OctaScene or another selected project's gates.
