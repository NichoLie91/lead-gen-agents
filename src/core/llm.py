"""Gemini client (spec section 4.4 / 7.5).

Uses ``google-genai`` with ``GEMINI_API_KEY``; when the key is absent or the
package is not installed, ``complete()`` returns "" so callers fall back to
template-based drafting. Kept behind a tiny wrapper so tests never need a key.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


class GeminiClient:
    def __init__(self, api_key: str, model: str = "gemini-2.5-flash"):
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
            resp = await self._client.models.generate_content(
                model=self._model,
                contents=prompt,
            )
            return (resp.text or "").strip()
        except Exception as exc:  # quota (429) etc. — fall back to templates
            log.warning("Gemini call failed: %s", exc)
            return ""
