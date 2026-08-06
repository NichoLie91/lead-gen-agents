"""Gemini load-splitting pool + per-role model config tests."""
from __future__ import annotations

import asyncio

from src.core.config import Settings
from src.core.llm import GeminiPool


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
        ("k1", "gemini-3.5-flash"), ("k2", "gemini-3.5-flash"),
        ("k1", "gemini-3.5-flash"), ("k2", "gemini-3.5-flash"),
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


def test_settings_load_key2_and_role_models():
    s = Settings.load({"GEMINI_API_KEY_2": "k2",
                       "GEMINI_MODEL_FAST": "fast-model",
                       "GEMINI_MODEL_PRO": "pro-model"})
    assert s.gemini_api_key_2 == "k2"
    assert s.gemini_model_fast == "fast-model"
    assert s.gemini_model_pro == "pro-model"
    # defaults when env vars are empty/absent (Actions sets them to "")
    s = Settings.load({"GEMINI_MODEL_FAST": "", "GEMINI_MODEL_PRO": ""})
    assert s.gemini_model_fast == "gemini-3.5-flash"
    assert s.gemini_model_pro == "gemini-3.5-flash"
