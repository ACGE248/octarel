"""ENG-AGENT-02-S4: deterministic steering parsing — zero AI, bounded, safety-gated."""

from __future__ import annotations

import subprocess

import pytest

from scripts.agents.control_plane.commands import ALL_COMMANDS
from scripts.agents.control_plane.steering import (
    DESTRUCTIVE_VERBS,
    STATUS_PARSED,
    STATUS_UNRECOGNIZED,
    nl_ai_route_available,
    parse_natural_language,
    parse_slash_command,
    parse_steering_text,
)
from scripts.agents.registry import load_registry

# --------------------------------------------------------------------------- slash grammar


@pytest.mark.parametrize(
    ("text", "verb", "args"),
    [
        ("/start T1", "start", {"task_id": "T1"}),
        ("/pause T1", "pause", {"task_id": "T1"}),
        ("/resume T1", "resume", {"task_id": "T1"}),
        ("/stop T1", "stop", {"task_id": "T1"}),
        ("/stop-after-current", "stop_after_current", {}),
        ("/enable grok-build", "provider_enable", {"name": "grok-build"}),
        ("/disable grok-build", "provider_disable", {"name": "grok-build"}),
        ("/drain grok-build", "provider_drain", {"name": "grok-build"}),
        ("/probe grok-build", "probe", {"name": "grok-build"}),
        ("/cost-clear grok-build", "provider_cost_clear", {"name": "grok-build"}),
        ("/failure-clear grok-build", "provider_failure_clear", {"name": "grok-build"}),
        ("/priority T1 5", "prioritize", {"task_id": "T1", "priority": 5}),
        ("/defer T1", "defer", {"task_id": "T1"}),
        ("/set-max-writers 3", "set_max_writers", {"count": 3}),
        ("/dry-run T1", "dry_run", {"task_id": "T1"}),
    ],
)
def test_slash_commands_map_to_known_verbs(text, verb, args):
    proposal = parse_slash_command(text)
    assert proposal.status == STATUS_PARSED
    assert proposal.verb == verb
    assert proposal.args == args
    assert proposal.verb in ALL_COMMANDS
    assert proposal.preview  # every parsed proposal must show a human preview


def test_slash_cost_block_captures_reason():
    proposal = parse_slash_command("/cost-block grok-build daily budget exceeded")
    assert proposal.status == STATUS_PARSED
    assert proposal.verb == "provider_cost_block"
    assert proposal.args == {"name": "grok-build", "reason": "daily budget exceeded"}
    assert "daily budget exceeded" in proposal.preview


def test_slash_cost_block_without_reason_is_still_parsed():
    proposal = parse_slash_command("/cost-block grok-build")
    assert proposal.status == STATUS_PARSED
    assert proposal.args == {"name": "grok-build", "reason": ""}


@pytest.mark.parametrize(
    "text",
    [
        "not a slash command",
        "/",
        "/bogus-verb foo",
    ],
)
def test_unrecognized_slash_input_never_produces_a_verb(text):
    proposal = parse_slash_command(text)
    assert proposal.status == STATUS_UNRECOGNIZED
    assert proposal.verb is None


def test_bare_start_is_a_valid_slash_form():
    proposal = parse_slash_command("/start")
    assert proposal.status == STATUS_PARSED
    assert proposal.verb == "start"
    assert proposal.args == {}


# --------------------------------------------------------------------------- destructive gating


def test_every_destructive_verb_is_flagged_on_its_proposal():
    cases = {
        "/stop T1": "stop",
        "/stop-after-current": "stop_after_current",
        "/disable grok-build": "provider_disable",
        "/drain grok-build": "provider_drain",
        "/cost-block grok-build reason": "provider_cost_block",
    }
    for text, verb in cases.items():
        proposal = parse_slash_command(text)
        assert proposal.verb == verb
        assert proposal.destructive is True
        assert verb in DESTRUCTIVE_VERBS


