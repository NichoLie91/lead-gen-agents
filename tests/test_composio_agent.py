"""ComposioAgent retry logic tests (spec 7.x).

Covers:
- which failures are classified as retryable (_is_retryable)
- execute_action actually retrying an HTTP 429 with exponential backoff and
  recovering on the next attempt (the Google Sheets quota crash scenario)
- retries being exhausted after repeated 429s
- semantic errors NOT being retried (they would just burn quota)
"""
from __future__ import annotations

import asyncio
import json

import pytest

from src.agents import composio_agent
from src.agents.composio_agent import ComposioAgent
from src.core.config import Settings

# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------


class FakeResp:
    def __init__(self, status_code: int, body: dict | str):
        self.status_code = status_code
        self._body = body

    @property
    def text(self) -> str:
        return self._body if isinstance(self._body, str) else json.dumps(self._body)

    def json(self):
        return self._body if isinstance(self._body, dict) else json.loads(self._body)


class FakeAsyncClient:
    """Async context manager whose post() pops responses off a queue."""

    def __init__(self, responses: list[FakeResp]):
        self._responses = list(responses)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, *args, **kwargs):
        return self._responses.pop(0)

    @property
    def remaining(self) -> int:
        return len(self._responses)


def make_agent() -> ComposioAgent:
    """Agent with a fake API key; slugs/connections pre-seeded so the retry
    test exercises ONLY the execute path (no catalog/connection network calls)."""
    agent = ComposioAgent(Settings(composio_api_key="test-key"))
    agent._slugs_resolved = True
    agent._account_by_toolkit = {"googlesheets": "acct-1"}
    return agent


@pytest.fixture
def no_backoff_delay(monkeypatch):
    """Record backoff delays instead of sleeping (tests stay fast)."""
    sleeps: list[float] = []

    async def fake_sleep(sec: float):
        sleeps.append(sec)

    monkeypatch.setattr(composio_agent.asyncio, "sleep", fake_sleep)
    return sleeps


def fake_http(monkeypatch, responses: list[FakeResp]) -> FakeAsyncClient:
    client = FakeAsyncClient(responses)
    monkeypatch.setattr(composio_agent.httpx, "AsyncClient", lambda **kw: client)
    return client


# --------------------------------------------------------------------------
# classification
# --------------------------------------------------------------------------


def test_quota_exceeded_is_retryable():
    # The exact Google Sheets throttle the pipeline used to crash on.
    assert ComposioAgent._is_retryable(
        "Quota exceeded for Read requests per minute"
    ) is True


def test_rate_limit_and_server_errors_are_retryable():
    assert ComposioAgent._is_retryable("HTTP 429 Too Many Requests") is True
    assert ComposioAgent._is_retryable("RESOURCE_EXHAUSTED: quota") is True
    assert ComposioAgent._is_retryable("upstream request timeout") is True
    assert ComposioAgent._is_retryable("connection reset by peer") is True


def test_semantic_errors_are_not_retryable():
    # Retrying these would just burn quota — the fix must NOT loop on them.
    assert ComposioAgent._is_retryable("Sheet Pipeline not found") is False
    assert ComposioAgent._is_retryable("Invalid argument: bad spreadsheet_id") is False
    assert ComposioAgent._is_retryable("") is False


# --------------------------------------------------------------------------
# execute_action retry behaviour
# --------------------------------------------------------------------------


def test_execute_action_retries_429_then_succeeds(monkeypatch, no_backoff_delay):
    """HTTP 429 (quota exceeded) then success -> ok, with one backoff sleep."""
    sleeps = no_backoff_delay
    client = fake_http(monkeypatch, [
        FakeResp(429, "Quota exceeded for Read requests per minute"),
        FakeResp(200, {"successful": True, "data": {"ok": 1}}),
    ])

    result = asyncio.run(
        make_agent().execute_action("GOOGLESHEETS_BATCH_UPDATE", {"spreadsheet_id": "s1"})
    )

    assert result["ok"] is True
    assert result["data"] == {"ok": 1}
    assert client.remaining == 0  # both responses consumed
    # Exactly one backoff between the failed attempt and the successful one,
    # starting at the 2s base (plus jitter), never blocking the test.
    assert len(sleeps) == 1
    assert sleeps[0] >= 2.0


def test_execute_action_exhausts_retries_on_repeated_429(monkeypatch, no_backoff_delay):
    """All attempts 429 -> returns failure after RETRY_MAX_ATTEMPTS tries."""
    sleeps = no_backoff_delay
    from src.agents.composio_agent import RETRY_MAX_ATTEMPTS

    client = fake_http(monkeypatch, [
        FakeResp(429, "quota exceeded") for _ in range(RETRY_MAX_ATTEMPTS)
    ])

    result = asyncio.run(
        make_agent().execute_action("GOOGLESHEETS_BATCH_UPDATE", {"spreadsheet_id": "s1"})
    )

    assert result["ok"] is False
    assert "429" in result["error"]
    assert client.remaining == 0  # every attempt was consumed
    # 4 backoffs before the 5th (final) attempt returned the failure, growing
    # exponentially from the 2s base: 2, 4, 8, 16 (+jitter).
    assert len(sleeps) == RETRY_MAX_ATTEMPTS - 1
    assert sleeps[0] >= 2.0
    assert sleeps[1] >= sleeps[0]  # non-decreasing (exponential + jitter)


