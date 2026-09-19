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

## Universal invariants

- Normal work never writes directly to `main`/`master`. Substantial work uses a dedicated branch/worktree,
  and exactly one write-capable owner may use a checkout at a time.
- Preserve unrelated work. Never force-push, destructively reset/clean, or bypass worktree locks.
- Never expose secrets or cross repository-data authorization boundaries.
- Never make live/billable product-provider calls without explicit authorization or silently enable API-key,
  paid, or premium fallback.
- Never bypass provider enablement, capability, CC-required, spend, accounting, authorization, or
  production-readiness controls.
- Never delete, skip, `xfail`, relax, or rewrite legitimate tests merely to pass.
- Use risk-based deterministic local validation. Exact-tree local-gate evidence—not GitHub Actions—is the
  engineering acceptance authority.
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
