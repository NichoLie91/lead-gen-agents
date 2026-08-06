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

log = logging.getLogger(__name__)


class GeminiClient:
    def __init__(self, api_key: str, model: str = "gemini-flash-latest"):
        self._api_key = api_key
        self._model = model
        self._client = None
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
        except Exception as exc:  # quota (429) etc. — fall back to templates
            log.warning("Gemini call failed: %s", exc)
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
    """Load-splitting Gemini wrapper — duck-types ``GeminiClient`` (has
    ``available`` + async ``complete``) so any code that takes an LLM can take
    a pool instead.

    With two API keys configured (``GEMINI_API_KEY`` + ``GEMINI_API_KEY_2``)
    each ``complete()`` round-robins to the next key, splitting the load and
    roughly doubling the free-tier daily quota. Roles pick the model:
    "fast" (quick judgment: Atlas queries, Scout rationale, Enrichment
    extraction, Followups polish, Inbound labels) vs "pro" (heavier writing:
    outreach drafts, the Lead Agent's conversational brain).

    Falls back to a single key (or fully offline) when fewer are configured.
    Every call is recorded into the optional ``usage`` recorder (LLMUsage)
    for the /usage dashboard command.
    """

    def __init__(self, settings=None, role: str = "fast", clients: list | None = None,
                 usage: LLMUsage | None = None):
        if clients is not None:
            self._clients = list(clients)
        else:
            keys = [k for k in (settings.gemini_api_key, settings.gemini_api_key_2) if k]
            model = (settings.gemini_model_pro if role == "pro"
                     else settings.gemini_model_fast)
            self._clients = [GeminiClient(k, model) for k in keys]
        self._idx = 0
        self._role = role
        self._usage = usage

    @property
    def available(self) -> bool:
        return any(getattr(c, "available", False) for c in self._clients)

    async def complete(self, prompt: str) -> str:
        if not self._clients:
            return ""
        idx = self._idx % len(self._clients)
        client = self._clients[idx]
        self._idx += 1
        start = time.monotonic()
        result = await client.complete(prompt)
        if self._usage is not None:
            self._usage.record(
                key=f"key{idx + 1}",
                model=getattr(client, "_model", "?"),
                role=self._role,
                ok=bool(result),
                latency_ms=int((time.monotonic() - start) * 1000),
            )
        return result
