# Codex / OpenAI provider adapter

The configured Codex routes use the maintainer's subscription-authenticated CLI session. They never fall
back to `OPENAI_API_KEY` or another billable API route. Codex Build is write-capable; Codex Review is a
separate read-only route.

Use Terra for routine bounded coding when sufficient, Sol for serious implementation/integration, and
Astra only for the hardest architecture/debugging after lower tiers prove insufficient. The configured
registry model is the last verified baseline. AO may advance it to a strictly newer model of the *same tier* that
`codex debug models` lists for the logged-in ChatGPT session (ENG-AO-04; for example a newer Sol), never to a different
tier (Astra remains a bounded-evidence escalation) and never through an API key; if unverifiable the baseline stays. Runbooks retain `conserve` by default plus the
ENG-AGENT-03 eligibility, invocation-limit, and audited override semantics. Avoid Codex for mechanical work
that an authorized lower-cost worker can perform.
