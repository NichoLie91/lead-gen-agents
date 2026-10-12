"""Phase 4 tests: A2A-shaped agent cards (discovery instead of hardcoded
wiring) and AgentTask lifecycle/dispatch (including peer-to-peer)."""
from __future__ import annotations

import asyncio

from src.agents import discovery
from src.agents.discovery import (
    AgentTask,
    assign,
    capabilities_of,
    card_for,
    discover_team,
    dispatch_task,
    load_cards,
    who_can,
)
from src.agents.lead_agent import LeadAgent, team_roster
from src.core.config import Settings
from src.core.state import StateStore


class FakeGithub:
    def __init__(self):
        self.calls: list[str] = []

    async def trigger_pipeline(self, mode: str) -> str:
        self.calls.append(mode)
        return f"triggered pipeline mode={mode} (fake)"

    async def pipeline_in_progress(self) -> bool:
        return False

    def set_stop(self) -> None:
        pass


def run(coro):
    return asyncio.run(coro)


# ---------- cards + discovery ----------

def test_cards_file_is_valid_a2a_shape():
    cards = load_cards()
    assert len(cards) == 6
    names = {c["name"] for c in cards}
    assert names == {"Atlas", "Scout", "Enrichment", "Outreach",
                     "Followups", "Inbound"}
    for card in cards:
        assert card["role"]
        assert isinstance(card["capabilities"], list) and card["capabilities"]
        assert "input_schema" in card and "output_schema" in card
        assert str(card.get("endpoint", "")).startswith("internal://agents/")


def test_card_for_is_case_insensitive_and_safe():
    assert card_for("scout")["role"] == "Lead Scoring"
    assert card_for("SCOUT")["name"] == "Scout"
    assert card_for("Nobody") is None
    assert card_for("") is None


def test_capabilities_lookup():
    assert "draft_critique_loop" in capabilities_of("Outreach")
    assert capabilities_of("Nobody") == []


def test_who_can_by_capability():
    assert who_can("maps_discovery") == ["Atlas"]
    assert who_can("reply_classification") == ["Inbound"]
    assert who_can("no_such_skill") == []


def test_discover_team_renders_all_six():
    roster = discover_team()
    for name in ("Atlas", "Scout", "Enrichment", "Outreach",
                 "Followups", "Inbound"):
        assert f"- {name}" in roster


def test_team_roster_uses_cards_and_is_pii_safe():
    roster = team_roster()
    assert "Atlas" in roster and "Inbound" in roster
    assert "Lead Scoring" in roster  # from the card, not the TEAM tuple
    for leaked in ("email@", "@gmail", "TELEGRAM_BOT_TOKEN", "GEMINI_API_KEY"):
        assert leaked not in roster


def test_cards_cache_reset_for_corrupt_file(tmp_path, monkeypatch):
    # Point discovery at a directory with no cards.json: empty registry.
    monkeypatch.setattr(discovery, "_CARDS_CACHE", None)
    monkeypatch.setattr(discovery, "__file__", str(tmp_path / "discovery.py"))
    assert discovery.load_cards() == []  # missing -> empty, never raises

    # Restore the real file and clear the cache: discovery recovers.
    monkeypatch.undo()
    monkeypatch.setattr(discovery, "_CARDS_CACHE", None)
    assert len(discovery.load_cards()) == 6


# ---------- AgentTask lifecycle ----------

def test_task_starts_submitted_with_id():
    task = AgentTask(kind="discovery", payload={"verticals": ["plumber"]})
    assert task.status == "submitted"
    assert task.task_id and len(task.task_id) == 12


def test_happy_path_transitions():
    task = AgentTask(kind="score")
    assert task.transition("working")
    assert task.transition("completed")
    assert task.status == "completed"


def test_illegal_transitions_rejected():
    task = AgentTask(kind="score")
    assert not task.transition("completed")   # submitted -> completed is a jump
    assert not task.transition("bogus")
    task.transition("working")
    assert not task.transition("submitted")   # no going back
    assert task.transition("failed")
    assert not task.transition("completed")   # failed is terminal


def test_input_required_is_supported():
    task = AgentTask(kind="draft")
    task.transition("working")
    assert task.transition("input-required")  # the approval-queue shape
    assert task.transition("working")
    assert task.transition("completed")


def test_to_dict_is_a2a_shaped():
    task = AgentTask(kind="enrich", payload={"lead_id": "abc"})
    task.transition("working")
    d = task.to_dict()
    assert set(d) == {"task_id", "kind", "status", "message",
                      "assigned_to", "artifacts"}
    assert d["status"] == "working"


# ---------- assignment + dispatch ----------

def test_assign_uses_cards():
    task = assign(AgentTask(kind="discovery"))
    assert task.assigned_to == "Atlas"
    task = assign(AgentTask(kind="draft"))
    assert task.assigned_to == "Outreach"


def test_dispatch_completes():
    async def handler(task):
        return {"ok": True, "message": "found 7 leads",
                "artifacts": {"candidates": 7}}

    task = run(dispatch_task(AgentTask(kind="discovery"), handler=handler))
    assert task.status == "completed"
    assert task.assigned_to == "Atlas"
    assert task.message == "found 7 leads"
    assert task.artifacts == {"candidates": 7}


def test_dispatch_failure_marks_failed_and_never_raises():
    async def boom(task):
        raise RuntimeError("sheets exploded")

    task = run(dispatch_task(AgentTask(kind="enrich"), handler=boom))
    assert task.status == "failed"
    assert "sheets exploded" in task.message


def test_dispatch_bad_handler_result_fails():
    async def weird(task):
        return "not a dict"

    task = run(dispatch_task(AgentTask(kind="inbound"), handler=weird))
    assert task.status == "failed"
    assert "bad handler result" in task.message


def test_dispatch_handler_ok_false_fails():
    async def nope(task):
        return {"ok": False, "message": "nothing to do"}

    task = run(dispatch_task(AgentTask(kind="followups"), handler=nope))
    assert task.status == "failed" and task.message == "nothing to do"


# ---------- peer-to-peer entry point ----------

def _make_agent(tmp_path) -> LeadAgent:
    settings = Settings(dry_run=True, repo_root=tmp_path)
    settings.telegram_bot_token = "test"
    state = StateStore(tmp_path / "state")
    return LeadAgent(settings, state, github=FakeGithub())


def test_peer_task_routes_through_agent_loop(tmp_path):
    agent = _make_agent(tmp_path)
    task = AgentTask(kind="check", payload={"detail": "what leads exist?"})
    answer = run(agent.run_peer_task(task))
    # No brain configured -> the loop degrades to deterministic routing, but
    # the task itself must complete and answer.
    assert task.status == "completed"
    assert answer  # non-empty reply


def test_peer_task_survives_handler_crash(tmp_path, monkeypatch):
    agent = _make_agent(tmp_path)

    async def boom(*a, **k):
        raise RuntimeError("nope")

    monkeypatch.setattr(agent, "_execute_agent_task", boom)
    task = AgentTask(kind="check", payload={"detail": "x"})
    answer = run(agent.run_peer_task(task))
    assert task.status == "failed"
    assert "failed" in answer.lower() or "nope" in answer
