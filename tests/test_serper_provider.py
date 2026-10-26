"""Serper places as a direct Maps provider (no Composio round trip).

2026-10-26: every Composio Maps action was unusable (Google Maps toolkit
disabled by the admin, serpapi/zenserp unconnected). Serper's places endpoint
needs no Composio connection and returns the rating/review fields Atlas gates
on, so it goes to the front of the chain when SERPER_API_KEY is set.
"""
from __future__ import annotations

import asyncio

import pytest

from src.agents import composio_agent as ca
from src.agents.composio_agent import SERPER_PLACES, ComposioAgent
from src.core.config import Settings

PLACES = "GOOGLE_MAPS_TEXT_SEARCH"

_SERPER_PLACE = {
    "title": "Blue Creek Plumbing",
    "address": "12 Main St, Houston, TX 77002",
    "phoneNumber": "(713) 555-0100",
    "website": "https://bluecreek.test",
    "rating": 4.8,
    "ratingCount": 84,
    "type": "Plumber",
    "openState": "Open",
    "cid": "1234567890",
}


class _Resp:
    def __init__(self, status: int, payload: dict):
        self.status_code = status
        self._payload = payload
        self.text = str(payload)

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    """Records each POST and answers from a queue of (status, payload)."""

    calls: list[dict] = []
    queue: list[tuple[int, dict]] = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, headers=None):
        _FakeClient.calls.append({"url": url, "json": json, "headers": headers})
        status, payload = (_FakeClient.queue.pop(0)
                           if _FakeClient.queue else (200, {"places": []}))
        return _Resp(status, payload)


@pytest.fixture
def fake_http(monkeypatch):
    _FakeClient.calls = []
    _FakeClient.queue = []
    monkeypatch.setattr(ca.httpx, "AsyncClient", _FakeClient)
    # The retry path sleeps 2s; tests must not.
    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(ca.asyncio, "sleep", no_sleep)
    return _FakeClient


def _agent(serper_key: str = "test-key") -> ComposioAgent:
    return ComposioAgent(Settings(dry_run=False, serper_api_key=serper_key))


# ---------- the shape of a Serper places response ----------

def test_serper_fields_are_normalized():
    items = ComposioAgent._normalize_maps({"places": [_SERPER_PLACE]})

    assert items[0]["name"] == "Blue Creek Plumbing"
    assert items[0]["phone"] == "(713) 555-0100"
    assert items[0]["website"] == "https://bluecreek.test"
    assert items[0]["rating"] == 4.8
    assert items[0]["reviews"] == 84
    assert items[0]["state"] == "TX"
    assert items[0]["open_state"] == "Open"


def test_normalized_serper_lead_passes_the_target_gate():
    from src.agents.atlas import passes_target_gate

    lead = ComposioAgent._normalize_maps({"places": [_SERPER_PLACE]})[0]
    assert passes_target_gate(lead) is True


# ---------- chain position ----------

def test_a_configured_key_puts_serper_first():
    assert _agent()._maps_attempt_order()[0] == SERPER_PLACES


def test_without_a_key_the_chain_is_unchanged():
    assert SERPER_PLACES not in _agent(serper_key="")._maps_attempt_order()
    assert _agent(serper_key="")._maps_attempt_order()[0] == PLACES


def test_serper_answering_never_touches_composio(fake_http):
    _FakeClient.queue = [(200, {"places": [_SERPER_PLACE]})]
    agent = _agent()

    async def boom(action, params):
        raise AssertionError(f"Composio should not be called, got {action}")

    agent.execute_action = boom  # type: ignore[method-assign]
    items = asyncio.run(agent.search_google_maps("plumber in Houston"))

    assert [i["name"] for i in items] == ["Blue Creek Plumbing"]
    assert agent.maps_provider() == SERPER_PLACES
    assert len(_FakeClient.calls) == 1
    call = _FakeClient.calls[0]
    assert call["url"] == ca.SERPER_URL
    assert call["headers"]["X-API-KEY"] == "test-key"
    assert call["json"]["q"] == "plumber in Houston"
    assert call["json"]["gl"] == "us"


# ---------- failover ----------

def test_serper_failure_falls_through_and_is_reported(fake_http):
    _FakeClient.queue = [(403, {"message": "Not enough credits"})]
    agent = _agent()
    seen: list[str] = []

    async def composio(action, params):
        seen.append(action)
        return {"ok": True, "data": {"places": [{"displayName": {"text": "Shops"},
                                                 "formattedAddress": "1 Main St, TX",
                                                 "nationalPhoneNumber": "555",
                                                 "rating": 4.5,
                                                 "userRatingCount": 20}]}}

    agent.execute_action = composio  # type: ignore[method-assign]
    items = asyncio.run(agent.search_google_maps("plumber in Houston"))

    assert items and seen == [PLACES]
    assert agent.maps_provider() == PLACES
    report = agent.maps_diagnostics()
    assert SERPER_PLACES not in report  # the winner is reporting, not failures


def test_a_dead_serper_is_not_retried_on_every_query(fake_http):
    _FakeClient.queue = [(401, {"message": "Unauthorized"})]
    agent = _agent()

    async def composio(action, params):
        return {"ok": True, "data": {"places": [
            {"displayName": {"text": "Shops"}, "formattedAddress": "1 Main St, TX",
             "nationalPhoneNumber": "555", "rating": 4.5, "userRatingCount": 20}]}}

    agent.execute_action = composio  # type: ignore[method-assign]
    asyncio.run(agent.search_google_maps("q1"))
    asyncio.run(agent.search_google_maps("q2"))

    assert len(_FakeClient.calls) == 1, "the dead provider is dropped after one failure"
    assert agent.maps_provider() == PLACES


def test_all_providers_dead_names_serpers_error(fake_http):
    _FakeClient.queue = [(403, {"message": "Not enough credits"})]
    agent = _agent()

    async def dead(action, params):
        return {"ok": False, "action": action, "error": "HTTP 403: disabled"}

    agent.execute_action = dead  # type: ignore[method-assign]

    assert asyncio.run(agent.search_google_maps("q")) == []
    assert agent.has_maps_provider() is False
    report = agent.maps_diagnostics()
    assert SERPER_PLACES in report and "Not enough credits" in report


def test_a_throttle_is_retried_once(fake_http):
    _FakeClient.queue = [(429, {"message": "rate limited"}),
                         (200, {"places": [_SERPER_PLACE]})]
    items = asyncio.run(_agent().search_google_maps("q"))

    assert len(items) == 1
    assert len(_FakeClient.calls) == 2


def test_ok_but_empty_serper_moves_on(fake_http):
    """An empty places block is not proof the provider works."""
    _FakeClient.queue = [(200, {"places": []})]
    agent = _agent()
    seen: list[str] = []

    async def composio(action, params):
        seen.append(action)
        return {"ok": True, "data": {"places": [
            {"displayName": {"text": "Shops"}, "formattedAddress": "1 Main St, TX",
             "nationalPhoneNumber": "555", "rating": 4.5, "userRatingCount": 20}]}}

    agent.execute_action = composio  # type: ignore[method-assign]
    items = asyncio.run(agent.search_google_maps("q"))

    assert items and seen == [PLACES]
    assert agent.maps_provider() == PLACES
