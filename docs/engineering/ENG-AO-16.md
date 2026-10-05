# ENG-AO-16 — explicit unattended-write capability

Issue #57 identified a mismatch between the implementation route and executable reality: `grok-build` was
write-capable, but the orchestrator had no explicit fact saying whether it could complete a headless write
session. The old command also named `work-tree`, which Grok CLI 1.0.34 treats as a custom sandbox profile rather
than a built-in profile. It refused to start in a clean repository without an undeclared user/project
`sandbox.toml`.

## Contract

Every write or focused-edit worker now declares one `unattended_write` object in `workers.json`:

- `supported`: whether Octarel has a bounded non-interactive invocation;
- `permission_profile`: the one profile that implements it, or `null` when unsupported;
- `reason`: a non-empty explanation surfaced by the adapter contract and routing failures.

Registry loading fails closed on missing or inconsistent declarations. `AdapterCapabilities` exposes the same
typed object without probing. Whole-worktree `session` launches, managed admission, Quick Start selection,
runbook creation/update/start/retry, and automatic fallback all require the explicit capability and exact profile.
Read-only routes do not declare the object because unattended writes do not apply to them.

## Grok invocation

`grok-build` and the explicit `grok-build-bots` primary bind `repo_configured_auto` to:

```text
--permission-mode auto --sandbox strict --no-subagents --disable-web-search
```

In headless mode, `auto` executes safety-classifier-approved work and reports rejected calls back to the model
instead of waiting for approval. It is not blanket automation: Octarel does not use `--always-approve` or
`bypassPermissions`. The CLI's built-in `strict` profile confines reads to the checkout/system paths and writes
to the checkout, Grok session state, and OS temporary paths; Octarel independently requires a dedicated Git
worktree and exclusive write lock. The worker subprocess receives Octarel's existing minimal environment
allowlist, and API-key fallback remains prohibited. Strict mode cannot read a user's global Git config, so the
runner sets `GIT_CONFIG_GLOBAL` to the OS null device for this exact command shape; repository-local and system
Git configuration remain available without granting access to the home-directory file.

The built-in profile replaces the old custom name because managed projects do not share Octarel configuration.
Depending on a per-project or global `work-tree` definition would make eligibility an undeclared machine-state
guess. Native-model freshness now accepts exactly one Grok write permission mode per template (`default` for
attended use or `auto` for the declared unattended profile) while continuing to reject always-approve/bypass,
conflicting values, missing strict sandboxing, subagents, or web search.

## Local verification

The installed native CLI was Grok 1.0.34. `grok models` confirmed an existing grok.com login and enumerated the
current Grok family without `XAI_API_KEY`. Isolated probes established that:

- native headless `auto` wrote the requested file inside the selected scratch checkout;
- the kernel sandbox rejected a write into a different task worktree;
- an explicitly requested undefined `work-tree` custom profile refused to start before model execution;
- no probe used always-approve/bypass, subagents, web search, or an API-key fallback.

The initial isolated probes reported $0.04531656 of native CLI usage, the configuration-resolution probe
reported $0.01133832, and the real adapter dispatch reported $0.03558236. Total CLI-reported usage for ENG-AO-16
verification was $0.09223724. These are actual provider amounts, not Octarel budget estimates. The real dispatch
manifest is `.agent-output/ENG-AO-16-PROBE/grok-build/20261005T173103.904558Z-30970-c4b4eed1/manifest.json`.
