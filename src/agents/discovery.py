"""Agent discovery + A2A-shaped task dispatch (Phase 4 of
RESEARCH_PROPER_AGENTS.md).

Two pieces:

1. Agent Cards (A2A shape): the six-agent team is declared in
   ``src/agents/cards.json`` (name, role, description, capabilities,
   input/output schema, endpoint). Anything that needs to know the team —
   the Lead Agent roster, tool descriptions, future peer-to-peer callers —
   reads the cards instead of duplicating hardcoded lists.

2. Task objects (A2A shape): internal dispatch (including peer-to-peer)
   flows through ``AgentTask``: {task_id, status: submitted/working/
   input-required/completed/failed, message, artifacts}. Status transitions
   are enforced by ``transition()``; artifacts are PII-safe by convention.

Everything here is framework-free and defensive: a missing/corrupt cards.json
degrades to an empty registry, dispatch never raises, and the mutation caps
live in the underlying agents (never bypassed by a task object).
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# A2A task lifecycle (subset we actually use).
TASK_STATUSES = ("submitted", "working", "input-required", "completed", "failed")

# Allowed transitions: forward progress + failure from anywhere live.
_ALLOWED = {
    "submitted": {"working", "failed"},
    "working": {"working", "completed", "failed", "input-required"},
    "input-required": {"working", "failed", "completed"},
}

# Who may execute which agent's work (peer-to-peer allowed for all peers).
_OWNERS = {
    "discovery": "Atlas", "score": "Scout", "scoring": "Scout",
    "enrich": "Enrichment", "draft": "Outreach", "send": "Outreach",
    "approve": "Outreach", "reject": "Outreach",
    "followups": "Followups", "inbound": "Inbound",
}

_CARDS_CACHE: dict[str, Any] | None = None


def load_cards() -> list[dict]:
    """The agent cards from src/agents/cards.json (cached). Empty on failure —
    a broken cards file must never take down the Lead Agent."""
    global _CARDS_CACHE
    if _CARDS_CACHE is not None:
        return _CARDS_CACHE.get("cards", [])
    try:
        path = Path(__file__).parent / "cards.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        _CARDS_CACHE = data if isinstance(data, dict) else {}
    except Exception as exc:
        log.warning("cards.json unreadable (%s); team discovery degraded", exc)
        _CARDS_CACHE = {}
    return _CARDS_CACHE.get("cards", [])


def card_for(agent_name: str) -> dict | None:
    """One agent's card by name (case-insensitive), or None."""
    want = (agent_name or "").strip().lower()
    return next((c for c in load_cards()
                 if str(c.get("name", "")).lower() == want), None)


def capabilities_of(agent_name: str) -> list[str]:
    card = card_for(agent_name) or {}
    caps = card.get("capabilities") or []
    return [c for c in caps if isinstance(c, str)]


def who_can(skill: str) -> list[str]:
    """Discovery by capability: every agent whose card lists ``skill``."""
    return [str(c.get("name")) for c in load_cards()
            if skill in (c.get("capabilities") or [])]


def discover_team() -> str:
    """Roster text rendered from the cards (replaces the hardcoded TEAM list
    for prompts; PII-safe by construction — cards carry no lead data)."""
    lines = []
    for card in load_cards():
        lines.append(f"- {card.get('name')} ({card.get('role')}): "
                     f"{card.get('description', '')}")
    return "\n".join(lines)


@dataclass
class AgentTask:
    """A2A-shaped task object for internal dispatch.

    status flows submitted -> working -> completed|failed (|input-required);
    ``transition`` rejects illegal jumps instead of silently corrupting state.
    ``artifacts`` stays PII-safe by convention (ids/metrics/links, never raw
    lead contact data).
    """

    kind: str                      # what to do: discovery|score|enrich|draft|send|...
    payload: dict = field(default_factory=dict)
    task_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: str = "submitted"
    message: str = ""
    artifacts: dict = field(default_factory=dict)
    assigned_to: str = ""

    def transition(self, new_status: str) -> bool:
        """Move to a new lifecycle status; returns False for illegal jumps."""
        if new_status not in TASK_STATUSES:
            return False
        if new_status == self.status:
            return True
        if new_status not in _ALLOWED.get(self.status, set()):
            return False
        self.status = new_status
        return True

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id, "kind": self.kind,
            "status": self.status, "message": self.message,
            "assigned_to": self.assigned_to, "artifacts": self.artifacts,
        }


def assign(task: AgentTask) -> AgentTask:
    """Card-based assignment: the card whose capabilities cover the task kind's
    owner skill wins. Falls back to the kind's fixed owner mapping."""
    skill = _OWNERS.get(task.kind, "")
    agent = who_can(skill)
    if agent:
        task.assigned_to = agent[0]
    else:
        task.assigned_to = skill or "Lead Agent"
    return task


async def dispatch_task(task: AgentTask, *, handler) -> AgentTask:
    """Run a task through its assigned handler with enforced transitions.

    ``handler(task) -> dict`` must return {"ok": bool, "message": str,
    "artifacts": {...}} (or anything dict-shaped). Dispatch never raises: a
    crash fails the task, it never propagates to the caller.
    """
    assign(task)
    if not task.transition("working"):
        task.message = f"illegal transition from {task.status}"
        return task
    try:
        raw = await handler(task)
        data = raw if isinstance(raw, dict) else {"ok": False, "message": f"bad handler result: {raw!r}"}
        ok = bool(data.get("ok", False))
        task.message = str(data.get("message", ""))[:500]
        artifacts = data.get("artifacts") or {}
        task.artifacts.update(artifacts if isinstance(artifacts, dict) else {})
        # failed -> completed is illegal; guard against sloppy handlers.
        task.transition("completed" if ok else "failed")
    except Exception as exc:
        task.message = f"handler failed: {exc!r}"
        task.transition("failed")
    return task
