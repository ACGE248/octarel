"""ENG-AO-04: native Grok/Codex model freshness, verified against deterministic fake CLIs only."""

from __future__ import annotations

import dataclasses
import datetime as dt
import json

import pytest
from fastapi.testclient import TestClient

from scripts.agents import model_catalog as mc
from scripts.agents import native_models as nm
from scripts.agents import orchestrate
from scripts.agents.control_plane.commands import CommandContext
from scripts.agents.control_plane.dashboard_api import create_app
from scripts.agents.control_plane.provider_state import seed_provider_states
from scripts.agents.control_plane.scheduler import Scheduler
from scripts.agents.control_plane.state import State
from scripts.agents.model_catalog import CommandResult
from scripts.agents.registry import load_registry

GROK_WORKERS = ("grok-build", "grok-build-bots", "grok-build-review", "grok-build-bot")
CODEX_WORKERS = ("codex-build", "codex-review")
NOW = dt.datetime(2026, 9, 24, 12, 0, tzinfo=dt.timezone.utc)

GROK_HELP = "\n".join(
    f"      {flag} <X>" for flag in (
        "--single", "--output-format", "--model", "--reasoning-effort", "--permission-mode", "--sandbox",
        "--tools", "--no-subagents", "--disable-web-search")
)
CODEX_HELP = "\n".join(f"      {flag} <X>" for flag in ("--sandbox", "--model", "--json", "--ephemeral"))


def grok_models(*ids: str, authenticated: bool = True) -> str:
    head = "You are logged in with grok.com.\n\n" if authenticated else "You are not authenticated.\n\n"
    return head + "Default model: grok-4.6\n\nAvailable models:\n" + "".join(
        f"  {'*' if i == 'grok-4.6' else '-'} {i}{' (default)' if i == 'grok-4.6' else ''}\n" for i in ids)


def codex_catalog(*entries: tuple[str, str]) -> str:
    return json.dumps({"models": [{"slug": slug, "visibility": vis, "display_name": slug} for slug, vis in entries]})


class FakeCli:
    """Answers the exact local commands discovery is allowed to run; records every argv and environment."""

    def __init__(self, **responses: CommandResult):
        self.responses = {tuple(k.split("|")): v for k, v in responses.items()}
        self.calls: list[tuple[str, ...]] = []
        self.envs: list[dict] = []

    def __call__(self, argv, cwd, timeout, env):
        self.calls.append(tuple(argv))
        self.envs.append(dict(env or {}))
        return self.responses.get(tuple(argv), CommandResult(1, error="not-scripted"))

    @staticmethod
    def grok(models: str, version: str = "grok 1.0.34", help_text: str = GROK_HELP) -> "FakeCli":
        return FakeCli(**{
            "grok|--version": CommandResult(0, version + "\n"), "grok|models": CommandResult(0, models),
            "grok|--help": CommandResult(0, help_text)})

    @staticmethod
    def codex(catalog: str, login: str = "Logged in using ChatGPT\n", version: str = "codex-cli 0.153.4",
              help_text: str = CODEX_HELP) -> "FakeCli":
        return FakeCli(**{
            "codex|--version": CommandResult(0, version + "\n"), "codex|login|status": CommandResult(0, login),
            "codex|debug|models": CommandResult(0, catalog), "codex|exec|--help": CommandResult(0, help_text)})


def _shapes(registry, family):
    return [nm.WorkerShape.of(w) for w in registry.workers.values() if w.native_model_family == family]


def _refresh(family, cli, registry=None, directory=None, configured=None):
    registry = registry or load_registry()
    workers = [w for w in registry.workers.values() if w.native_model_family == family]
    return nm.refresh(family, configured or workers[0].default_model, _shapes(registry, family), runner=cli,
                      which=lambda _b: "/x", now=NOW, directory=directory)


@pytest.fixture
def state(tmp_path):
    return tmp_path / "native-models-state"


# --------------------------------------------------------------------------- registry contract


def test_family_membership_and_stable_worker_identities():
    registry = load_registry()
    assert {n for n, w in registry.workers.items() if w.native_model_family == "grok"} == set(GROK_WORKERS)
    assert {n for n, w in registry.workers.items() if w.native_model_family == "codex"} == set(CODEX_WORKERS)
    assert registry.get("grok-build").default_model == "grok-4.6"
    assert registry.get("codex-review").default_model == "gpt-5.6-sol"
    for name, worker in registry.workers.items():  # #7 pool workers and every other provider never advance natively
        if worker.model_pool or worker.cli_bin not in {"grok", "codex"}:
            assert worker.native_model_family == "" and worker.effective_model == worker.default_model


# --------------------------------------------------------------------------- Grok


