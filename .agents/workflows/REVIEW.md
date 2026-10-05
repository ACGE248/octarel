# Review workflow

Review only the bounded exact-tree diff and relevant contracts/evidence. Check correctness, regression,
scope, security/secrets, provider/spend behavior, persistence, tests, documentation, worktree safety, and UI/
accessibility when applicable. Do not edit, stage, commit, push, or create a PR.

## Execution boundary and counterfactuals

Every review dispatch includes the selected worker's `review_execution` declaration from `workers.json`: the
standard permission profile, execution mode, whether test execution is supported, the guaranteed read-only
command prefixes (if any), and a concrete reason. This declaration is the single orchestration authority; the
provider sandbox/preset remains the enforcement layer and is never widened by prompt text.

Follow that boundary exactly. In particular, when `supports_test_execution` is false:

- reason from the bounded code/diff and supplied evidence;
- do not invoke tests, builds, or a counterfactual command, even when a brief accidentally asks for one;
- treat caller/orchestrator check entries as reported evidence, never as commands you ran; and
- if a missing counterfactual result would decide the review, put the missing evidence and exact command under
  `Test gaps`. The orchestrator/tester must run it separately and redispatch review with the result supplied.

An unsupported execution attempt is not evidence that the candidate passed or failed. Do not infer boundary
cleanliness from prose-only denial output; typed boundary evidence remains governed by the existing review
contract and fail-closed parser.

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
