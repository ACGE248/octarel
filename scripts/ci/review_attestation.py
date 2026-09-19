#!/usr/bin/env python3
"""Bind an already-committed independent read-only review to an exact PR candidate.

``scripts/ci/local_gate.py``'s existing ``--independent-review-evidence`` path binds
a review manifest to a candidate via ``candidate_tree_sha`` -- set only by the bounded
``orchestrate run --include-diff`` flow, built for reviewing an *uncommitted* staged
candidate within one continuous implementation session (ENG-AGENT-05).

That does not fit an independent review performed externally, in one or more rounds,
against an already-committed and pushed PR head (ENG-AGENT-13, issue #138's own PR
#139) -- there is no uncommitted diff for ``--include-diff`` to bound, yet the review
is exactly as real and exactly as bound to one exact tree.

This module adds a second, additive evidence shape for that case: a small, explicit
*attestation* derived from an existing ``orchestrate`` manifest/log pair. It never
replaces the existing ``candidate_tree_sha``-bound manifest path -- ``local_gate.py``
accepts either shape, distinguished by an explicit schema marker.

Trust model (round-2 independent-review finding on this module itself): the head/tree/
repository an attestation claims to bind to cannot be re-derived from the reviewer's own
manifest content (a session-mode review never records a candidate tree), so those
*context* fields are sealed with an HMAC-SHA256 keyed by a local, gitignored secret
(``.agent-output/.review-attestation.key``, generated on first use, never committed) --
editing a sealed field without that key invalidates the signature. Every *substance*
field the sealed context does not need to carry an opinion on -- provider, model,
worker, role, result, files_changed, and the READY conclusion itself -- is never trusted
from the attestation's own copy at validation time: it is re-derived fresh from the
still-hash-pinned source manifest/log every time. An attestation is therefore useless to
forge in either direction: retargeting the sealed head/tree without the key breaks the
signature, and relabeling the provider/result/readiness in the envelope changes nothing,
because validation never reads those fields from the envelope in the first place.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import hmac
import json
import os
import subprocess
from pathlib import Path
from typing import Any

try:
    from scripts.ci.local_gate import _review_response_is_ready, candidate
except ModuleNotFoundError:  # pragma: no cover - direct script entry point
    from local_gate import _review_response_is_ready, candidate  # type: ignore

ATTESTATION_SCHEMA = "octages/pr-review-attestation/1"
KEY_RELATIVE_PATH = ".agent-output/.review-attestation.key"

# Providers that may never satisfy an independent-review requirement, regardless of
# evidence shape: Codex/OpenAI per existing repository policy, and Anthropic/Claude
# because the implementer itself is commonly Claude Code -- an implementer can never
# be its own independent reviewer.
DISALLOWED_REVIEW_PROVIDERS = frozenset({"openai", "codex", "anthropic", "claude"})

# The context fields sealed by the HMAC signature: everything an attestation asserts
# that cannot be independently re-derived from the hash-pinned source manifest/log at
# validation time. Order matters -- it is part of the canonical signed payload.
_SEALED_FIELDS = (
    "schema", "repository", "pr_number", "base", "head_sha", "tree_sha",
    "source_manifest", "source_manifest_sha256", "source_log", "source_log_sha256",
)


class AttestationError(ValueError):
    pass


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=30, check=False)
    if result.returncode != 0:
        raise AttestationError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _repo_root_for(root: Path) -> Path:
    top = _git(root, "rev-parse", "--show-toplevel")
    return Path(top).resolve()


def _load_or_create_key(root: Path) -> bytes:
    """Return the local HMAC signing key, generating one on first use.

    The key lives under the (already gitignored) ``.agent-output/`` tree, is never
    committed, and never leaves this checkout. Losing or rotating it invalidates every
    previously issued attestation in this checkout -- an acceptable, visible failure
    mode for a local development-tool integrity control, never a silent one.
    """

    key_path = _repo_root_for(root) / KEY_RELATIVE_PATH
    if key_path.is_file():
        key = key_path.read_bytes()
        if len(key) >= 32:
            return key
    key = os.urandom(32)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(key)
    try:
        key_path.chmod(0o600)
    except OSError:  # pragma: no cover - best-effort on platforms without chmod bits
        pass
    return key


def _canonical_sealed_payload(attestation: dict[str, Any]) -> bytes:
    sealed = {field: attestation.get(field) for field in _SEALED_FIELDS}
    return json.dumps(sealed, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sign(root: Path, attestation: dict[str, Any]) -> str:
    key = _load_or_create_key(root)
    return hmac.new(key, _canonical_sealed_payload(attestation), hashlib.sha256).hexdigest()


def build_attestation(
    root: Path, manifest_path: Path, *, repository: str, pr_number: int | None = None, base: str = "origin/main",
) -> dict[str, Any]:
    """Derive a tree/head-bound, HMAC-sealed attestation from an existing manifest.

    Raises :class:`AttestationError` if the candidate is not fully committed (an
    uncommitted/staged candidate should use the existing ``candidate_tree_sha`` path
    instead, not this one), or if the referenced manifest does not actually prove a
    passing, read-only, READY-concluded diff-review.
    """

    manifest_path = manifest_path.resolve()
    if not manifest_path.is_file():
        raise AttestationError(f"manifest not found: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AttestationError(f"manifest is not readable JSON: {exc}") from None

    try:
        tree, head, _paths = candidate(root, base)
    except RuntimeError as exc:
        raise AttestationError(
            f"candidate has staged/uncommitted changes ({exc}) -- use the existing candidate_tree_sha "
            "(--include-diff) evidence path for an uncommitted candidate, not an attestation"
        ) from None
    if head is None:
        raise AttestationError(
            "candidate has staged/uncommitted changes -- use the existing candidate_tree_sha "
            "(--include-diff) evidence path for an uncommitted candidate, not an attestation"
        )

    if manifest.get("role") != "diff-review":
        raise AttestationError(f"manifest role must be 'diff-review', got {manifest.get('role')!r}")
    if manifest.get("result") != "PASS":
        raise AttestationError(f"manifest result must be 'PASS', got {manifest.get('result')!r}")
    if manifest.get("files_changed"):
        raise AttestationError("manifest files_changed must be empty for a read-only reviewer")
    if not _review_response_is_ready(manifest_path, manifest):
        raise AttestationError("manifest does not prove a READY conclusion from the reviewer's own response")

    log_rel = str((manifest.get("paths") or {}).get("log") or "")
    if not log_rel:
        raise AttestationError("manifest is missing its paths.log pointer")
    # Mirrors _review_response_is_ready's own path resolution exactly: the manifest
    # lives at <repo>/.agent-output/<task>/<worker>/<run>/manifest.json, four levels
    # under the repo root the log path is relative to.
    manifest_repo_root = manifest_path.parents[4]
    log_path = (manifest_repo_root / log_rel).resolve()
    if not log_path.is_file():
        raise AttestationError(f"manifest's referenced log is missing: {log_path}")

    attestation: dict[str, Any] = {
        "schema": ATTESTATION_SCHEMA,
        "repository": repository,
        "pr_number": pr_number,
        "base": base,
        "head_sha": head,
        "tree_sha": tree,
        "source_manifest": str(manifest_path.relative_to(manifest_repo_root)),
        "source_manifest_sha256": _sha256_file(manifest_path),
        "source_log": log_rel,
        "source_log_sha256": _sha256_file(log_path),
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    }
    # Everything below is informational/audit-trail only -- validate_attestation never
    # trusts these copies; it re-derives the same facts fresh from the hash-verified
    # source manifest/log at validation time.
    actual = manifest.get("actual") or {}
    attestation.update({
        "role": manifest.get("role"),
        "result": manifest.get("result"),
        "provider": actual.get("provider"),
        "model": actual.get("model"),
        "worker": manifest.get("worker"),
        "files_changed": manifest.get("files_changed") or [],
    })
    attestation["signature"] = _sign(root, attestation)
    return attestation


def validate_attestation(
    root: Path, attestation_path: Path, *, base: str, review_provider: str | None, expected_repository: str | None,
) -> tuple[bool, str]:
    """Return ``(ready, reason)`` for an attestation evaluated against the live candidate.

    Every check below corresponds to one required rejection case: a wrong commit or
    tree (the sealed context no longer matches the live candidate, or was retargeted
    without the signing key), a stale review (same as above), a review recorded against
    another repository, a tampered/mutated source (a content-hash mismatch), a missing
    READY result, or a disallowed/self-review provider (Codex, or the implementer's own
    Claude/Anthropic route) -- the last two are decided from the live source manifest,
    never from the attestation's own unsealed copy of those fields.
    """

    try:
        attestation = json.loads(attestation_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False, "attestation is not readable JSON"
    if attestation.get("schema") != ATTESTATION_SCHEMA:
        return False, f"not a recognized attestation (schema={attestation.get('schema')!r})"

    signature = attestation.get("signature")
    if not signature or not hmac.compare_digest(str(signature), _sign(root, attestation)):
        return False, (
            "attestation signature is missing or invalid -- its sealed repository/head/tree/source-hash "
            "fields cannot be trusted"
        )

    try:
        tree, head, _paths = candidate(root, base)
    except RuntimeError as exc:
        return False, f"candidate has staged/uncommitted changes ({exc}); an attestation only binds a committed candidate"
    if head is None:
        return False, "candidate has staged/uncommitted changes; an attestation only binds a committed candidate"
    if attestation.get("head_sha") != head:
        return False, f"attestation head_sha {attestation.get('head_sha')!r} does not match candidate HEAD {head!r}"
    if attestation.get("tree_sha") != tree:
        return False, f"attestation tree_sha {attestation.get('tree_sha')!r} does not match candidate tree {tree!r}"
    if expected_repository and attestation.get("repository") != expected_repository:
        return False, (
            f"attestation repository {attestation.get('repository')!r} does not match "
            f"expected repository {expected_repository!r}"
        )

    # Tamper-evidence for the source: the signature above seals these hash fields, so
    # an attacker cannot retarget source_manifest_sha256/source_log_sha256 without the
    # key either -- but we still re-read and re-verify the *live* files against those
    # sealed hashes, because the signature alone does not prove the files on disk
    # right now still match what was reviewed.
    source_manifest = str(attestation.get("source_manifest") or "")
    source_log = str(attestation.get("source_log") or "")
    if not source_manifest or not source_log:
        return False, "attestation is missing its source manifest/log pointers"
    # Both source_manifest and source_log are stored repo-root-relative (they already
    # include the leading ".agent-output/..." segment -- matching how a manifest's own
    # paths.log field is written), not relative to .agent-output/ itself.
    evidence_root = (root / ".agent-output").resolve()
    manifest_path = (root / source_manifest).resolve()
    log_path = (root / source_log).resolve()
    if not manifest_path.is_relative_to(evidence_root) or not log_path.is_relative_to(evidence_root):
        return False, "attestation source paths must stay under .agent-output/"
    if not manifest_path.is_file() or not log_path.is_file():
        return False, "attestation's referenced source manifest/log no longer exists"
    if _sha256_file(manifest_path) != attestation.get("source_manifest_sha256"):
        return False, "attestation's source manifest content has changed since the review (mutable reviewer output)"
    if _sha256_file(log_path) != attestation.get("source_log_sha256"):
        return False, "attestation's source log content has changed since the review (mutable reviewer output)"

    # Substance fields are re-derived fresh from the hash-verified live source, never
    # trusted from the attestation's own (unsealed, editable) copy of them -- this is
    # what makes relabeling a Claude/Codex review's provider, or a non-empty
    # files_changed, or a non-PASS result, ineffective: validation never looks at the
    # envelope for these facts in the first place.
    try:
        source_manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False, "attestation's source manifest is no longer readable JSON"
    if source_manifest_data.get("role") != "diff-review":
        return False, f"source manifest role must be 'diff-review', got {source_manifest_data.get('role')!r}"
    if source_manifest_data.get("result") != "PASS":
        return False, f"source manifest result must be 'PASS', got {source_manifest_data.get('result')!r}"
    if source_manifest_data.get("files_changed"):
        return False, "source manifest records a non-empty files_changed; the reviewer must be read-only"
    if not _review_response_is_ready(manifest_path, source_manifest_data):
        return False, "source manifest no longer proves a READY conclusion"

    provider = str((source_manifest_data.get("actual") or {}).get("provider") or "")
    if provider.casefold() in DISALLOWED_REVIEW_PROVIDERS:
        return False, f"source manifest provider {provider!r} may never satisfy independent review (Codex/self-review)"
    if review_provider and provider.casefold() != review_provider.casefold():
        return False, f"source manifest provider {provider!r} does not match requested review_provider {review_provider!r}"

    return True, "attestation proves a passing, read-only, READY independent review of this exact commit/tree"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_build = sub.add_parser("build", help="derive an attestation from an existing orchestrate manifest")
    p_build.add_argument("--repo-root", type=Path, default=Path.cwd())
    p_build.add_argument("--manifest", type=Path, required=True)
    p_build.add_argument("--repository", required=True, help="e.g. ACGE248/octages")
    p_build.add_argument("--pr", type=int, default=None)
    p_build.add_argument("--base", default="origin/main")
    p_build.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    if args.command == "build":
        root = args.repo_root.resolve()
        try:
            attestation = build_attestation(
                root, args.manifest, repository=args.repository, pr_number=args.pr, base=args.base,
            )
        except AttestationError as exc:
            print(json.dumps({"ok": False, "error": str(exc)}))
            return 1
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(attestation, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps({"ok": True, "path": str(args.out), "attestation": attestation}, indent=2))
        return 0
    parser.error(f"unknown command {args.command!r}")
    return 2  # pragma: no cover - parser.error exits


if __name__ == "__main__":
    raise SystemExit(main())
