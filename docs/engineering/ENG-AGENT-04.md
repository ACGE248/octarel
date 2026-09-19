# ENG-AGENT-04 — normalized agent policy and fallback execution

Status: complete; merged through PR #113 at `a3f5515006c4bdf958d807936237462fbafe193b`,
based on merged #109/#110 through PR #112 (`2964765513377e80b2d655551e30461b570d2c39`).

`AGENTS.md` is the concise universal entrypoint. Canonical reusable policy now lives by concern under
`.agents/core`, `.agents/roles`, `.agents/workflows`, and `.agents/providers`. Root `CLAUDE.md` remains only
as Claude Code's native direct-session bootstrap; orchestrated Claude and every non-Claude worker receive an
explicit deterministic bundle from `scripts/agents/policy.py`.

The loader extends the existing ENG-AGENT-03 context manifest. It records policy paths/version, core/role/
workflow/provider selection, bounded contracts, capability, context size, exclusions, fallback reason, and
actual worker/provider/model without invoking AI. Registry validation fails closed for missing provider
policy, incompatible canonical roles, write workers without isolation, or API-billing routes.

Provider fallback preserves the bundle identity and read/write mode. Quota/rate/auth/outage/capability
failures may choose an eligible equivalent-role provider; context overflow first requests minimization;
reasoning failure uses evidence-based escalation; safety/policy/secret/worktree/authorization failures block.
Codex conserve/budget/override rules and exact-tree review evidence remain authoritative.

`.agents/core/TESTING.md` defines T0 static, T1 focused, T2 subsystem, and T3 full validation. The existing
change-risk/local-gate seam records the tier, review level, and path-relevant product versus Control Center
audit scope. Normal work does not require version/release-note churn unless it is release-bearing.
