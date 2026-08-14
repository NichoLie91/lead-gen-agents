"""Telegram bot transport-resilience tests.

Regression: on 2026-08-14 an uncaught ``httpx.ConnectError`` in get_updates
failed the whole bot-poll job (run 31766417723) and broke the keep-alive
chain. These tests pin down the fix: transient failures (ConnectError,
ReadTimeout, 429, 5xx) are retried with backoff, and NO network-layer
exception may ever crash a poll pass.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from src.bot import telegram_bot
from src.bot.telegram_bot import get_updates, poll_once, send_message
from src.core.config import Settings
from src.core.state import StateStore

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
    """Async context manager; each get/post pops the next queued item, which
    may be a FakeResp OR an exception (simulating a transport failure). When
    the queue runs dry the LAST item repeats, so a retry loop exercising all
    attempts against one failure stays faithful to production."""

    def __init__(self, responses: list):
        self._responses = list(responses)
        self._last = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, *args, **kwargs):
        if self._responses:
            self._last = self._responses.pop(0)
        item = self._last
        if isinstance(item, BaseException):
            raise item
        return item

    async def post(self, *args, **kwargs):
        return await self.get(*args, **kwargs)


@pytest.fixture
def no_backoff(monkeypatch):
    """Keep retry/backoff tests fast: record attempts, never sleep."""

    async def fake_sleep(sec: float):
        return None

    monkeypatch.setattr(telegram_bot, "_retry_sleep", fake_sleep)


def fake_http(monkeypatch, responses: list) -> FakeAsyncClient:
    client = FakeAsyncClient(responses)
    monkeypatch.setattr(telegram_bot.httpx, "AsyncClient", lambda **kw: client)
    return client


def make_harness(tmp_path) -> tuple[Settings, StateStore]:
    settings = Settings(dry_run=True, repo_root=tmp_path)
    settings.telegram_bot_token = "test"
    state = StateStore(tmp_path / "state")
    return settings, state


# --------------------------------------------------------------------------
# get_updates transport resilience
# --------------------------------------------------------------------------


def test_get_updates_survives_connect_error(monkeypatch, no_backoff):
    """A ConnectError (the exact 2026-08-14 crash) must return [] — never raise."""
    fake_http(monkeypatch, [httpx.ConnectError("connection refused")])
    assert asyncio.run(get_updates("tok", 0, timeout=2)) == []


def test_get_updates_survives_read_timeout(monkeypatch, no_backoff):
    fake_http(monkeypatch, [httpx.ReadTimeout("timed out")])
    assert asyncio.run(get_updates("tok", 0, timeout=2)) == []


def test_get_updates_retries_429_then_succeeds(monkeypatch, no_backoff):
    """Flood control (429) must retry and recover, not return [] on the blip."""
    client = fake_http(monkeypatch, [
        FakeResp(429, "Too Many Requests"),
        FakeResp(200, {"ok": True, "result": [{"update_id": 1}]}),
    ])
    result = asyncio.run(get_updates("tok", 0, timeout=2))
    assert [u["update_id"] for u in result] == [1]
    assert client._responses == []  # both attempts consumed


def test_get_updates_retries_500_then_succeeds(monkeypatch, no_backoff):
    fake_http(monkeypatch, [
        FakeResp(500, "internal error"),
        FakeResp(200, {"ok": True, "result": [{"update_id": 2}]}),
    ])
    result = asyncio.run(get_updates("tok", 0, timeout=2))
    assert [u["update_id"] for u in result] == [2]


def test_get_updates_permanent_error_returns_empty(monkeypatch, no_backoff):
    """4xx (except 429) is not transient: no point retrying, return [] once."""
    client = fake_http(monkeypatch, [FakeResp(401, "Unauthorized")])
    assert asyncio.run(get_updates("tok", 0, timeout=2)) == []
    assert client._responses == []  # only one attempt


def test_get_updates_gives_up_after_all_retries(monkeypatch, no_backoff):
    """Persistent transport failure -> [] after exhausting the retry budget."""
    client = fake_http(monkeypatch, [httpx.ConnectError("down")] * 5)
    assert asyncio.run(get_updates("tok", 0, timeout=2)) == []
    assert len(client._responses) == 5 - telegram_bot._RETRY_ATTEMPTS


# --------------------------------------------------------------------------
# send_message transport resilience
# --------------------------------------------------------------------------


def test_send_message_survives_connect_error(monkeypatch, no_backoff):
    fake_http(monkeypatch, [httpx.ConnectError("connection refused")])
    assert asyncio.run(send_message("tok", 1, "hi")) is False


def test_send_message_retries_500_then_succeeds(monkeypatch, no_backoff):
    client = fake_http(monkeypatch, [
        FakeResp(500, "internal error"),
        FakeResp(200, {"ok": True}),
    ])
    assert asyncio.run(send_message("tok", 1, "hi")) is True
    assert client._responses == []


def test_send_message_chunks_long_text(monkeypatch, no_backoff):
    """Long replies are chunked at 4096 chars; each chunk must succeed."""
    client = fake_http(monkeypatch, [FakeResp(200, {"ok": True})] * 3)
    assert asyncio.run(send_message("tok", 1, "x" * 9000)) is True
    assert client._responses == []  # 3 chunks: 4096 + 4096 + 808


# --------------------------------------------------------------------------
# poll_once stress: the poll job must never crash on network failures
# --------------------------------------------------------------------------


def test_poll_once_survives_transient_get_updates_failure(tmp_path, monkeypatch):
    """One blip then recovery: the poll processes the update that arrives
    after the failure instead of aborting the whole pass."""
    settings, state = make_harness(tmp_path)
    settings.poll_max_wait_sec = 0.3
    sent: list[str] = []
    calls = {"n": 0}

    async def fake_get_updates(token: str, offset: int, timeout: int = 50):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("boom")  # exactly the 2026-08-14 crash
        return [{"update_id": 41,
                 "message": {"chat": {"id": 7}, "from": {"id": 7}, "text": "/run"}}]

    async def fake_send(token: str, chat_id: int, text: str) -> bool:
        sent.append(text)
        return True

    monkeypatch.setattr("src.bot.telegram_bot.get_updates", fake_get_updates)
    monkeypatch.setattr("src.bot.telegram_bot.send_message", fake_send)
    processed = asyncio.run(poll_once(settings, state, github=None))
    assert processed == 1      # the update after the blip was still handled
    assert sent                # and its reply was sent
    assert calls["n"] >= 2     # polling continued past the failure


def test_poll_once_never_crashes_on_repeated_network_failures(tmp_path, monkeypatch):
    """Persistent outage: poll_once must bail cleanly (return 0), never raise —
    the keep-alive chain then dispatches the next run and recovers."""
    settings, state = make_harness(tmp_path)
    settings.poll_max_wait_sec = 60  # would spin forever without the bail
    calls = {"n": 0}

    async def fake_get_updates(token: str, offset: int, timeout: int = 50):
        calls["n"] += 1
        raise httpx.ConnectError("down")

    monkeypatch.setattr("src.bot.telegram_bot.get_updates", fake_get_updates)
    processed = asyncio.run(poll_once(settings, state, github=None))
    assert processed == 0
    assert calls["n"] <= 4  # bailed after the 3-empty guard, not the full 60s
