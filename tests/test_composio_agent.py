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
