"""Telegram remote-control bot — raw ``getUpdates`` polling (spec 4.2, 5.3).

Runs as a single pass inside an ephemeral GitHub Actions job (bot-poll.yml,
every 5 minutes): fetch updates >= persisted offset, dispatch commands,
respond, persist the new offset, exit. No long-lived process, no framework.
"""
from __future__ import annotations

import asyncio
import logging

import httpx

from src.agents.github_agent import GitHubAgent
from src.bot.commands import build_help, format_status, is_allowed, parse_command
from src.core.config import Settings
from src.core.state import StateStore

log = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"


# ---------- low-level Telegram API ----------
async def get_updates(token: str, offset: int) -> list[dict]:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{TELEGRAM_API}/bot{token}/getUpdates",
            params={"offset": offset, "timeout": 2,
                    "allowed_updates": '["message"]'},
        )
        if resp.status_code != 200:
            log.warning("getUpdates failed: %s %s", resp.status_code, resp.text[:200])
            return []
        return resp.json().get("result", [])


async def send_message(token: str, chat_id: int, text: str) -> bool:
    for chunk in _chunk(text, 4096):
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{TELEGRAM_API}/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": chunk},
            )
        if resp.status_code != 200:
            log.warning("sendMessage failed: %s", resp.text[:200])
            return False
    return True


def _chunk(text: str, size: int) -> list[str]:
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


# ---------- command dispatch ----------
async def handle_update(
    update: dict,
    settings: Settings,
    state: StateStore,
    github: GitHubAgent,
) -> None:
    message = update.get("message") or {}
    chat_id = message.get("chat", {}).get("id")
    user_id = message.get("from", {}).get("id")
    text = message.get("text", "")
    if chat_id is None or user_id is None:
        return
    if not is_allowed(user_id, settings.admin_telegram_ids):
        log.info("ignoring message from unauthorized user %s", user_id)
        return

    command, _args = parse_command(text)
    if not command:
        return

    if command == "/help":
        await send_message(settings.telegram_bot_token, chat_id, build_help())
    elif command == "/id":
        await send_message(settings.telegram_bot_token, chat_id, f"Your Telegram user ID: {user_id}")
    elif command == "/status":
        report = state.load("last_run")
        running = bool(state.get("pipeline_running", "running", False))
        sheet_url = _sheet_url(settings)
        await send_message(settings.telegram_bot_token, chat_id,
                           format_status(report, sheet_url, running))
    elif command == "/sheet":
        await send_message(settings.telegram_bot_token, chat_id, _sheet_url(settings))
    elif command == "/run":
        if await github.pipeline_in_progress():
            await send_message(settings.telegram_bot_token, chat_id,
                               "A pipeline run is already in progress. Use /status or /stop.")
            return
        reply = await github.trigger_pipeline("full")
        await send_message(settings.telegram_bot_token, chat_id, reply)
    elif command == "/send all email":
        reply = await github.trigger_pipeline("outreach-email")
        await send_message(settings.telegram_bot_token, chat_id, reply)
    elif command == "/send all instagram":
        reply = await github.trigger_pipeline("outreach-ig")
        await send_message(settings.telegram_bot_token, chat_id, reply)
    elif command == "/stop":
        github.set_stop()
        await send_message(settings.telegram_bot_token, chat_id,
                           "Stop flag set. The running pipeline will halt between stages.")
    elif command in ("/approve", "/reject", "/reject all"):
        # v1: recorded in state; actual draft approval applies on the next
        # outreach-email run (spec 5.1).
        state.set("review", "decision", command)
        await send_message(settings.telegram_bot_token, chat_id,
                           f"{command} recorded. Warm drafts will follow this decision "
                           f"on the next /send all email.")


def _sheet_url(settings: Settings) -> str:
    if settings.google_sheet_id:
        return f"https://docs.google.com/spreadsheets/d/{settings.google_sheet_id}"
    return "No live sheet yet (set GOOGLE_SHEET_ID or run the pipeline once)."


# ---------- single poll pass ----------
async def poll_once(settings: Settings, state: StateStore, github: GitHubAgent) -> int:
    token = settings.telegram_bot_token
    if not token:
        log.error("TELEGRAM_BOT_TOKEN not set")
        return 0
    offset = int(state.get("telegram_offset", "offset", 0))
    updates = await get_updates(token, offset)
    processed = 0
    for update in updates:
        update_id = int(update.get("update_id", 0))
        if update_id <= offset:
            continue
        await handle_update(update, settings, state, github)
        offset = max(offset, update_id)
        processed += 1
    if processed:
        state.set("telegram_offset", "offset", offset)
    return processed


async def main() -> int:
    import src.core.logging as logmod
    logmod.setup_logging()
    settings = Settings.load()
    state = StateStore(settings.state_dir)
    github = GitHubAgent(settings, state)
    processed = await poll_once(settings, state, github)
    log.info("poll pass finished; processed %d update(s)", processed)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
