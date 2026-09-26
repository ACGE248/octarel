# OCTAREL-UI-07 — subscription-aware usage value and actual cost

Issue #42. Follows OCTAREL-UI-06 (issue #25), which built the usage read model,
and OCTAREL-UI-04 (issue #23), whose design system this UI uses.

The problem this change exists to solve is narrow and easy to get wrong.
Subscription-backed work has a real API-equivalent value — the same tokens
bought on the public API would have cost something, and an operator benefits
from seeing roughly how much. But the moment that number is presented as
*spend*, the panel is lying about an account. So the change is not "add a cost
column"; it is "make two different claims, and make them impossible to
confuse".

## Two fields, not one

| Field | Question it answers | Class | Produced when |
|---|---|---|---|
| `estimated_api_equivalent_usd` | What would these tokens have cost at public API rates? | Always `DERIVED` | Any route, given a price and token counts of either quality |
| `actual_cost_usd` | What did this run add to a bill? | `DERIVED` or `UNKNOWN` | Billing class establishes it, or exact tokens × a price |

They are separate keys in the payload even where the arithmetic coincides (an
API-billed run with exact counts produces the same number twice), because the
requirement is a contract about evidence, not about formatting. Their
`formula` strings differ deliberately, so the two cells never read identically
in a tooltip.

The asymmetry between them is the point:

- An **estimate** may be built from `ESTIMATED` token counts — a local
  character-length approximation recorded by the supervisor. It is already
  labelled approximate, so approximate input is appropriate.
- An **actual charge** may not. It requires `EXACT` provider-reported counts.
  Deriving "what you were billed" from a local guess would put a fabricated
  number in the one field that must never carry one.

## Billing class

Who paid, as distinct from what a model costs. Derived from the worker's
`cost_class` through `telemetry.execution_route_for_cost_class` — the same
mapping routing already uses, so there is no second classifier to drift.

| Billing class | Label shown | Actual incremental cost |
|---|---|---|
| `SUBSCRIPTION_INCLUDED` | Included with subscription | `$0.00` |
| `API_BILLED` | API-billed | computed, or `UNKNOWN` |
| `FREE_TIER` | Free tier | `$0.00` |
| `UNKNOWN` | Billing not established | `UNKNOWN` |

Two wording decisions worth stating:

- A free route is never described as "included with a subscription". Both cost
  nothing, but only one is covered by something the operator pays for, and
  conflating them misdescribes what the account is buying.
- An unclassified route reports `UNKNOWN`, never `$0.00`. "We do not know" is
  not "it was free", and a defaulted zero is the single most dangerous value
  this panel could render.

Each row also reports how well established the classification itself is:
`MEASURED` when the run recorded its own `cost_class`, `DERIVED` when it was
filled in from today's registry, `UNKNOWN` when there is none.

## Pricing: one source, no new table

`control_plane/pricing.py` reads the **OpenCode model catalog**
(`scripts/agents/model_catalog.py`, snapshot `catalog.json`). That catalog is
already the authority `classify_cost` uses to decide whether a route is metered
or free; adding a hand-entered price file beside it would create a second truth
that could silently disagree with the one routing trusts. **No new pricing file
was introduced.**

- The catalog quotes dollars per *million* tokens (`google/gemini-2.5-flash`
  declares `input: 0.3` against a published $0.30/M). `ModelPricing` converts
  once to the per-1K rates `telemetry.PricingSnapshot` already speaks.
- A model resolves by **exact catalog id**, or — when a worker records a bare
  name — by its provider-qualified id through a hand-maintained
  provider map. Nothing else. No fuzzy or cross-provider matching: a plausible
  price attached to the wrong model is worse than an honest blank.
- Only the top-level `input`/`output` rates are used. The catalog also carries
  tiered and cache rates, but they apply under conditions no run records, so
  using them would mean assuming which tier applied.
- Staleness uses a seven-day threshold, deliberately *not*
  `CATALOG_MAX_AGE_SECONDS` (30 minutes). That window governs re-discovering
  availability and capability, which change often; published rates change on
  the order of months, and reusing it would flag nearly every snapshot as stale
  so the flag would carry no information. A stale snapshot still prices — it
  just says it is stale.

### Models that cannot be priced

Two providers in `workers.json` are absent from the catalog by construction:
**Anthropic** (`claude-code` runs through the Claude CLI, not OpenCode) and
**DeepSeek**. Their runs report the estimate as unavailable with that reason.
This is the designed outcome, not a gap. Pricing failing never affects the
billing classification — a `claude-code` run still shows "Included with
subscription" and `$0.00` incremental cost, with an unavailable estimate.

## Historical attribution

`route_history` entries now record `cost_class` at launch, alongside the
`worker`/`provider`/`model` they already carried. Reading billing class back
off the registry describes a worker *as configured now*, so a later
`workers.json` edit would silently relabel what past runs cost. A fallback run
is attributed — and priced — at the worker that actually executed, not the one
originally preferred.

Records written before this change carry no `cost_class`; they fall back to the
registry and are reported as `DERIVED` with that caveat rather than presented as
the run's own fact.

## Aggregates

Three windows (UTC calendar day, ISO week from Monday, calendar month), each
reporting three *value* totals split by who paid, and exactly one *spend*
total drawn only from API-billed rows' actual-cost cells. Nothing crosses
between them, and the payload states that invariant in text.

- A row whose occurrence time is unknown is counted in **no** window and
  reported as excluded. Dropping it into the current window would inflate
  today's figures with work of unknown age.
- Occurrence time prefers the finalized attempt's `ended_at` (`MEASURED`) over
  the record's `updated_at` (`DERIVED`), which moves whenever the record is
  rewritten for any reason.
- Every total carries how many rows in its window actually contributed. A
  small number because little was used must not read the same as a small
  number because most rows could not be priced.

## UI

In the merged Glass Orchestration Studio surface, on the existing Providers
view. Money is deliberately **not** another cell in the metric grid: an
approximate value and a real charge are different kinds of claim, and a row of
interchangeable-looking cells is how they get confused. They get their own
block above the grid, and differ on every axis a glance can use — position,
weight, colour, and a leading `~` on anything approximate. An unestablished
money cell renders its class at text size, never as a figure, exactly as the
metric grid already does.

Aggregates sit at the top of the card, with the spend row separated from the
value rows by a rule so the grouping itself says they are not one column of
numbers to be added. Every colour is an existing token declared in the same
three blocks as the rest of the sheet, so light/dark parity stays structural.

## Superseded

`OCTAREL-UI-04.md` and `ARCHITECTURE.md` previously stated that a subscription
route reports `NOT_APPLICABLE` for cost because `$0.00` "would imply API
pricing that does not apply". Issue #42 replaces that with a more precise
answer: the pricing *is* shown, as an explicitly approximate equivalent value,
and the `$0.00` is an incremental-cost claim justified by the billing class
rather than an implied API rate. Both documents are reconciled. The
`NOT_APPLICABLE` class remains in the published vocabulary — consumers render
it — but no cost cell produces it any more.

## Deliberate non-goals

- **No live pricing lookup.** Rates come from the cached snapshot on disk.
  Refreshing the catalog remains `model_catalog`'s job and runs a local
  `opencode` status command; nothing in this path spawns a process or contacts
  a provider.
- **No change to spend enforcement.** `telemetry.compute_cost` and
  `check_budget` are untouched. A subscription route still contributes no
  dollar figure to a budget check, and this change enables no billing,
  fallback or escalation.
- **Cache and context metrics are still `NOT_EXPOSED`.** Nothing here changes
  what the stack can measure.
