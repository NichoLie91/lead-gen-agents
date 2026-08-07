"""Gemini load-splitting pool + per-role model config tests."""
from __future__ import annotations

import asyncio

from src.core.config import Settings
from src.core.llm import GeminiPool, LLMUsage


class FakeClient:
    def __init__(self, name: str, available: bool = True):
        self.name = name
        self.available = available
        self.calls = 0

    async def complete(self, prompt: str) -> str:
        self.calls += 1
        return f"{self.name}:ok"


def test_pool_round_robins_across_keys():
    a, b = FakeClient("A"), FakeClient("B")
    pool = GeminiPool(clients=[a, b])
    assert asyncio.run(pool.complete("x")) == "A:ok"
    assert asyncio.run(pool.complete("x")) == "B:ok"
    assert asyncio.run(pool.complete("x")) == "A:ok"
    assert a.calls == 2 and b.calls == 1  # load split 2/1


def test_pool_single_key_no_round_robin():
    a = FakeClient("A")
    pool = GeminiPool(clients=[a])
    assert asyncio.run(pool.complete("x")) == "A:ok"
    assert asyncio.run(pool.complete("x")) == "A:ok"
    assert a.calls == 2


def test_pool_available_if_any_client_available():
    assert GeminiPool(clients=[FakeClient("A", available=True),
                               FakeClient("B", available=False)]).available
    assert not GeminiPool(clients=[FakeClient("A", available=False)]).available


def test_pool_empty_is_offline():
    pool = GeminiPool(clients=[])
    assert not pool.available
    assert asyncio.run(pool.complete("x")) == ""


def test_pool_role_picks_model(monkeypatch):
    captured: list[tuple[str, str]] = []

    def fake_client(api_key: str, model: str):
        captured.append((api_key, model))
        return FakeClient(api_key)

    monkeypatch.setattr("src.core.llm.GeminiClient", fake_client)
    settings = Settings(gemini_api_key="k1", gemini_api_key_2="k2")
    GeminiPool(settings, role="pro")
    GeminiPool(settings, role="fast")
    # fast == pro == the always-current alias -> NO fallback clients built.
    assert captured == [
        ("k1", "gemini-flash-latest"), ("k2", "gemini-flash-latest"),
        ("k1", "gemini-flash-latest"), ("k2", "gemini-flash-latest"),
    ]
    # Per-role overrides win; the always-current alias trails as the fallback
    # MODEL for each key (chain: k1/pro-x, k2/pro-x, k1/alias, k2/alias, ...).
    settings = Settings(gemini_api_key="k1", gemini_api_key_2="k2",
                        gemini_model_fast="fast-x", gemini_model_pro="pro-x")
    captured.clear()
    GeminiPool(settings, role="pro")
    GeminiPool(settings, role="fast")
    assert captured == [
        ("k1", "pro-x"), ("k2", "pro-x"),
        ("k1", "gemini-flash-latest"), ("k2", "gemini-flash-latest"),
        ("k1", "fast-x"), ("k2", "fast-x"),
        ("k1", "gemini-flash-latest"), ("k2", "gemini-flash-latest"),
    ]


def test_pool_uses_only_configured_keys(monkeypatch):
    captured: list[str] = []

    def fake_client(api_key: str, model: str):
        captured.append(api_key)
        return FakeClient(api_key)

    monkeypatch.setattr("src.core.llm.GeminiClient", fake_client)
    GeminiPool(Settings(gemini_api_key="only-one"), role="fast")
    assert captured == ["only-one"]


def test_usage_records_and_rings(tmp_path):
    usage = LLMUsage(tmp_path / "state")
    for i in range(5):
        usage.record(key="key1", model="gemini-3.5-flash", role="fast",
                     ok=i % 2 == 0, latency_ms=100 + i)
    recent = usage.recent(3)
    assert len(recent) == 3
    assert recent[-1]["ms"] == 104  # newest kept
    assert usage.totals(5) == {"by_key": {"key1": 5}, "ok": 3, "failed": 2}
    # persisted: a fresh instance reads the same file
    again = LLMUsage(tmp_path / "state")
    assert len(again.recent(100)) == 5


