# Grok provider adapter

Grok Build may implement in its own dedicated write worktree; Grok Build Review is a separate read-only
plan+sandbox route. Use only when the configured xAI session, repository authorization, metered-cost policy,
and assigned role permit it. Web search and subagents remain disabled for repository delegation.

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
