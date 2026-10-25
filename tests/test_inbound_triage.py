"""Phase 3 inbound triage tests: classify floor, STOP floor, CRM-history tool,
bounded actions. No real LLM, no network."""
from __future__ import annotations

import asyncio

from src.inbound_triage import (
    ACTIONS,
    build_triage_registry,
    run_inbound_triage,
    triage_reply,
)


class FakeLLM:
    def __init__(self, raw: str = "", available: bool = True):
        self.raw = raw
        self.available = available
        self.prompts: list[str] = []

    async def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.raw


class FakeCrm:
    """Duck-typed CrmAgent over a StateStore-backed timeline."""

    def __init__(self, tmp_path, rows: dict[str, dict] | None = None):
        self._loaded = False
        self._rows = rows or {}

    async def load(self):
        self._loaded = True
        return self._rows

    def find_by_lead_id(self, lead_id):
        return self._rows.get(lead_id)

    def timeline(self, lead_id):
        return [
            {"ts": "2026-09-01", "event": "email-sent", "detail": "After-hours calls"},
            {"ts": "2026-09-03", "event": "reply", "detail": "[QUESTION] who is this?"},
        ]


def run(coro):
    return asyncio.run(coro)


# ---------- triage_reply: classification floor ----------

def test_keyword_floor_without_llm():
    out = run(triage_reply(text="Please remove me from this list"))
    assert out["kind"] == "STOP"
    assert out["source"] == "keyword"
    assert "unsubscribed" in out["suggested"].lower()


def test_llm_label_wins_when_valid():
    llm = FakeLLM("price_objection\n")
    out = run(triage_reply(text="sounds interesting", llm=llm,
                           budget={"left": 3}))
    assert out["kind"] == "PRICE_OBJECTION"
    assert out["source"] == "llm"
    assert llm.prompts and "Classify" in llm.prompts[0]


def test_invalid_llm_label_falls_back_to_keywords():
    llm = FakeLLM("SOMETHING WEIRD")
    out = run(triage_reply(text="how much does it cost?", llm=llm,
                           budget={"left": 3}))
    assert out["kind"] == "PRICE_OBJECTION"
    assert out["source"] == "keyword"


def test_no_budget_goes_keyword_only():
    llm = FakeLLM("INTERESTED")
    out = run(triage_reply(text="plain text", llm=llm, budget={"left": 0}))
    assert out["source"] == "keyword"
    assert llm.prompts == []


def test_raising_llm_falls_back_to_keywords():
    class Raising(FakeLLM):
        async def complete(self, prompt):
            raise RuntimeError("boom")

    out = run(triage_reply(text="let's talk", llm=Raising(available=True),
                           budget={"left": 3}))
    assert out["kind"] == "INTERESTED"  # keyword floor caught it
    assert out["source"] == "keyword"


# ---------- run_inbound_triage: the bounded loop ----------

def test_stop_is_code_enforced_even_with_llm_disagreement():
    llm = FakeLLM("INTERESTED")  # model says interested...
    out = run(run_inbound_triage(
        text="STOP sending me emails", lead={"Lead ID": "abc"}, llm=llm,
        budget={"left": 5}))
    # keyword floor reclassifies to STOP after the bad LLM label, and the
    # action is the deterministic auto-reply regardless.
    assert out["action"] == "auto_reply"
    assert out["notify"] is False
    assert out["steps"][0].startswith("classify:")


def test_stop_skips_crm_history():
    calls = []

    crm = FakeCrm(None)

    import src.inbound_triage as mod

    orig = mod.build_triage_registry

    def spy(c):
        calls.append(1)
        return orig(c)

    mod.build_triage_registry = spy
    try:
        out = run(run_inbound_triage(text="unsubscribe me", lead={"Lead ID": "x"},
                                     crm=crm, llm=None))
    finally:
        mod.build_triage_registry = orig
    assert calls == []  # STOP path never touches the tool
    assert out["action"] == "auto_reply"


def test_interested_escalates_with_history():
    out = run(run_inbound_triage(
        text="Yes, let's talk this week",
        lead={"Lead ID": "lead-1"},
        crm=FakeCrm(None, {"lead-1": {"Status": "CONTACTED"}}),
        llm=None))
    assert out["kind"] == "INTERESTED"
    assert out["history_used"] is True
    assert out["action"] == "notify_owner_with_history"
    assert out["notify"] is True
    assert "Recent history" in out["suggested"]
    assert any("crm_history ok" in s for s in out["steps"])


def test_question_without_crm_notifies_plainly():
    out = run(run_inbound_triage(text="Do you serve the east side?",
                                 lead={"Lead ID": "lead-2"}, crm=None, llm=None))
    assert out["kind"] == "QUESTION"
    assert out["history_used"] is False
    assert out["action"] == "notify_owner"
    assert "Recent history" not in out["suggested"]


def test_action_always_in_allowed_set():
    for text in ("stop", "how much?", "yes interested", "what are your hours?"):
        out = run(run_inbound_triage(text=text, lead={}, crm=None, llm=None))
        assert out["action"] in ACTIONS


def test_reply_referencing_prior_contact_gets_history():
    out = run(run_inbound_triage(
        text="following up on your email from last week",
        lead={"Lead ID": "lead-1"},
        crm=FakeCrm(None, {"lead-1": {"Status": "DRAFTED"}}),
        llm=None))
    assert out["history_used"] is True
    assert "email-sent" in out["suggested"]


# ---------- the CRM-history tool itself ----------

def test_crm_history_tool_requires_lead_id():
    reg = build_triage_registry(FakeCrm(None))
    out = run(reg.dispatch("crm_history", {}))
    assert out["ok"] is False and "lead_id" in out["error"]


def test_crm_history_tool_not_found_is_ok():
    reg = build_triage_registry(FakeCrm(None, {}))
    out = run(reg.dispatch("crm_history", {"lead_id": "nope"}))
    assert out["ok"] is True and out["result"]["found"] is False


def test_crm_history_tool_handles_crm_failure():
    class BadCrm:
        _loaded = False

        async def load(self):
            raise RuntimeError("sheets down")

        def find_by_lead_id(self, lead_id):
            return None

    reg = build_triage_registry(BadCrm())
    out = run(reg.dispatch("crm_history", {"lead_id": "x"}))
    # dispatch wraps the tool result: outer ok=True, the tool's own error
    # inside. Either way the triage loop must treat it as "no history".
    assert out["result"].get("ok") is False
    assert "crm lookup failed" in out["result"].get("error", "")
