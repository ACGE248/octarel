"""ENG-AO-03: dynamic OpenCode model catalog, free-model qualification, and free reviewer fallback.

Everything is deterministic: a fake ``opencode`` (an in-process runner, or an executable stand-in on PATH for the
end-to-end runs) supplies the catalog and every probe/run.  No live provider or billable call is ever made.
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scripts.agents import model_catalog as mc
from scripts.agents import orchestrate
from scripts.agents.control_plane.commands import CommandContext
from scripts.agents.control_plane.dashboard_api import create_app
from scripts.agents.control_plane.dispatch import _candidate_scores
from scripts.agents.control_plane.models import KIND_READ, ProviderState, Task
from scripts.agents.control_plane.provider_state import (
    STATE_AVAILABLE,
    seed_provider_states,
)
from scripts.agents.control_plane.scheduler import Scheduler
from scripts.agents.control_plane.state import State
from scripts.agents.control_plane.supervisor import Supervisor
from scripts.agents.registry import RegistryError, load_registry

NOW = dt.datetime(2026, 9, 24, 12, 0, tzinfo=dt.timezone.utc)
ZEN = "https://opencode.ai/zen/v1"


# --------------------------------------------------------------------------- fake catalog / fake OpenCode


def entry(provider, model_id, name=None, *, family="", cost=(0, 0), url=ZEN, context=262144, tool=True,
          status="active", text=True, drop=()):
    body = {
        "id": model_id, "providerID": provider, "name": name or model_id, "family": family,
        "api": {"id": model_id, "url": url, "npm": "@ai-sdk/openai-compatible"}, "status": status,
        "cost": {"input": cost[0], "output": cost[1], "cache": {"read": 0, "write": 0}},
        "limit": {"context": context, "output": 32000},
        "capabilities": {"toolcall": tool, "input": {"text": text}, "output": {"text": text}},
    }
    for key in drop:
        body.pop(key, None)
    return body


def verbose(entries) -> str:
    return "\n".join(f"{e['providerID']}/{e['id']}\n{json.dumps(e, indent=2)}" for e in entries) + "\n"


PROVIDERS_TEXT = "┌  Credentials ~/.local/share/opencode/auth.json\n│\n●  OpenAI \x1b[90moauth\n│\n●  Google \x1b[90mapi\n│\n●  xAI \x1b[90moauth\n│\n└  3 credentials\n"

DEFAULT_ENTRIES = [
    entry("opencode", "nemotron-3-ultra-free", "Nemotron 3 Ultra Free", family="nemotron", context=1_000_000),
    entry("opencode", "muse-spark-1.3-contributor-free", "Muse Spark 1.3 Free", family="muse-spark", context=1_048_576),
    entry("opencode", "big-pickle", "Big Pickle", family="big-pickle", context=200_000),
    entry("opencode", "paid-thing-free", "Looks Free But Is Priced", family="thing", cost=(0.5, 2)),
    entry("opencode", "tiny-free", "Tiny Free", family="tiny", context=8000),
    entry("google", "gemini-3.5-flash-lite", "Gemini 3.5 Flash Lite", cost=(0.3, 2.5), url="", context=1_048_576),
    entry("openai", "gpt-5.6-sol", "GPT 5.6 Sol", cost=(2, 6), url="", context=400_000),
    entry("xai", "grok-4.6", "Grok 4.6", cost=(2, 6), url="", context=500_000),
    entry("mystery", "model-x", "Mystery", cost=(0, 0), url="https://example.invalid/v1"),
]


class FakeOpenCode:
    """In-process runner: serves the catalog, agent config and per-model run behaviour, recording every call."""

    def __init__(self, entries=None, *, version="9.9.9", providers=PROVIDERS_TEXT, catalog_ok=True):
        self.entries = list(DEFAULT_ENTRIES if entries is None else entries)
        self.version = version
        self.providers = providers
        self.catalog_ok = catalog_ok
        self.run_output: dict[str, str] = {}
        self.run_code: dict[str, int] = {}
        self.deny_edit = True
        self.write_on_run: set[str] = set()
        self.calls: list[list[str]] = []

    def __call__(self, argv, cwd, timeout, env):
        argv = list(argv)
        self.calls.append(argv)
        if argv[1:] == ["--version"]:
            return mc.CommandResult(0, self.version + "\n")
        if argv[1:3] == ["models", "--verbose"]:
            return mc.CommandResult(0, verbose(self.entries)) if self.catalog_ok else mc.CommandResult(1, "", "boom")
        if argv[1:3] == ["providers", "list"]:
            return mc.CommandResult(0, self.providers) if self.providers is not None else mc.CommandResult(1)
        if argv[1:3] == ["debug", "agent"]:
            action = "deny" if self.deny_edit else "allow"
            perms = [{"permission": p, "pattern": "*", "action": action} for p in ("edit", "task", "bash")]
            return mc.CommandResult(0, json.dumps({"mode": "primary", "permission": perms}))
        if argv[1] == "run":
            model = argv[argv.index("--model") + 1]
            if model in self.write_on_run and cwd is not None:
                (Path(cwd) / "sneaky.txt").write_text("edit")
            code = self.run_code.get(model, 0)
            out = self.run_output.get(model, "READY" if "--agent" in argv and argv[argv.index("--agent") + 1] == "reviewer" else "TESTER_OK")
            return mc.CommandResult(code, out, "")
        raise AssertionError(f"unexpected opencode call {argv}")

    def runs(self, model=None):
        return [c for c in self.calls if c[1] == "run" and (model is None or c[c.index("--model") + 1] == model)]


@pytest.fixture
def fake():
    return FakeOpenCode()


@pytest.fixture
def state_dir(tmp_path):
    return tmp_path / "oc-state"


def discover(fake, state_dir, **kw):
    return mc.discover_catalog(runner=fake, which=lambda _b: "/usr/bin/opencode", binary="opencode", now=NOW,
                               state_dir=state_dir, **kw)


def select(catalog, fake, state_dir, role="diff-review", **kw):
    return mc.select_pool_model(catalog, role, runner=fake, binary="opencode", state_dir=state_dir, now=NOW, **kw)


# --------------------------------------------------------------------------- discovery and classification


def test_discovery_reads_machine_readable_catalog_and_versions(fake, state_dir):
    catalog = discover(fake, state_dir)
    assert catalog.status == mc.CATALOG_OK
    assert catalog.opencode_version == "9.9.9" and catalog.refreshed_at.startswith("2026-09-24")
    assert {m.id for m in catalog.models} >= {"opencode/nemotron-3-ultra-free", "xai/grok-4.6", "openai/gpt-5.6-sol"}
    # Only OpenCode's own non-interactive, non-generating commands are used (local; --refresh is opt-in).
    assert [c[1:] for c in fake.calls] == [["--version"], ["models", "--verbose"], ["providers", "list"]]
    assert mc.load_catalog(state_dir).fingerprint == catalog.fingerprint  # persisted snapshot round-trips


def test_cost_classification_uses_metadata_not_names(fake, state_dir):
    by_id = {m.id: m for m in discover(fake, state_dir).models}
    assert by_id["opencode/nemotron-3-ultra-free"].cost_class == mc.COST_FREE_OPENCODE
    # No "free" in the name, but OpenCode proves it: zero cost on the Zen transport.
    assert by_id["opencode/big-pickle"].cost_class == mc.COST_FREE_OPENCODE
    # "Free" in the name is not proof.
    assert by_id["opencode/paid-thing-free"].cost_class == mc.COST_METERED
    assert by_id["openai/gpt-5.6-sol"].cost_class == mc.COST_SUBSCRIPTION  # oauth session
    assert by_id["xai/grok-4.6"].cost_class == mc.COST_SUBSCRIPTION
    assert by_id["google/gemini-3.5-flash-lite"].cost_class == mc.COST_METERED  # api-key credential
    # Zero cost off the Zen transport with no reported credential proves nothing: unknown fails closed.
    assert by_id["mystery/model-x"].cost_class == mc.COST_UNKNOWN


def test_tiered_price_metadata_from_real_opencode_is_understood(state_dir):
    tiers = {"tiers": [{"input": 4, "output": 12, "cache": {"read": 1, "write": 0},
                        "tier": {"type": "context", "size": 200000}}]}
    paid = entry("xai", "grok-4.6", cost=(2, 6), url="")
    paid["cost"].update(tiers)
    free = entry("opencode", "tiered-free", "Tiered Free")
    free["cost"]["tiers"] = [{"input": 0, "output": 0, "tier": {"type": "context", "size": 200000}}]
    sneaky = entry("opencode", "sneaky-free", "Sneaky Free")
    sneaky["cost"]["tiers"] = [{"input": 3, "output": 9, "tier": {"type": "context", "size": 200000}}]
    by_id = {m.id: m for m in discover(FakeOpenCode([paid, free, sneaky]), state_dir).models}
    assert by_id["xai/grok-4.6"].cost_class == mc.COST_SUBSCRIPTION
    assert by_id["opencode/tiered-free"].cost_class == mc.COST_FREE_OPENCODE
    assert by_id["opencode/sneaky-free"].cost_class == mc.COST_METERED  # a paid tier makes it non-free


def test_incomplete_metadata_and_unreadable_credentials_fail_closed(state_dir):
    entries = [entry("opencode", "no-cost-free", "No Cost Free", drop=("cost",)),
               entry("opencode", "no-caps-free", "No Caps Free", drop=("capabilities",)),
               entry("opencode", "ok-free", "Ok Free")]
    catalog = discover(FakeOpenCode(entries, providers=None), state_dir)
    by_id = {m.id: m for m in catalog.models}
    assert by_id["opencode/no-cost-free"].cost_class == mc.COST_UNKNOWN
    assert by_id["opencode/no-caps-free"].capable is False
    assert by_id["opencode/ok-free"].cost_class == mc.COST_FREE_OPENCODE  # Zen free needs no credential
    assert catalog.credentials_status == "unavailable"
    # With credentials unreadable, a credential-backed provider is unknown, never subscription/free.
    other = discover(FakeOpenCode([entry("xai", "grok-4.6", cost=(2, 6), url="")], providers=None), state_dir)
    assert other.models[0].cost_class == mc.COST_UNKNOWN


def test_plain_model_list_without_metadata_is_never_free(state_dir):
    catalog = mc.discover_catalog(
        runner=lambda argv, *a: mc.CommandResult(0, "opencode/mystery-free\nopencode/another-free\n"),
        which=lambda _b: "/x", binary="opencode", now=NOW, state_dir=state_dir,
    )
    assert catalog.status == mc.CATALOG_OK and {m.cost_class for m in catalog.models} == {mc.COST_UNKNOWN}
    assert mc.select_pool_model(catalog, "diff-review", state_dir=state_dir, now=NOW).model is None


def test_opencode_absent_or_failing_gives_no_candidates_without_raising(fake, state_dir):
    absent = mc.discover_catalog(which=lambda _b: None, binary="opencode", now=NOW, state_dir=state_dir)
    assert absent.status == mc.CATALOG_ABSENT and absent.models == ()
    failing = discover(FakeOpenCode(catalog_ok=False), state_dir)
    assert failing.status == mc.CATALOG_UNAVAILABLE and "unauthenticated" in failing.reason
    timeout = mc.discover_catalog(runner=lambda *a: mc.CommandResult(None, error="timeout"), which=lambda _b: "/x",
                                  binary="opencode", now=NOW, state_dir=state_dir)
    assert timeout.status == mc.CATALOG_UNAVAILABLE
    garbage = mc.discover_catalog(runner=lambda *a: mc.CommandResult(0, "<<not a catalog>>\n"), which=lambda _b: "/x",
                                  binary="opencode", now=NOW, state_dir=state_dir)
    assert garbage.status == mc.CATALOG_UNPARSEABLE
    for catalog in (absent, failing, timeout, garbage):
        sel = mc.select_pool_model(catalog, "diff-review", state_dir=state_dir, now=NOW)
        assert sel.model is None and sel.evidence["catalog"]["status"] == catalog.status
    assert mc.select_pool_model(None, "diff-review", state_dir=state_dir).model is None


def test_catalog_changes_take_effect_without_source_edits(fake, state_dir):
    first = discover(fake, state_dir)
    assert select(first, fake, state_dir, allow_probe=True).model.id == "opencode/muse-spark-1.3-contributor-free"
    # The installed OpenCode's catalog changes: that model disappears and a brand-new free model appears.
    fake.entries = [e for e in fake.entries if e["id"] != "muse-spark-1.3-contributor-free"]
    fake.entries.append(entry("opencode", "brand-new-lite-free", "Brand New Lite Free", family="lite", context=2_000_000))
    second = discover(fake, state_dir)
    assert second.fingerprint != first.fingerprint
    assert second.get("opencode/muse-spark-1.3-contributor-free") is None
    assert select(second, fake, state_dir, allow_probe=True).model.id == "opencode/brand-new-lite-free"


def test_get_catalog_reuses_fresh_snapshot_and_refreshes_stale(fake, state_dir):
    discover(fake, state_dir)
    calls = len(fake.calls)
    kw = dict(runner=fake, which=lambda _b: "/x", binary="opencode", state_dir=state_dir)
    assert mc.get_catalog(now=NOW + dt.timedelta(minutes=5), **kw).status == mc.CATALOG_OK
    assert len(fake.calls) == calls  # fresh: no spawn
    assert mc.get_catalog(now=NOW + dt.timedelta(hours=2), discover=False, **kw) is not None
    assert len(fake.calls) == calls  # discover=False never spawns even when stale
    mc.get_catalog(now=NOW + dt.timedelta(hours=2), **kw)
    assert len(fake.calls) > calls  # stale: re-discovered
    mc.get_catalog(refresh=True, now=NOW, **kw)


# --------------------------------------------------------------------------- qualification


def test_only_proven_free_capable_models_are_ever_probed(fake, state_dir):
    catalog = discover(fake, state_dir)
    for model_id in ("openai/gpt-5.6-sol", "xai/grok-4.6", "google/gemini-3.5-flash-lite", "mystery/model-x",
                     "opencode/paid-thing-free", "opencode/tiny-free"):
        q = mc.qualify_model(catalog.get(model_id), "review", opencode_version="9.9.9", runner=fake, binary="opencode",
                             state_dir=state_dir, now=NOW)
        assert q.status == mc.UNQUALIFIED and q.reason.startswith("not probed")
    assert fake.runs() == []  # no model launch for anything not proven free: no billing path exists


def test_qualification_verifies_contract_readonly_and_is_cached(fake, state_dir):
    catalog = discover(fake, state_dir)
    model = catalog.get("opencode/nemotron-3-ultra-free")
    q = mc.qualify_model(model, "review", opencode_version="9.9.9", runner=fake, binary="opencode",
                         state_dir=state_dir, now=NOW)
    assert q.status == mc.QUALIFIED
    assert q.checks["launch"] == "ok" and q.checks["read_only_permissions"] == "denied"
    assert q.checks["tree_unchanged"] == "ok" and q.checks["review_contract"] == "ok" and q.checks["billing"] == "none-observed"
    # Selection reuses the cached qualification: no second probe for the same version/model/preset.
    assert select(catalog, fake, state_dir, allow_probe=True, only_model=model.id).model.id == model.id
    assert len(fake.runs(model.id)) == 1
    later = NOW + dt.timedelta(days=1)
    assert mc.cached_qualification(model, "review", opencode_version="9.9.9", state_dir=state_dir, now=later)
    # Invalidated by OpenCode version change, model metadata change, or expiry.
    assert mc.cached_qualification(model, "review", opencode_version="10.0.0", state_dir=state_dir, now=NOW) is None
    assert mc.cached_qualification(model, "review", opencode_version="9.9.9", state_dir=state_dir,
                                   now=NOW + dt.timedelta(days=8)) is None
    changed = discover(FakeOpenCode([entry("opencode", "nemotron-3-ultra-free", "Nemotron 3 Ultra Free",
                                           family="nemotron", context=500_000)]), state_dir)
    assert mc.cached_qualification(changed.models[0], "review", opencode_version="9.9.9", state_dir=state_dir,
                                   now=NOW) is None


@pytest.mark.parametrize(
    "mutate, expected",
    [
        (lambda f: f.run_output.update({"opencode/nemotron-3-ultra-free": "I think the diff looks fine to me."}), "strict review contract"),
        (lambda f: f.run_code.update({"opencode/nemotron-3-ultra-free": 1}), "did not launch"),
        (lambda f: f.write_on_run.add("opencode/nemotron-3-ultra-free"), "modified the workspace"),
        (lambda f: setattr(f, "deny_edit", False), "read-only permissions"),
        (lambda f: f.run_output.update({"opencode/nemotron-3-ultra-free": 'Agent "reviewer" not found. Falling back to default agent\nREADY'}), "default agent"),
        (lambda f: f.run_output.update({"opencode/nemotron-3-ultra-free": "Error: insufficient credit, add an API key"}), "billing"),
    ],
)
def test_qualification_failures_are_unqualified_with_a_reason(fake, state_dir, mutate, expected):
    mutate(fake)
    catalog = discover(fake, state_dir)
    model = catalog.get("opencode/nemotron-3-ultra-free")
    q = mc.qualify_model(model, "review", opencode_version="9.9.9", runner=fake, binary="opencode",
                         state_dir=state_dir, now=NOW)
    assert q.status == mc.UNQUALIFIED and expected in q.reason
    # A failed result is cached briefly so a broken model is not re-probed on every task.
    before = len(fake.calls)
    sel = select(catalog, fake, state_dir, allow_probe=True, only_model=model.id)
    assert sel.model is None and len(fake.calls) == before


def test_tester_profile_uses_the_tester_preset(fake, state_dir):
    catalog = discover(fake, state_dir)
    q = mc.qualify_model(catalog.get("opencode/nemotron-3-ultra-free"), "tests", opencode_version="9.9.9",
                         runner=fake, binary="opencode", state_dir=state_dir, now=NOW)
    assert q.status == mc.QUALIFIED
    assert fake.runs()[0][fake.runs()[0].index("--agent") + 1] == "tester"


# --------------------------------------------------------------------------- selection / routing


def test_multiple_free_models_deterministic_choice_and_rejection_reasons(fake, state_dir):
    catalog = discover(fake, state_dir)
    sel = select(catalog, fake, state_dir, allow_probe=True)
    assert sel.model.id == "opencode/muse-spark-1.3-contributor-free"  # largest context, then id
    by_id = {c["model"]: c for c in sel.evidence["candidates"]}
    assert by_id["opencode/muse-spark-1.3-contributor-free"]["decision"] == "selected"
    for rejected, fragment in {
        "openai/gpt-5.6-sol": "subscription", "xai/grok-4.6": "subscription", "google/gemini-3.5-flash-lite": "metered",
        "mystery/model-x": "unknown", "opencode/paid-thing-free": "metered", "opencode/tiny-free": "context limit",
    }.items():
        assert by_id[rejected]["decision"] == "rejected" and fragment in by_id[rejected]["reason"]
    assert sel.evidence["catalog"]["opencode_version"] == "9.9.9" and sel.evidence["catalog"]["refreshed_at"]
    assert sel.evidence["provider"] == "OpenCode Zen"


def test_unqualified_free_model_is_skipped_for_the_next_one(fake, state_dir):
    fake.run_output["opencode/muse-spark-1.3-contributor-free"] = "no idea"
    sel = select(discover(fake, state_dir), fake, state_dir, allow_probe=True)
    assert sel.model.id == "opencode/nemotron-3-ultra-free"
    skipped = next(c for c in sel.evidence["candidates"] if c["model"] == "opencode/muse-spark-1.3-contributor-free")
    assert skipped["decision"] == "rejected" and "strict review contract" in skipped["reason"]
    assert skipped["qualification"] == mc.UNQUALIFIED


def test_selection_without_probing_is_cache_only_and_probe_budget_is_bounded(fake, state_dir):
    catalog = discover(fake, state_dir)
    before = len(fake.calls)
    assert select(catalog, fake, state_dir, allow_probe=False).model is None
    assert len(fake.calls) == before  # nothing spawned
    for model in fake.entries:
        fake.run_output[f"opencode/{model['id']}"] = "nope"
    select(catalog, fake, state_dir, allow_probe=True)
    assert len(fake.runs()) == mc.MAX_PROBES_PER_SELECTION  # bounded: at most two probes per selection


@pytest.mark.parametrize(
    "avoid, family, model_id, name",
    [("xAI", "grok", "grok-fast-free", "Grok Fast Free"), ("Google", "gemma", "gemma-free", "Gemma Free"),
     ("OpenAI", "gpt", "gpt-nano-free", "GPT Nano Free"), ("Anthropic", "claude", "claude-lite-free", "Claude Lite Free")],
)
def test_same_vendor_free_model_is_rejected_when_diversity_is_required(fake, state_dir, avoid, family, model_id, name):
    fake.entries = [entry("opencode", model_id, name, family=family, context=2_000_000),
                    entry("opencode", "nemotron-3-ultra-free", "Nemotron 3 Ultra Free", family="nemotron")]
    catalog = discover(fake, state_dir)
    assert select(catalog, fake, state_dir, allow_probe=True).model.id == f"opencode/{model_id}"  # free and fine w/o diversity
    sel = select(catalog, fake, state_dir, allow_probe=True, avoid_provider=avoid)
    assert sel.model.id == "opencode/nemotron-3-ultra-free"
    rejected = next(c for c in sel.evidence["candidates"] if c["model"] == f"opencode/{model_id}")
    assert "provider diversity" in rejected["reason"]
    # If nothing diverse exists, the stage fails closed instead of accepting the same vendor.
    fake.entries = fake.entries[:1]
    assert select(discover(fake, state_dir), fake, state_dir, allow_probe=True, avoid_provider=avoid).model is None


def test_quota_failure_moves_to_another_free_model_never_to_a_stronger_one(fake, state_dir):
    catalog = discover(fake, state_dir)
    first = select(catalog, fake, state_dir, allow_probe=True)
    assert mc.record_model_failure(first.model.id, "quota", state_dir=state_dir, now=NOW)
    second = select(catalog, fake, state_dir, allow_probe=True)
    assert second.model.id != first.model.id and second.model.cost_class == mc.COST_FREE_OPENCODE
    cooled = next(c for c in second.evidence["candidates"] if c["model"] == first.model.id)
    assert "cooling down after quota" in cooled["reason"]
    # Only availability-type failures cool a model; a capability failure does not, and nothing escalates.
    assert mc.record_model_failure("opencode/x", "reasoning", state_dir=state_dir) is False
    # The cooldown expires on its own, deterministically.
    later = mc.select_pool_model(catalog, "diff-review", state_dir=state_dir, runner=fake, binary="opencode",
                                 now=NOW + dt.timedelta(hours=2), allow_probe=True)
    assert later.model.id == first.model.id


def test_only_read_only_roles_are_served(fake, state_dir):
    catalog = discover(fake, state_dir)
    for role in ("primary-implementation", "secondary-implementation", "bot-implementation", "overflow"):
        sel = select(catalog, fake, state_dir, role=role, allow_probe=True)
        assert sel.model is None and "not a read-only role" in sel.reason
    for role in ("diff-review", "doc-drift-review", "focused-tests", "mechanical-testing", "impact-search"):
        assert select(catalog, fake, state_dir, role=role, allow_probe=True).model is not None


# --------------------------------------------------------------------------- registry / routing integration


def test_pool_workers_are_read_only_and_configured_gemini_route_is_preserved():
    registry = load_registry()
    review, tests = registry.get("opencode-free-review"), registry.get("opencode-free-tests")
    for worker in (review, tests):
        assert worker.is_read_only and not worker.is_write_capable and worker.model_pool == "opencode-free"
        assert worker.default_model == "" and worker.allow_api_billing is False
        assert not ({"IMPLEMENTER", "ORCHESTRATOR"} & set(worker.allowed_policy_roles))
        with pytest.raises(RegistryError):
            worker.build_command(model=None, intensity="low", prompt="p")
        assert worker.build_command(model="opencode/x-free", intensity="low", prompt="p")[:5] == [
            "opencode", "run", "--model", "opencode/x-free", "--agent"]
    gemini = registry.get("opencode2-gemini-flash-lite")
    assert gemini.default_model == "google/gemini-3.5-flash-lite" and gemini.model_pool == ""
    assert registry.get("opencode2-gemini-flash-lite-review").default_model == "google/gemini-3.5-flash-lite"
    diff = registry.route("diff-review")
    assert diff.index("opencode2-gemini-flash-lite-review") < diff.index("opencode-free-review") < diff.index("grok-build-review")
    assert registry.route("focused-tests")[0] == "opencode2-gemini-flash-lite"


def test_a_write_capable_pool_worker_is_rejected_by_the_registry(tmp_path):
    raw = json.loads((Path("scripts/agents/workers.json")).read_text())
    raw["workers"]["opencode-free-review"].update(capability="write", requires_isolated_worktree=True,
                                                  allowed_policy_roles=["IMPLEMENTER"])
    path = tmp_path / "workers.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(RegistryError, match="must be read-only"):
        load_registry(path)


def _dispatch_state(unavailable=()):
    registry = load_registry()
    state = State(":memory:")
    for provider in seed_provider_states(registry):
        state.upsert_provider_state(provider)
    for worker in registry.workers.values():
        state.upsert_provider_state(ProviderState(
            name=worker.name, execution_system=worker.execution_system, provider=worker.provider,
            cost_class=worker.cost_class, configured=True,
            state="FAILED" if worker.name in unavailable else STATE_AVAILABLE,
        ))
    return state, registry


def _prime_pool(fake, state_dir, monkeypatch, role="diff-review", **qualify):
    monkeypatch.setattr(mc, "opencode_state_dir", lambda: state_dir)
    catalog = mc.discover_catalog(runner=fake, which=lambda _b: "/x", binary="opencode", state_dir=state_dir)
    mc.select_pool_model(catalog, role, runner=fake, binary="opencode", state_dir=state_dir, allow_probe=True, **qualify)
    return catalog


def test_preferred_reviewer_unavailable_falls_back_to_qualified_free_before_premium(fake, state_dir, monkeypatch):
    _prime_pool(fake, state_dir, monkeypatch)
    task = Task(id="r1", task_ref="ENG-AO-03", role="diff-review", worker="codex-review", kind=KIND_READ,
                avoid_provider="OpenAI")
    state, registry = _dispatch_state()
    eligible, scores, blocked = _candidate_scores(state=state, registry=registry, task=task)
    # Configured Gemini review is still preferred while it is healthy.
    assert eligible[0] == "opencode2-gemini-flash-lite-review"
    assert eligible.index("opencode-free-review") < eligible.index("grok-build-review")
    # Preferred reviewer unavailable -> another qualified free OpenCode reviewer, ahead of subscription/metered ones.
    state, registry = _dispatch_state(unavailable={"opencode2-gemini-flash-lite-review", "antigravity-diff-review"})
    eligible, _, blocked = _candidate_scores(state=state, registry=registry, task=task)
    assert eligible[0] == "opencode-free-review"
    assert "codex-review" not in eligible  # same-provider implementer stays excluded


def test_dispatch_excludes_pool_without_a_diverse_qualified_model_or_catalog(fake, state_dir, monkeypatch):
    task = Task(id="r2", task_ref="ENG-AO-03", role="diff-review", worker="grok-build-review", kind=KIND_READ,
                avoid_provider="xAI")
    state, registry = _dispatch_state()
    # No catalog at all: OpenCode absent/never discovered -> the pool is not eligible, nothing else changes.
    eligible, _, blocked = _candidate_scores(state=state, registry=registry, task=task)
    assert "opencode-free-review" not in eligible
    assert any(b.startswith("opencode-free-review:") and "no eligible free OpenCode model" in b for b in blocked)
    # Only a same-vendor free model exists -> excluded for an xAI implementer, allowed for an OpenAI one.
    fake.entries = [entry("opencode", "grok-fast-free", "Grok Fast Free", family="grok")]
    _prime_pool(fake, state_dir, monkeypatch)
    eligible, _, blocked = _candidate_scores(state=state, registry=registry, task=task)
    assert "opencode-free-review" not in eligible
    other = Task(id="r3", task_ref="ENG-AO-03", role="diff-review", worker="codex-review", kind=KIND_READ,
                 avoid_provider="OpenAI")
    assert "opencode-free-review" in _candidate_scores(state=state, registry=registry, task=other)[0]


def test_unqualified_only_pool_and_stale_catalog_are_ineligible(fake, state_dir, monkeypatch):
    fake.run_output = {f"opencode/{e['id']}": "garbage" for e in fake.entries}
    monkeypatch.setattr(mc, "opencode_state_dir", lambda: state_dir)
    catalog = mc.discover_catalog(runner=fake, which=lambda _b: "/x", binary="opencode", state_dir=state_dir)
    for model in catalog.models:
        if model.cost_class == mc.COST_FREE_OPENCODE and model.capable:
            mc.qualify_model(model, "review", opencode_version="9.9.9", runner=fake, binary="opencode", state_dir=state_dir)
    assert "no qualified free" in mc.pool_block_reason("diff-review", state_dir=state_dir)
    assert "stale" in mc.pool_block_reason("diff-review", state_dir=state_dir,
                                          now=dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=3))


# --------------------------------------------------------------------------- OpenCode + xAI, native Grok


def test_opencode_xai_is_discovered_generically_but_never_auto_routed(fake, state_dir):
    catalog = discover(fake, state_dir)
    grok = catalog.get("xai/grok-4.6")
    assert grok is not None and grok.provider_id == "xai" and grok.cost_class == mc.COST_SUBSCRIPTION
    view = mc.inventory(state_dir=state_dir, now=NOW, catalog=catalog)
    row = next(r for r in view["models"] if r["id"] == "xai/grok-4.6")
    assert row["states"]["review"] == mc.STATE_SUBSCRIPTION
    sel = select(catalog, fake, state_dir, allow_probe=True)
    assert all(c["decision"] != "selected" or not c["model"].startswith("xai/") for c in sel.evidence["candidates"])
    assert fake.runs("xai/grok-4.6") == []


def test_native_grok_routes_are_unchanged():
    registry = load_registry()
    build, review = registry.get("grok-build"), registry.get("grok-build-review")
    assert (build.cli_bin, build.default_model, build.provider, build.is_write_capable) == ("grok", "grok-4.6", "xAI", True)
    assert (review.cli_bin, review.default_model, review.provider, review.is_read_only) == ("grok", "grok-4.6", "xAI", True)
    assert build.model_pool == "" and review.model_pool == ""
    assert registry.route("primary-implementation") == ("claude-code", "codex-build", "grok-build")
    assert registry.route("bot-implementation") == ("grok-build-bots",)
    assert "--no-subagents" in build.cli_template and "--no-subagents" in review.cli_template


# --------------------------------------------------------------------------- end to end through orchestrate


def _git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "managed"
    root.mkdir()
    _git(root, "init", "-q", "-b", "work")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "src").mkdir()
    (root / "src/app.py").write_text("x = 1\n")
    (root / ".gitignore").write_text(".agent-output/\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed")
    (root / "src/app.py").write_text("x = 2\n")
    _git(root, "add", "-A")
    return root


FAKE_CLI = """#!{python}
import json, sys
from pathlib import Path
here = Path(__file__).resolve().parent
cfg = json.loads((here / "config.json").read_text())
args = sys.argv[1:]
with (here / "calls.log").open("a") as log:
    log.write(json.dumps(args[:5]) + "\\n")
