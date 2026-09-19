"""ENG-AGENT-02-S4: deterministic steering — slash commands and bounded NL parsing.

Two distinct entry points, both zero-AI and pure functions of their input:

- ``parse_slash_command`` recognizes an explicit ``/verb args...`` grammar and
  maps it 1:1 onto an existing :mod:`commands` verb. This is the "deterministic
  slash/control commands" surface the product spec requires to cost zero AI
  calls, always.
- ``parse_natural_language`` runs a small bounded keyword/regex matcher over
  free text before ever considering an AI route. It returns the same
  :class:`SteeringProposal` shape as the slash parser when it finds a
  confident bounded match, or a proposal with ``status=UNRECOGNIZED`` when it
  cannot — it never guesses a destructive action from ambiguous text.

Neither function ever calls a provider/model or spawns a subprocess. When
free text does not match any bounded pattern, the caller may separately ask
``nl_ai_route()`` whether a cheap, currently authorized worker exists that
*could* be escalated to for a one-shot parse — that escalation is a distinct,
explicit, human-triggered action (``dashboard_api.py``'s
``/api/steering/escalate``), never automatic and never part of this module.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .commands import ALL_COMMANDS
from .models import utc_now_iso

# --------------------------------------------------------------------------- shape

STATUS_PARSED = "PARSED"
STATUS_UNRECOGNIZED = "UNRECOGNIZED"

# Verbs whose effect is disruptive/hard-to-reverse enough that the dashboard
# must show a preview and require an explicit confirmation before executing,
# regardless of whether the proposal came from a slash command or bounded NL.
DESTRUCTIVE_VERBS = frozenset(
    {
        "stop",
        "stop_after_current",
        "provider_disable",
        "provider_drain",
        "provider_cost_block",
    }
)

# The cheapest currently-authorized worker role this repository designates
# for one-shot NL-intent parsing, per docs/engineering/ENG-AGENT-02.md and
# AGENTS.md's usage-aware model escalation policy. Never invoked by this
# module; only named here so a caller can report whether escalation is even
# possible before offering it.
NL_ESCALATION_WORKER = "opencode2-gemini-flash-lite"


@dataclass
class SteeringProposal:
    """A parsed-but-not-yet-executed steering action.

    ``verb``/``args`` are ``None`` when ``status`` is ``UNRECOGNIZED`` — there
    is nothing safe to execute. ``preview`` is always a human-readable
    sentence describing exactly what would happen, suitable for display
    before a destructive confirmation.
    """

    status: str
    source: str  # "slash" | "nl-deterministic" | "unrecognized"
    raw_text: str
    verb: str | None = None
    args: dict[str, Any] = field(default_factory=dict)
    destructive: bool = False
    preview: str = ""
    reason: str = ""
    ts: str = field(default_factory=utc_now_iso)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "source": self.source,
            "raw_text": self.raw_text,
            "verb": self.verb,
            "args": self.args,
            "destructive": self.destructive,
            "preview": self.preview,
            "reason": self.reason,
            "ts": self.ts,
        }


def _unrecognized(raw_text: str, *, source: str = "unrecognized", reason: str = "") -> SteeringProposal:
    return SteeringProposal(
        status=STATUS_UNRECOGNIZED,
        source=source,
        raw_text=raw_text,
        preview="No bounded action matched this input; nothing will be executed.",
        reason=reason or "no deterministic pattern matched",
    )


def _proposal(raw_text: str, *, source: str, verb: str, args: dict[str, Any], preview: str) -> SteeringProposal:
    if verb not in ALL_COMMANDS:
        raise AssertionError(f"steering produced an unknown verb {verb!r}; this is a bug in steering.py")
    return SteeringProposal(
        status=STATUS_PARSED,
        source=source,
        raw_text=raw_text,
        verb=verb,
        args=args,
        destructive=verb in DESTRUCTIVE_VERBS,
        preview=preview,
    )


# --------------------------------------------------------------------------- slash grammar

# Each entry: (regex, verb, arg-builder). Matched top-to-bottom, first match wins.
_ID = r"(?P<id>[A-Za-z0-9._\-/]+)"
_NAME = r"(?P<name>[A-Za-z0-9._\-]+)"
_INT = r"(?P<n>-?\d+)"

_SLASH_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(rf"^/start\s+{_ID}$"), "start"),
    (re.compile(r"^/start$"), "start"),
    (re.compile(rf"^/pause\s+{_ID}$"), "pause"),
    (re.compile(rf"^/resume\s+{_ID}$"), "resume"),
    (re.compile(rf"^/stop\s+{_ID}$"), "stop"),
    (re.compile(r"^/stop-after-current$"), "stop_after_current"),
    (re.compile(r"^/stop-all$"), "stop_after_current"),
    (re.compile(rf"^/enable\s+{_NAME}$"), "provider_enable"),
    (re.compile(rf"^/disable\s+{_NAME}$"), "provider_disable"),
    (re.compile(rf"^/drain\s+{_NAME}$"), "provider_drain"),
    (re.compile(rf"^/probe\s+{_NAME}$"), "probe"),
    (re.compile(rf"^/cost-block\s+{_NAME}(?:\s+(?P<reason>.+))?$"), "provider_cost_block"),
    (re.compile(rf"^/cost-clear\s+{_NAME}$"), "provider_cost_clear"),
    (re.compile(rf"^/failure-clear\s+{_NAME}$"), "provider_failure_clear"),
    (re.compile(rf"^/priority\s+{_ID}\s+{_INT}$"), "prioritize"),
    (re.compile(rf"^/defer\s+{_ID}$"), "defer"),
    (re.compile(rf"^/set-max-writers\s+{_INT}$"), "set_max_writers"),
    (re.compile(rf"^/dry-run\s+{_ID}$"), "dry_run"),
]


def _args_for(verb: str, match: re.Match[str]) -> dict[str, Any]:
    groups = match.groupdict()
    if verb == "quickstart_start":
        return {"key": groups.get("quick_key") or "continue-video-editor"}
    if verb in ("start", "pause", "resume", "stop", "dry_run", "prioritize", "defer"):
        args: dict[str, Any] = {"task_id": groups.get("id")}
        if verb == "prioritize":
            args["priority"] = int(groups["n"])
        return {k: v for k, v in args.items() if v is not None}
    if verb in (
        "provider_enable",
        "provider_disable",
        "provider_drain",
        "probe",
        "provider_cost_clear",
        "provider_failure_clear",
    ):
        return {"name": groups["name"]}
    if verb == "provider_cost_block":
        return {"name": groups["name"], "reason": groups.get("reason") or ""}
    if verb == "set_max_writers":
        return {"count": int(groups["n"])}
    return {}


def _preview_for(verb: str, args: dict[str, Any]) -> str:
    previews = {
        "start": lambda a: f"Start task {a.get('task_id', '(next runnable)')}.",
        "pause": lambda a: f"Pause task {a['task_id']} — it will not be scheduled until resumed.",
        "resume": lambda a: f"Resume task {a['task_id']}.",
        "stop": lambda a: f"Stop task {a['task_id']} (marks it cancelled; a live subprocess is not force-killed).",
        "stop_after_current": lambda a: "Stop scheduling new tasks once current work finishes.",
        "provider_enable": lambda a: f"Enable provider {a['name']}.",
        "provider_disable": lambda a: f"Disable provider {a['name']} — no new work will route to it.",
        "provider_drain": lambda a: f"Drain provider {a['name']} — no new work; in-flight work is unaffected.",
        "provider_cost_block": lambda a: (
            f"Hard cost-block provider {a['name']}" + (f" ({a['reason']})" if a.get("reason") else "") + "."
        ),
        "provider_cost_clear": lambda a: f"Clear the cost block on provider {a['name']}.",
        "provider_failure_clear": lambda a: (
            f"Clear the failure state on provider {a['name']} (resets consecutive_failures/last_error)."
        ),
        "probe": lambda a: f"Probe provider {a['name']}'s CLI availability (no billable call).",
        "prioritize": lambda a: f"Set task {a['task_id']} priority to {a['priority']}.",
        "defer": lambda a: f"Defer task {a['task_id']} (lower its priority so it yields to others).",
        "set_max_writers": lambda a: f"Set max concurrent write workers to {a['count']}.",
        "dry_run": lambda a: f"Dry-run task {a['task_id']} (zero writes, zero subprocess).",
        "quickstart_start": lambda a: (
            f"Prepare {a.get('key', 'continue-video-editor')} from current repository truth for explicit review; "
            "nothing starts from this intent alone."
        ),
    }
    builder = previews.get(verb)
    return builder(args) if builder else f"{verb}({args})"


def parse_slash_command(text: str) -> SteeringProposal:
    """Parse an explicit ``/verb ...`` command. Zero AI, always deterministic."""

    stripped = text.strip()
    if not stripped.startswith("/"):
        return _unrecognized(text, source="slash", reason="not a slash command")
    for pattern, verb in _SLASH_PATTERNS:
        match = pattern.match(stripped)
        if match:
            args = _args_for(verb, match)
            return _proposal(text, source="slash", verb=verb, args=args, preview=_preview_for(verb, args))
    return _unrecognized(text, source="slash", reason=f"unrecognized slash command: {stripped!r}")


# --------------------------------------------------------------------------- bounded NL matching

# A bare identifier with no explicit "task"/"provider" keyword in front must
# look like a real ref (contains a digit, e.g. "T1", "ENG-AGENT-02") rather
# than an ordinary pronoun/word ("it", "that", "things") — otherwise phrases
# like "kill it" or "shut things down" would resolve to an executable verb
# against a nonsense target.
_ID_STRICT = r"(?P<id>(?=[A-Za-z0-9._\-/]*\d)[A-Za-z0-9._\-/]+)"

# An explicit "task X"/"provider X" keyword signals deliberate intent about
# *which kind* of target follows, but it does not make "X" itself a real
# reference — "stop task it" and "disable provider it" are exactly as vague
# as "kill it". Every captured id/name, regardless of which pattern matched,
# is rejected here if it is a pronoun/vague-reference word rather than an
# actual identifier.
_VAGUE_WORDS = frozenset(
    {
        "it",
        "its",
        "them",
        "they",
        "that",
        "this",
        "these",
        "those",
        "everything",
        "everyone",
        "everybody",
        "anything",
        "anybody",
        "anyone",
        "something",
        "somebody",
        "someone",
        "things",
        "stuff",
        "all",
        "him",
        "her",
        "us",
        "me",
        "one",
        "ones",
    }
)


def _task_intent_patterns(verbs: tuple[str, ...]) -> str:
    return rf"(?:{'|'.join(verbs)})"


def _nl_task_rule(verbs: tuple[str, ...], verb: str) -> list[tuple[re.Pattern[str], str]]:
    action = _task_intent_patterns(verbs)
    return [
        (re.compile(rf"\b{action}\s+task\s+{_ID}\b", re.IGNORECASE), verb),
        (re.compile(rf"\b{action}\s+{_ID_STRICT}\b", re.IGNORECASE), verb),
    ]


# A development-intent phrase that names the Video Editor explicitly (never
# a bare "continue development" — that is ambiguous between this and other
# Quick Start options, e.g. "Continue OctaScene next eligible task", and
# ambiguity always loses to safety here) maps onto the same `quickstart_start`
# command the Runs tab's "Continue Video Editor" button uses. This still
# only ever produces a PARSED *proposal*: the caller must separately resolve
# and show the real prepared-run details (see dashboard_api.py's
# steering_parse) and the operator must explicitly start it — recognizing
# the phrase never starts anything by itself (ENG-AGENT-02-S7, issue #97).
_VIDEO_EDITOR_INTENT = re.compile(
    r"\b(?:continue|resume|keep working on|start work(?:ing)? on|work on|start)\s+"
    r"(?:the\s+)?(?:standalone\s+)?video[\s-]?editor\b",
    re.IGNORECASE,
)

_QUICKSTART_INTENTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (_VIDEO_EDITOR_INTENT, "continue-video-editor"),
    (re.compile(r"\bcontinue\s+(?:octascene|octages)(?:\s+development)?\b", re.IGNORECASE), "continue-octascene"),
    (re.compile(r"\bfinish\s+(?:the\s+)?current\s+(?:pr|pull request)\b", re.IGNORECASE), "finish-current-pr"),
    (
        re.compile(r"\brun\s+focused\s+tests?(?:\s+and\s+fix(?:\s+failures?)?)?\b", re.IGNORECASE),
        "focused-test-fix",
    ),
)

# Small, explicit keyword-intent table. Each entry is tried in order; the
# first that matches every one of its required groups wins. This is
# intentionally not a general NLU model — every pattern here maps onto
# exactly one existing verb, and anything it cannot confidently place lands
# in UNRECOGNIZED rather than a best-effort guess.
_NL_RULES: list[tuple[re.Pattern[str], str]] = [
    # Whole-fleet phrasing must be checked before the single-task "stop" rule
    # so "stop everything" never gets parsed as stop(task_id="everything").
    (re.compile(r"\b(?:stop everything|stop all|halt after current)\b", re.IGNORECASE), "stop_after_current"),
    *_nl_task_rule(("pause",), "pause"),
    *_nl_task_rule(("resume",), "resume"),
    *_nl_task_rule(("stop", "cancel", "kill"), "stop"),
    *_nl_task_rule(("start",), "start"),
    *_nl_task_rule(("defer", "deprioritize"), "defer"),
    (re.compile(rf"\benable\s+(?:provider\s+)?{_NAME}\b", re.IGNORECASE), "provider_enable"),
    (re.compile(rf"\bdisable\s+(?:provider\s+)?{_NAME}\b", re.IGNORECASE), "provider_disable"),
    (re.compile(rf"\bdrain\s+(?:provider\s+)?{_NAME}\b", re.IGNORECASE), "provider_drain"),
    (re.compile(rf"\bprobe\s+(?:provider\s+)?{_NAME}\b", re.IGNORECASE), "probe"),
]


def parse_natural_language(text: str) -> SteeringProposal:
    """Bounded keyword/regex NL matcher. Zero AI; never escalates by itself.

    Returns a ``PARSED`` proposal for the small set of intents it recognizes
    with high confidence, or ``UNRECOGNIZED`` for anything else — including
    text that is merely *similar* to a known intent. Ambiguity always loses
    to safety here; a human decides whether to retry with a slash command or
    explicitly escalate to an AI-assisted parse.
    """

    stripped = text.strip()
    if not stripped:
        return _unrecognized(text, source="nl-deterministic", reason="empty input")
    if stripped.startswith("/"):
        return parse_slash_command(text)
    for pattern, key in _QUICKSTART_INTENTS:
        if pattern.search(stripped):
            args = {"key": key}
            return _proposal(
                text,
                source="nl-deterministic",
                verb="quickstart_start",
                args=args,
                preview=_preview_for("quickstart_start", args),
            )
    for pattern, verb in _NL_RULES:
        match = pattern.search(stripped)
        if not match:
            continue
        args = _args_for(verb, match)
        target = args.get("task_id") or args.get("name")
        if target is not None and target.lower() in _VAGUE_WORDS:
            # e.g. "stop task it" / "disable provider it": the keyword marks
            # intent but the target itself is a pronoun, not a real
            # reference. Keep trying other rules instead of accepting it.
            continue
        return _proposal(text, source="nl-deterministic", verb=verb, args=args, preview=_preview_for(verb, args))
    return _unrecognized(text, source="nl-deterministic")


def parse_steering_text(text: str) -> SteeringProposal:
    """Single entry point the dashboard/CLI call: try slash first, then bounded NL."""

    stripped = text.strip()
    if stripped.startswith("/"):
        return parse_slash_command(text)
    return parse_natural_language(text)


# --------------------------------------------------------------------------- escalation eligibility (info only)


def nl_ai_route_available(registry: Any) -> dict[str, Any]:
    """Report whether a cheap, currently authorized NL-escalation worker exists.

    This never invokes the worker. It only inspects the same registry/CLI
    presence check every other read endpoint uses (``worker.cli_available()``)
    so the dashboard can honestly say whether "escalate to AI" is even an
    option right now, per the product requirement that deterministic controls
    stay fully usable with zero AI routes configured.
    """

    try:
        worker = registry.get(NL_ESCALATION_WORKER)
    except Exception:  # noqa: BLE001 - unknown/renamed worker is just "unavailable"
        return {"available": False, "worker": NL_ESCALATION_WORKER, "reason": "worker not found in registry"}
    if not worker.enabled:
        return {"available": False, "worker": NL_ESCALATION_WORKER, "reason": "worker disabled in workers.json"}
    available = worker.cli_available()
    return {
        "available": available,
        "worker": NL_ESCALATION_WORKER,
        "reason": "" if available else f"{worker.cli_bin} CLI not installed/authorized",
    }


__all__ = [
    "STATUS_PARSED",
    "STATUS_UNRECOGNIZED",
    "DESTRUCTIVE_VERBS",
    "NL_ESCALATION_WORKER",
    "SteeringProposal",
    "parse_slash_command",
    "parse_natural_language",
    "parse_steering_text",
    "nl_ai_route_available",
]
