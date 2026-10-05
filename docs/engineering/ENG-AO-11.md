# ENG-AO-11 — native Grok review response adapter

Issue #49 recorded an incompatibility between native Grok review output and the canonical independent-review
contract. The Grok CLI can concatenate lead-in narration directly onto the model's final text. A requested bare
`READY` therefore arrived as prose immediately followed by `READY`, which the parser correctly rejected rather than
inferring readiness from vague prose.

## Provider-specific shape

The shared review parser remains unchanged. Instead, `.agents/providers/GROK.md` now requires
`grok-build-review` to use the canonical structured shape on every response:

```text
Blockers: None
Important findings: None
Minor findings: None
Test gaps: None
READY
```

The reviewer replaces `None` with complete findings when needed and ends a findings response with standalone
`BLOCKED`. It starts at `Blockers:` and emits nothing after the verdict. This policy file is part of every Grok
review bundle, so callers do not need to repeat the transport workaround in individual prompts.

The parser already recognizes a field label concatenated after unavoidable transport narration, but it deliberately
does not accept narration concatenated onto a bare `READY`. That distinction preserves the fail-closed guarantee:
all four fields and a terminal verdict remain necessary whenever the response is not exactly bare `READY`.

## Verification

Deterministic coverage proves a Grok review command receives the provider adapter even when the caller merely asks
to review the candidate, the observed glued-preamble structured shape parses, and the equivalent prose-prefixed bare
shape remains rejected. Final acceptance also requires a real native `grok-build-review` dispatch to produce a
parser-accepted response without caller-supplied shape instructions.
