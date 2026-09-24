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
| `grok-build-bots` | Grok Build | configured CLI session | write (IMPLEMENTER); explicit bot-enabled route, never automatic | required | prohibited |
| `grok-build-bot` | Grok Build | configured CLI session | read-only, non-recursive bot (plan + sandbox); launched only by AO fan-out, not a reviewer | not required | prohibited |
| `opencode2-gemini-flash-lite` | OpenCode | local OpenCode session | read-only tests/search | not required | prohibited |
| `opencode2-gemini-flash-lite-review` | OpenCode | local OpenCode session | read-only review | not required | prohibited |
| `opencode-free-review` | OpenCode (runtime free pool) | proven-free OpenCode Zen model, qualified | read-only review fallback; model chosen per run | not required | prohibited |
| `opencode-free-tests` | OpenCode (runtime free pool) | proven-free OpenCode Zen model, qualified | read-only tests/search fallback; model chosen per run | not required | prohibited |
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

## Repository intelligence (Graphify)

Every worker receives the same optional, advisory Graphify context through the normal bounded prompt/context
bundle: Claude Code, Codex, Gemini through OpenCode or Antigravity, native Grok (`grok-build` /
`grok-build-review`), and Grok/xAI (or any approved model) selected on an OpenCode worker. It is
transport-neutral, so a model change never needs new Graphify code and never changes routing priority. It is
local and deterministic (no API key, billing, or LLM enrichment), provider-native Graphify installers are not
used, and provider policy files are never rewritten. Details: `scripts/agents/README.md`.

## Native model freshness (Grok / Codex)

Native Grok and Codex workers keep stable identities and routing; their configured `default_model` is the last
verified baseline, and the installed native CLI may advance the effective model to a strictly newer same-tier model it
enumerates itself, but only when the existing subscription/session is authenticated and the worker flags and
read-only/worktree permission shape still verify (`python -m scripts.agents.native_models refresh|list`,
`GET /api/native-models`). Otherwise the configured model is retained with the reason recorded. OpenCode labels are
hints only, a different tier (for example `gpt-6-astra`) is never adopted by freshness, and no API key, paid, or
premium route is involved. Details: `scripts/agents/README.md`, `.agents/providers/GROK.md`,
`.agents/providers/CODEX_OPENAI.md`.

## Dynamic OpenCode models (free fallback)

`opencode-free-review` / `opencode-free-tests` take their model from the installed OpenCode's current catalog rather
than `workers.json` (`python -m scripts.agents.model_catalog refresh|list|qualify`, `GET /api/opencode-models`). Only a
model OpenCode's own metadata proves free (OpenCode Zen transport, zero declared cost) that also passed a cached,
bounded read-only qualification probe can serve, and only after the configured workers; `subscription`, `metered`, and
`unknown` models are catalogued but never auto-selected, same-vendor candidates are rejected when provider diversity is
required, and quota/rate/context failures move to another free model instead of a stronger one. No API key, purchase,
or top-up is ever involved. OpenCode+xAI appears in the same catalog without replacing native Grok. Details:
`scripts/agents/README.md`, `.agents/providers/OPENCODE.md`.

## Controlled Grok bot fan-out

`grok-build-bots` is an explicit, separate route for serious integration, hard debugging, architecture/high-risk
investigation, and broad impact analysis. AO runs at most three one-level, read-only `grok-build-bot` workers in
parallel with scoped Graphify slices; the primary Grok remains the only writer, keeps `--no-subagents`, and
synthesizes their findings. Bot failures are recorded once (no retry, no stronger-model escalation, no API
billing). Bot output is not independent review; provider-diverse review still applies. Normal `grok-build` and
`grok-build-review` behavior and routing priority are unchanged. A future OpenCode/xAI transport can reuse the
same orchestration through the `BotTransport` seam. Details: `scripts/agents/README.md`.

## Policy adapters

Provider-specific execution notes live under `.agents/providers/`. Canonical
cost/safety policy is `.agents/core/COST_AND_PROVIDER_SAFETY.md`.