def test_execute_action_does_not_retry_semantic_error(monkeypatch, no_backoff_delay):
    """A 'successful: false' semantic error (no quota hint) is not retried."""
    sleeps = no_backoff_delay
    client = fake_http(monkeypatch, [
        FakeResp(200, {"successful": False, "error": "Sheet Pipeline not found"}),
    ])

    result = asyncio.run(
        make_agent().execute_action("GOOGLESHEETS_BATCH_UPDATE", {"spreadsheet_id": "s1"})
    )

    assert result["ok"] is False
    assert "Sheet Pipeline not found" in result["error"]
    assert sleeps == []          # no backoff: nothing was retried
    assert client.remaining == 0  # exactly one API call was made


# --------------------------------------------------------------------------
# search_web response normalization (Tavily response_data wrapper)
# --------------------------------------------------------------------------


def test_search_web_normalizes_tavily_response_data(monkeypatch):
    """Tavily via Composio v3 returns {"response_data": {"results": [...]}};
    search_web must flatten it into [{snippet, content, url, title}] items."""
    agent = make_agent()
    agent._slugs["web_search"] = "TAVILY_TAVILY_SEARCH"

    async def fake_execute(action, params):
        assert action == "TAVILY_TAVILY_SEARCH"
        assert params == {"query": "Plumbco Plumbing Houston"}
        return {"ok": True, "data": {"response_data": {"results": [
            {"title": "Plumbco", "url": "https://plumbco.example",
             "content": "Reach us at info@plumbco.example"},
        ]}}}

    monkeypatch.setattr(agent, "execute_action", fake_execute)
    results = asyncio.run(agent.search_web("Plumbco Plumbing Houston"))
    assert len(results) == 1
    assert results[0]["snippet"] == "Reach us at info@plumbco.example"
    assert results[0]["content"] == "Reach us at info@plumbco.example"
    assert results[0]["url"] == "https://plumbco.example"
    assert results[0]["title"] == "Plumbco"


def test_search_web_returns_empty_list_on_failure(monkeypatch):
    agent = make_agent()
    agent._slugs["web_search"] = "TAVILY_TAVILY_SEARCH"

    async def fake_execute(action, params):
        return {"ok": False, "error": "HTTP 404"}

    monkeypatch.setattr(agent, "execute_action", fake_execute)
    assert asyncio.run(agent.search_web("anything")) == []


# --------------------------------------------------------------------------
# fetch_url -> Tavily extract fallback (Composio fetch tool removed 2026-08)
# --------------------------------------------------------------------------


def test_fetch_url_falls_back_to_tavily_extract(monkeypatch):
    """No resolvable Composio fetch tool + TAVILY_API_KEY -> direct extract."""
    agent = ComposioAgent(Settings(composio_api_key="test-key", tavily_api_key="tvly-test"))
    agent._slugs_resolved = True  # fetch_url not in catalog -> skip dead tool
    client = fake_http(monkeypatch, [
        FakeResp(200, {"results": [
            {"url": "https://x.example",
             "raw_content": "Contact: mailto:info@x.example for quotes"},
        ]}),
    ])

    html = asyncio.run(agent.fetch_url("https://x.example"))
    assert "info@x.example" in html
    # Second fetch of the SAME url hits the per-run cache (one extract call).
    html2 = asyncio.run(agent.fetch_url("https://x.example"))
    assert html2 == html
    assert client.remaining == 0  # both calls served by the single queued response


def test_fetch_url_returns_empty_without_tavily_key(monkeypatch):
    agent = make_agent()  # no TAVILY_API_KEY
    agent._slugs_resolved = True
    assert asyncio.run(agent.fetch_url("https://x.example")) == ""


# --------------------------------------------------------------------------
# v3 Gmail payload shape (recipient_email, not userId/to)
# --------------------------------------------------------------------------


def test_gmail_send_email_uses_v3_recipient_email(monkeypatch):
    """Composio v3 GMAIL_SEND_EMAIL requires `recipient_email` — the old
    userId/to shape was rejected with 400 "fields are missing:
    {'recipient_email'}", which is why every send bounced."""
    agent = make_agent()
    agent._slugs["send_email"] = "GMAIL_SEND_EMAIL"
    captured: dict = {}

    async def fake_execute(action, params):
        captured["action"] = action
        captured["params"] = params
        return {"ok": True, "data": {"message_id": "m1"}}

    monkeypatch.setattr(agent, "execute_action", fake_execute)
    result = asyncio.run(agent.gmail_send_email(
        to="info@plumbco.example", subject="Hi", body="Hello"))
    assert result["ok"] is True
    assert captured["action"] == "GMAIL_SEND_EMAIL"
    assert captured["params"]["recipient_email"] == "info@plumbco.example"
    assert "to" not in captured["params"]       # v1 field must be gone
    assert "userId" not in captured["params"]   # v1 field must be gone


