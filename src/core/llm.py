"""Gemini client (spec section 4.4 / 7.5).

Uses ``google-genai`` with ``GEMINI_API_KEY``; when the key is absent or the
package is not installed, ``complete()`` returns "" so callers fall back to
template-based drafting. Kept behind a tiny wrapper so tests never need a key.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def classify_gemini_error(exc: Exception) -> str:
    """Categorize a google-genai failure for the pool's fallback chain.

    Returns:
    - ``"quota"`` — 429 / RESOURCE_EXHAUSTED (retry another key or model)
    - ``"model"`` — unknown model 404 (retry a different model name)
    - ``"transient"`` — 5xx / timeout (retry)
    - ``"other"`` — semantic errors (retrying never helps)

    Order matters: the 429 body mentions "model: ..." but must classify as
    quota, so quota/rate-limit markers are checked first.
    """
    text = str(exc).lower()
    if any(s in text for s in ("429", "resource_exhausted", "quota", "rate limit")):
        return "quota"
    if ("404" in text or "not found" in text) and "model" in text:
        return "model"
    if any(s in text for s in ("500", "502", "503", "504", "unavailable",
                               "deadline", "timeout", "temporarily")):
        return "transient"
    return "other"


class GeminiClient:
    def __init__(self, api_key: str, model: str = "gemini-flash-latest"):
        self._api_key = api_key
        self._model = model
        self._client = None
        # Failure classification from the last call, for the pool's chain.
        self.last_error: str | None = None
        self.error_kind: str | None = None
        if api_key:
            try:
                from google import genai  # type: ignore[import-not-found]

                self._client = genai.Client(api_key=api_key)
            except ImportError:
                log.warning("google-genai not installed; LLM features offline")

    @property
    def available(self) -> bool:
        return self._client is not None

    async def complete(self, prompt: str) -> str:
        self.last_error = None
        self.error_kind = None
        if not self._client:
            return ""
        try:
            # In google-genai (>=1.x), client.models.generate_content is SYNC
            # and client.aio.models.generate_content is the awaitable variant.
            # The pipeline awaits complete() everywhere, so call through the
            # aio client — awaiting the sync call used to raise TypeError and
            # silently fall back to templates.
            resp = await self._client.aio.models.generate_content(
                model=self._model,
                contents=prompt,
            )
            return (resp.text or "").strip()
        except Exception as exc:  # quota (429), model 404, transient — pool retries
            self.last_error = str(exc)[:300]
            self.error_kind = classify_gemini_error(exc)
            log.warning("Gemini call failed (%s): %s", self.error_kind, self.last_error)
            return ""


class LLMUsage:
    """Persisted ring buffer of recent Gemini calls (dashboard data).

    PII-SAFE (spec 11): stores only the key alias ("key1"/"key2"), model,
    role, success flag and latency — never prompts, key material or lead data.
    Written to ``state/llm_usage.json`` and committed with the other state
    files so the Telegram bot (a separate ephemeral job) can report the last
    N calls with the /usage command.
    """

    MAX = 100

    def __init__(self, state_dir):
        self._path = Path(state_dir) / "llm_usage.json"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._calls: list[dict] = []
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                self._calls = data[-self.MAX:]
        except (FileNotFoundError, json.JSONDecodeError):
            self._calls = []

    def record(self, *, key: str, model: str, role: str, ok: bool,
               latency_ms: int) -> None:
        self._calls.append({
            "key": key, "model": model, "role": role,
            "ok": bool(ok), "ms": int(latency_ms),
            "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        })
        self._calls = self._calls[-self.MAX:]
        try:
            self._path.write_text(
                json.dumps(self._calls, separators=(",", ":")), encoding="utf-8")
        except OSError:
            pass

    def recent(self, n: int = 20) -> list[dict]:
        return self._calls[-n:]

    def totals(self, n: int = 50) -> dict:
        """Per-key + ok/failed counts over the last n calls (any key count)."""
        calls = self._calls[-n:]
        by_key: dict[str, int] = {}
        for call in calls:
            key = call.get("key", "?")
            by_key[key] = by_key.get(key, 0) + 1
        return {
            "by_key": by_key,
            "ok": sum(1 for c in calls if c.get("ok")),
            "failed": sum(1 for c in calls if not c.get("ok")),
        }


class GeminiPool:
    """Load-splitting + FAILOVER Gemini wrapper — duck-types ``GeminiClient``
    (has ``available`` + async ``complete``) so any code that takes an LLM can
    take a pool instead.

    Fallback chain (per logical call, in order):
        1. primary model on key1 -> key2      (role model: fast / pro)
        2. fallback model on key1 -> key2     (always-current alias,
           ``gemini_model``) — skipped when it equals the primary model
    It advances to the next attempt on quota (429), unknown-model 404 and
    transient errors, so a quota-exhausted key or model can never silence the
    bot. A semantic error (``other``) stops the chain immediately — retrying
    other keys/models would just burn the same failure.

    With two API keys configured (``GEMINI_API_KEY`` + ``GEMINI_API_KEY_2``)
    the chain also round-robins which key goes first, splitting the load and
    roughly doubling the free-tier daily quota. Roles pick the primary model:
    "fast" (quick judgment: Atlas queries, Scout rationale, Enrichment
    extraction, Followups polish, Inbound labels) vs "pro" (heavier writing:
    outreach drafts, the Lead Agent's conversational brain).

    Every logical call is recorded once into the optional ``usage`` recorder
    (LLMUsage) — the successful attempt's key/model, or the final failure —
    for the /usage dashboard command.
    """

    def __init__(self, settings=None, role: str = "fast", clients: list | None = None,
                 usage: LLMUsage | None = None, fallback_clients: list | None = None):
        if clients is not None:
            self._clients = list(clients)
            self._fallback = list(fallback_clients) if fallback_clients is not None else []
        else:
            keys = [k for k in (settings.gemini_api_key, settings.gemini_api_key_2) if k]
            primary = (settings.gemini_model_pro if role == "pro"
                       else settings.gemini_model_fast)
            self._clients = [GeminiClient(k, primary) for k in keys]
            fallback = getattr(settings, "gemini_model", "") or ""
            if fallback and fallback != primary:
                self._fallback = [GeminiClient(k, fallback) for k in keys]
            else:
                self._fallback = []
        self._start = 0
        self._role = role
        self._usage = usage

    @property
    def available(self) -> bool:
        return any(getattr(c, "available", False)
                   for c in self._clients + self._fallback)

    def _chain(self) -> list[tuple[Any, str]]:
        """Ordered (client, key_label) attempts: primary model per key, then
        fallback model per key. Rotates so key1/key2 alternate going first
        (load splitting); the fallback model always trails its key's primary.
        """
        chain: list[tuple[Any, str]] = []
        for i, client in enumerate(self._clients):
            chain.append((client, f"key{i + 1}"))
        for i, client in enumerate(self._fallback):
            chain.append((client, f"key{i + 1}"))
        step = len(self._clients)
        if step:
            shift = self._start % step
            self._start += 1
            if shift:
                chain = chain[shift:] + chain[:shift]
        return chain

    async def complete(self, prompt: str) -> str:
        if not self._clients and not self._fallback:
            return ""
        start = time.monotonic()
        last_label: str = "?"
        last_model: str = "?"
        for client, label in self._chain():
            result = await client.complete(prompt)
            if result:
                if self._usage is not None:
                    self._usage.record(
                        key=label,
                        model=getattr(client, "_model", "?"),
                        role=self._role,
                        ok=True,
                        latency_ms=int((time.monotonic() - start) * 1000),
                    )
                return result
            last_label, last_model = label, getattr(client, "_model", "?")
            kind = getattr(client, "error_kind", None) or "other"
            if kind == "other":
                break  # semantic failure — other keys/models fail identically
        if self._usage is not None:
            self._usage.record(
                key=last_label, model=last_model, role=self._role, ok=False,
                latency_ms=int((time.monotonic() - start) * 1000),
            )
        return ""