def test_newer_grok_verified_advances_every_grok_worker_and_bots_inherit(state):
    registry = load_registry()
    record = _refresh("grok", FakeCli.grok(grok_models("grok-4.7", "grok-4.7-build-fast", "grok-4.6", "grok-4.5")),
                      registry, state)
    assert record["status"] == nm.STATUS_VERIFIED and record["candidate"]["native_id"] == "grok-4.7"
    assert record["last_verified_model"] == "grok-4.7" and record["previous_verified_model"] == "grok-4.6"
    assert record["probe"] == "none" and record["auth"]["authenticated"] is True
    for name in GROK_WORKERS:
        worker = registry.get(name)
        assert nm.effective_model("grok", worker.default_model, directory=state) == "grok-4.7"
        assert worker.default_model == "grok-4.6"  # the configured, last verified baseline is retained


def test_effective_model_reaches_the_command_of_the_stable_workers(state, monkeypatch):
    monkeypatch.setattr(nm, "state_dir", lambda: state)
    registry = load_registry()
    before = {n: registry.get(n).build_command(model=None, intensity="low", prompt="p") for n in GROK_WORKERS}
    _refresh("grok", FakeCli.grok(grok_models("grok-4.7", "grok-4.6")), registry, state)
    for name in GROK_WORKERS:
        worker = registry.get(name)
        after = worker.build_command(model=None, intensity="low", prompt="p")
        assert worker.effective_model == "grok-4.7"
        # Only the model value moved: every flag, sandbox, and permission token is byte-identical.
        assert [t.replace("grok-4.7", "grok-4.6") for t in after] == before[name]
        assert (worker.capability, worker.roles, worker.requires_isolated_worktree) == (
            registry.get(name).capability, registry.get(name).roles, registry.get(name).requires_isolated_worktree)
    assert "grok-4.7" in registry.get("grok-build-bot").build_command(model=None, intensity="low", prompt="p")
    assert "--sandbox" in after and "read-only" in registry.get("grok-build-review").cli_template


def test_unauthenticated_grok_lists_but_does_not_advance(state):
    record = _refresh("grok", FakeCli.grok(grok_models("grok-4.7", "grok-4.6", authenticated=False)), directory=state)
    assert record["status"] == nm.STATUS_UNVERIFIED and record["candidate"]["native_id"] == "grok-4.7"
    assert "not authenticated" in record["reason"] and record["effective_model"] == "grok-4.6"
    assert nm.effective_model("grok", "grok-4.6", directory=state) == "grok-4.6"


def test_no_newer_grok_is_current_and_only_the_plain_family_is_a_candidate(state):
    record = _refresh("grok", FakeCli.grok(grok_models("grok-4.6", "grok-4.5", "grok-4.6-build-fast")), directory=state)
    assert record["status"] == nm.STATUS_CURRENT and record["candidate"] is None
    only_variant = _refresh("grok", FakeCli.grok(grok_models("grok-4.6", "grok-4.7-build-fast")), directory=state)
    assert only_variant["status"] == nm.STATUS_CURRENT  # a build-fast variant is a different route, not a successor


def test_grok_help_that_drops_a_templated_flag_blocks_the_advance(state):
    help_text = GROK_HELP.replace("--no-subagents", "").replace("--tools", "")
    record = _refresh("grok", FakeCli.grok(grok_models("grok-4.7", "grok-4.6"), help_text=help_text), directory=state)
    assert record["status"] == nm.STATUS_UNVERIFIED and "--no-subagents" in record["reason"]
    assert record["checks"]["invocation_flags"]["missing"] == ["--no-subagents", "--tools"]
    assert nm.effective_model("grok", "grok-4.6", directory=state) == "grok-4.6"


# --------------------------------------------------------------------------- Codex


def test_newer_codex_sol_verified_advances_build_and_review(state):
    registry = load_registry()
    catalog = codex_catalog(("gpt-6-sol", "list"), ("gpt-6-astra", "list"), ("gpt-5.6-sol", "list"), ("gpt-5.5", "list"))
    record = _refresh("codex", FakeCli.codex(catalog), registry, state)
    assert record["status"] == nm.STATUS_VERIFIED and record["candidate"]["native_id"] == "gpt-6-sol"
    assert record["newer_other_tier"] == ["gpt-6-astra"]
    for name in CODEX_WORKERS:
        assert nm.effective_model("codex", registry.get(name).default_model, directory=state) == "gpt-6-sol"
    assert "read-only" in registry.get("codex-review").cli_template and registry.get("codex-review").is_read_only
    assert "workspace-write" in registry.get("codex-build").cli_template


