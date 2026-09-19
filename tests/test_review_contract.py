"""ENG-AGENT-17 (issue #148): the canonical independent-review response contract.

Focused, deterministic coverage for ``scripts.ci.review_contract.parse_review_response``
and its fail-closed guarantees -- the exact scenarios issue #148 enumerates.
"""

from __future__ import annotations

from scripts.ci.review_contract import indicates_agent_fallback, parse_review_response


def test_bare_ready_response() -> None:
    assert parse_review_response("READY").ready is True
    assert parse_review_response("READY.").ready is True
    assert parse_review_response("  ready!  ").ready is True


def test_canonical_structured_ready_response() -> None:
    text = "Blockers: None\nImportant findings: None\nMinor findings: None\nTest gaps: None\nREADY\n"
    verdict = parse_review_response(text)
    assert verdict.ready is True


def test_markdown_heading_and_list_formatting_is_tolerated() -> None:
    text = (
        "### 1. Blockers\n- None.\n\n"
        "### 2. Important findings\n- None.\n\n"
        "### 3. Minor findings\n- None.\n\n"
        "### 4. Test gaps\n- None.\n\n"
        "**READY**\n"
    )
    verdict = parse_review_response(text)
    assert verdict.ready is True


def test_crlf_and_extra_whitespace_are_normalized() -> None:
    text = (
        "  Blockers:   None  \r\n\r\n"
        "Important findings: None\r\n"
        "\r\n"
        "Minor findings: None   \r\n"
        "Test gaps: None\r\n"
        "\r\n\r\n"
        "READY\r\n"
    )
    verdict = parse_review_response(text)
    assert verdict.ready is True


def test_genuine_blockers_are_not_ready() -> None:
    text = (
        "Blockers: the acceptance stage double-dispatches review on retry\n"
        "Important findings: None\n"
        "Minor findings: None\n"
        "Test gaps: None\n"
        "BLOCKED\n"
    )
    verdict = parse_review_response(text)
    assert verdict.ready is False
    assert "double-dispatches" in verdict.fields["Blockers"]


def test_contradictory_ready_with_real_blocker_fails_closed() -> None:
    text = (
        "Blockers: the fix does not actually exclude .orchestrator-state/\n"
        "Important findings: None\n"
        "Minor findings: None\n"
        "Test gaps: None\n"
        "READY\n"
    )
    verdict = parse_review_response(text)
    assert verdict.ready is False
    assert "contradictory" in verdict.reason.lower()


def test_missing_required_field_fails_closed() -> None:
    text = "Blockers: None\nImportant findings: None\nMinor findings: None\nREADY\n"
    verdict = parse_review_response(text)
    assert verdict.ready is False
    assert "Test gaps" in verdict.reason


def test_empty_response_fails_closed() -> None:
    assert parse_review_response("").ready is False
    assert parse_review_response("   \n  \n").ready is False


def test_malformed_response_with_no_verdict_fails_closed() -> None:
    verdict = parse_review_response("I looked at the diff and it seems mostly fine, some nitpicks.")
    assert verdict.ready is False
    assert "verdict" in verdict.reason.lower()


def test_narration_merged_onto_the_same_line_as_a_field_label_is_still_recognized() -> None:
    """Grok Build's CLI concatenates prior turn narration with no reliable

    line break, so a field label can appear mid-line rather than at its
    start; the label must still be recognized wherever it appears.
    """

    text = (
        "after reviewing every changed file I conclude Blockers: None\n"
        "Important findings: None\nMinor findings: None\nTest gaps: None\nREADY\n"
    )
    verdict = parse_review_response(text)
    assert verdict.ready is True


def test_a_quoted_template_earlier_in_the_response_does_not_override_a_later_unstructured_blocker() -> None:
    """Independent-review finding: the field block and the verdict must be

    one contiguous, terminal block -- a response that quotes this contract
    as an illustrative example early on, then states a genuine unstructured
    problem afterward with no fields/verdict of its own, must never resolve
    to READY just because a qualifying block exists earlier in the text.
    """

    text = (
        "Here is the expected format if everything were fine:\n"
        "Blockers: None\nImportant findings: None\nMinor findings: None\nTest gaps: None\nREADY\n\n"
        "However, I found a real problem: the acceptance stage double-dispatches review on retry. "
        "This needs to be fixed before merge."
    )
    verdict = parse_review_response(text)
    assert verdict.ready is False


def test_content_after_the_verdict_line_fails_closed() -> None:
    text = "Blockers: None\nImportant findings: None\nMinor findings: None\nTest gaps: None\nREADY\n\nP.S. thanks!"
    verdict = parse_review_response(text)
    assert verdict.ready is False


def test_agent_fallback_diagnostic_is_never_accepted_even_if_ready_shaped() -> None:
    text = (
        'agent "reviewer" not found. Falling back to default agent\n'
        "Blockers: None\nImportant findings: None\nMinor findings: None\nTest gaps: None\nREADY\n"
    )
    assert indicates_agent_fallback(text) is True
    verdict = parse_review_response(text)
    assert verdict.ready is False
    assert "fell back" in verdict.reason
