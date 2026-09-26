# Stitch "Glass Orchestration Studio" integration (OCTAREL-UI-04)

How the Stitch-designed interface was adopted as the real Control Center UI, and
the decisions taken where the design and the product disagreed.

Authorities, in the order they were applied:

1. the **Stitch project** for visual design, where it has a canonical screen;
2. **`OCTAREL_UI_DESIGN.md`** (shipped inside that project) for anything Stitch
   does not explicitly design;
3. **this repository** for behaviour, data and contracts;
4. **`AGENTS.md`** for workflow and policy.

The design specification states its own scope plainly: *"a UI/UX redesign, not a
replacement architecture."* No parallel frontend was created, no second state
system exists, and no static mock-up replaced a dynamic surface.

## Screen mapping

| Stitch screen | Octarel surface | Backing state |
|---|---|---|
| Overview / Command Hub | `view-overview` | `/api/overview`, `/api/workflow`, `/api/tasks`, `/api/attention`, `/api/usage` |
| Manager Chat | `view-steering` (labelled **Manager**) | `/api/manager/route`, `/api/manager/message`, `/api/steering/*` |
| Flow / Orchestrator Studio | `view-flow` | `/api/flow` |
| Priority & Fallback Matrix | `view-priority` *(new)* | `/api/priority-matrix` |
| Runs / Run Detail | `view-runs` | `/api/quickstart`, `/api/runbooks*` |
| Tasks | `view-tasks` | `/api/tasks` |
| Agent Fleet | `view-agents` | `/api/models`, `/api/agent-activity/*` |
| Providers | `view-providers` | `/api/providers`, `/api/opencode-models`, `/api/native-models`, `/api/usage-routing`, `/api/usage-telemetry` |
| History & Evidence | `view-history` | `/api/events`, `/api/run-evidence` |
| Worktrees & Branch Isolation | `view-worktrees` | `/api/worktrees`, `/api/operations`, `/api/repository-health` |
| System & Runtime Health | `view-system` | `/api/resources`, `/api/local-gate`, `/api/tests` |
| Terminal | `view-terminal` | `/api/terminal/*` (real PTY over WebSocket) |
| Settings | `view-settings` | `localStorage` (appearance/notifications), `set_max_writers` |
| Roadmap | `view-roadmap` *(promoted)* | `/api/roadmap` |
| Operational overlays & approvals | `#confirm-sheet`, `#attention-popover`, `#report-sheet`, `#workflow-detail-sheet`, `#palette` | their respective surfaces |

Two deliberate departures from the mock-up's information architecture:

- **Priority & Fallback Matrix columns are Octarel's real roles**
  (`primary-implementation`, `diff-review`, `focused-tests`, …), not the four
  illustrative columns named Architect / Worker / Tester / Reviewer. Octarel has
  ten differently named configured roles; renaming them to match a mock-up would
  misrepresent how this orchestrator actually routes.
- **Roadmap owns its live table.** It previously rendered inside Settings with
  the roadmap view left as a stub pointing back at it — one concern in two
  places. It is now a real destination and the duplicate is gone.

## Unsupported-control triage

Every control the Stitch screens show that has no backing behaviour, and what
was decided. The rule applied throughout: *never display a control that appears
functional when it does nothing.*