def test_this_machines_codex_shape_has_astra_but_no_newer_sol_so_sol_is_retained(state):
    catalog = codex_catalog(("gpt-6-astra", "list"), ("gpt-reserve", "hide"), ("gpt-5.6-sol", "list"),
                            ("gpt-5.6-terra", "list"), ("codex-auto-review", "hide"))
    record = _refresh("codex", FakeCli.codex(catalog), directory=state)
    assert record["status"] == nm.STATUS_CURRENT and record["candidate"] is None
    assert record["newer_other_tier"] == ["gpt-6-astra"] and "different tier" in record["reason"]
    assert nm.effective_model("codex", "gpt-5.6-sol", directory=state) == "gpt-5.6-sol"


def test_hidden_codex_model_is_never_a_candidate(state):
    record = _refresh("codex", FakeCli.codex(codex_catalog(("gpt-6-sol", "hide"), ("gpt-5.6-sol", "list"))),
                      directory=state)
    assert record["status"] == nm.STATUS_CURRENT and record["candidate"] is None


@pytest.mark.parametrize("login,fragment", [
    ("Not logged in\n", "no logged-in ChatGPT subscription session"),
    ("Logged in using an API key\n", "API key"),
])
def test_codex_without_a_subscription_session_never_advances(state, login, fragment):
    cli = FakeCli.codex(codex_catalog(("gpt-6-sol", "list"), ("gpt-5.6-sol", "list")), login=login)
    record = _refresh("codex", cli, directory=state)
    assert record["status"] == nm.STATUS_UNAVAILABLE and fragment in record["reason"]
    assert record["auth"]["authenticated"] is False
    assert record["effective_model"] == "gpt-5.6-sol"
    assert ("codex", "debug", "models") not in cli.calls  # no enumeration once the session is ineligible


@pytest.mark.parametrize("stdout", ["not json", json.dumps({"models": [{"visibility": "list"}]}), json.dumps({"x": 1}),
                                    codex_catalog(("gpt-6-sol", "hide")), ""])
def test_malformed_or_incomplete_codex_catalog_retains_the_configured_model(state, stdout):
    record = _refresh("codex", FakeCli.codex(stdout), directory=state)
    assert record["status"] == nm.STATUS_UNAVAILABLE and record["effective_model"] == "gpt-5.6-sol"
    assert nm.effective_model("codex", "gpt-5.6-sol", directory=state) == "gpt-5.6-sol"


def test_grok_that_does_not_confirm_a_session_or_uses_an_api_key_never_advances(state):
    for text, fragment in (("Default model: grok-4.6\n\nAvailable models:\n  - grok-4.7\n  * grok-4.6\n", "did not confirm"),
                           ("Using API key.\nAvailable models:\n  - grok-4.7\n  * grok-4.6\n", "API key")):
        record = _refresh("grok", FakeCli.grok(text), directory=state)
        assert record["status"] == nm.STATUS_UNVERIFIED and fragment in record["reason"]
        assert record["effective_model"] == "grok-4.6"


@pytest.mark.parametrize("stdout", ["", "garbage\n", "Available models:\n"])
def test_malformed_grok_listing_retains_the_configured_model(state, stdout):
    record = _refresh("grok", FakeCli.grok(stdout), directory=state)
    assert record["status"] == nm.STATUS_UNAVAILABLE and record["effective_model"] == "grok-4.6"


# --------------------------------------------------------------------------- failure containment, safety


@pytest.mark.parametrize("family", ["grok", "codex"])
def test_missing_cli_is_unavailable_and_never_raises(state, family):
    registry = load_registry()
    workers = [w for w in registry.workers.values() if w.native_model_family == family]
    record = nm.refresh(family, workers[0].default_model, _shapes(registry, family), runner=FakeCli(),
                        which=lambda _b: None, now=NOW, directory=state)
    assert record["status"] == nm.STATUS_UNAVAILABLE and "not installed" in record["reason"]
    assert record["effective_model"] == workers[0].default_model


def test_a_crashing_cli_wrapper_cannot_break_startup(state):
    def boom(*_args):
        raise RuntimeError("cli exploded")

    record = _refresh("grok", boom, directory=state)
    assert record["status"] == nm.STATUS_UNAVAILABLE and "cli exploded" in record["reason"]


def test_stale_configured_model_is_reported_without_breaking_anything(state):
    record = _refresh("grok", FakeCli.grok(grok_models("grok-4.5")), directory=state)  # 4.6 was withdrawn
    assert record["status"] == nm.STATUS_UNVERIFIED and "stale default" in record["reason"]
    assert record["effective_model"] == "grok-4.6"  # nothing is invented; the configured value stays visible


def test_discovery_never_sees_an_api_key_and_never_generates(state, monkeypatch):
    for key in nm._API_ENV:
        monkeypatch.setenv(key, "sk-must-not-leak")
    grok = FakeCli.grok(grok_models("grok-4.7", "grok-4.6"))
    codex = FakeCli.codex(codex_catalog(("gpt-6-sol", "list"), ("gpt-5.6-sol", "list")))
    _refresh("grok", grok, directory=state)
    _refresh("codex", codex, directory=state)
    for cli in (grok, codex):
        assert all(not (set(nm._API_ENV) & set(env)) for env in cli.envs)
        for argv in cli.calls:  # only version / help / model-listing / login-status commands
            assert argv[1:] in {("--version",), ("--help",), ("models",), ("exec", "--help"), ("login", "status"),
                                ("debug", "models")}
    assert nm.PROBE == "none"


