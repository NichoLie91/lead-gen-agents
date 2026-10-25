"""Composio Maps failover: a 403-ing provider must not empty the Pipeline tab.

On 2026-10-24 all 750 Google Maps calls returned 403 and the run reported an
empty market while other Maps actions in the same Composio catalog were never
tried. These tests pin the provider chain, the winner lock, and the diagnostics
that name every provider's error.
"""
from __future__ import annotations

import asyncio

import pytest

from src.agents.composio_agent import SLUG_ALIASES, ComposioAgent
from src.agents.maps_agent import DiscoveryUnavailable, MapsAgent
from src.core.config import Settings

PLACES = "GOOGLE_MAPS_TEXT_SEARCH"
SERPAPI = "SERPAPI_GOOGLE_MAPS_SEARCH"
ZENSERP = "ZENSERP_ZENSERP_GOOGLE_MAPS_SEARCH"

_LEAD = {
    "displayName": {"text": "Blue Creek Plumbing"},
    "formattedAddress": "12 Main St, Houston, TX",
    "nationalPhoneNumber": "(713) 555-0100",
    "websiteUri": "https://bluecreek.test",
    "rating": 4.8,
    "userRatingCount": 84,
    "businessStatus": "OPERATIONAL",
}


def _agent(slugs: list[str]) -> ComposioAgent:
    agent = ComposioAgent(Settings(dry_run=False))
    agent._candidate_slugs = {"maps_search": list(slugs)}
    return agent


def _calls(agent: ComposioAgent, responses: dict[str, list[dict]]) -> list[str]:
    """Replace execute_action; each slug answers with its queued response."""
    seen: list[str] = []

    async def fake(action: str, params: dict) -> dict:
        seen.append(action)
        queued = responses[action].pop(0) if responses.get(action) else {"ok": False}
        return queued

    agent.execute_action = fake  # type: ignore[method-assign]
    return seen


# ---------- the chain ----------

def test_a_403_provider_falls_through_to_the_next_one():
    agent = _agent([PLACES, SERPAPI])
    seen = _calls(agent, {
        PLACES: [{"ok": False, "error": "HTTP 403: PERMISSION_DENIED"}],
        SERPAPI: [{"ok": True, "data": {"local_results": [_LEAD]}}],
    })

    items = asyncio.run(agent.search_google_maps("plumber in Houston"))

    assert [i["name"] for i in items] == ["Blue Creek Plumbing"]
    assert seen == [PLACES, SERPAPI]
    assert agent.maps_provider() == SERPAPI
    assert PLACES in agent.maps_diagnostics() or "403" not in agent.maps_diagnostics()


def test_the_winner_is_locked_in_for_the_rest_of_the_run():
    agent = _agent([PLACES, SERPAPI])
    seen = _calls(agent, {
        PLACES: [{"ok": False, "error": "HTTP 403"}],
        SERPAPI: [{"ok": True, "data": {"local_results": [_LEAD]}}] * 5,
    })

    asyncio.run(agent.search_google_maps("q1"))
    asyncio.run(agent.search_google_maps("q2"))
    asyncio.run(agent.search_google_maps("q3"))

    # Dead providers are dropped after their first failure, so 800 queries do
    # not each pay for a 403 round trip.
    assert seen == [PLACES] + [SERPAPI] * 3


def test_an_ok_but_empty_answer_moves_on_until_a_provider_proves_itself():
    agent = _agent([PLACES, SERPAPI])
    seen = _calls(agent, {
        PLACES: [{"ok": True, "data": {"places": []}}],
        SERPAPI: [{"ok": True, "data": {"local_results": [_LEAD]}}],
    })
    items = asyncio.run(agent.search_google_maps("plumber in Houston"))
    assert items and seen == [PLACES, SERPAPI]


def test_the_proven_winner_answering_empty_is_final():
    """No spending the other providers' quota once one has answered this run."""
    agent = _agent([PLACES, SERPAPI])
    seen = _calls(agent, {
        SERPAPI: [{"ok": True, "data": {"local_results": [_LEAD]}},
                  {"ok": True, "data": {"local_results": []}}],
    })
    asyncio.run(agent.search_google_maps("q1"))   # proves SERPAPI, kills PLACES
    assert asyncio.run(agent.search_google_maps("nothing here")) == []
    # The second call went straight to the proven winner, not back to PLACES.
    assert seen == [PLACES, SERPAPI, SERPAPI]