| Stitch control | Decision | Reason |
|---|---|---|
| Priority drag-to-reorder | **Omitted** | No backend contract exists for mutating route order. The design specification itself permits reordering only "where the actual product supports configuration". A test asserts no draggable affordance is rendered. |
| Provider "Rotate Key" | **Omitted** | No endpoint, and credential rotation is a security-sensitive operation that must not be prototyped in UI ahead of an audited implementation. |
| Provider "Inspect Headers" | **Omitted** | Would surface raw provider response headers, which can carry auth material. Requires a redaction contract before it could ship. |
| Settings: PKCE enforcement, AST pre-commit, secret-leak filter, candidate-tree guard | **Omitted** | These are gate policies enforced by the local gate, not runtime preferences. A toggle would imply an operator can relax a gate from the browser, which is exactly what the gate exists to prevent. |
| Settings: autonomy level (Supervised / Semi / Nightly) | **Omitted** | Octarel expresses autonomy through runbook permission profiles and overnight-session bounds, which already have real controls. A second, differently-shaped control over the same concern would be ambiguous about which one wins. |
| Settings: agent heartbeat interval, worker task timeout | **Omitted** | Illustrative values in the mock-up; no corresponding configuration exists. |
| Settings: auto-prune stale worktrees + threshold | **Omitted** | Worktree cleanup is deliberately explicit and confirmation-gated. An automatic pruner is a product decision, not a UI one. |
| Execution Events: CVE counts, review attestation card | **Omitted** | No scanner or attestation record produces these. Rendering zeros would assert a security posture that was never measured. |
| Execution Events: type-to-confirm destructive modal | **Not adopted** | Octarel already has a server-enforced confirmation gate that re-derives destructiveness rather than trusting the client. Replacing it with a client-side typing challenge would be weaker, not stronger. |
| Flow canvas: minimap, fit-to-screen, zoom %, fullscreen | **Deferred** | Genuinely useful, but pure canvas work with no data dependency. Recorded here so it is a known gap rather than an oversight. |
| AO-style cache metrics and context utilization | **Shown as `NOT_EXPOSED`** | Implemented as visible, explained gaps rather than omitted — see below. |

## Metrics the stack cannot produce

The usage panel (OCTAREL-UI-06) displays the Agent Orchestrator's metric set,
but two families have no counterpart in this stack and are labelled
`NOT_EXPOSED` with their reason rather than estimated:

- **Cache categories.** Nothing records fresh input, cache reads or cache
  writes, so the cache hit rate derived from them cannot be computed. It is
  never approximated from totals.
- **Effective context limit.** No runtime reports one. The OpenCode catalog's
  per-model `context_limit` is a fact about a model, not the limit the active
  worker ran under, and the design specification forbids inferring a context
  window from a model name.

Dollar cost is produced only for an API route with both a pricing snapshot and
known token counts. Subscription and free routes report `NOT_APPLICABLE`, never
`$0.00`, which would imply API pricing that does not apply.

## Accessibility departures from the palette

The design specification supplies one status palette and separately requires
"contrast-compliant light and dark themes". Measured against the themed glass
surfaces, its dark values all pass as text, but its light values do not —
success 2.95:1, warning 2.76:1, amber 2.75:1, against a 4.5:1 requirement —
because those hues are tuned for fills and dots on a warm canvas rather than for
small text.

The specification's values are therefore kept for decorative use (pills, dots,
rims, glows, borders), and status **text** uses the same hue at a lightness that
clears 4.5:1. Hue family and semantic meaning are unchanged; only lightness
moves, and only in light mode. Measured ratios are recorded beside each value in
`styles.css`.

## Light/dark parity

The specification requires that "light and dark share the same layout and
components so theme switching does not change information architecture".

This is enforced continuously rather than audited once: `glass-shell.spec.js`
compares the rendered control and heading inventory of **every** view across
both themes and fails on any difference. The theme layer makes this structural
by construction — every themed value is a custom property declared in exactly
three places (`:root`, the `prefers-color-scheme` media query, and
`html[data-theme="dark"]`), and nothing below that layer hard-codes a themed
colour.

## Typography and the Content-Security-Policy

Plus Jakarta Sans and JetBrains Mono are vendored as latin `woff2` from pinned
`@fontsource` devDependencies and served same-origin, following the existing
`vendor/xterm` pattern. The Control Center's CSP is `default-src 'self'` with no
external hosts; a hosted webfont would have required weakening it, which is not
an acceptable trade for typography. Licenses are vendored alongside the files
and recorded in `ASSET_LICENSES.md` and `THIRD_PARTY.md`.
