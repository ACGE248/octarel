# ENG-AO-17 — explicit reviewer execution capability

Issue #63 identified a contract mismatch: Octarel could compose a review brief asking a read-only reviewer to
execute a decisive test even though the enforced reviewer boundary did not allow it. Repeated denied attempts
wasted review turns and made a correctly bounded review look shallow. The reviewer sandbox was correct; the
orchestrator lacked a typed fact describing what the selected reviewer could execute.

## Contract

Every worker serving `diff-review` or `doc-drift-review` now declares exactly one `review_execution` object in
`scripts/agents/workers.json`:

- `permission_profile`: the applicable profile (`standard` for every review route);
- `mode`: `prompt-context-only`, `allowlisted-read-only`, `sandboxed-read-only`, or `unavailable`;
- `supports_test_execution`: whether Octarel supports test execution for that reviewer;
- `allowed_command_prefixes`: the guaranteed read-only prefixes, populated only for allowlisted mode; and
- `reason`: a non-empty explanation of the concrete boundary.

Registry loading fails closed when a review worker omits the object, a non-review worker declares it, the mode
and allowlist disagree, or an unavailable route remains enabled. `AdapterCapabilities.review_execution` exposes
the same facts as a typed, side-effect-free view. It does not infer capability from a model name, CLI, sandbox
prose, or generic read-only filesystem access.

## Current reviewer boundaries

All current review routes declare `supports_test_execution: false`.

- OpenCode review uses `allowlisted-read-only` and guarantees only `git diff`, `git status`, `git show`, and `rg`,
  matching the unchanged `.opencode/agents/reviewer.md` enforcement preset.
- Native Grok review uses `prompt-context-only`; its template passes `--tools ""` and receives the bounded diff
  in prompt context.
- Codex review and Antigravity documentation review use `sandboxed-read-only`, with no Octarel-guaranteed shell
  or test prefixes.
- Antigravity diff review uses `unavailable` and remains disabled while its headless transport auto-denies the
  command permission needed to inspect a diff.

## Review-brief protocol

`run_delegation` injects guidance derived from the actual selected worker's declaration into every diff or
documentation review prompt. When tests are unsupported, the reviewer must reason from the bounded code/diff and
supplied evidence, never attempt tests, builds, or counterfactual commands. Caller/orchestrator `--check` entries
are redacted and labelled as evidence the reviewer did not execute.

If an unsupplied counterfactual result would decide the review, the reviewer reports the missing evidence and
exact command under `Test gaps`. An eligible test route runs it; a new review dispatch presents the result through
`--check`. Prompt text grants no additional permission, and no provider allowlist or sandbox was widened.

## Verification

Focused tests cover registry completeness and malformed declarations, adapter serialization for multiple review
modes, parity between OpenCode declarations and the enforced preset, prompt injection, evidence redaction, and the
absence of review guidance on non-review routes. The existing adapter and orchestration suites continue to cover
all registered workers and provider-neutral dispatch behavior.