def test_read_only_or_worktree_regression_blocks_promotion(state):
    registry = load_registry()
    review = registry.get("grok-build-review")
    widened = dataclasses.replace(review, cli_template=review.cli_template + ("--dangerously-skip-permissions",))
    shapes = [nm.WorkerShape.of(widened)]
    record = nm.refresh("grok", "grok-4.6", shapes, runner=FakeCli.grok(grok_models("grok-4.7", "grok-4.6")),
                        which=lambda _b: "/x", now=NOW, directory=state)
    assert record["status"] == nm.STATUS_UNVERIFIED and "read-only route carries forbidden --dangerously-skip-permissions" in record["reason"]
    build = registry.get("grok-build")
    loose = dataclasses.replace(build, requires_isolated_worktree=False)
    record = nm.refresh("grok", "grok-4.6", [nm.WorkerShape.of(loose)], runner=FakeCli.grok(grok_models("grok-4.7")),
                        which=lambda _b: "/x", now=NOW, directory=state)
    assert record["status"] == nm.STATUS_UNVERIFIED and "isolated worktree" in record["reason"]


@pytest.mark.parametrize("worker,drop", [
    ("grok-build-review", ("--sandbox", "read-only")), ("grok-build-review", ("--permission-mode", "plan")),
    ("grok-build-bot", ("--sandbox", "read-only")), ("grok-build", ("--sandbox", "work-tree")),
    ("grok-build-bots", ("--permission-mode", "default")), ("grok-build", ("--no-subagents",)),
    ("grok-build-review", ("--disable-web-search",)),
])
def test_removing_a_required_guard_blocks_promotion_fail_closed(state, worker, drop):
    registry = load_registry()
    original = registry.get(worker)
    template = list(original.cli_template)
    for i in range(len(template) - len(drop) + 1):
        if tuple(template[i:i + len(drop)]) == drop:
            del template[i:i + len(drop)]
            break
    else:
        pytest.fail("test setup: guard not present in template")
    weakened = dataclasses.replace(original, cli_template=tuple(template))
    others = [nm.WorkerShape.of(w) for w in registry.workers.values()
              if w.native_model_family == "grok" and w.name != worker]
    record = nm.refresh("grok", "grok-4.6", [nm.WorkerShape.of(weakened), *others],
                        runner=FakeCli.grok(grok_models("grok-4.7", "grok-4.6")), which=lambda _b: "/x", now=NOW,
                        directory=state)
    assert record["status"] == nm.STATUS_UNVERIFIED and worker in record["reason"] and "lost required" in record["reason"]
    assert record["effective_model"] == "grok-4.6"


@pytest.mark.parametrize("worker,extra", [
    ("grok-build-review", ("--sandbox", "work-tree")), ("grok-build-bot", ("--sandbox", "work-tree")),
    ("grok-build-review", ("--permission-mode", "default")), ("grok-build", ("--sandbox", "read-only")),
    ("grok-build-bots", ("--permission-mode", "bypassPermissions")), ("grok-build", ("--permission-mode", "plan")),
])
def test_a_conflicting_later_value_for_a_guard_blocks_promotion(state, worker, extra):
    registry = load_registry()
    original = registry.get(worker)
    widened = dataclasses.replace(original, cli_template=original.cli_template + extra)
    others = [nm.WorkerShape.of(w) for w in registry.workers.values()
              if w.native_model_family == "grok" and w.name != worker]
    record = nm.refresh("grok", "grok-4.6", [nm.WorkerShape.of(widened), *others],
                        runner=FakeCli.grok(grok_models("grok-4.7", "grok-4.6")), which=lambda _b: "/x", now=NOW,
                        directory=state)
    assert record["status"] == nm.STATUS_UNVERIFIED and worker in record["reason"]
    assert ("conflicting" in record["reason"] or "forbidden" in record["reason"])
    assert record["effective_model"] == "grok-4.6"


@pytest.mark.parametrize("worker,extra", [("codex-review", ("--sandbox", "workspace-write")),
                                          ("codex-build", ("--sandbox", "read-only"))])
def test_a_conflicting_codex_sandbox_blocks_promotion(state, worker, extra):
    registry = load_registry()
    original = registry.get(worker)
    widened = dataclasses.replace(original, cli_template=original.cli_template + extra)
    record = nm.refresh("codex", "gpt-5.6-sol", [nm.WorkerShape.of(widened)],
                        runner=FakeCli.codex(codex_catalog(("gpt-6-sol", "list"), ("gpt-5.6-sol", "list"))),
                        which=lambda _b: "/x", now=NOW, directory=state)
    assert record["status"] == nm.STATUS_UNVERIFIED and "conflicting --sandbox values" in record["reason"]


