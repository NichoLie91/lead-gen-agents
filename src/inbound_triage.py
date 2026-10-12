"""Inbound triage loop (Phase 3 of RESEARCH_PROPER_AGENTS.md).

Wraps the existing deterministic inbound helpers (classify_reply /
suggested_reply / parse_sender_email) in a bounded agentic flow:

    classify (LLM label + keyword floor) -> act per kind -> decide whether to
    escalate with CRM history in the notification -> one bounded tool call.

The safety floor is unchanged and stays in code: STOP always auto-confirms
opt-out regardless of what the model says, nothing sends without the existing
guards, and the LLM only ever picks between a fixed set of actions. No brain /
no budget => deterministic keyword classification (house degradation rule).
"""
from __future__ import annotations

import logging

from src.inbound import KINDS, classify_reply, suggested_reply

log = logging.getLogger(__name__)

MAX_TRIAGE_STEPS = 3  # classify + (history lookup) + decide = bounded

# Actions the triage brain may take — never free-form sends.
ACTIONS = ("auto_reply", "notify_owner", "notify_owner_with_history", "no_action")


def _label_prompt(text: str) -> str:
    return (
        "Classify this lead's reply as exactly one of: INTERESTED, "
        "PRICE_OBJECTION, STOP, QUESTION. Reply with only the label.\n"
        f"Reply: {text[:800]}"
    )


def _brain_available(llm) -> bool:
    """Duck-typed availability (attr OR method) — same pattern as agent_loop."""
    try:
        avail = llm.available
        return bool(avail() if callable(avail) else avail)
    except Exception:
        return False


async def triage_reply(*, text: str, lead: dict | None = None,
                       llm=None, budget: dict | None = None) -> dict:
    """Step 1 — classify a reply, deterministically when the brain is absent.

    Returns {kind, source, suggested}. The keyword classifier is the floor:
    STOP detection in particular must never depend on the model.
    """
    kind = ""
    source = "keyword"
    # STOP floor first: an unsubscribe request is NEVER downgraded by a model
    # opinion — the keyword classifier outranks the LLM for STOP only.
    if classify_reply(text) == "STOP":
        return {
            "kind": "STOP",
            "source": "keyword",
            "suggested": suggested_reply("STOP", lead),
        }
    if llm is not None and (budget or {"left": 0}).get("left", 0) > 0 \
            and _brain_available(llm):
        budget["left"] -= 1
        try:
            raw = await llm.complete(_label_prompt(text))
        except Exception as exc:
            log.debug("triage label call failed: %s", exc)
            raw = ""
        label = (raw or "").strip().upper()
        if label in KINDS:
            kind, source = label, "llm"
    if not kind:
        kind = classify_reply(text)
    return {
        "kind": kind,
        "source": source,
        "suggested": suggested_reply(kind, lead),
    }


def crm_history_tool(crm):
    """A2A-shaped tool: last 5 CRM timeline events for one lead (PII floor:
    the CRM row itself never enters a prompt, only the timeline projection)."""

    async def crm_history(lead_id: str) -> dict:
        lead_id = (lead_id or "").strip()
        if not lead_id:
            return {"ok": False, "error": "lead_id required (sha256 hash)"}
        if not crm:
            return {"ok": False, "error": "crm unavailable"}
        try:
            if not crm._loaded:
                await crm.load()
            row = crm.find_by_lead_id(lead_id)
        except Exception as exc:
            return {"ok": False, "error": f"crm lookup failed: {exc!r}"}
        if not row:
            return {"ok": True, "found": False}
        return {
            "ok": True,
            "found": True,
            "status": row.get("Status", ""),
            "timeline": [
                f"{e.get('ts', '')} {e.get('event', '')}: {e.get('detail', '')}"
                for e in (crm.timeline(lead_id) or [])[-5:]
            ],
        }

    return crm_history


def build_triage_registry(crm=None):
    """Registry for the triage loop: one read-only CRM-history tool."""
    from src.core.tools import Tool, ToolRegistry

    reg = ToolRegistry(max_calls_per_tool=2)
    reg.register(Tool(
        name="crm_history",
        description=(
            "Last 5 timeline events + status for a lead_id hash. Use when a "
            "reply seems to reference prior contact and the owner would need "
            "that context."
        ),
        params={"lead_id": {"type": "string",
                            "description": "sha256 lead id", "required": True}},
        fn=crm_history_tool(crm),
    ))
    return reg


async def run_inbound_triage(*, text: str, lead: dict, crm=None, llm=None,
                             budget: dict | None = None) -> dict:
    """Full triage for one reply: classify -> act -> escalate decision.

    Returns {
      kind, source, suggested,
      action: one of ACTIONS,
      notify: bool — surface to the owner on Telegram,
      history_used: bool — whether the history tool ran,
      steps: bounded step trace
    }. Never raises; falls back to deterministic behavior everywhere.
    """
    budget = budget or {"left": 4}
    steps: list[str] = []

    triage = await triage_reply(text=text, lead=lead, llm=llm, budget=budget)
    kind = triage["kind"]
    steps.append(f"classify: {kind} ({triage['source']})")

    result = {
        "kind": kind,
        "source": triage["source"],
        "suggested": triage["suggested"],
        "action": "no_action",
        "notify": False,
        "history_used": False,
        "steps": steps,
    }

    # The floor: STOP is handled in code, never by model opinion.
    if kind == "STOP":
        result["action"] = "auto_reply"
        steps.append("act: auto-confirm opt-out")
        return result

    # Decide whether the owner benefits from CRM history in the notification:
    # a bounded tool call through the Phase 1 registry (never raises).
    lead_id = str((lead or {}).get("Lead ID") or "")
    history_blob = ""
    if lead_id and crm is not None and budget.get("left", 0) > 0:
        try:
            tool_result = await build_triage_registry(crm).dispatch(
                "crm_history", {"lead_id": lead_id})
            if tool_result.get("ok"):
                data = tool_result.get("result") or {}
                if data.get("found"):
                    result["history_used"] = True
                    history_blob = "; ".join(str(e) for e in (data.get("timeline") or []))
                    steps.append("tool: crm_history ok")
        except Exception as exc:
            steps.append(f"tool: crm_history failed {exc!r}")

    # The action choice is deterministic: escalate with history when we have
    # it, plain notification otherwise. The model never picks looser actions.
    result["action"] = "notify_owner_with_history" if result["history_used"] \
        else "notify_owner"
    result["notify"] = True
    if history_blob:
        result["suggested"] += f"\n\nRecent history: {history_blob[:400]}"
    steps.append(f"act: {result['action']}")
    return result
