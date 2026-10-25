"""Logging setup and a Telegram failure-alert hook (spec 5.3 / 10)."""
from __future__ import annotations

import logging
import sys

import httpx

TELEGRAM_API = "https://api.telegram.org"


def setup_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(
        level=level,
        stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


class TelegramNotifier:
    """Sends alert messages to a Telegram chat. No-op when unconfigured.

    Used for pipeline failures (spec 10) and optionally the run report.
    """

    def __init__(self, bot_token: str, chat_id: str):
        self._token = bot_token
        self._chat_id = chat_id
        self._enabled = bool(bot_token and chat_id)

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def notify(self, text: str) -> bool:
        return await self._send({"chat_id": self._chat_id, "text": text[:4096]})

    async def notify_html(self, text: str) -> bool:
        """Same message rendered as HTML (used for the <pre> batch table)."""
        return await self._send({
            "chat_id": self._chat_id, "text": text[:4096],
            "parse_mode": "HTML",
        })

    async def notify_document(self, filename: str, content: str) -> bool:
        """Attach a generated file (the research batch CSV) to the chat."""
        if not self._enabled:
            return False
        try:
            async with httpx.AsyncClient(timeout=25) as client:
                resp = await client.post(
                    f"{TELEGRAM_API}/bot{self._token}/sendDocument",
                    data={"chat_id": self._chat_id, "caption": filename[:200]},
                    files={"document": (filename, content.encode("utf-8"),
                                        "text/csv")},
                )
            return resp.status_code == 200
        except httpx.HTTPError as exc:
            logging.getLogger(__name__).warning("Telegram document failed: %s", exc)
            return False

    async def _send(self, payload: dict) -> bool:
        if not self._enabled:
            return False
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"{TELEGRAM_API}/bot{self._token}/sendMessage", json=payload,
                )
            return resp.status_code == 200
        except httpx.HTTPError as exc:  # never let alerts break the pipeline
            logging.getLogger(__name__).warning("Telegram notify failed: %s", exc)
            return False
