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
from src.approvals import ApprovalQueue
from src.bot.commands import build_help, format_status, is_allowed, parse_command
from src.core.config import Settings
from src.core.state import StateStore

log = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"


# ---------- approval helpers (Step 04: human-in-the-loop) ----------
def _approve_drafts(args: str, state: StateStore) -> str:
    queue = ApprovalQueue(state)
    if args and args.lower() != "all":
        if queue.decide(args, "approved"):
            return f"✅ Approved draft {args[:8]}. It sends on the next /send all email."
        return f"No pending draft matches '{args[:16]}'. Try /list drafts."
    count = queue.decide_all("approved")
    if count:
        return f"✅ Approved {count} draft(s). They send on the next /send all email."
    return "No drafts waiting for approval. WARM drafts appear after an outreach run."


def _reject_drafts(args: str, state: StateStore) -> str:
    queue = ApprovalQueue(state)
    if args and args.lower() != "all":
        if queue.decide(args, "rejected"):
            return f"🚫 Rejected draft {args[:8]}."
        return f"No pending draft matches '{args[:16]}'. Try /list drafts."
    count = queue.decide_all("rejected")
    if count:
        return f"🚫 Rejected {count} draft(s)."
    return "No drafts waiting for approval."


async def _list_drafts(settings: Settings, state: StateStore) -> str:
    queue = ApprovalQueue(state)
    pending = queue.pending()
    if not pending:
        return "No drafts waiting for approval. WARM leads become drafts after an outreach run."
    names: dict[str, str] = {}
    try:
        # Enrich with lead names from the private sheet (bot has Composio creds).
        from src.agents.composio_agent import ComposioAgent
        from src.agents.sheets_agent import SheetsAgent

        sheets = SheetsAgent(ComposioAgent(settings), settings, state)
        raw = await sheets.read_tab("Outreach")
        if raw and raw[0]:
            header = [str(h).strip() for h in raw[0]]
            lid_i = header.index("Lead ID") if "Lead ID" in header else None
            name_i = header.index("Lead") if "Lead" in header else None
            for row in raw[1:]:
                if lid_i is not None and lid_i < len(row):
                    names[row[lid_i]] = row[name_i] if (name_i is not None and name_i < len(row)) else ""
    except Exception as exc:
        log.warning("list drafts sheet lookup failed: %s", exc)
    lines = ["Drafts awaiting approval:"]
    for lid in pending[:20]:
        lines.append(f"• `{lid[:8]}`  {names.get(lid, '')}")
    lines.append("\n/approve all — or /approve <id> · /reject <id> · /reject all")
    return "\n".join(lines)


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
    elif command == "/list drafts":
        await send_message(settings.telegram_bot_token, chat_id,
                           await _list_drafts(settings, state))
    elif command == "/approve":
        await send_message(settings.telegram_bot_token, chat_id,
                           _approve_drafts(_args, state))
    elif command == "/reject":
        await send_message(settings.telegram_bot_token, chat_id,
                           _reject_drafts(_args, state))
    elif command == "/reject all":
        await send_message(settings.telegram_bot_token, chat_id,
                           _reject_drafts("all", state))
    elif command == "/inbound":
        reply = await github.trigger_pipeline("inbound")
        await send_message(settings.telegram_bot_token, chat_id, reply)
    elif command == "/followups":
        reply = await github.trigger_pipeline("followups")
        await send_message(settings.telegram_bot_token, chat_id, reply)


def _sheet_url(settings: Settings) -> str:
    if settings.google_sheet_id:
        return f"https://docs.google.com/spreadsheets/d/{settings.google_sheet_id}"
    return "No live sheet yet (set GOOGLE_SHEET_ID or run the pipeline once)."


# ---------- single poll pass ----------
async def poll_once(settings: Settings, state: StateStore, github: GitHubAgent) -> int:
    """Fetch + process ONE batch of updates, then return.

    Deliberately NOT an infinite loop: each poll pass is a short-lived job
    (bot-poll.yml runs every 5 minutes), so after the batch is processed we
    exit cleanly. One broken update (e.g. a GitHub 403 reply) is caught and
    skipped so it can never abort the whole pass.
    """
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
        try:
            await handle_update(update, settings, state, github)
        except Exception:  # never let one update crash the batch
            log.exception("update %s failed", update_id)
        offset = max(offset, update_id)
        processed += 1
    if processed:
        state.set("telegram_offset", "offset", offset)
    return processed


async def main() -> int:
    import src.core.logging as logmod
    logmod.setup_logging()
    settings = Settings.load()
    if not settings.admin_telegram_ids:
        log.warning(
            "ADMIN_TELEGRAM_IDS is empty: the bot is OPEN, any chat with the "
            "link can send commands (use /id to fetch your user ID)"
        )
    state = StateStore(settings.state_dir)
    github = GitHubAgent(settings, state)
    # Single pass: process whatever batch is pending, persist the offset, then
    # exit — no tight polling loop, so the Actions job always terminates.
    processed = await poll_once(settings, state, github)
    # Commit + push state (approvals decisions, telegram_offset) so the next
    # ephemeral job — and the pipeline — actually sees them. Without this, the
    # files written here vanish when the job ends and /approve would never
    # reach the outreach run.
    if processed and github.commit_state():
        log.info("state committed to repo")
    log.info("poll pass finished; processed %d update(s); exiting", processed)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