def test_every_provider_dead_returns_empty_and_stops_probing():
    agent = _agent([PLACES, SERPAPI, ZENSERP])
    seen = _calls(agent, {
        PLACES: [{"ok": False, "error": "HTTP 403: PERMISSION_DENIED"}],
        SERPAPI: [{"ok": False, "error": "HTTP 401: invalid api key"}],
        ZENSERP: [{"ok": False, "error": "HTTP 402: quota exceeded"}],
    })

    assert asyncio.run(agent.search_google_maps("q")) == []
    assert agent.has_maps_provider() is False
    assert asyncio.run(agent.search_google_maps("q2")) == []
    assert seen == [PLACES, SERPAPI, ZENSERP], "dead providers are not retried"

    report = agent.maps_diagnostics()
    assert PLACES in report and "403" in report
    assert SERPAPI in report and "401" in report
    assert ZENSERP in report and "402" in report


def test_a_winner_that_later_fails_falls_through_again():
    agent = _agent([PLACES, SERPAPI])
    seen = _calls(agent, {
        PLACES: [{"ok": True, "data": {"places": [_LEAD]}},
                 {"ok": False, "error": "HTTP 403: quota"}],
        SERPAPI: [{"ok": True, "data": {"local_results": [_LEAD]}}],
    })
    asyncio.run(agent.search_google_maps("q1"))
    assert agent.maps_provider() == PLACES
    assert asyncio.run(agent.search_google_maps("q2"))
    assert agent.maps_provider() == SERPAPI
    assert seen == [PLACES, PLACES, SERPAPI]


# ---------- request shapes ----------

def test_places_gets_text_query_and_scrapers_get_q():
    assert "textQuery" in ComposioAgent._maps_params(PLACES, "plumber")
    assert ComposioAgent._maps_params(SERPAPI, "plumber") == {"q": "plumber"}


def test_places_v1_nesting_is_normalized():
    items = ComposioAgent._normalize_maps({"response_data": {"places": [_LEAD]}})
    assert items[0]["name"] == "Blue Creek Plumbing"
    assert items[0]["reviews"] == 84
    assert items[0]["state"] == "TX"


def test_the_static_chain_starts_with_the_official_places_api():
    assert SLUG_ALIASES["maps_search"][0] == PLACES
    assert SERPAPI in SLUG_ALIASES["maps_search"]


# ---------- discovery level ----------

class _Provider:
    """Maps provider stub with the failover surface MapsAgent now uses."""

    def __init__(self, results_for: int, diagnostics: str = ""):
        self.connected = True
        self.calls = 0
        self._results_for = results_for
        self._alive = True
        self._diagnostics = diagnostics or "GOOGLE_MAPS_TEXT_SEARCH: HTTP 403"

    async def search_google_maps(self, query, start=0):
        self.calls += 1
        if self.calls > self._results_for:
            self._alive = False
        return [] if self.calls > self._results_for else [dict(_LEAD, name=f"Shop {self.calls}")]

    def has_maps_provider(self) -> bool:
        return self._alive

    def maps_provider(self) -> str:
        return "SERPAPI_GOOGLE_MAPS_SEARCH" if self._results_for else ""

    def maps_diagnostics(self) -> str:
        return self._diagnostics


def test_discovery_stops_instead_of_burning_every_query():
    provider = _Provider(results_for=0)
    agent = MapsAgent(provider, Settings(dry_run=False))
    with pytest.raises(DiscoveryUnavailable):
        asyncio.run(agent.discover(verticals=["plumber"], metros=["Houston", "Dallas"]))
    assert provider.calls == 1, "one dead query is enough to conclude the provider is gone"


def test_discovery_reports_which_provider_answered():
    provider = _Provider(results_for=2)
    agent = MapsAgent(provider, Settings(dry_run=False))
    pool = asyncio.run(agent.discover(verticals=["plumber"], metros=["Houston"]))
    assert len(pool) == 1
    assert agent.last_provider == "SERPAPI_GOOGLE_MAPS_SEARCH"
    assert pool[0]["city"] == "Houston"


def test_failure_message_names_the_provider_errors():
    provider = _Provider(results_for=0, diagnostics="SERPAPI_GOOGLE_MAPS_SEARCH: HTTP 401")
    agent = MapsAgent(provider, Settings(dry_run=False))
    with pytest.raises(DiscoveryUnavailable) as exc:
        asyncio.run(agent.discover(verticals=["plumber"], metros=["Houston"]))
    assert "SERPAPI_GOOGLE_MAPS_SEARCH" in str(exc.value)
    assert "401" in str(exc.value)


def test_old_stub_providers_without_the_failover_surface_still_work():
    """MapsAgent must not require has_maps_provider/maps_diagnostics."""
    class Legacy:
        connected = True

        async def search_google_maps(self, query, start=0):
            return [dict(_LEAD, name="Legacy Shop")]

    agent = MapsAgent(Legacy(), Settings(dry_run=False))
    pool = asyncio.run(agent.discover(verticals=["plumber"], metros=["Houston"]))
    assert len(pool) == 1
    assert agent.last_provider == ""