def test_non_destructive_verbs_are_not_flagged():
    for text, verb in {
        "/start T1": "start",
        "/pause T1": "pause",
        "/resume T1": "resume",
        "/enable grok-build": "provider_enable",
        "/probe grok-build": "probe",
    }.items():
        proposal = parse_slash_command(text)
        assert proposal.verb == verb
        assert proposal.destructive is False


def test_destructive_verb_set_only_contains_known_commands():
    assert DESTRUCTIVE_VERBS <= set(ALL_COMMANDS)


# --------------------------------------------------------------------------- bounded NL matching


@pytest.mark.parametrize(
    ("text", "verb", "task_id"),
    [
        ("pause task T1 please", "pause", "T1"),
        ("resume T1", "resume", "T1"),
        ("stop task T1 now", "stop", "T1"),
        ("cancel T1", "stop", "T1"),
        ("start task T1", "start", "T1"),
        ("defer T1", "defer", "T1"),
    ],
)
def test_bounded_nl_matches_recognized_intents(text, verb, task_id):
    proposal = parse_natural_language(text)
    assert proposal.status == STATUS_PARSED
    assert proposal.verb == verb
    assert proposal.args.get("task_id") == task_id
    assert proposal.source == "nl-deterministic"


def test_bounded_nl_matches_provider_intents():
    proposal = parse_natural_language("please disable provider grok-build")
    assert proposal.status == STATUS_PARSED
    assert proposal.verb == "provider_disable"
    assert proposal.args == {"name": "grok-build"}


def test_stop_everything_maps_to_stop_after_current_not_a_force_kill():
    proposal = parse_natural_language("stop everything")
    assert proposal.status == STATUS_PARSED
    assert proposal.verb == "stop_after_current"


@pytest.mark.parametrize(
    "text",
    [
        "continue the video editor",
        "continue video editor",
        "resume video editor",
        "work on the video-editor",
        "keep working on the video editor",
        "start working on the standalone video editor",
    ],
)
def test_video_editor_development_intent_maps_to_quickstart_start(text):
    """ENG-AGENT-02-S7 (issue #97): NL steering must actually be able to start

    the intended run, not just report PARSED with no way to act on it. A
    development-intent phrase that names the Video Editor explicitly maps
    onto the same quickstart_start command the Runs tab's "Continue Video
    Editor" button uses -- and, like every other steering proposal, this is
    still only a PARSED *proposal*: recognizing the phrase never starts
    anything by itself (see test_dashboard.py / dashboard_api.py for the
    resolved-details enrichment and the separate execute step).
    """

    proposal = parse_natural_language(text)
    assert proposal.status == STATUS_PARSED
    assert proposal.verb == "quickstart_start"
    assert proposal.args == {"key": "continue-video-editor"}
    assert proposal.destructive is False


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("Continue OctaScene development", "continue-octascene"),
        ("Finish the current PR", "finish-current-pr"),
        ("Run focused tests and fix failures", "focused-test-fix"),
    ],
)
def test_named_development_intents_map_to_reviewable_quickstarts(text, key):
    proposal = parse_natural_language(text)
    assert proposal.status == STATUS_PARSED
    assert proposal.verb == "quickstart_start"
    assert proposal.args == {"key": key}
    assert proposal.destructive is False


def test_bare_continue_development_is_never_assumed_to_mean_the_video_editor():
    """ "Continue development" is genuinely ambiguous between Quick Start

    options (Video Editor vs. the next OctaScene task vs. finishing an open
    PR) -- ambiguity always loses to safety here, exactly like every other
    vague-target case this module already refuses to guess.
    """

    for text in ["continue development", "continue the roadmap", "keep working on it"]:
        proposal = parse_natural_language(text)
        assert proposal.status == STATUS_UNRECOGNIZED, text