if args[:1] == ["--version"]:
    print(cfg["version"])
elif args[:2] == ["models", "--verbose"]:
    print(cfg["models"])
elif args[:2] == ["providers", "list"]:
    print(cfg["providers"])
elif args[:2] == ["debug", "agent"]:
    print(json.dumps({{"mode": "primary", "permission": [
        {{"permission": p, "pattern": "*", "action": "deny"}} for p in ("edit", "task", "bash")]}}))
elif args[:1] == ["run"]:
    model = args[args.index("--model") + 1]
    agent = args[args.index("--agent") + 1]
    behaviour = cfg["run"].get(model, {{"stdout": "TESTER_OK" if agent == "tester" else "READY", "code": 0}})
    print(behaviour["stdout"])
    sys.exit(behaviour["code"])
"""


@pytest.fixture
def fake_cli(tmp_path, monkeypatch, state_dir):
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    script = bin_dir / "opencode"
    script.write_text(FAKE_CLI.format(python=sys.executable))
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    config = {"version": "9.9.9", "models": verbose(DEFAULT_ENTRIES), "providers": PROVIDERS_TEXT, "run": {}}

    class Cli:
        path = bin_dir

        def write(self):
            (bin_dir / "config.json").write_text(json.dumps(config))

        def set_run(self, model, stdout, code=0):
            config["run"][model] = {"stdout": stdout, "code": code}
            self.write()

        def runs(self):
            lines = [json.loads(line) for line in (bin_dir / "calls.log").read_text().splitlines()]
            return [line[line.index("--model") + 1] for line in lines if line[:1] == ["run"] and "--model" in line]

    cli = Cli()
    cli.config = config
    cli.write()
    monkeypatch.setenv("PATH", f"{bin_dir}:{__import__('os').environ['PATH']}")
    monkeypatch.setenv("OCTAREL_GRAPHIFY", "off")
    monkeypatch.setattr(mc, "OPENCODE_BIN", "opencode")
    monkeypatch.setattr(mc, "opencode_state_dir", lambda: state_dir)
    return cli


def run_review(project, worker="opencode-free-review", role="diff-review", **kw):
    args = dict(
        registry=load_registry(), root=project, task="ENG-AO-03", worker_name=worker, role=role, model=None,
        intensity=None, why="fallback review", prompt_args=["Review the staged diff."], scope_paths=["src/app.py"],
        dry_run=False, allow_write=False, allow_overflow=False, timeout=120, include_diff=(role == "diff-review"),
    )
    args.update(kw)
    return orchestrate.run_delegation(**args)


def test_end_to_end_free_fallback_review_records_full_evidence(project, fake_cli):
    result = run_review(project)
    assert result.record.result == "PASS" and result.exit_code == 0
    record = result.record
    assert record.actual_provider == "OpenCode Zen" and record.actual_model == "opencode/muse-spark-1.3-contributor-free"
    assert record.planned_model == record.actual_model and record.files_changed == []
    selection = record.policy_manifest["model_selection"]
    assert selection["selected"] == record.actual_model and selection["role"] == "diff-review"
    assert selection["catalog"]["opencode_version"] == "9.9.9" and selection["catalog"]["refreshed_at"]
    rejected = {c["model"]: c["reason"] for c in selection["candidates"] if c["decision"] == "rejected"}
    assert "metered" in rejected["opencode/paid-thing-free"] and "subscription" in rejected["openai/gpt-5.6-sol"]
    assert any("model pool opencode-free" in note for note in record.notes)
    # Persisted in the normal run manifest, not a parallel evidence system.
    assert result.manifest["policy_manifest"]["model_selection"]["selected"] == record.actual_model
    # The launched command is the read-only reviewer preset with the discovered model.
    assert record.requested_command[:7] == ["opencode", "run", "--model", record.actual_model, "--agent", "reviewer", record.requested_command[6]]
    # One qualification probe plus the real review; a second review reuses the cached qualification.
    assert fake_cli.runs().count(record.actual_model) == 2
    run_review(project)
    assert fake_cli.runs().count(record.actual_model) == 3


def test_end_to_end_provider_diversity_and_explicit_model_gate(project, fake_cli):
    fake_cli.config["models"] = verbose([
        entry("opencode", "grok-fast-free", "Grok Fast Free", family="grok", context=2_000_000),
        entry("opencode", "nemotron-3-ultra-free", "Nemotron 3 Ultra Free", family="nemotron"),
        entry("openai", "gpt-5.6-sol", "GPT 5.6 Sol", cost=(2, 6), url="")])
    fake_cli.write()
    diverse = run_review(project, avoid_provider="xAI")
    assert diverse.record.actual_model == "opencode/nemotron-3-ultra-free"
    # An explicit --model on a pool worker must still be a proven-free, qualified, diverse candidate.
    paid = run_review(project, model="openai/gpt-5.6-sol")
    assert paid.record.result == "BLOCKED" and "subscription" in json.dumps(paid.record.policy_manifest["model_selection"])
    assert "openai/gpt-5.6-sol" not in fake_cli.runs()
    same = run_review(project, model="opencode/grok-fast-free", avoid_provider="xAI")
    assert same.record.result == "BLOCKED" and "opencode/grok-fast-free" not in fake_cli.runs()


def test_end_to_end_quota_failure_cools_down_and_next_run_uses_another_free_model(project, fake_cli):
    first_model = "opencode/muse-spark-1.3-contributor-free"
    first = run_review(project)
    assert first.record.actual_model == first_model
    fake_cli.set_run(first_model, "Error: usage limit reached, quota exhausted", code=1)
    failed = run_review(project)
    assert failed.record.result == "FAIL" and failed.record.actual_model == first_model
    assert any("cooled down after a quota failure" in note and "No stronger" in note for note in failed.record.notes)
    nxt = run_review(project)
    assert nxt.record.result == "PASS"
    assert nxt.record.actual_model != first_model
    cls = mc.load_catalog().get(nxt.record.actual_model).cost_class
    assert cls == mc.COST_FREE_OPENCODE  # rerouted to an equivalent free model, never a premium one


def test_end_to_end_no_eligible_model_blocks_without_escalation(project, fake_cli):
    fake_cli.config["models"] = verbose([entry("openai", "gpt-5.6-sol", cost=(2, 6), url=""),
                                         entry("xai", "grok-4.6", cost=(2, 6), url="")])
    fake_cli.write()
    result = run_review(project)
    assert result.record.result == "BLOCKED" and result.exit_code == 3
    assert "no paid/API fallback" in " ".join(result.record.notes)
    assert fake_cli.runs() == []


def test_end_to_end_absent_opencode_blocks_cleanly(project, tmp_path, monkeypatch, state_dir):
    only_git = tmp_path / "only-git"
    only_git.mkdir()
    (only_git / "git").symlink_to(shutil.which("git"))
    monkeypatch.setenv("PATH", str(only_git))
    monkeypatch.setattr(mc, "OPENCODE_BIN", "opencode")
    monkeypatch.setenv("OCTAREL_GRAPHIFY", "off")
    result = run_review(project)
    assert result.record.result == "BLOCKED"
    assert result.record.policy_manifest["model_selection"]["catalog"]["status"] == mc.CATALOG_ABSENT


def test_end_to_end_tests_role_and_readonly_enforcement(project, fake_cli):
    tests = run_review(project, worker="opencode-free-tests", role="focused-tests", scope_paths=["src"])
    assert tests.record.result == "PASS", tests.record.notes
    assert tests.record.requested_command[5] == "tester"
    assert tests.record.policy_manifest["model_selection"]["profile"] == "tests"
    with pytest.raises(orchestrate.ValidationError, match="cannot upgrade read-only"):
        run_review(project, allow_write=True)
    dry = run_review(project, dry_run=True)
    assert dry.record.result in {"DRY_RUN"} and dry.record.actual_model == ""


def test_supervisor_passes_the_diversity_constraint_to_the_worker(tmp_path):
    supervisor = Supervisor.__new__(Supervisor)
    supervisor.repo_root = tmp_path
    task = Task(id="t1", task_ref="ENG-AO-03", role="diff-review", worker="opencode-free-review",
                command=["scope:src/app.py"], avoid_provider="xAI")
    argv = supervisor._build_argv(task, dry_run=False)
    assert argv[argv.index("--avoid-provider") + 1] == "xAI"
    task.avoid_provider = None
    assert "--avoid-provider" not in supervisor._build_argv(task, dry_run=False)


# --------------------------------------------------------------------------- dashboard / API


@pytest.fixture
def client(tmp_path):
    registry = load_registry()
    state = State(":memory:")
    for provider in seed_provider_states(registry):
        state.upsert_provider_state(provider)
    ctx = CommandContext(state=state, registry=registry, scheduler=Scheduler(), supervisor=None, repo_root=tmp_path)
    return TestClient(create_app(ctx))


def test_dashboard_reports_inventory_states_without_spawning_anything(client, fake, state_dir, monkeypatch):
    monkeypatch.setattr(mc, "opencode_state_dir", lambda: state_dir)
    empty = client.get("/api/opencode-models").json()
    assert empty["catalog"] is None and empty["models"] == []
    assert {w["worker"] for w in empty["configured_workers"]} >= {
        "opencode2-gemini-flash-lite", "opencode-free-review", "opencode-free-tests"}
    catalog = mc.discover_catalog(runner=fake, which=lambda _b: "/x", binary="opencode", state_dir=state_dir)
    mc.select_pool_model(catalog, "diff-review", runner=fake, binary="opencode", state_dir=state_dir, allow_probe=True)
    fake.run_output["opencode/nemotron-3-ultra-free"] = "junk"
    mc.qualify_model(catalog.get("opencode/nemotron-3-ultra-free"), "review", opencode_version="9.9.9", runner=fake,
                     binary="opencode", state_dir=state_dir)

    def boom(*_a, **_k):
        raise AssertionError("the dashboard must never spawn a subprocess")

    monkeypatch.setattr("subprocess.Popen", boom)
    monkeypatch.setattr("subprocess.run", boom)
    body = client.get("/api/opencode-models").json()
    rows = {r["id"]: r for r in body["models"]}
    assert body["catalog"]["opencode_version"] == "9.9.9" and body["catalog"]["status"] == "ok"
    assert rows["opencode/muse-spark-1.3-contributor-free"]["states"]["review"] == mc.STATE_QUALIFIED_FREE
    assert rows["opencode/nemotron-3-ultra-free"]["states"]["review"] == mc.STATE_UNQUALIFIED
    assert rows["opencode/big-pickle"]["states"]["review"] == mc.STATE_UNTESTED_FREE
    assert rows["xai/grok-4.6"]["states"]["review"] == mc.STATE_SUBSCRIPTION
    assert rows["google/gemini-3.5-flash-lite"]["states"]["review"] == mc.STATE_METERED
    assert rows["mystery/model-x"]["states"]["review"] == mc.STATE_UNKNOWN_COST
    assert rows["opencode/tiny-free"]["states"]["review"] == mc.STATE_INCAPABLE
    configured = {w["worker"]: w for w in body["configured_workers"]}
    assert configured["opencode2-gemini-flash-lite"]["in_catalog"] is True
    assert configured["opencode2-gemini-flash-lite"]["role_kind"] == "configured-worker"
    assert configured["opencode-free-review"]["role_kind"] == "dynamic-pool-worker"
    models = {m["worker"]: m for m in client.get("/api/models").json()}
    assert models["opencode-free-review"]["model_pool"] == "opencode-free"
    assert models["opencode2-gemini-flash-lite"]["model_pool"] is None
    provider = next(p for p in client.get("/api/providers").json() if p["name"] == "opencode-free-review")
    assert provider["route_type"] == "Free" and provider["capability"] == "read-only"
