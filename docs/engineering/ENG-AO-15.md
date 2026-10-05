# ENG-AO-15 — enforce the whole-worktree session write boundary

Issue #55 identified two inconsistent execution paths. `run_delegation` failed a read-only worker that changed the
worktree, while `run_session` accepted a read-only worker under the standard profile and could record an editing
process as `PASS`. The `session --worker` contract already required a write-capable worker; only the enforcement was
conditional on a non-standard permission profile.

## Admission contract

`run_session` now rejects every worker that is not write-capable immediately after worker/role validation. The
check is independent of permission profile, so neither `standard` nor `repo_configured_auto` can admit a read-only
worker. Write-capable workers still must separately declare unattended-write support and use the exact profile
named by `AdapterCapabilities.unattended_write`; this change does not weaken those ENG-AO-16 controls.

## Runtime defense in depth

Delegated and whole-worktree execution now use the same `_record_worktree_changes` helper after a primary process
finishes. The helper records changed tracked/unignored paths and, when the worker is read-only, sets
`RunRecord.read_only_violation`, changes the result to `FAIL`, and writes the existing contract-violation note.
`manifest.classify_failure` therefore yields `READ_ONLY_VIOLATION` from the typed flag on either path, independent
of provider prose or `boundary_evidence`.

The runtime check is deliberately retained even though normal session admission rejects read-only workers. It is a
backstop against an admission regression or alternate internal caller. A write-capable session that legitimately
changes the worktree records its paths and otherwise follows the existing result classification unchanged.

## Verification

Deterministic coverage proves read-only session rejection under every known permission profile, bypasses admission
in one test to exercise and classify a real session worktree mutation, preserves the existing delegation-path
classification, and confirms an authorized write-capable session can change the tree and pass.
