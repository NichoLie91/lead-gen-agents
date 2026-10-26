"""Sharing the Telegram bot: guests get read access, the owner keeps control.

Before this, anyone not in ADMIN_TELEGRAM_IDS was ignored in silence — so the
person the owner invited saw a dead bot, and the owner had no way to grant
anything short of adding a second owner. Guests now get a read-only surface,
strangers are told their ID so they can be added, and the owner is unaffected.
"""
from __future__ import annotations

import asyncio
import json

from src.agents.lead_agent import LeadAgent
from src.bot.commands import GUEST_COMMANDS, denied_message, is_allowed, role_for
from src.bot.telegram_bot import handle_update
from src.core.config import Settings
from src.core.state import StateStore

OWNER = 111
GUEST = 222
STRANGER = 333


class FakeLLM:
    def __init__(self, reply: str):
        self.reply = reply
        self.available = True

    async def complete(self, prompt: str, **kwargs) -> str:
        return self.reply


def _settings(tmp_path) -> Settings:
    settings = Settings.load({})
    settings.repo_root = tmp_path
    settings.telegram_bot_token = "token"
    settings.admin_telegram_ids = [OWNER]
    settings.guest_telegram_ids = [GUEST]
    settings.dry_run = True
    return settings


def _capture_send(monkeypatch) -> dict:
    captured: dict = {}

    async def fake_send(token: str, chat_id: int, text: str) -> bool:
        captured["text"] = text
        captured["chat_id"] = chat_id
        return True

    monkeypatch.setattr("src.bot.telegram_bot.send_message", fake_send)
    return captured


def _update(user_id: int, text: str) -> dict:
    return {"update_id": 1,
            "message": {"chat": {"id": user_id}, "from": {"id": user_id},
                        "text": text}}


# ---------- who is who ----------

def test_roles():
    assert role_for(OWNER, [OWNER], [GUEST]) == "owner"
    assert role_for(GUEST, [OWNER], [GUEST]) == "guest"
    assert role_for(STRANGER, [OWNER], [GUEST]) == "blocked"


def test_empty_allow_list_stays_open_for_first_test():
    assert role_for(STRANGER, [], []) == "owner"
    assert is_allowed(STRANGER, []) is True


def test_guests_count_as_allowed():
    assert is_allowed(GUEST, [OWNER], [GUEST]) is True
    assert is_allowed(STRANGER, [OWNER], [GUEST]) is False


def test_denied_message_carries_the_id_to_copy():
    text = denied_message(STRANGER)
    assert str(STRANGER) in text
    assert "guest" in text.lower()


# ---------- guest surface ----------

def test_guest_gets_status_but_not_a_run(tmp_path):
    settings = _settings(tmp_path)
    lead = LeadAgent(settings, StateStore(tmp_path))

    allowed = asyncio.run(lead.handle_message("/status", GUEST, role="guest"))
    assert "No runs yet" in allowed or "/run" in allowed

    refused = asyncio.run(lead.handle_message("/run", GUEST, role="guest"))
    assert "owner-only" in refused
    assert "guest you can use" in refused


def test_every_read_only_command_is_a_guest_command():
    for command in ("/help", "/id", "/status", "/batch", "/sheet",
                    "/drafts", "/list drafts", "/usage"):
        assert command in GUEST_COMMANDS


def test_guest_cannot_approve_or_stop(tmp_path):
    settings = _settings(tmp_path)
    lead = LeadAgent(settings, StateStore(tmp_path))
    for command in ("/approve all", "/reject all", "/stop", "/next", "/purge"):
        reply = asyncio.run(lead.handle_message(command, GUEST, role="guest"))
        assert "owner-only" in reply, command


def test_guest_question_is_answered(tmp_path):
    settings = _settings(tmp_path)
    llm = FakeLLM(json.dumps({"action": "reply", "text": "Atlas finds the leads."}))
    lead = LeadAgent(settings, StateStore(tmp_path), llm=llm)
    reply = asyncio.run(lead.handle_message("what does Atlas do?", GUEST, role="guest"))
    assert reply == "Atlas finds the leads."


def test_guest_agent_task_is_refused_before_it_runs(tmp_path):
    settings = _settings(tmp_path)
    llm = FakeLLM(json.dumps({"action": "agent", "task": "find plumber leads in Dallas",
                              "params": {"vertical": "plumber", "city": "Dallas"}}))
    lead = LeadAgent(settings, StateStore(tmp_path), llm=llm)
    reply = asyncio.run(lead.handle_message("find plumber leads in Dallas", GUEST,
                                            role="guest"))
    assert "owner access" in reply


def test_owner_keeps_full_control_while_sharing(tmp_path, monkeypatch):
    """The whole point of sharing: it must not lock the owner out."""
    settings = _settings(tmp_path)
    captured = _capture_send(monkeypatch)
    asyncio.run(handle_update(_update(OWNER, "/run"), settings,
                              StateStore(tmp_path), __import__(
                                  "src.agents.github_agent", fromlist=["GitHubAgent"]
                              ).GitHubAgent(settings, StateStore(tmp_path))))
    assert "skipped (dry-run" in captured.get("text", "")


# ---------- strangers + non-text ----------

def test_stranger_is_told_their_id_instead_of_silence(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    captured = _capture_send(monkeypatch)
    from src.agents.github_agent import GitHubAgent
    state = StateStore(tmp_path)
    asyncio.run(handle_update(_update(STRANGER, "/status"), settings, state,
                              GitHubAgent(settings, state)))
    assert str(STRANGER) in captured.get("text", "")
    assert captured.get("chat_id") == STRANGER


def test_non_text_message_gets_a_hint_not_an_empty_llm_call(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    captured = _capture_send(monkeypatch)
    from src.agents.github_agent import GitHubAgent
    state = StateStore(tmp_path)
    update = {"update_id": 2,
              "message": {"chat": {"id": OWNER}, "from": {"id": OWNER},
                          "photo": [{"file_id": "x"}]}}
    asyncio.run(handle_update(update, settings, state, GitHubAgent(settings, state)))
    assert "text messages" in captured.get("text", "")


def test_guest_ids_are_parsed_tolerantly():
    settings = Settings.load({"GUEST_TELEGRAM_IDS": " 222 , junk, 333,"})
    assert settings.guest_telegram_ids == [222, 333]


def test_guest_ids_can_be_empty():
    assert Settings.load({}).guest_telegram_ids == []