def test_codex_read_only_and_write_guards_are_required_too(state):
    registry = load_registry()
    review = registry.get("codex-review")
    weakened = dataclasses.replace(review, cli_template=tuple(t for t in review.cli_template if t != "read-only"))
    record = nm.refresh("codex", "gpt-5.6-sol", [nm.WorkerShape.of(weakened)],
                        runner=FakeCli.codex(codex_catalog(("gpt-6-sol", "list"), ("gpt-5.6-sol", "list"))),
                        which=lambda _b: "/x", now=NOW, directory=state)
    assert record["status"] == nm.STATUS_UNVERIFIED and "lost required --sandbox read-only" in record["reason"]


def test_a_cached_advance_is_ignored_for_a_worker_whose_flags_changed_since_verification(state):
    registry = load_registry()
    _refresh("grok", FakeCli.grok(grok_models("grok-4.7", "grok-4.6")), registry, state)
    review = registry.get("grok-build-review")
    same = nm.shape_hash(nm.WorkerShape.of(review))
    assert nm.effective_model("grok", "grok-4.6", worker=review.name, shape=same, directory=state, now=NOW) == "grok-4.7"
    edited = dataclasses.replace(review, cli_template=review.cli_template + ("--debug",))
    changed = nm.shape_hash(nm.WorkerShape.of(edited))
    assert nm.effective_model("grok", "grok-4.6", worker=review.name, shape=changed, directory=state, now=NOW) == "grok-4.6"
    assert nm.effective_model("grok", "grok-4.6", worker="not-a-worker", shape=same, directory=state, now=NOW) == "grok-4.6"


def test_a_nonzero_codex_login_status_is_never_a_subscription_session(state):
    cli = FakeCli.codex(codex_catalog(("gpt-6-sol", "list"), ("gpt-5.6-sol", "list")))
    cli.responses[("codex", "login", "status")] = CommandResult(1, "Logged in using ChatGPT\n")
    record = _refresh("codex", cli, directory=state)
    assert record["status"] == nm.STATUS_UNAVAILABLE and "exited 1" in record["reason"]
    assert record["effective_model"] == "gpt-5.6-sol" and ("codex", "debug", "models") not in cli.calls


def test_a_failed_cache_write_never_leaves_an_older_verified_advance_in_force(state, monkeypatch):
    registry = load_registry()
    _refresh("grok", FakeCli.grok(grok_models("grok-4.7", "grok-4.6")), registry, state)
    assert nm.effective_model("grok", "grok-4.6", directory=state, now=NOW) == "grok-4.7"

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(nm.os, "replace", boom)
    record = _refresh("grok", FakeCli.grok(grok_models("grok-4.7", "grok-4.6", authenticated=False)), registry, state)
    assert record["status"] == nm.STATUS_UNVERIFIED
    assert nm.effective_model("grok", "grok-4.6", directory=state, now=NOW) == "grok-4.6"
    # even when the stale file cannot be removed either, the newest in-process result wins
    monkeypatch.undo()
    _refresh("grok", FakeCli.grok(grok_models("grok-4.7", "grok-4.6")), registry, state)
    assert nm.effective_model("grok", "grok-4.6", directory=state, now=NOW) == "grok-4.7"
    monkeypatch.setattr(nm.os, "replace", boom)
    monkeypatch.setattr(nm.Path, "unlink", boom)
    _refresh("grok", FakeCli.grok(grok_models("grok-4.7", "grok-4.6", authenticated=False)), registry, state)
    assert nm.effective_model("grok", "grok-4.6", directory=state, now=NOW) == "grok-4.6"
    nm._UNWRITABLE.clear()


def test_a_flag_is_documented_only_as_a_whole_token(state):
    lookalike = GROK_HELP.replace("--no-subagents <X>", "--no-subagents-forever <X>\n  see also --tools-list and prose --sandbox-ish")
    record = _refresh("grok", FakeCli.grok(grok_models("grok-4.7", "grok-4.6"), help_text=lookalike), directory=state)
    assert record["status"] == nm.STATUS_UNVERIFIED and "--no-subagents" in record["reason"]
    prose = GROK_HELP.replace("--no-subagents <X>", "the old --no-subagentsx flag was removed")
    assert _refresh("grok", FakeCli.grok(grok_models("grok-4.7"), help_text=prose),
                    directory=state)["status"] == nm.STATUS_UNVERIFIED


