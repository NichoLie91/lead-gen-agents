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
    assert captured == [
        ("k1", "gemini-flash-latest"), ("k2", "gemini-flash-latest"),
        ("k1", "gemini-flash-latest"), ("k2", "gemini-flash-latest"),
    ]
    # per-role overrides win
    settings = Settings(gemini_api_key="k1", gemini_api_key_2="k2",
                        gemini_model_fast="fast-x", gemini_model_pro="pro-x")
    captured.clear()
    GeminiPool(settings, role="pro")
    GeminiPool(settings, role="fast")
    assert captured == [
        ("k1", "pro-x"), ("k2", "pro-x"),
        ("k1", "fast-x"), ("k2", "fast-x"),
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
