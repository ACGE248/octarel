# Grok provider adapter

Grok Build may implement in its own dedicated write worktree; Grok Build Review is a separate read-only
plan+sandbox route. Use only when the configured xAI session, repository authorization, metered-cost policy,
and assigned role permit it. Web search and subagents remain disabled for repository delegation.

## Grok Build Review response adapter (ENG-AO-11)

For `grok-build-review` only, never use the bare `READY` response option described by the canonical REVIEW
workflow. The native CLI can concatenate turn narration directly onto that token, making the bare shape invalid.
Always return the structured four-field shape, even when there are no findings:

```text
Blockers: None
Important findings: None
Minor findings: None
Test gaps: None
READY
```

When a field has a real finding, replace `None` with the complete finding and end with a standalone `BLOCKED`
line. Start the answer with `Blockers:`; do not add a lead-in, summary, or text after the final verdict. The
acceptance parser deliberately remains strict. This provider-specific output adapter does not authorize the
caller to relax or reinterpret the canonical review contract.

Unattended writes (ENG-AO-16) are an explicit capability, not an inference from `capability: write` or a
missing/present CLI template. `grok-build` and the explicitly selected `grok-build-bots` primary declare
`repo_configured_auto`: native `--permission-mode auto` in headless mode, where calls not accepted by the
safety classifier fail back to the model instead of prompting, plus the CLI's built-in `strict` sandbox.
That sandbox limits reads to the selected checkout and system paths and writes to the checkout, Grok session
state, and OS temporary paths. Octarel also requires its dedicated worktree and write lock. This is deliberately
not `--always-approve`, `bypassPermissions`, or an API-key route. Standard mode remains attended and is rejected
for a write-capable `session` launch.

The built-in `strict` profile is intentional. `work-tree` is not a native profile in Grok CLI 1.0.34; it only
works when a matching user or per-project `sandbox.toml` happens to exist, so advertising it globally made
managed-project eligibility depend on undeclared machine/project state. Explicitly requested custom profiles
fail closed when absent, as the verification probe confirmed. The built-in profile makes the registry command
self-contained and at least as restrictive for repository work.

Controlled bot fan-out (ENG-AO-02) is a separate, explicit route and never a change to the two routes above:
`grok-build-bots` is a write-capable Grok primary that AO may select only for serious integration, hard
debugging, architecture/high-risk investigation, or broad impact analysis. AO (not the model) first runs at
most three one-level, read-only, non-recursive `grok-build-bot` workers in parallel, each with a scoped
Graphify slice, then hands their concise findings to the primary, which stays the only writer and keeps
`--no-subagents`. Bots never commit, push, merge, or change branches; a bot that changes the checkout blocks
the primary. A failed bot is recorded once and never retried or escalated to a stronger model. Bot output is
assistance, not independent review: provider-diverse review remains a separate stage. Subscription/OIDC
session only; no API-key or paid fallback.

Native model freshness (ENG-AO-04): `grok-4.6` in `workers.json` is the last verified baseline. AO may advance the
effective model of every Grok route (including `grok-build-bots` and its bots) to a strictly newer `grok-N.M` that
`grok models` itself lists, only for an authenticated existing CLI session with unchanged flags and permission shape;
otherwise the baseline stays. A newer model never changes routing priority, permissions, or cost policy, and OpenCode
labels are hints, not native identifiers.

Configured xAI development workers are persistently pre-authorized for minimized, redacted,
task-relevant Octarel and selected managed-project repository-data reuse without repeated per-task consent. This does not waive
authentication, health, role/capability, permission-profile, worktree/write-lock, cost/concurrency,
secret-exclusion, or separate live/billable-call authorization.
