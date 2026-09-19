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

Use the existing deterministic impact selector and exact-tree local gate. Never create a competing
classifier or acceptance system. Never delete, skip, `xfail`, relax, or rewrite a legitimate test to
turn a gate green. Diagnose first, fix the root cause, and rerun only evidence invalidated by the fix;
rerun T3 only when the T3 evidence itself became stale.

`scripts/ci/test_impact.py` is the executable-selection seam beneath the risk authority: T1 resolves exact
owned tests plus smoke/contract anchors, T2 resolves subsystem/domain/contract groups, and T3 retains the
complete applicable suites. Unknown executable paths fail closed to T3. The local gate records its phase DAG,
isolated ports, environment fingerprint, concrete selectors, and deterministic evidence-reuse proof. Reuse is
allowed only when command, toolchain/environment, and all phase-relevant input fingerprints are identical.

UI audit scope is path/risk based: none for backend/prose; focused product or Control Center audit for
an ordinary scoped UI change; full relevant matrix for shared shell/design-system/navigation/
accessibility or release/high-risk UI. Do not run smoke plus full when full already supplies the evidence.

The authoritative local gate binds commands/results to the exact candidate tree. High/critical risk
also binds read-only independent-review evidence to that tree. GitHub Actions is not acceptance evidence.
