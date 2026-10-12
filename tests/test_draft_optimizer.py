"""Phase 2 evaluator-optimizer tests: critique -> revise -> keep-the-best."""
from __future__ import annotations

import asyncio
import json

from src.draft_optimizer import (
    DRAFT_REVISE_MAX_STEPS,
    DraftResult,
    draft_with_critique,
)
from src.pipeline import Pipeline


def perfect_rate(subject, body, facts=""):
    return 10, []


def weak_rate(subject, body, facts=""):
    return 4, ["no low-friction CTA question", "AI tells: em-dash"]


def climbing_rate(scores):
    """Evaluator whose score rises with each call (simulates improvement)."""
    calls = {"n": 0}

    def _rate(subject, body, facts=""):
        i = min(calls["n"], len(scores) - 1)
        calls["n"] += 1
        return scores[i], [f"iteration {i + 1} note"]

    return _rate


class FakeLLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.available = True
        self.prompts: list[str] = []

    async def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if self.replies:
            return self.replies.pop(0)
        return json.dumps({"tool": "final_answer", "args": {"answer": "done"}})


class UnavailableLLM(FakeLLM):
    def __init__(self):
        super().__init__([])
        self.available = False


def run(coro):
    return asyncio.run(coro)


def test_no_llm_returns_template_draft_unchanged():
    result = run(draft_with_critique(
        initial_subject="Missed calls", initial_body="Worth a look?",
        facts="Business: X", rate_email=weak_rate, llm=None))
    assert isinstance(result, DraftResult)
    assert result.subject == "Missed calls" and result.body == "Worth a look?"
    assert result.score == 4 and result.iterations == 1
    assert len(result.history) == 1


def test_unavailable_brain_returns_template_draft():
    result = run(draft_with_critique(
        initial_subject="Missed calls", initial_body="Worth a look?",
        facts="Business: X", rate_email=weak_rate, llm=UnavailableLLM()))
    assert result.iterations == 1 and result.score == 4


def test_revision_improving_score_wins():
    llm = FakeLLM(["Better subject\n\nBetter body with a question?"])
    result = run(draft_with_critique(
        initial_subject="S", initial_body="B", facts="F",
        rate_email=climbing_rate([4, 8]), llm=llm))
    assert result.score == 8
    assert result.subject == "Better subject"
    assert result.body.startswith("Better body")
    assert result.iterations == 2
    assert llm.prompts and "RUBRIC FINDINGS" in llm.prompts[0]
    assert "iteration 1 note" in llm.prompts[0]  # the evaluator's findings feed the revise prompt


def test_revision_never_lets_score_drop():
    llm = FakeLLM(["Worse subject\n\nWorse body no question"])
    result = run(draft_with_critique(
        initial_subject="Good", initial_body="Good body?", facts="F",
        rate_email=climbing_rate([7, 3]), llm=llm))
    assert result.subject == "Good" and result.body == "Good body?"
    assert result.score == 7  # best candidate kept


def test_loop_stops_at_target_score():
    llm = FakeLLM(["x\n\ny"])  # would be consumed if a second pass happened
    result = run(draft_with_critique(
        initial_subject="S", initial_body="B", facts="F",
        rate_email=climbing_rate([9]), llm=llm))
    assert result.score == 9
    assert llm.prompts == []  # target reached before any critique call


def test_budget_is_bounded_at_three_iterations():
    llm = FakeLLM(["r1\n\nbody one?", "r2\n\nbody two?", "r3\n\nbody three?"])
    result = run(draft_with_critique(
        initial_subject="S", initial_body="B", facts="F",
        rate_email=climbing_rate([2, 3, 4, 5]), llm=llm))
    assert result.score == 4
    assert result.iterations == DRAFT_REVISE_MAX_STEPS
    assert len(result.history) == DRAFT_REVISE_MAX_STEPS


def test_unparseable_revision_keeps_best():
    llm = FakeLLM(["no newline here at all"])
    result = run(draft_with_critique(
        initial_subject="Keep", initial_body="Keep?", facts="F",
        rate_email=climbing_rate([6]), llm=llm))
    assert result.subject == "Keep" and result.score == 6


def test_raising_brain_is_safe():
    class Raising:
        available = True

        async def complete(self, prompt):
            raise RuntimeError("boom")

    result = run(draft_with_critique(
        initial_subject="S", initial_body="B", facts="F",
        rate_email=weak_rate, llm=Raising()))
    assert result.score == 4 and result.iterations == 1


def test_fenced_revision_is_parsed():
    llm = FakeLLM(["```\nFenced Sub\n\nFenced body with a question?\n```"])
    result = run(draft_with_critique(
        initial_subject="S", initial_body="B", facts="F",
        rate_email=climbing_rate([5, 7]), llm=llm))
    assert result.subject == "Fenced Sub"
    assert result.score == 7


def test_memory_block_is_injected_into_critique_prompt():
    llm = FakeLLM(["Better\n\nBetter body?"])
    result = run(draft_with_critique(
        initial_subject="S", initial_body="B", facts="F",
        rate_email=climbing_rate([4, 6]), llm=llm,
        memory="LESSONS FROM PREVIOUS RUNS:\n1. avoid em-dashes"))
    assert any("avoid em-dashes" in p for p in llm.prompts)
    assert result.score == 6


# ---------- the real evaluator closes the loop ----------

def test_real_rate_email_gates_draft_quality():
    facts = "Business: Blue Creek Plumbing\nCity: Tampa\nGoogle rating: 4.8 (120 reviews)"
    good = ("Blue Creek team. Your 4.8 star profile in Tampa with 120 reviews "
            "suggests after-hours calls are landing in voicemail. A Tampa "
            "plumber fixed the same gap and added 11 bookings in month one. "
            "Worth a look for your shop?")
    score, notes = Pipeline.rate_email("After-hours calls", good, facts)
    assert score >= 9, notes
    bad = "We guarantee to increase revenue — click here now! Free trial."
    score_bad, notes_bad = Pipeline.rate_email("HUGE OFFER!!!", bad, facts)
    assert score_bad <= 4
    assert any("spam" in n for n in notes_bad)
