# Providers and subscription-backed workers

Octarel delegates to **local CLIs** configured in `scripts/agents/workers.json`.
It never silently enables an API key, paid overflow, or premium fallback.

Check local status (auth probes are local CLI commands, not product APIs):

```bash
python -m octarel providers
python -m octarel providers --probe
```

`--probe` may run `claude auth status`, `codex login status`, and similar
metadata commands. It does not make live product-provider generation calls.

## Supported workers

| Worker | System | Auth | Capability | Isolated worktree | Silent API fallback |
|---|---|---|---|---|---|
| `claude-code` | Claude Code | Claude.ai subscription session (`claude auth status` must show `claude.ai`, never an API key) | write (ORCHESTRATOR/IMPLEMENTER) | required | prohibited |
| `codex-build` | Codex | ChatGPT subscription (`codex login status`) | write | required | prohibited |
| `codex-review` | Codex | same ChatGPT session | read-only review | not required | prohibited |
| `grok-build` | Grok Build | configured CLI session | write (IMPLEMENTER) | required | prohibited |
| `grok-build-review` | Grok Build | configured CLI session | read-only review (plan + sandbox) | not required | prohibited |
| `opencode2-gemini-flash-lite` | OpenCode | local OpenCode session | read-only tests/search | not required | prohibited |
| `opencode2-gemini-flash-lite-review` | OpenCode | local OpenCode session | read-only review | not required | prohibited |
| `antigravity-focused-tests` | Antigravity (`agy`) | local Antigravity session | read-only tests | not required | prohibited |
| `antigravity-impact-search` | Antigravity | local session | read-only search | not required | prohibited |
| `antigravity-doc-drift` | Antigravity | local session | read-only docs review | not required | prohibited |
| `antigravity-diff-review` | Antigravity | local session | read-only diff review; **disabled for unattended use while headless command permission is deterministically auto-denied** | not required | prohibited |
| `deepseek-overflow` | OpenCode | manual API authorization | focused-edit | required | **disabled**; never auto-selected |

## Model selection

Each worker's `default_model` in `workers.json` is authoritative. Operators
may pass an explicit model only when the worker CLI supports `{model}`.
Intensity maps to CLI effort flags where the worker declares them. Codex
model aliases already encode capability tier; no extra effort flag is
templated.

## Write vs review

Write workers require an isolated worktree and a write lock. Review workers
are read-only (`plan`/`sandbox`/`read-only` as declared). An implementer must
not be its own final independent reviewer. Exact-tree high-risk gates reject
Claude/Anthropic and Codex as the independent review provider.

## Limits and fallback

Quota, rate limits, missing CLI, and unauthenticated sessions are reported as
availability reasons (`NOT_AUTHENTICATED`, `CLI_MISSING`, `DISABLED`, …).
They do **not** authorize a stronger model or a paid API route. DeepSeek
overflow stays disabled unless `--allow-overflow` and prior authorization.

Runbook `worker_routes` are authoritative allowlists. Dispatch, retry, resume,
fallback, and acceptance stages may use only the workers listed for that role.
Quick Start exposes and persists the same selected route object; a rejected
start rolls back its provisional draft instead of leaving a reusable runbook.
If none is eligible, the stage blocks and reports each unavailability reason;
it never widens to the registry route. The disabled Antigravity diff-review
route therefore yields to the configured free
`opencode2-gemini-flash-lite-review` route when that route is permitted.

## Policy adapters

Provider-specific execution notes live under `.agents/providers/`. Canonical
cost/safety policy is `.agents/core/COST_AND_PROVIDER_SAFETY.md`.