def test_the_dashboard_view_marks_an_expired_verification_stale(state, monkeypatch):
    registry = load_registry()
    _refresh("grok", FakeCli.grok(grok_models("grok-4.7", "grok-4.6")), registry, state)
    monkeypatch.setattr(nm, "_now", lambda: NOW + dt.timedelta(seconds=nm.READ_MAX_AGE_SECONDS + 5))
    monkeypatch.setattr(nm, "state_dir", lambda: state)
    row = next(r for r in nm.inventory(registry, directory=state)["families"] if r["family"] == "grok")
    assert row["status"] == "stale" and row["effective_model"] == "grok-4.6" and "seven days" in row["reason"]


def test_a_negated_success_phrase_is_not_a_session_and_stderr_is_read(state):
    cli = FakeCli.codex(codex_catalog(("gpt-6-sol", "list"), ("gpt-5.6-sol", "list")), login="Not logged in using ChatGPT\n")
    record = _refresh("codex", cli, directory=state)
    assert record["status"] == nm.STATUS_UNAVAILABLE and "no logged-in ChatGPT" in record["reason"]
    assert ("codex", "debug", "models") not in cli.calls
    grok = FakeCli.grok(grok_models("grok-4.7", "grok-4.6"))
    grok.responses[("grok", "models")] = CommandResult(0, grok_models("grok-4.7", "grok-4.6"), "Error: not logged in\n")
    record = _refresh("grok", grok, directory=state)
    assert record["status"] == nm.STATUS_UNVERIFIED and "not authenticated" in record["reason"]
    assert record["effective_model"] == "grok-4.6"


def test_live_runs_reverify_auth_even_when_a_recent_verified_record_exists(state):
    registry = load_registry()
    good = FakeCli.grok(grok_models("grok-4.7", "grok-4.6"))
    nm.ensure_fresh(registry, directory=state, now=NOW, runner=good, which=lambda _b: "/x")
    assert nm.effective_model("grok", "grok-4.6", directory=state, now=NOW) == "grok-4.7"
    # Session logs out (same CLI version, record only seconds old): the live-run check must not trust the cache.
    logged_out = FakeCli.grok(grok_models("grok-4.7", "grok-4.6", authenticated=False))
    record = nm.ensure_fresh(registry, max_age_seconds=0, directory=state, now=NOW, runner=logged_out,
                             which=lambda _b: "/x")["grok"]
    assert record["status"] == nm.STATUS_UNVERIFIED and ("grok", "models") in logged_out.calls
    assert nm.effective_model("grok", "grok-4.6", directory=state, now=NOW) == "grok-4.6"
    # ...and the same for a switch to API-key auth on Codex.
    ok = FakeCli.codex(codex_catalog(("gpt-6-sol", "list"), ("gpt-5.6-sol", "list")))
    nm.ensure_fresh(registry, directory=state, now=NOW, runner=ok, which=lambda _b: "/x")
    assert nm.effective_model("codex", "gpt-5.6-sol", directory=state, now=NOW) == "gpt-6-sol"
    keyed = FakeCli.codex(codex_catalog(("gpt-6-sol", "list"), ("gpt-5.6-sol", "list")), login="Logged in using an API key\n")
    nm.ensure_fresh(registry, max_age_seconds=0, directory=state, now=NOW, runner=keyed, which=lambda _b: "/x")
    assert nm.effective_model("codex", "gpt-5.6-sol", directory=state, now=NOW) == "gpt-5.6-sol"


# --------------------------------------------------------------------------- OpenCode is a hint only


def _seed_opencode_catalog(*labels: tuple[str, str, str]):
    models = tuple(
        mc.CatalogModel(id=f"{pid}/{mid}", provider_id=pid, model_id=mid, display_name=label, family="f", status="active",
                        api_url="https://x", context_limit=200000, output_limit=8000, cost=None, credential="oauth",
                        cost_class=mc.COST_SUBSCRIPTION, cost_reason="oauth", capable=True, capability_reasons=())
        for pid, mid, label in labels)
    catalog = mc.Catalog(mc.CATALOG_OK, "ok", "9.9.9", NOW.isoformat(), "ok", models)
    mc._write_json(mc.opencode_state_dir() / mc.CATALOG_FILENAME, catalog.to_json())


def test_opencode_label_is_recorded_as_a_hint_and_never_becomes_the_native_id(state):
    _seed_opencode_catalog(("openai", "gpt-6-sol-oc", "GPT-6 Sol"), ("xai", "grok-4.7-oc", "Grok 4.7"))
    grok = _refresh("grok", FakeCli.grok(grok_models("grok-4.6", "grok-4.5")), directory=state)
    codex = _refresh("codex", FakeCli.codex(codex_catalog(("gpt-5.6-sol", "list"))), directory=state)
    assert grok["opencode_hints"] == ["Grok 4.7"] and codex["opencode_hints"] == ["GPT-6 Sol"]
    assert grok["status"] == nm.STATUS_CURRENT and codex["status"] == nm.STATUS_CURRENT
    assert grok["candidate"] is None and codex["candidate"] is None
    assert nm.effective_model("grok", "grok-4.6", directory=state) == "grok-4.6"
    assert nm.effective_model("codex", "gpt-5.6-sol", directory=state) == "gpt-5.6-sol"
    assert "no matching native model identifier" in grok["reason"]