def test_usage_caps_at_max(tmp_path):
    usage = LLMUsage(tmp_path / "state")
    for i in range(LLMUsage.MAX + 25):
        usage.record(key="key2", model="m", role="pro", ok=True, latency_ms=1)
    assert len(usage.recent(1000)) == LLMUsage.MAX
    assert usage.totals(1000)["by_key"]["key2"] == LLMUsage.MAX


def test_usage_missing_file_is_empty(tmp_path):
    assert LLMUsage(tmp_path / "nope").recent(5) == []


def test_pool_records_usage(tmp_path):
    a, b = FakeClient("A"), FakeClient("B")
    a._model, b._model = "gemini-3.5-flash", "gemini-3.5-flash"
    usage = LLMUsage(tmp_path / "state")
    pool = GeminiPool(clients=[a, b], role="pro", usage=usage)
    asyncio.run(pool.complete("x"))
    asyncio.run(pool.complete("y"))
    calls = usage.recent(5)
    assert [c["key"] for c in calls] == ["key1", "key2"]
    assert all(c["role"] == "pro" for c in calls)
    assert all(c["ok"] for c in calls)


class FailClient(FakeClient):
    """FakeClient that fails the first N calls with a given error_kind, then
    succeeds — lets tests drive the fallback chain deterministically."""

    def __init__(self, name: str, error_kind: str = "quota", fail_times: int = 99):
        super().__init__(name)
        self._error_kind = error_kind
        self._fail_times = fail_times

    async def complete(self, prompt: str) -> str:
        self.calls += 1
        if self.calls <= self._fail_times:
            self.error_kind = self._error_kind
            self.last_error = "simulated failure"
            return ""
        return f"{self.name}:ok"


def test_pool_falls_back_to_second_key_on_quota():
    """key1 429s (quota) -> the chain tries key2 and succeeds."""
    a = FailClient("A", error_kind="quota")
    b = FailClient("B", fail_times=0)
    pool = GeminiPool(clients=[a, b])
    assert asyncio.run(pool.complete("x")) == "B:ok"
    assert a.calls == 1 and b.calls == 1


def test_pool_stops_chain_on_semantic_error():
    """A non-retryable (other) failure stops the chain — no wasted calls."""
    a = FailClient("A", error_kind="other")
    b = FailClient("B")
    pool = GeminiPool(clients=[a, b])
    assert asyncio.run(pool.complete("x")) == ""
    assert a.calls == 1 and b.calls == 0


def test_pool_model_fallback_chain():
    """Both keys quota-dead on the primary model -> the fallback MODEL wins."""
    a = FailClient("A", error_kind="quota")
    b = FailClient("B", error_kind="quota")
    c = FailClient("C", fail_times=0)
    pool = GeminiPool(clients=[a, b], fallback_clients=[c])
    assert asyncio.run(pool.complete("x")) == "C:ok"
    assert a.calls == 1 and b.calls == 1 and c.calls == 1


def test_pool_records_fallback_outcome(tmp_path):
    """The /usage dashboard records the SUCCESSFUL fallback key/model."""
    a = FailClient("A", error_kind="quota")
    b = FailClient("B", fail_times=0)
    a._model = b._model = "gemini-flash-latest"
    usage = LLMUsage(tmp_path / "state")
    pool = GeminiPool(clients=[a, b], role="pro", usage=usage)
    assert asyncio.run(pool.complete("x")) == "B:ok"
    calls = usage.recent(1)
    assert calls[0]["key"] == "key2"
    assert calls[0]["model"] == "gemini-flash-latest"
    assert calls[0]["role"] == "pro"
    assert calls[0]["ok"] is True


def test_settings_load_key2_and_role_models():
    s = Settings.load({"GEMINI_API_KEY_2": "k2",
                       "GEMINI_MODEL_FAST": "fast-model",
                       "GEMINI_MODEL_PRO": "pro-model"})
    assert s.gemini_api_key_2 == "k2"
    assert s.gemini_model_fast == "fast-model"
    assert s.gemini_model_pro == "pro-model"
    # defaults when env vars are empty/absent (Actions sets them to "")
    s = Settings.load({"GEMINI_MODEL_FAST": "", "GEMINI_MODEL_PRO": ""})
    assert s.gemini_model_fast == "gemini-flash-latest"
    assert s.gemini_model_pro == "gemini-flash-latest"
