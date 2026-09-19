"""ENG-AGENT-14 (issue #140): binding an already-committed independent review

to an exact PR candidate. Focused, deterministic coverage for the additive
attestation evidence shape (scripts/ci/review_attestation.py) that
scripts/ci/local_gate.py's run_gate() accepts alongside its original
candidate_tree_sha-bound manifest path for an uncommitted candidate.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts.ci.review_attestation import (
    AttestationError,
    build_attestation,
    validate_attestation,
)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True).stdout.strip()


def _git_repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "gate@example.test")
    _git(tmp_path, "config", "user.name", "Gate Test")
    (tmp_path / ".gitignore").write_text(".agent-output/\n.local-gate/\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("one\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "one")
    return tmp_path


def _write_manifest(root: Path, *, task: str = "ENG-X", worker: str = "grok-build-review",
                     role: str = "diff-review", result: str = "PASS", files_changed: list | None = None,
                     provider: str = "xAI", model: str = "grok-4.6-build", response: str = "READY") -> Path:
    run_dir = root / ".agent-output" / task / worker / "run-1"
    log_dir = run_dir / "logs"
    log_dir.mkdir(parents=True)
    log_path = log_dir / "run.log"
    log_path.write_text(json.dumps({"response": response}), encoding="utf-8")
    manifest = {
        "role": role,
        "result": result,
        "files_changed": files_changed or [],
        "actual": {"provider": provider, "model": model},
        "worker": worker,
        "paths": {"log": f".agent-output/{task}/{worker}/run-1/logs/run.log"},
    }
    manifest_path = run_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path


def test_build_attestation_requires_a_fully_committed_candidate(tmp_path):
    repo = _git_repo(tmp_path)
    manifest_path = _write_manifest(repo)
    (repo / "README.md").write_text("dirty\n", encoding="utf-8")  # unstaged change

    with pytest.raises(AttestationError, match="staged/uncommitted"):
        build_attestation(repo, manifest_path, repository="ACGE248/octages", pr_number=139, base="main")


@pytest.mark.parametrize(
    "kwargs,match",
    [
        ({"role": "primary-implementation"}, "role must be"),
        ({"result": "FAIL"}, "result must be"),
        ({"files_changed": ["a.py"]}, "files_changed must be empty"),
        ({"response": "High: a real finding"}, "READY conclusion"),
    ],
)
def test_build_attestation_requires_a_passing_ready_review(tmp_path, kwargs, match):
    repo = _git_repo(tmp_path)
    manifest_path = _write_manifest(repo, **kwargs)

    with pytest.raises(AttestationError, match=match):
        build_attestation(repo, manifest_path, repository="ACGE248/octages", pr_number=139, base="main")


def test_build_and_validate_attestation_roundtrip_succeeds(tmp_path):
    repo = _git_repo(tmp_path)
    manifest_path = _write_manifest(repo)

    attestation = build_attestation(repo, manifest_path, repository="ACGE248/octages", pr_number=139, base="main")
    attestation_path = repo / ".agent-output" / "attestation.json"
    attestation_path.write_text(json.dumps(attestation), encoding="utf-8")

    ready, reason = validate_attestation(
        repo, attestation_path, base="main", review_provider="xAI", expected_repository="ACGE248/octages",
    )

    assert ready is True, reason
    assert attestation["head_sha"] == _git(repo, "rev-parse", "HEAD")
    assert attestation["tree_sha"] == _git(repo, "rev-parse", "HEAD^{tree}")


def test_validate_rejects_a_stale_review_after_a_new_commit(tmp_path):
    repo = _git_repo(tmp_path)
    manifest_path = _write_manifest(repo)
    attestation = build_attestation(repo, manifest_path, repository="ACGE248/octages", pr_number=139, base="main")
    attestation_path = repo / ".agent-output" / "attestation.json"
    attestation_path.write_text(json.dumps(attestation), encoding="utf-8")

    (repo / "README.md").write_text("two\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "two")

    ready, reason = validate_attestation(
        repo, attestation_path, base="main", review_provider="xAI", expected_repository="ACGE248/octages",
    )

    assert ready is False
    assert "head_sha" in reason or "does not match" in reason


def test_validate_rejects_a_hand_edited_tree_sha_via_signature_mismatch(tmp_path):
    """An attacker retargeting a stale/foreign review's sealed tree_sha (without

    the local signing key) must be caught by signature verification -- not
    merely by a plain field comparison an attacker could satisfy by editing
    the same field the check reads.
    """

    repo = _git_repo(tmp_path)
    manifest_path = _write_manifest(repo)
    attestation = build_attestation(repo, manifest_path, repository="ACGE248/octages", pr_number=139, base="main")
    attestation["tree_sha"] = "0" * 40  # retarget without re-signing
    attestation_path = repo / ".agent-output" / "attestation.json"
    attestation_path.write_text(json.dumps(attestation), encoding="utf-8")

    ready, reason = validate_attestation(
        repo, attestation_path, base="main", review_provider="xAI", expected_repository="ACGE248/octages",
    )

    assert ready is False
    assert "signature" in reason


def test_validate_rejects_a_different_repository(tmp_path):
    repo = _git_repo(tmp_path)
    manifest_path = _write_manifest(repo)
    attestation = build_attestation(repo, manifest_path, repository="someone-else/unrelated", pr_number=1, base="main")
    attestation_path = repo / ".agent-output" / "attestation.json"
    attestation_path.write_text(json.dumps(attestation), encoding="utf-8")

    ready, reason = validate_attestation(
        repo, attestation_path, base="main", review_provider="xAI", expected_repository="ACGE248/octages",
    )

    assert ready is False
    assert "repository" in reason


def test_validate_rejects_tampered_source_manifest_content(tmp_path):
    """A review attestation content-addresses its source manifest/log -- editing

    either after the fact (e.g. to fabricate a READY result the reviewer
    never gave) must invalidate the attestation, not be silently trusted.
    """

    repo = _git_repo(tmp_path)
    manifest_path = _write_manifest(repo)
    attestation = build_attestation(repo, manifest_path, repository="ACGE248/octages", pr_number=139, base="main")
    attestation_path = repo / ".agent-output" / "attestation.json"
    attestation_path.write_text(json.dumps(attestation), encoding="utf-8")

    # Tamper with the source manifest after the attestation was built.
    tampered = json.loads(manifest_path.read_text(encoding="utf-8"))
    tampered["result"] = "PASS"  # already PASS; simulate a no-op edit still changing bytes
    manifest_path.write_text(json.dumps(tampered) + " ", encoding="utf-8")

    ready, reason = validate_attestation(
        repo, attestation_path, base="main", review_provider="xAI", expected_repository="ACGE248/octages",
    )

    assert ready is False
    assert "mutable reviewer output" in reason or "content has changed" in reason


def test_validate_ignores_a_spoofed_provider_and_uses_the_live_source_instead(tmp_path):
    """Round-2 independent-review finding: provider/result/files_changed/READY

    used to be trusted from the attestation's own (unsealed, editable) copy.
    An implementer could build a legitimate, correctly-signed attestation
    from their own Claude/Anthropic review, then simply edit the envelope's
    "provider" field to claim "xAI" -- the signature only ever sealed the
    repository/head/tree/source-hash fields, never provider. Validation must
    ignore that copy entirely and re-derive the real provider from the still
    hash-pinned source manifest every time, so this spoof has no effect.
    """

    repo = _git_repo(tmp_path)
    manifest_path = _write_manifest(repo, provider="Anthropic")
    attestation = build_attestation(repo, manifest_path, repository="ACGE248/octages", pr_number=139, base="main")
    assert attestation["provider"] == "Anthropic"  # honestly recorded by build()

    attestation["provider"] = "xAI"  # spoofed after the fact; not part of the signature
    attestation_path = repo / ".agent-output" / "attestation.json"
    attestation_path.write_text(json.dumps(attestation), encoding="utf-8")

    ready, reason = validate_attestation(
        repo, attestation_path, base="main", review_provider="xAI", expected_repository="ACGE248/octages",
    )

    assert ready is False
    assert "Codex/self-review" in reason


def test_build_refuses_a_non_ready_source(tmp_path):
    repo = _git_repo(tmp_path)
    manifest_path = _write_manifest(repo, response="High: a real finding")

    with pytest.raises(AttestationError, match="READY conclusion"):
        build_attestation(repo, manifest_path, repository="ACGE248/octages", pr_number=139, base="main")


def test_validate_rejects_a_legitimately_signed_attestation_of_a_non_ready_source(tmp_path):
    """Round-2 independent-review test-gap finding: a key-holder who signs an

    attestation for a non-READY source *without* going through build()'s own
    refusal (e.g. a future caller, or a hand-built envelope) must still be
    rejected at validate time -- READY is re-derived from the live source on
    every validation, never assumed from a valid signature alone.
    """

    from scripts.ci.review_attestation import _sha256_file, _sign

    repo = _git_repo(tmp_path)
    manifest_path = _write_manifest(repo, response="High: a real finding")
    tree = _git(repo, "rev-parse", "HEAD^{tree}")
    head = _git(repo, "rev-parse", "HEAD")

    attestation = {
        "schema": "octages/pr-review-attestation/1",
        "repository": "ACGE248/octages", "pr_number": 139, "base": "main",
        "head_sha": head, "tree_sha": tree,
        "source_manifest": "ENG-X/grok-build-review/run-1/manifest.json",
        "source_manifest_sha256": _sha256_file(manifest_path),
        "source_log": ".agent-output/ENG-X/grok-build-review/run-1/logs/run.log",
        "source_log_sha256": _sha256_file(manifest_path.parent / "logs" / "run.log"),
        "generated_at": "2026-01-01T00:00:00+00:00",
    }
    attestation["source_manifest"] = ".agent-output/ENG-X/grok-build-review/run-1/manifest.json"
    attestation["signature"] = _sign(repo, attestation)  # legitimately signed with the real local key
    attestation_path = repo / ".agent-output" / "attestation.json"
    attestation_path.write_text(json.dumps(attestation), encoding="utf-8")

    ready, reason = validate_attestation(
        repo, attestation_path, base="main", review_provider="xAI", expected_repository="ACGE248/octages",
    )

    assert ready is False
    assert "READY" in reason


def test_validate_fails_closed_when_the_signing_key_is_deleted(tmp_path):
    repo = _git_repo(tmp_path)
    manifest_path = _write_manifest(repo)
    attestation = build_attestation(repo, manifest_path, repository="ACGE248/octages", pr_number=139, base="main")
    attestation_path = repo / ".agent-output" / "attestation.json"
    attestation_path.write_text(json.dumps(attestation), encoding="utf-8")

    (repo / ".agent-output" / ".review-attestation.key").unlink()

    ready, reason = validate_attestation(
        repo, attestation_path, base="main", review_provider="xAI", expected_repository="ACGE248/octages",
    )

    assert ready is False
    assert "signature" in reason


@pytest.mark.parametrize("field,value", [("result", "FAIL"), ("files_changed", ["a.py"]), ("role", "primary-implementation")])
def test_validate_ignores_envelope_spoofs_of_substance_fields(tmp_path, field, value):
    """Only "provider" was explicitly exercised before; result/files_changed/role

    must be equally ineffective to spoof in the envelope, since none of them
    are read from there at validation time either.
    """

    repo = _git_repo(tmp_path)
    manifest_path = _write_manifest(repo)
    attestation = build_attestation(repo, manifest_path, repository="ACGE248/octages", pr_number=139, base="main")
    attestation[field] = value  # spoofed after the fact; not part of the signature
    attestation_path = repo / ".agent-output" / "attestation.json"
    attestation_path.write_text(json.dumps(attestation), encoding="utf-8")

    ready, reason = validate_attestation(
        repo, attestation_path, base="main", review_provider="xAI", expected_repository="ACGE248/octages",
    )

    assert ready is True, reason  # the spoof is simply irrelevant -- live source is still honest


@pytest.mark.parametrize("provider", ["OpenAI", "Codex", "Anthropic", "Claude"])
def test_validate_rejects_disallowed_review_providers(tmp_path, provider):
    repo = _git_repo(tmp_path)
    manifest_path = _write_manifest(repo, provider=provider)
    attestation = build_attestation(repo, manifest_path, repository="ACGE248/octages", pr_number=139, base="main")
    attestation_path = repo / ".agent-output" / "attestation.json"
    attestation_path.write_text(json.dumps(attestation), encoding="utf-8")

    ready, reason = validate_attestation(
        repo, attestation_path, base="main", review_provider=provider, expected_repository="ACGE248/octages",
    )

    assert ready is False
    assert "Codex/self-review" in reason


def test_validate_rejects_provider_mismatch(tmp_path):
    repo = _git_repo(tmp_path)
    manifest_path = _write_manifest(repo, provider="xAI")
    attestation = build_attestation(repo, manifest_path, repository="ACGE248/octages", pr_number=139, base="main")
    attestation_path = repo / ".agent-output" / "attestation.json"
    attestation_path.write_text(json.dumps(attestation), encoding="utf-8")

    ready, reason = validate_attestation(
        repo, attestation_path, base="main", review_provider="Google", expected_repository="ACGE248/octages",
    )

    assert ready is False
    assert "does not match requested review_provider" in reason


def test_signing_key_persists_across_builds_in_the_same_checkout(tmp_path):
    repo = _git_repo(tmp_path)
    manifest_path = _write_manifest(repo)

    first = build_attestation(repo, manifest_path, repository="ACGE248/octages", pr_number=139, base="main")
    second = build_attestation(repo, manifest_path, repository="ACGE248/octages", pr_number=139, base="main")

    # Same sealed content signed twice with the same persisted key -> identical signature.
    assert first["signature"] == second["signature"]
    key_path = repo / ".agent-output" / ".review-attestation.key"
    assert key_path.is_file()