def test_gmail_create_draft_uses_v3_fields(monkeypatch):
    agent = make_agent()
    agent._slugs["create_draft"] = "GMAIL_CREATE_EMAIL_DRAFT"
    captured: dict = {}

    async def fake_execute(action, params):
        captured["params"] = params
        return {"ok": True, "data": {"id": "d1"}}

    monkeypatch.setattr(agent, "execute_action", fake_execute)
    asyncio.run(agent.gmail_create_draft(
        to="info@plumbco.example", subject="Hi", body="Hello"))
    assert captured["params"]["recipient_email"] == "info@plumbco.example"
    assert captured["params"]["subject"] == "Hi"
    assert "to" not in captured["params"]
    assert "userId" not in captured["params"]


# --------------------------------------------------------------------------
# IG DM requires a numeric PSID (handle cannot send)
# --------------------------------------------------------------------------


def test_ig_send_dm_rejects_handle_recipient(monkeypatch):
    """INSTAGRAM_SEND_TEXT_MESSAGE needs a numeric PSID; a @handle can never
    send. Fail fast with a readable error instead of a 400 round-trip."""
    agent = make_agent()
    agent._slugs["ig_send_dm"] = "INSTAGRAM_SEND_TEXT_MESSAGE"
    called: list[str] = []

    async def fake_execute(action, params):
        called.append(action)
        return {"ok": True}

    monkeypatch.setattr(agent, "execute_action", fake_execute)
    result = asyncio.run(agent.ig_send_dm(recipient_id="bluecreek", message="Hi"))
    assert result["ok"] is False
    assert "numeric Instagram PSID" in result["error"]
    assert called == []  # never hit the API


def test_ig_send_dm_accepts_numeric_psid(monkeypatch):
    agent = make_agent()
    agent._slugs["ig_send_dm"] = "INSTAGRAM_SEND_TEXT_MESSAGE"
    captured: dict = {}

    async def fake_execute(action, params):
        captured["params"] = params
        return {"ok": True, "data": {}}

    monkeypatch.setattr(agent, "execute_action", fake_execute)
    result = asyncio.run(agent.ig_send_dm(recipient_id="123456789", message="Hi"))
    assert result["ok"] is True
    assert captured["params"]["recipient_id"] == "123456789"


# --------------------------------------------------------------------------
# slug resolution prefers CONNECTED toolkits
# --------------------------------------------------------------------------


class FakeCatalogClient:
    """Returns per-toolkit catalog entries for GET /api/v3/tools."""

    def __init__(self, toolkits: dict[str, list[str]]):
        self._toolkits = toolkits

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, params=None, **kwargs):
        slug = (params or {}).get("toolkit_slug", "")
        items = [{"slug": s} for s in self._toolkits.get(slug, [])]
        return FakeResp(200, {"items": items})


def test_resolve_slugs_prefers_connected_toolkit(monkeypatch):
    """web_search resolves to Tavily only when tavily is ACTIVE; otherwise it
    falls through to the next connected provider (SerpAPI) instead of picking
    a catalog-listed-but-unconnected slug that would 404 at execute time."""
    agent = ComposioAgent(Settings(composio_api_key="test-key"))
    agent._account_by_toolkit = {
        "tavily": "acct-t", "serpapi": "acct-s", "google_maps": "acct-m",
    }
    toolkits = {
        "tavily": ["TAVILY_TAVILY_SEARCH"],
        "serpapi": ["SERPAPI_SEARCH", "SERPAPI_GOOGLE_LIGHT_SEARCH",
                    "SERPAPI_GOOGLE_MAPS_SEARCH"],
        "zenserp": ["ZENSERP_ZENSERP_GOOGLE_MAPS_SEARCH"],
        "google_maps": ["GOOGLE_MAPS_TEXT_SEARCH"],
        "gmail": ["GMAIL_SEND_EMAIL"],
        "googlesheets": ["GOOGLESHEETS_BATCH_UPDATE"],
        "instagram": ["INSTAGRAM_SEND_TEXT_MESSAGE"],
        "github": ["GITHUB_GET_USER"],
    }
    monkeypatch.setattr(
        composio_agent.httpx, "AsyncClient", lambda **kw: FakeCatalogClient(toolkits)
    )

    asyncio.run(agent.resolve_slugs())
    assert agent.slug("web_search") == "TAVILY_TAVILY_SEARCH"
    assert agent.slug("maps_search") == "GOOGLE_MAPS_TEXT_SEARCH"

    # Tavily disconnected, SerpAPI connected -> web_search now prefers SerpAPI.
    agent._account_by_toolkit = {"serpapi": "acct-s"}
    agent._slugs = {}
    agent._slugs_resolved = False
    asyncio.run(agent.resolve_slugs())
    assert agent.slug("web_search") == "SERPAPI_SEARCH"
