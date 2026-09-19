"""Issue #148: the one canonical independent-review response contract shared

by review prompt construction (``.agents/workflows/REVIEW.md``, the maintained
canonical source every provider's policy bundle includes, and any dedicated
per-provider preset such as ``.opencode/agents/reviewer.md``) and acceptance
parsing (this module, consumed by ``scripts/ci/local_gate.py``).

A response is recognized as READY in exactly two shapes:

1. **Bare** -- the entire response, once ordinary markdown wrapping (a
   surrounding ``**``/``*``/backtick, a leading ``#`` heading marker) is
   stripped, is nothing but the word ``READY`` (optionally followed by ``.``
   or ``!``). This is the simplest valid response and requires no other
   structure.
2. **Structured** -- the response states all four required fields --
   ``Blockers``, ``Important findings``, ``Minor findings``, ``Test gaps`` --
   each resolving to ``None`` (the literal word, case-insensitive, optional
   trailing punctuation), and a standalone ``READY`` verdict line appears
   somewhere in the response. Ordinary markdown around each field (a ``#``..
   ``######`` heading, a leading number like ``1.``, a leading ``-``/``*``
   bullet, surrounding ``**bold**``/``*italic*``, a trailing ``:``) is
   tolerated and stripped before matching; CRLF line endings and incidental
   extra whitespace never change the result.

Every other case -- a genuine blocker/important-finding/minor-finding/test-gap
with real content, a missing required field, a verdict that contradicts the
field contents (``READY`` alongside a non-``None`` field), an unrecognized or
absent verdict, or an empty/unparseable response -- resolves to **not ready**.
This module never infers READY from vague prose: the only two ways to satisfy
it are the two shapes above. There is no requirement for one single magic
literal trailing line beyond what is specified and tested here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

REQUIRED_FIELDS: tuple[str, ...] = ("Blockers", "Important findings", "Minor findings", "Test gaps")
_VERDICT_TOKENS = {"READY", "BLOCKED", "NOT READY", "NOT_READY"}

# The literal fallback diagnostic observed when an OpenCode2 ``--agent``
# preset name does not resolve on the target worktree (issue #148's second
# root cause): the CLI silently substitutes its own default agent, which
# never received the reviewer's strict output contract. A response produced
# this way must never be accepted as a real review, however it happens to be
# worded -- this is checked before any content parsing below.
_AGENT_FALLBACK_RE = re.compile(r'agent\s+["\'][^"\']*["\']\s+not found.{0,120}falling back', re.IGNORECASE | re.DOTALL)

_HEADING_HASH_RE = re.compile(r"^#{1,6}\s*")
_BLOCKQUOTE_RE = re.compile(r"^>+\s*")
_ORDERED_MARKER_RE = re.compile(r"^\d{1,2}[.)]\s*")
_BULLET_MARKER_RE = re.compile(r"^[-*]\s+")
# Bold/italic emphasis markers are stripped wherever they occur on the line,
# not only at its very start/end: "- **Blockers:** None" wraps only the
# label in "**", so an edges-only strip would leave a stray "**" glued to
# "Blockers:" and break field-name matching.
_EMPHASIS_RE = re.compile(r"[*_`]")


def indicates_agent_fallback(raw_text: str) -> bool:
    """True when ``raw_text`` shows a CLI substituted its default agent.

    Independent of any response-content parsing: a reviewer invoked under
    the wrong (unconfigured-for-review) agent preset must never be treated
    as having produced a real review, however READY-shaped its actual words
    happen to look.
    """

    return bool(_AGENT_FALLBACK_RE.search(raw_text))


def _bare(line: str) -> str:
    """Strip ordinary markdown wrapping from one line, preserving its content."""

    s = line.strip()
    s = _BLOCKQUOTE_RE.sub("", s)
    s = _HEADING_HASH_RE.sub("", s)
    s = _ORDERED_MARKER_RE.sub("", s)
    s = _BULLET_MARKER_RE.sub("", s)
    s = _EMPHASIS_RE.sub("", s)
    return s.strip()


def _verdict_token(bare_line: str) -> str | None:
    token = bare_line.upper().rstrip(".!").strip().replace("_", " ")
    return token if token in {t.replace("_", " ") for t in _VERDICT_TOKENS} else None


def _field_match(bare_line: str) -> tuple[str, str] | None:
    """Return ``(field_name, inline_remainder)`` for the *first* required

    field named in ``bare_line`` -- header-only (``remainder == ""``) or with
    an inline value (``"Blockers: None"`` -> ``("Blockers", "None")``). The
    label is matched anywhere in the line, not only at its start: some CLI
    wrappers concatenate every prior turn's reasoning/tool-narration into the
    same response string with no reliable line-break separator, so narration
    can run directly into ``"Blockers: None"`` on the very same line.

    A line naming more than one field (all four crammed onto one physical
    line with no breaks) is deliberately not fully decomposed here -- only
    the first field is recognized, and the remaining text becomes its
    (non-``None``) remainder. That is intentionally conservative: the
    contract does not depend on ever accepting that degenerate shape, and
    treating the rest as unrecognized content still fails closed (missing
    fields) rather than risking a misattributed match.
    """

    lowered = bare_line.lower()
    for name in REQUIRED_FIELDS:
        needle = name.lower()
        if lowered == needle or lowered == needle + ":":
            return name, ""
        idx = lowered.find(needle + ":")
        if idx != -1:
            return name, bare_line[idx + len(name) + 1 :].strip()
    return None


def _is_none(body: str) -> bool:
    return body.strip().rstrip(".!").strip().lower() == "none"


@dataclass(frozen=True)
class ReviewVerdict:
    ready: bool
    reason: str
    fields: dict[str, str] = field(default_factory=dict)
    verdict_token: str | None = None


def parse_review_response(text: str) -> ReviewVerdict:
    """Parse a reviewer's free-text response into a fail-closed verdict.

    See the module docstring for the two shapes that resolve to ``ready``;
    every other input resolves to ``ready=False`` with a truthful ``reason``.

    The structured shape requires the four required fields and the verdict
    to form one *contiguous, terminal* block: the verdict must be the last
    meaningful line of the whole response, and each field must be found by
    walking backward from it with nothing but blank lines and other-field
    content in between. This is deliberate, not merely tolerant of markdown:
    a reviewer response that quotes this contract as an illustrative example
    earlier, then states a genuine, unstructured finding afterward with no
    fields/verdict of its own, must never resolve to READY just because a
    qualifying block exists *somewhere* in the text -- only the response's
    actual final word counts.
    """

    if indicates_agent_fallback(text):
        return ReviewVerdict(False, "reviewer CLI fell back to a default agent (preset not found)")

    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return ReviewVerdict(False, "empty response")

    if "\n" not in normalized:
        bare_whole = _bare(normalized)
        if _verdict_token(bare_whole) == "READY":
            return ReviewVerdict(True, "bare READY response")

    lines = normalized.split("\n")
    n = len(lines)

    # The verdict must be the *last* meaningful (non-blank) line of the
    # entire response -- anything found after it, however unrelated, means
    # this was not really the reviewer's final answer.
    verdict_idx: int | None = None
    verdict_token: str | None = None
    for idx in range(n - 1, -1, -1):
        bare_line = _bare(lines[idx])
        if not bare_line:
            continue
        token = _verdict_token(bare_line)
        if token is None:
            return ReviewVerdict(False, "no READY/BLOCKED verdict line found")
        verdict_idx, verdict_token = idx, token
        break
    if verdict_idx is None:
        return ReviewVerdict(False, "no READY/BLOCKED verdict line found")

    # Walk backward from just before the verdict, collecting the contiguous
    # field block that must immediately precede it. A body-continuation line
    # is provisionally buffered until the header/inline line that owns it is
    # reached (walking backward, a field's body is encountered before its own
    # header); any content that turns out not to belong to a field (because a
    # second verdict-shaped line is hit first) ends the block early.
    field_bodies: dict[str, list[str]] = {name: [] for name in REQUIRED_FIELDS}
    found_fields: set[str] = set()
    pending: list[str] = []
    idx = verdict_idx - 1
    while idx >= 0 and found_fields != set(REQUIRED_FIELDS):
        bare_line = _bare(lines[idx])
        idx -= 1
        if not bare_line:
            continue
        matched = _field_match(bare_line)
        if matched is not None:
            name, remainder = matched
            body = ([remainder] if remainder else []) + list(reversed(pending))
            field_bodies[name] = body + field_bodies[name]
            found_fields.add(name)
            pending = []
            continue
        if _verdict_token(bare_line) is not None:
            break
        pending.append(bare_line)

    bodies = {name: " ".join(field_bodies[name]).strip() for name in REQUIRED_FIELDS}
    missing = [name for name in REQUIRED_FIELDS if name not in found_fields]
    if missing:
        return ReviewVerdict(
            False, f"missing required field(s): {', '.join(missing)}", fields=bodies, verdict_token=verdict_token,
        )

    non_none = [name for name in REQUIRED_FIELDS if not _is_none(bodies[name])]
    if verdict_token == "READY" and not non_none:
        return ReviewVerdict(True, "all required fields None and verdict READY", fields=bodies, verdict_token=verdict_token)
    if verdict_token == "READY" and non_none:
        return ReviewVerdict(
            False,
            f"contradictory: verdict READY but non-None field(s): {', '.join(non_none)}",
            fields=bodies, verdict_token=verdict_token,
        )
    return ReviewVerdict(False, f"verdict was {verdict_token!r}, not READY", fields=bodies, verdict_token=verdict_token)
