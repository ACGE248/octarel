# OpenCode dynamic-model provider adapter

`opencode-free-review` and `opencode-free-tests` are read-only fallback workers whose model is chosen per run from
the installed OpenCode's *current* catalog (`scripts/agents/model_catalog.py`) instead of from `workers.json`. They
supplement, and never replace, the configured Gemini/Antigravity/Grok/Codex routes.

- **Discovery** uses OpenCode's machine-readable local commands only. An absent, unauthenticated, failing, or
  incomplete OpenCode yields no candidates rather than an error or a guess.
- **Free means proven free.** A model is a free-fallback candidate only when OpenCode's own metadata shows an
  OpenCode Zen transport with zero declared cost on every price field. A name containing "Free" proves nothing.
  OAuth/subscription providers are `subscription`, API-key or paid providers are `metered`, and anything unclear is
  `unknown`; none of these enter automatic free fallback. No API-key billing, purchase, or top-up is ever used.
- **Qualification before trust.** A free model must pass one bounded, cached probe (launch, agent preset, read-only
  permissions, unchanged tree, strict review contract, no billing signal) before it may serve unattended.
- **Provider diversity holds.** A candidate from the vendor that produced the change is rejected whenever independent
  review is required, even when it is free.
- **No silent escalation.** A quota/rate/context/outage failure cools that model down so the next attempt uses another
  free model; it never selects a stronger, subscription, or paid one. These workers are never implementers.
- **Grok/xAI through OpenCode** appears in the same generic catalog (as subscription-backed when OAuth-authenticated)
  but is not automatically routed and does not replace native Grok.

Configured OpenCode data authorization is unchanged: minimized, redacted, task-relevant context only.
