# ENG-PC-12 — advisory/enforced budgets and activation scope

Issue #65 resolves the product decision left by ENG-PC-05: a usage budget now declares whether it is
`advisory` or `enforced`, and every persisted definition has an activation timestamp.

## Policy contract

- `enforced` is the safe default, including for migrated definitions. A breached hard limit vetoes execution,
  and `UNKNOWN` evidence that may belong to the active scope continues to fail closed.
- `advisory` reports `WARNING` at or beyond its warning/hard threshold, and also warns when its evidence is
  `UNKNOWN`, but it never vetoes a launch or fallback.
- A new definition activates when it is created unless the operator supplies an explicit timezone-aware
  `activated_at`. Ordinary updates preserve the existing activation timestamp; an explicit replacement is an
  intentional scope reset.
- Existing definitions migrate as `enforced` and use their original `created_at` as `activated_at`, preserving
  both their safety posture and their historical boundary.

Only ledger rows with a trustworthy `occurred_at` at or after activation contribute to a budget. A row proven
to precede activation is out of scope. A missing, malformed, or timezone-naive timestamp cannot prove that it
precedes activation: it remains `UNKNOWN` evidence, blocks an enforced budget, and warns under an advisory
budget. The implementation never substitutes zero or presents a known subtotal as a complete total.

## Presentation and provenance

The existing Usage & Costs budget surface exposes the definition mode and activation timestamp. Advisory
thresholds render as advisory warnings; only enforced breaches render as hard blocks. Evidence values retain
the existing `MEASURED`, `DERIVED`, `UNKNOWN`, and `NOT_EXPOSED` vocabulary. Mode changes policy effect, not
the confidence of the underlying evidence.

## Verification

Coverage pins schema migration, default and update persistence, inclusive activation boundaries, exclusion of
provably pre-activation history, fail-closed malformed timestamps, advisory non-veto behavior, mixed advisory
and enforced decisions, admission events, API projection, and the existing Control Center surface.
