"""Phase 3 enrichment agentic-loop tests: decide -> search -> extract, budgeted."""
from __future__ import annotations

import asyncio
import json

from src.enrichment import _find_email, _llm_decide_next, _llm_extract


class FakeLLM:
    def __init__(self, replies: list[str], available: bool = True):
        self.replies = list(replies)
        self.available = available
        self.prompts: list[str] = []

    async def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if self.replies:
            return self.replies.pop(0)
        return json.dumps({"next": None})


class OffLLM(FakeLLM):
    def __init__(self):
        super().__init__([], available=False)


class FakeComposio:
    """Records queries; serves canned snippets per query."""

    def __init__(self, emails_by_query=None, snippets_by_query=None):
        self.queries: list[str] = []
        self.emails = emails_by_query or {}
        self.snippets = snippets_by_query or {}

    async def search_web(self, query: str) -> list[dict]:
        self.queries.append(query)
        if query in self.emails:
            return [{"snippet": f"mail {self.emails[query]}", "url": "s://x"}]
        if query in self.snippets:
            return [{"snippet": self.snippets[query], "url": "s://x"}]
        return []

    async def fetch_url(self, url: str) -> str:
        return "<html>no contact here</html>"


def run(coro):
    return asyncio.run(coro)


# ---------- _llm_decide_next ----------

def test_decide_returns_query_when_model_says_go():
    llm = FakeLLM([json.dumps({"next": "{name} {city} owner email"})])
    q = run(_llm_decide_next(llm, "context", ["tried one"], {"left": 5}))
    assert q == "{name} {city} owner email"
    assert llm.prompts and "QUERIES ALREADY TRIED" in llm.prompts[0]
    assert "tried one" in llm.prompts[0]


def test_decide_returns_none_when_model_says_stop():
    llm = FakeLLM([json.dumps({"next": None})])
    assert run(_llm_decide_next(llm, "ctx", [], {"left": 5})) is None


def test_decide_charges_budget():
    llm = FakeLLM([json.dumps({"next": None})])
    budget = {"left": 2}
    run(_llm_decide_next(llm, "ctx", [], budget))
    assert budget["left"] == 1


def test_decide_never_runs_when_out_of_budget():
    llm = FakeLLM([json.dumps({"next": "q"})])
    assert run(_llm_decide_next(llm, "ctx", [], {"left": 0})) is None
    assert llm.prompts == []


def test_decide_offline_brain_is_none():
    assert run(_llm_decide_next(OffLLM(), "ctx", [], {"left": 5})) is None


def test_decide_defends_against_garbage():
    llm = FakeLLM(["not json at all", "}{", json.dumps({"next": 42})])
    assert run(_llm_decide_next(llm, "ctx", [], {"left": 9})) is None
    assert run(_llm_decide_next(llm, "ctx", [], {"left": 9})) is None
    assert run(_llm_decide_next(llm, "ctx", [], {"left": 9})) is None


# ---------- wired into _find_email ----------

def test_find_email_runs_decide_loop_when_regex_misses():
    llm = FakeLLM([
        json.dumps({"email": None}),                # first extract: nothing
        json.dumps({"next": "Blue Creek owner contact"}),  # decide: one more
        json.dumps({"email": None}),                # second extract: nothing
    ])
    comp = FakeComposio(emails_by_query={"Blue Creek owner contact": ""})
    comp.emails = {}  # decide-loop query returns NO email -> still None
    lead = {"name": "Blue Creek", "city": "Tampa", "vertical": "cleaning"}
    assert run(_find_email(comp, lead, llm=llm, budget={"left": 9},
                           settings=None)) is None
    assert "Blue Creek owner contact" in comp.queries  # the decided query RAN
    assert any("is one more query likely" in p.lower() for p in llm.prompts)


def test_find_email_decided_query_can_find_the_email():
    llm = FakeLLM([
        json.dumps({"email": None}),
        json.dumps({"next": "Blue Creek Plumbing contact page"}),
    ])
    comp = FakeComposio(emails_by_query={
        "Blue Creek Plumbing contact page": "info@found.example"})
    lead = {"name": "Blue Creek Plumbing", "city": "Tampa"}
    got = run(_find_email(comp, lead, llm=llm, budget={"left": 9}, settings=None))
    assert got == "info@found.example"


def test_find_email_no_budget_skips_decide_loop():
    llm = FakeLLM([json.dumps({"email": None})])
    comp = FakeComposio()
    lead = {"name": "X Co", "city": "Tampa"}
    assert run(_find_email(comp, lead, llm=llm, budget={"left": 0},
                           settings=None)) is None
    # No LLM call at all (extract is budget-gated too), no decided query.
    # (No vertical on the lead -> only one deterministic base query.)
    assert llm.prompts == []
    assert comp.queries == ["X Co Tampa"]


def test_find_email_offline_llm_is_unchanged():
    comp = FakeComposio()
    lead = {"name": "X Co", "city": "Tampa"}
    assert run(_find_email(comp, lead, llm=OffLLM(), budget={"left": 9},
                           settings=None)) is None
    # Only the deterministic base queries ran; no decided query.
    assert comp.queries == ["X Co Tampa"]


def test_find_email_stops_deciding_after_budget_one():
    """budget left=1: extract consumes it; decide loop never runs."""
    llm = FakeLLM([json.dumps({"email": None})])
    comp = FakeComposio()
    lead = {"name": "X", "city": "Y"}
    run(_find_email(comp, lead, llm=llm, budget={"left": 1}, settings=None))
    assert len(llm.prompts) == 1  # extract only, no decide call


# ---------- extraction still defended ----------

def test_llm_extract_still_works():
    llm = FakeLLM([json.dumps({"email": "info@x.example"})])
    got = run(_llm_extract(llm, "contact us anytime", "email", {"left": 3}))
    assert got == "info@x.example"
