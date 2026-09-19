# Review workflow

Review only the bounded exact-tree diff and relevant contracts/evidence. Check correctness, regression,
scope, security/secrets, provider/spend behavior, persistence, tests, documentation, worktree safety, and UI/
accessibility when applicable. Do not edit, stage, commit, push, or create a PR.

## Response contract

This is the one canonical response contract every reviewer must follow, whatever provider/CLI is running it.
It is machine-parsed by `scripts/ci/review_contract.py`, which every acceptance/local-gate reviewer-readiness
check consults; that parser's own docstring restates this same contract so the two can never silently drift.
Respond in exactly one of these two shapes:

1. **Bare.** If there is no actionable finding at all, respond with nothing but the single word `READY`
   (optionally followed by `.` or `!`) -- no heading, summary, rationale, or other text.
2. **Structured.** Otherwise, state all four fields -- `Blockers`, `Important findings`, `Minor findings`,
   `Test gaps` -- each followed by either `None` (nothing to report for that field) or its actual content, then
   a final standalone `READY` line only if every field above resolved to `None`. Ordinary markdown around each
   field is fine and is stripped before matching: a `#`-`######` heading, a leading number (`1.`) or bullet
   (`-`/`*`), surrounding `**bold**`/`*italic*`, and a trailing `:` are all tolerated. Do not rely on any other
   formatting or on the fields appearing in a particular order; do not write `READY` unless every field is
   genuinely `None` -- a `READY` verdict alongside a real finding is treated as a contradiction and rejected,
   never as an override.

Any other response -- missing one of the four fields, an unrecognized or absent verdict, or empty/malformed
output -- is treated as **not ready**, exactly like a real blocker; this parser never infers readiness from
vague prose. When you do report a genuine finding, state it plainly and completely under its field so it can
be acted on directly -- it is preserved verbatim as review evidence.
