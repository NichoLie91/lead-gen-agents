"""Gemini client (spec section 4.4 / 7.5).

Uses ``google-genai`` with ``GEMINI_API_KEY``; when the key is absent or the
package is not installed, ``complete()`` returns "" so callers fall back to
template-based drafting. Kept behind a tiny wrapper so tests never need a key.
"""
from __future__ import annotations

import logging

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
    """

    def __init__(self, settings=None, role: str = "fast", clients: list | None = None):
        if clients is not None:
            self._clients = list(clients)
        else:
            keys = [k for k in (settings.gemini_api_key, settings.gemini_api_key_2) if k]
            model = (settings.gemini_model_pro if role == "pro"
                     else settings.gemini_model_fast)
            self._clients = [GeminiClient(k, model) for k in keys]
        self._idx = 0

    @property
    def available(self) -> bool:
        return any(getattr(c, "available", False) for c in self._clients)

    async def complete(self, prompt: str) -> str:
        if not self._clients:
            return ""
        client = self._clients[self._idx % len(self._clients)]
        self._idx += 1
        return await client.complete(prompt)
