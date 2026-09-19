# AGENTS.md

Universal repository rules for Octarel development agents.

## Bootstrap and scope

1. Read this file first. Repository code and maintained canonical documentation override memory, chat,
   handoffs, and historical evidence.
2. Determine the assigned role, workflow, provider, capability, task scope, and stable task/reference.
3. Load only the relevant canonical policies indexed by `.agents/README.md` plus affected program/ADR/task
   contracts. Provider-native files are adapters, not policy authorities.
4. Inspect current Git status, branch/worktree, merge base, and relevant maintained implementation state.

Octarel orchestrates work against **managed projects**. A managed project's own `AGENTS.md`, roadmap, and
validation command remain authoritative for that project. Never copy a managed project's product truth into
Octarel state.

## Octarel owner override

The repository owner may explicitly waive Octarel-only process requirements for a bounded change. An override
must be stated in the current instruction and should identify the control being waived when practical.

Owner-overridable Octarel process controls include:
- dedicated branch/worktree requirements;
- PR/review workflow;
- independent-provider review;
- focused, subsystem, full-suite, browser, UI-audit, or exact-tree gate execution;
- documentation reconciliation;
- commit/push/merge sequencing.

For a valid owner override:
- follow the override only for the stated Octarel change; do not silently generalize it;
- report every waived control as `WAIVED_BY_OWNER` in the final result;
- never describe skipped review/tests/gates as passed or produce fabricated evidence;
- preserve existing legitimate tests even when their execution is waived;
- do not apply an Octarel override to a managed project such as OctaScene unless that project's own current
  policy is separately satisfied or explicitly changed.

The following protections are not process conveniences and are not waived by a generic "skip rules" request:
never expose secrets, cross repository-data authorization boundaries, make unauthorized live/billable calls,
bypass spend/provider/security controls, falsify evidence, or destructively overwrite unrelated work. Any
instruction seeking one of those actions must be handled explicitly and safely.

## Universal invariants

- Default development is risk-based. Low-risk Octarel-only work may use the canonical checkout; a separate
  sibling worktree is not required. Use a dedicated branch by default unless the owner explicitly authorizes
  direct canonical-main work for a bounded low-risk change.
- Substantial, parallel, stateful, security-sensitive, migration, provider-routing, or cross-cutting work should
  use a dedicated branch/worktree unless explicitly waived by the owner.
- Exactly one write-capable owner may use a checkout at a time. Parallel writers require separate worktrees
  unless the owner deliberately serializes ownership.
- Preserve unrelated work. Never force-push, destructively reset/clean, or bypass worktree locks.
- Never expose secrets or cross repository-data authorization boundaries.
- Never make live/billable product-provider calls without explicit authorization or silently enable API-key,
  paid, or premium fallback.
- Never bypass provider enablement, capability, CC-required, spend, accounting, authorization, or
  production-readiness controls.
- Never delete, skip, `xfail`, relax, or rewrite legitimate tests merely to pass. Owner test waivers skip
  execution only; they do not permit weakening the test suite.
- Use risk-based deterministic validation by default. Exact-tree local-gate evidence is the engineering
  acceptance authority when that gate is required and not explicitly waived.
- Keep Octarel code-root distinct from every selected project root. Process cwd is never implicit project truth.
- Report actual providers/models/results and failure/fallback reasons. Never claim work or evidence that did
  not run on the stated candidate.

## Canonical policy pointers

- Core: `.agents/core/SECURITY.md`, `GIT_WORKTREES.md`, `TESTING.md`, `DOCUMENTATION.md`,
  `COST_AND_PROVIDER_SAFETY.md`
- Roles: `.agents/roles/`
- Workflows: `.agents/workflows/`
- Provider adapters: `.agents/providers/`
- Executable worker facts: `scripts/agents/workers.json`
