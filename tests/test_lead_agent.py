"""Lead Agent tests: the single Gemini brain owns every reply and delegates
to the six-agent team; per-agent Gemini brains fall back safely offline."""
from __future__ import annotations

import asyncio

from src.agents.lead_agent import COMMAND_OWNERS, TEAM, LeadAgent, team_roster
from src.agents.maps_agent import MapsAgent
from src.agents.scout import Scout
from src.core.config import Settings
from src.core.state import StateStore
from src.enrichment import _llm_extract, enrich_leads


class FakeLLM:
    def __init__(self, raw: str):
        self.raw = raw
        self.available = True
        self.last_prompt = ""

    async def complete(self, prompt: str) -> str:
        self.last_prompt = prompt
        return self.raw


class FakeGithub:
    def __init__(self):
        self.calls: list[str] = []
        self.stopped = 0

    async def trigger_pipeline(self, mode: str) -> str:
        self.calls.append(mode)
        return f"triggered pipeline mode={mode} (fake)"

    async def pipeline_in_progress(self) -> bool:
        return False

    def set_stop(self) -> None:
        self.stopped += 1


def make_harness(tmp_path) -> tuple[Settings, StateStore]:
    settings = Settings(dry_run=True, repo_root=tmp_path)
    settings.telegram_bot_token = "test"
    state = StateStore(tmp_path / "state")
    return settings, state


def test_team_is_the_six_agents():
    names = {m["name"] for m in TEAM}
    assert names == {"Atlas", "Scout", "Enrichment", "Outreach", "Followups", "Inbound"}


def test_roster_is_pii_safe():
    roster = team_roster()
    assert "Atlas" in roster and "Inbound" in roster
    for leaked in ("email@", "@gmail", "TELEGRAM_BOT_TOKEN", "GEMINI_API_KEY"):
        assert leaked not in roster


def test_delegation_map_covers_all_six_agents():
    owned = set().union(*COMMAND_OWNERS.values())
    assert owned == {m["name"] for m in TEAM}


def test_delegate_run_mode(tmp_path):
    settings, state = make_harness(tmp_path)
    github = FakeGithub()
    lead = LeadAgent(settings, state, github=github)
    reply = asyncio.run(lead.delegate("/run", "discovery", 0))
    assert github.calls == ["discovery"]
    assert "mode=discovery" in reply
    # Unknown mode safely falls back to full.
    reply = asyncio.run(lead.delegate("/run", "evil", 0))
    assert github.calls == ["discovery", "full"]


def test_delegate_status_and_id(tmp_path):
    settings, state = make_harness(tmp_path)
    lead = LeadAgent(settings, state, github=FakeGithub())
    assert "No runs yet" in asyncio.run(lead.delegate("/status", "", 0))
    assert "42" in asyncio.run(lead.delegate("/id", "", 42))


def test_delegate_approve_all(tmp_path):
    settings, state = make_harness(tmp_path)
    lead = LeadAgent(settings, state, github=FakeGithub())
    lead.approvals.register("a" * 64)
    lead.approvals.register("b" * 64)
    reply = asyncio.run(lead.delegate("/approve", "all", 0))
    assert "Approved 2 draft" in reply
    assert len(lead.approvals.pending()) == 0


def test_delegate_stop(tmp_path):
    settings, state = make_harness(tmp_path)
    github = FakeGithub()
    lead = LeadAgent(settings, state, github=github)
    reply = asyncio.run(lead.delegate("/stop", "", 0))
    assert github.stopped == 1
    assert "Stop flag set" in reply


def test_intent_context_is_pii_safe_and_mentions_team(tmp_path):
    settings, state = make_harness(tmp_path)
    lead = LeadAgent(settings, state, github=FakeGithub())
    ctx = lead._intent_context()
    assert "Atlas" in ctx and "Scout" in ctx and "Inbound" in ctx
    for leaked in ("@gmail", "TELEGRAM_BOT_TOKEN", "GEMINI_API_KEY"):
        assert leaked not in ctx


def test_handle_message_exact_command_fast_path(tmp_path):
    settings, state = make_harness(tmp_path)
    github = FakeGithub()
    lead = LeadAgent(settings, state, github=github)
    asyncio.run(lead.handle_message("/run discovery", 0))
    assert github.calls == ["discovery"]


def test_handle_message_free_text_to_command(tmp_path):
    settings, state = make_harness(tmp_path)
    github = FakeGithub()
    llm = FakeLLM('{"action": "command", "command": "/followups", "args": ""}')
    lead = LeadAgent(settings, state, github=github, llm=llm)
    reply = asyncio.run(lead.handle_message("send the follow-ups", 0))
    assert github.calls == ["followups"]
    assert "mode=followups" in reply


