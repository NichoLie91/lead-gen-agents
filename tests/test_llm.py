"""Gemini load-splitting pool + per-role model config tests."""
from __future__ import annotations

import asyncio

from src.core.config import Settings
from src.core.llm import GeminiKeyState, GeminiPool, LLMUsage


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


# ---------- persisted key circuit breaker (quota-aware key switching) ----------


def test_key_state_quota_sets_cooldown_and_persists(tmp_path):
    ks = GeminiKeyState(tmp_path / "state")
    assert ks.healthy("key1")
    ks.on_quota("key1")
    assert not ks.healthy("key1")
    health = ks.health()["key1"]
    assert health["state"] == "cooling-down"
    assert health["failures"] == 1
    assert health["cooldown_until"] > 0
    # survives a fresh process-equivalent instance
    assert not GeminiKeyState(tmp_path / "state").healthy("key1")


def test_key_state_success_clears_cooldown(tmp_path):
    ks = GeminiKeyState(tmp_path / "state")
    ks.on_quota("key1")
    ks.on_success("key1")
    assert ks.healthy("key1")
    assert ks.health()["key1"]["failures"] == 0
    assert GeminiKeyState(tmp_path / "state").healthy("key1")


def test_key_state_unknown_key_is_healthy(tmp_path):
    ks = GeminiKeyState(tmp_path / "state")
    assert ks.healthy("key9")  # never failed -> never parked
    assert ks.health() == {}


def test_pool_leads_with_healthy_key_after_quota(tmp_path):
    """key1 429s once -> key2 succeeds -> the NEXT call leads with key2 and
    key1 is parked (not re-tried per round-robin) until its cooldown passes."""
    a = FailClient("A", error_kind="quota", fail_times=1)
    b = FailClient("B", fail_times=0)
    ks = GeminiKeyState(tmp_path / "state")
    pool = GeminiPool(clients=[a, b], key_state=ks)
    assert asyncio.run(pool.complete("x")) == "B:ok"  # quota -> fallback
    assert ks.health()["key1"]["state"] == "cooling-down"
    assert asyncio.run(pool.complete("y")) == "B:ok"
    assert asyncio.run(pool.complete("z")) == "B:ok"
    assert a.calls == 1  # key1 never re-tried while cooling down
    assert b.calls == 3


def test_pool_parks_exhausted_key_until_cooldown(tmp_path):
    """A key that stays exhausted is parked for the whole cooldown — no wasted
    429 attempts on every round-robin cycle."""
    a = FailClient("A", error_kind="quota", fail_times=99)
    b = FailClient("B", fail_times=0)
    pool = GeminiPool(clients=[a, b],
                      key_state=GeminiKeyState(tmp_path / "state"))
    for _ in range(4):
        assert asyncio.run(pool.complete("x")) == "B:ok"
    assert a.calls == 1  # only the very first attempt; parked afterwards
    assert b.calls == 4


def test_pool_retries_cooled_key_after_cooldown_expires(tmp_path):
    """When the cooldown passes, round-robin resumes and the key is tried again
    (a quota hit is not a permanent ban)."""
    a = FailClient("A", error_kind="quota", fail_times=1)
    b = FailClient("B", fail_times=0)
    ks = GeminiKeyState(tmp_path / "state", cooldown_sec=-1)  # expires instantly
    pool = GeminiPool(clients=[a, b], key_state=ks)
    asyncio.run(pool.complete("x"))  # A quota'd once, B ok
    asyncio.run(pool.complete("y"))  # round-robin -> B leads, ok
    asyncio.run(pool.complete("z"))  # round-robin -> A leads again, succeeds
    assert a.calls == 2
    assert asyncio.run(pool.complete("w")) in ("A:ok", "B:ok")


def test_key_switch_persists_across_pool_instances(tmp_path):
    """The switch survives ephemeral jobs: a brand-new pool (fresh process
    equivalent) reads the parked key from state and never tries it first."""
    a = FailClient("A", error_kind="quota", fail_times=99)
    b = FailClient("B", fail_times=0)
    pool1 = GeminiPool(clients=[a, b],
                       key_state=GeminiKeyState(tmp_path / "state"))
    assert asyncio.run(pool1.complete("x")) == "B:ok"  # A parked
    # Fresh pool in a new "job": same state dir -> key1 still parked.
    c = FailClient("A2", error_kind="quota", fail_times=99)
    d = FailClient("B2", fail_times=0)
    pool2 = GeminiPool(clients=[c, d],
                       key_state=GeminiKeyState(tmp_path / "state"))
    assert asyncio.run(pool2.complete("y")) == "B2:ok"
    assert c.calls == 0  # parked key never tried first


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