@pytest.mark.parametrize(
    "text",
    [
        "what is the weather today",
        "please do something clever with the providers",
        "make it faster somehow",
        "",
        "   ",
    ],
)
def test_ambiguous_or_unrelated_text_is_never_silently_actioned(text):
    proposal = parse_natural_language(text)
    assert proposal.status == STATUS_UNRECOGNIZED
    assert proposal.verb is None
    assert proposal.args == {}


def test_nl_never_infers_a_destructive_action_from_vague_phrasing():
    """A phrase that merely mentions a destructive-sounding word without a
    bound, recognized target must never resolve to an executable verb."""

    for text in ["stop", "disable", "kill it", "shut things down"]:
        proposal = parse_natural_language(text)
        assert proposal.status == STATUS_UNRECOGNIZED, text


def test_nl_rejects_a_vague_target_even_behind_an_explicit_keyword():
    """Independent review (Grok Build) found that an explicit "task"/
    "provider" keyword did not, by itself, guard against a pronoun target:
    'stop task it' and 'disable provider it' previously resolved to real
    verbs against a nonsense target. The keyword signals intent about which
    *kind* of thing follows, not that the captured word is a real reference."""

    for text in [
        "stop task it",
        "pause task it",
        "resume task them",
        "disable it",
        "disable provider it",
        "drain them",
        "drain provider everything",
    ]:
        proposal = parse_natural_language(text)
        assert proposal.status == STATUS_UNRECOGNIZED, text
        assert proposal.verb is None


def test_nl_still_matches_legitimate_targets_after_the_vague_word_fix():
    """The vague-word guard must not regress ordinary, legitimate phrasing."""

    cases = {
        "stop task T1": ("stop", {"task_id": "T1"}),
        "disable grok-build": ("provider_disable", {"name": "grok-build"}),
        "disable provider grok-build": ("provider_disable", {"name": "grok-build"}),
    }
    for text, (verb, args) in cases.items():
        proposal = parse_natural_language(text)
        assert proposal.status == STATUS_PARSED, text
        assert proposal.verb == verb
        assert proposal.args == args


def test_parse_steering_text_dispatches_slash_vs_nl():
    slash = parse_steering_text("/pause T1")
    assert slash.source == "slash"
    assert slash.verb == "pause"

    nl = parse_steering_text("pause task T1")
    assert nl.source == "nl-deterministic"
    assert nl.verb == "pause"


# --------------------------------------------------------------------------- zero-AI guarantee


def test_parsing_never_spawns_a_subprocess(monkeypatch):
    def _boom(*_a, **_k):
        raise AssertionError("steering parsing must never spawn a subprocess")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    monkeypatch.setattr(subprocess, "run", _boom)

    inputs = [
        "/start T1",
        "/stop T1",
        "pause task T1",
        "do something ambiguous",
        "/cost-block grok-build reason",
        "",
    ]
    for text in inputs:
        parse_steering_text(text)


# --------------------------------------------------------------------------- AI-escalation eligibility (info only)


def test_ai_route_reports_availability_without_invoking_anything(monkeypatch):
    def _boom(*_a, **_k):
        raise AssertionError("checking AI-route availability must never spawn a subprocess")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    monkeypatch.setattr(subprocess, "run", _boom)

    registry = load_registry()
    report = nl_ai_route_available(registry)
    assert set(report.keys()) == {"available", "worker", "reason"}
    assert isinstance(report["available"], bool)


def test_ai_route_unavailable_for_unknown_worker():
    class _EmptyRegistry:
        def get(self, name):
            raise KeyError(name)

    report = nl_ai_route_available(_EmptyRegistry())
    assert report["available"] is False
    assert "not found" in report["reason"]


def test_deterministic_controls_stay_usable_with_no_ai_route():
    """The product requirement: even if no AI route is ever configured,
    slash commands and the bounded NL matcher must still fully resolve."""

    proposal = parse_steering_text("/pause T1")
    assert proposal.status == STATUS_PARSED
    proposal = parse_steering_text("pause task T1")
    assert proposal.status == STATUS_PARSED
