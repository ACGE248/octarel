# ENG-AO-10 — first-class Graphify lifecycle

Issue #41 extends ENG-AO-01's safe `build_graph_context*` worker seam and issue #26's per-run evidence. It does
not replace either one and does not make Graphify an acceptance authority.

## Reviewed upstream and support policy

- Upstream: <https://github.com/Graphify-Labs/graphify> (Apache-2.0).
- Default branch: `v8`, reviewed HEAD `9fd5aadfd8ff7c2de95c78ef90f9b9f2721cbd98` on 2026-09-29.
- Supported/latest release: `v0.9.71`, release commit `d6eaa8aae8df155874ebb1044302c055c286342a`
  (2026-09-28).
- A `v1.0.0` tag exists at `0a31c0862b600d0755b0b8da41d6cdf99df135df`, but it was not the latest
  published release at review time and is not accepted by the pinned capability probe.
- Distribution: `graphifyy` (two `y` characters), Python 3.10+. It installs the `graphify` command.
- Explicit operator install: `uv tool install graphifyy` or `pipx install graphifyy`.

Octarel never installs the package. `python -m octarel graphify install` is instruction-only. `status` verifies
the exact pinned version and that top-level `graphify --help` advertises `extract` plus `--code-only`; an unverified or different
version reports `OUTDATED` and is not invoked for generation.

The exact default pin is deliberate: Graphify output and CLI compatibility are treated as untrusted until that
specific release has been reviewed, so Octarel does not accept version ranges or automatically follow the newer
`v1.0.0` tag. An operator who has separately reviewed another release may set
`OCTAREL_GRAPHIFY_VERIFIED_VERSION=<exact-semver>` (for example `1.0.0`). That setting is an explicit local
attestation, not auto-detection: changing it requires reviewing the release's `extract`/`update` and
`--code-only` behavior, then running the Graphify focused tests and the risk-selected Octarel gate. Non-exact
values such as ranges fail closed as `OUTDATED`.

`GRAPHIFY_NO_LLM` is not an upstream variable and is intentionally not set. No comment or control relies on it.
The real no-model controls are the documented `extract <snapshot> --code-only` command, deterministic `update`
on later isolated snapshots, removal of every provider credential-shaped environment variable, and never
invoking semantic/query/provider install paths.

## Lifecycle and safety

`scripts/agents/graph_lifecycle.py` owns capability/health, cached status and refresh coordination.
`scripts/agents/graph_context.py` continues to own filtered snapshots, sensitive-path rejection, content-tree
identity, cache validation/retention, graph normalization/redaction, and the only worker-facing seam.

1. Project selection durably changes selection, then queues a non-blocking canonical warm refresh.
2. Acceptance waits until `_review_task_in_flight` is false, stages the candidate and calls `gate_candidate`.
   Only after that existing writer boundary does it wait up to the explicit two-second checkpoint budget for an
   `implementation-checkpoint` refresh; on timeout the build continues in the background and acceptance records
   that review context may be one tree behind before review planning/dispatch follows.
3. Advancement re-reads canonical repository truth. Only when a successor will actually auto-start does it
   queue a non-blocking `post-merge` refresh; it never holds the advancement lock/lease for a graph build, and
   the successor's existing context seam refreshes its exact tree on demand if the warm-up is incomplete.
4. A dashboard or CLI manual action queues the same coordinator. Dashboard GETs only read cached status.

The coordinator has one local worker. An identical project/worktree/tree request shares one future; a newer
tree replaces an older queued request for that worktree; shutdown resolves queued requests as failed-safe. An
active build uses a hidden staging directory and publishes atomically. Completed cache retention is two trees
per worktree and never removes the selected current tree or hidden active build.

Every refresh records project/worktree/tree identities, trigger, pinned version, timing, file/exclusion and
node/edge counts, cache key, result/reason, and `api_llm_disabled: true`. The current cached record is derived
state; durable history reuses the existing Control Plane events table. Failures are `FAILED_SAFE` and never
convert an otherwise valid merge, acceptance, or advancement into failure.

## Control Center

System / Operational Overlays shows selected-project Graphify health, version, branch/worktree/tree, last
refresh/duration/age, counts, reason and the active run's existing #26 injection evidence. There is no second
Graphify page. `GET /api/graphify` never probes or generates; Check installation and Refresh graph are explicit
operator POST actions.
The API omits Graphify executable/worktree absolute paths, including paths present in internal cached evidence.
Remote Check installation and Refresh graph actions are recorded in the existing remote-identity audit ledger.

## Deliberate deferral

Requirement 10's local query service is deferred. No real Graphify binary is installed in the implementation
environment, so the output contracts of `query`, `path` and `explain` could not be verified. Stubbing a service
against an assumed unstable shape would create a coupling the issue explicitly permits deferring. Workers keep
using the proven bounded, redacted `build_graph_context*` summary seam.

Graphify Git hooks are also deliberately unsupported. `graphify hook install` mutates a managed repository and
installs a merge driver; Octarel never calls it and lifecycle correctness never depends on it.