# --------------------------------------------------------------------------- cache / fingerprint


def test_read_path_ignores_stale_mismatched_or_unverified_records(state):
    _refresh("grok", FakeCli.grok(grok_models("grok-4.7", "grok-4.6")), directory=state)
    assert nm.effective_model("grok", "grok-4.6", directory=state, now=NOW) == "grok-4.7"
    assert nm.effective_model("grok", "grok-4.5", directory=state, now=NOW) == "grok-4.5"  # configured baseline changed
    later = NOW + dt.timedelta(seconds=nm.READ_MAX_AGE_SECONDS + 1)
    assert nm.effective_model("grok", "grok-4.6", directory=state, now=later) == "grok-4.6"
    (state / "grok.json").write_text("{not json")
    assert nm.effective_model("grok", "grok-4.6", directory=state, now=NOW) == "grok-4.6"


def test_ensure_fresh_reuses_a_valid_record_and_reverifies_on_cli_or_capability_change(state):
    registry = load_registry()
    cli = FakeCli.grok(grok_models("grok-4.7", "grok-4.6"))
    first = nm.ensure_fresh(registry, directory=state, now=NOW, runner=cli, which=lambda _b: "/x")["grok"]
    enumerations = cli.calls.count(("grok", "models"))
    again = nm.ensure_fresh(registry, directory=state, now=NOW, runner=cli, which=lambda _b: "/x")["grok"]
    assert again["fingerprint"] == first["fingerprint"] and cli.calls.count(("grok", "models")) == enumerations
    # the CLI is upgraded: the cheap --version check invalidates the record and the new capability set is used
    upgraded = FakeCli.grok(grok_models("grok-4.6"), version="grok 1.1.0")
    changed = nm.ensure_fresh(registry, directory=state, now=NOW, runner=upgraded, which=lambda _b: "/x")["grok"]
    assert changed["status"] == nm.STATUS_CURRENT and changed["fingerprint"] != first["fingerprint"]
    assert upgraded.calls.count(("grok", "models")) == 1
    assert nm.effective_model("grok", "grok-4.6", directory=state, now=NOW) == "grok-4.6"  # advance withdrawn
    # an expired record is re-enumerated too
    old = NOW + dt.timedelta(seconds=nm.REFRESH_MAX_AGE_SECONDS + 1)
    nm.ensure_fresh(registry, directory=state, now=old, runner=upgraded, which=lambda _b: "/x")
    assert upgraded.calls.count(("grok", "models")) == 2


def test_fingerprint_changes_when_the_auth_state_or_supported_set_changes(state):
    a = _refresh("grok", FakeCli.grok(grok_models("grok-4.7", "grok-4.6")), directory=state)["fingerprint"]
    b = _refresh("grok", FakeCli.grok(grok_models("grok-4.7", "grok-4.6", authenticated=False)), directory=state)
    c = _refresh("grok", FakeCli.grok(grok_models("grok-4.6")), directory=state)["fingerprint"]
    assert len({a, b["fingerprint"], c}) == 3


# --------------------------------------------------------------------------- routing, #7, evidence, dashboard


def test_freshness_changes_no_routing_priority_and_leaves_the_opencode_fallback_alone(state, monkeypatch):
    monkeypatch.setattr(nm, "state_dir", lambda: state)
    registry = load_registry()
    routes = dict(registry.routes)
    fingerprint = {n: (w.capability, w.cost_class, w.roles, w.allowed_policy_roles, w.auth_mode, w.allow_api_billing,
                       w.cli_template, w.permission_profile_templates) for n, w in registry.workers.items()}
    _refresh("grok", FakeCli.grok(grok_models("grok-4.7", "grok-4.6")), registry, state)
    _refresh("codex", FakeCli.codex(codex_catalog(("gpt-6-sol", "list"), ("gpt-5.6-sol", "list"))), registry, state)
    reloaded = load_registry()
    assert dict(reloaded.routes) == routes
    assert {n: (w.capability, w.cost_class, w.roles, w.allowed_policy_roles, w.auth_mode, w.allow_api_billing,
                w.cli_template, w.permission_profile_templates) for n, w in reloaded.workers.items()} == fingerprint
    assert not any(w.allow_api_billing for w in reloaded.workers.values())
    for name in ("opencode-free-review", "opencode-free-tests", "opencode2-gemini-flash-lite"):
        assert reloaded.get(name).effective_model == reloaded.get(name).default_model