def test_handle_message_free_text_reply(tmp_path):
    settings, state = make_harness(tmp_path)
    llm = FakeLLM('{"action": "reply", "text": "Six agents, one lead agent."}')
    lead = LeadAgent(settings, state, github=FakeGithub(), llm=llm)
    reply = asyncio.run(lead.handle_message("how does the team work?", 0))
    assert reply == "Six agents, one lead agent."


def test_handle_message_without_gemini_key(tmp_path):
    settings, state = make_harness(tmp_path)
    settings.gemini_api_key = ""
    lead = LeadAgent(settings, state, github=FakeGithub())
    reply = asyncio.run(lead.handle_message("run the pipeline", 0))
    assert "no gemini key" in reply.lower()


def test_handle_message_gemini_busy(tmp_path):
    settings, state = make_harness(tmp_path)
    lead = LeadAgent(settings, state, github=FakeGithub(), llm=FakeLLM(""))
    reply = asyncio.run(lead.handle_message("run it", 0))
    assert "busy" in reply.lower()
    assert "/command" in reply


def test_handle_message_unparseable_falls_back(tmp_path):
    settings, state = make_harness(tmp_path)
    lead = LeadAgent(settings, state, github=FakeGithub(),
                     llm=FakeLLM("I have no idea"))
    reply = asyncio.run(lead.handle_message("zzz", 0))
    assert "/help" in reply


# ---------- per-agent Gemini brains ----------

def test_maps_llm_query_shapes_parses(tmp_path):
    settings, _ = make_harness(tmp_path)
    llm = FakeLLM('["{vertical} near {city} missing calls", "{vertical} {city} no booking"]')
    maps = MapsAgent(None, settings, llm=llm)
    shapes = asyncio.run(maps._llm_query_shapes())
    assert shapes == ["{vertical} near {city} missing calls",
                      "{vertical} {city} no booking"]


def test_maps_llm_query_shapes_offline_and_garbage(tmp_path):
    settings, _ = make_harness(tmp_path)
    # no llm -> offline
    maps = MapsAgent(None, settings)
    assert asyncio.run(maps._llm_query_shapes()) == []
    # garbage / non-conforming shapes -> filtered
    llm = FakeLLM('["{city} only", "no placeholders", "ok {vertical} {city}"]')
    maps = MapsAgent(None, settings, llm=llm)
    assert asyncio.run(maps._llm_query_shapes()) == ["ok {vertical} {city}"]


def test_scout_explain_with_llm(tmp_path):
    settings, _ = make_harness(tmp_path)
    scout = Scout(settings, llm=FakeLLM("Strong fit: 4.8 stars, phone + email."))
    lead = {"name": "Apex Plumbing", "vertical": "plumber", "rating": 4.8,
            "reviews": 120, "phone": "x", "email": "x@y.com"}
    score = {"icp": 25, "intent": 20, "budget": 15, "reachability": 10,
             "timing": 5, "penalties": 0, "total": 75, "tier": "WARM"}
    assert "Strong fit" in asyncio.run(scout.explain(lead, score))


def test_scout_explain_offline(tmp_path):
    settings, _ = make_harness(tmp_path)
    scout = Scout(settings)  # no llm
    assert asyncio.run(scout.explain({}, {})) == ""


def test_enrichment_llm_extract_email(tmp_path):
    llm = FakeLLM('{"email": "owner@example.com"}')
    budget = {"left": 15}
    got = asyncio.run(_llm_extract(llm, "text without regex-able email", "email", budget))
    assert got == "owner@example.com"
    assert budget["left"] == 14  # budget decremented once per call


def test_enrichment_llm_extract_budget_exhausted(tmp_path):
    llm = FakeLLM('{"email": "owner@example.com"}')
    assert asyncio.run(_llm_extract(llm, "text", "email", {"left": 0})) is None
    assert asyncio.run(_llm_extract(llm, "", "email", {"left": 5})) is None


def test_enrichment_llm_extract_offline(tmp_path):
    assert asyncio.run(_llm_extract(None, "text", "email", {"left": 5})) is None


class FakeComposio:
    """Connected fake whose search finds nothing (forces the LLM fallback)."""

    def __init__(self):
        self.connected = True

    async def search_web(self, query: str) -> list[dict]:
        return [{"snippet": "no contact info visible here"}]

    async def fetch_url(self, url: str) -> str:
        return ""


def test_enrich_leads_uses_llm_fallback(tmp_path):
    settings, _ = make_harness(tmp_path)
    settings.dry_run = False  # exercise the real (connected) enrichment branch
    llm = FakeLLM('{"email": "info@bluecreek.example"}')
    leads = [{"name": "Blue Creek", "city": "Houston", "website": "https://x.example"}]
    out = asyncio.run(enrich_leads(leads, FakeComposio(), settings, llm=llm))
    assert out[0]["email"] == "info@bluecreek.example"
    assert out[0]["email_status"] == "VERIFIED"
