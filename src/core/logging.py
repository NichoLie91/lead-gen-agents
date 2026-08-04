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
        if not self._enabled:
            return False
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"{TELEGRAM_API}/bot{self._token}/sendMessage",
                    json={"chat_id": self._chat_id, "text": text[:4096]},
                )
            return resp.status_code == 200
        except httpx.HTTPError as exc:  # never let alerts break the pipeline
            logging.getLogger(__name__).warning("Telegram notify failed: %s", exc)
            return False