def test_run_evidence_records_the_actual_model_and_the_freshness_state(state, monkeypatch, tmp_path):
    monkeypatch.setattr(nm, "state_dir", lambda: state)
    registry = load_registry()
    _refresh("grok", FakeCli.grok(grok_models("grok-4.7", "grok-4.6")), registry, state)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x = 1\n")
    result = orchestrate.run_delegation(
        registry=registry, root=tmp_path, task="ENG-AO-04", worker_name="grok-build", role="primary-implementation",
        model=None, intensity="low", why="test", prompt_args=["do it"], scope_paths=["src/app.py"], dry_run=True,
        allow_write=True, allow_overflow=False, timeout=5.0)
    policy = result.manifest["policy_manifest"]
    assert policy["actual_worker"] == "grok-build" and policy["actual_model"] == "grok-4.7"
    fresh = policy["model_freshness"]
    assert (fresh["configured_default"], fresh["actual_model"], fresh["candidate"], fresh["status"]) == (
        "grok-4.6", "grok-4.7", "grok-4.7", nm.STATUS_VERIFIED)
    assert fresh["probe"] == "none" and fresh["fingerprint"] and fresh["checked_at"] and fresh["last_verified_model"] == "grok-4.7"
    assert result.record.planned_model == "grok-4.7" and "grok-4.7" in result.record.requested_command
    explicit = orchestrate.run_delegation(
        registry=registry, root=tmp_path, task="ENG-AO-04", worker_name="grok-build", role="primary-implementation",
        model="grok-4.5", intensity="low", why="test", prompt_args=["do it"], scope_paths=["src/app.py"],
        dry_run=True, allow_write=True, allow_overflow=False, timeout=5.0)
    assert explicit.manifest["policy_manifest"]["actual_model"] == "grok-4.5"  # an explicit request still wins


def test_a_live_run_refreshes_through_the_local_cli_and_a_dry_run_spawns_nothing(state, monkeypatch):
    monkeypatch.setattr(nm, "state_dir", lambda: state)
    cli = FakeCli.grok(grok_models("grok-4.7", "grok-4.6"))
    monkeypatch.setattr(nm, "subprocess_runner", cli)
    monkeypatch.setattr(nm, "_which", lambda _b: "/x")
    registry = load_registry()
    orchestrate._refresh_native_models(registry, registry.get("grok-build-review"), dry_run=True)
    assert cli.calls == [] and registry.get("grok-build-review").effective_model == "grok-4.6"
    orchestrate._refresh_native_models(registry, registry.get("codex-review"), dry_run=False)  # codex is not scripted
    orchestrate._refresh_native_models(registry, registry.get("grok-build-review"), dry_run=False)
    assert ("grok", "models") in cli.calls
    assert registry.get("grok-build-review").effective_model == "grok-4.7"
    assert registry.get("codex-review").effective_model == "gpt-5.6-sol"  # unscripted/failed enumeration retains it


def test_dashboard_reports_configured_vs_candidate_without_spawning_anything(state, tmp_path, monkeypatch):
    monkeypatch.setattr(nm, "state_dir", lambda: state)
    registry = load_registry()
    st = State(":memory:")
    for provider in seed_provider_states(registry):
        st.upsert_provider_state(provider)
    client = TestClient(create_app(CommandContext(
        state=st, registry=registry, scheduler=Scheduler(), supervisor=None, repo_root=tmp_path)))
    empty = {f["family"]: f for f in client.get("/api/native-models").json()["families"]}
    assert empty["grok"]["status"] == "not-refreshed" and empty["grok"]["effective_model"] == "grok-4.6"
    _refresh("grok", FakeCli.grok(grok_models("grok-4.7", "grok-4.6", authenticated=False)), registry, state)
    _refresh("codex", FakeCli.codex(codex_catalog(("gpt-6-astra", "list"), ("gpt-5.6-sol", "list"))), registry, state)

    def boom(*_a, **_k):
        raise AssertionError("the dashboard must never spawn a subprocess")

    monkeypatch.setattr("subprocess.Popen", boom)
    monkeypatch.setattr("subprocess.run", boom)
    rows = {f["family"]: f for f in client.get("/api/native-models").json()["families"]}
    assert rows["grok"]["candidate"]["native_id"] == "grok-4.7" and rows["grok"]["status"] == "unverified"
    assert "not authenticated" in rows["grok"]["reason"] and rows["grok"]["effective_model"] == "grok-4.6"
    assert rows["codex"]["status"] == "current" and rows["codex"]["newer_other_tier"] == ["gpt-6-astra"]
    assert sorted(rows["grok"]["workers"]) == sorted(GROK_WORKERS)
    models = {m["worker"]: m for m in client.get("/api/models").json()}
    assert models["grok-build"]["default_model"] == "grok-4.6" and models["grok-build"]["native_model_family"] == "grok"
