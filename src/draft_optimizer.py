"""Evaluator-optimizer loop for outreach drafts (Phase 2 of
RESEARCH_PROPER_AGENTS.md).

Shape (Anthropic's evaluator-optimizer [1], Ng's reflection pattern [7]):
generate -> critique with `Pipeline.rate_email` -> revise with the critique in
the prompt -> repeat, bounded. The deterministic evaluator already existed
(`rate_email`) — this closes the loop. The floor stays in code:

- max 3 iterations (DRAFT_REVISE_MAX_STEPS), no model can raise it;
- every candidate is scored by the deterministic rubric, never by the model's
  own opinion of itself;
- the best-scoring candidate is returned even when no revision improves —
  a revision can only help, never hurt.

With no LLM available, returns the template draft unchanged (deterministic
degradation, house rule: no key => pipeline still runs).
"""
from __future__ import annotations

import dataclasses
import logging

log = logging.getLogger(__name__)

DRAFT_REVISE_MAX_STEPS = 3  # hard bound: initial draft + 2 critiques
DRAFT_TARGET_SCORE = 9  # stop early once a draft scores this high


@dataclasses.dataclass
class DraftResult:
    subject: str
    body: str
    score: int
    notes: list[str]
    iterations: int  # 1 = accepted the initial draft, 3 = budget exhausted
    history: list[dict]  # [{iteration, score, notes}] for the run report


def _evaluate(rate_email, subject: str, body: str, facts: str) -> tuple[int, list[str]]:
    """Score a candidate. Never raises: a broken evaluator scores 1."""
    try:
        score, notes = rate_email(subject, body, facts)
        return max(1, min(10, int(score))), list(notes or [])
    except Exception as exc:
        log.warning("rate_email failed (%s); treating as score 1", exc)
        return 1, [f"evaluator failed: {exc!r}"]


def _critique_prompt(subject: str, body: str, score: int, notes: list[str], facts: str,
                     memory: str = "") -> str:
    note_lines = "\n".join(f"- {n}" for n in notes) or "- (none — improve anyway)"
    memory_block = f"\n{memory.strip()}\n\n" if memory and memory.strip() else ""
    return (
        "Your rewritten cold email was scored by a deterministic rubric and "
        f"got {score}/10. Fix EVERY problem listed and return the full "
        "rewritten email (subject line on the first line, then a blank line, "
        "then the body). Keep every fact accurate and follow the FACTS.\n\n"
        f"RUBRIC FINDINGS:\n{note_lines}\n\n"
        f"{memory_block}"
        f"FACTS:\n{facts}\n\n"
        f"SUBJECT:\n{subject}\n\n"
        f"EMAIL:\n{body}"
    )


def _parse_rewrite(raw: str) -> tuple[str, str] | None:
    """Split 'subject line\n\nbody' into (subject, body). Defensive: None when
    the reply has no clear subject/body split or either half is empty."""
    if not raw:
        return None
    text = raw.strip()
    for fence in ("```",):
        if text.startswith(fence):
            text = text.strip(fence).removeprefix("json").strip()
    if "\n" not in text:
        return None
    subject, _, body = text.partition("\n")
    subject, body = subject.strip().strip('"'), body.strip()
    if not subject or not body or "\n" in subject:
        return None
    return subject, body


async def draft_with_critique(
    *,
    initial_subject: str,
    initial_body: str,
    facts: str,
    rate_email,   # Pipeline.rate_email — the deterministic evaluator
    llm=None,     # pro GeminiPool (or FakeLLM); None/absent => template draft
    memory: str = "",  # lessons block from previous runs (compounding memory)
    max_steps: int = DRAFT_REVISE_MAX_STEPS,
    target_score: int = DRAFT_TARGET_SCORE,
) -> DraftResult:
    """Generate -> critique -> revise, bounded; return the best candidate.

    `llm.complete(prompt)` must be async (GeminiPool shape). The initial
    (template) draft is always evaluated, so the loop can only improve on it.
    """
    max_steps = max(1, int(max_steps))
    history: list[dict] = []

    best_subject, best_body = initial_subject, initial_body
    best_score, best_notes = _evaluate(rate_email, best_subject, best_body, facts)
    history.append({"iteration": 1, "score": best_score, "notes": best_notes})

    available = False
    if llm is not None:
        try:
            avail = llm.available
            available = bool(avail() if callable(avail) else avail)
        except Exception:
            available = False

    iterations = 1
    if not available:
        return DraftResult(best_subject, best_body, best_score, best_notes,
                           iterations, history)

    subject, body = best_subject, best_body
    for step in range(2, max_steps + 1):
        if best_score >= target_score:
            break
        try:
            raw = await llm.complete(
                _critique_prompt(subject, body, best_score, best_notes, facts,
                                 memory=memory))
        except Exception as exc:
            log.warning("draft critique call failed: %s", exc)
            break
        parsed = _parse_rewrite(raw or "")
        if parsed is None:
            break  # unparseable revision -> keep the best so far
        subject, body = parsed
        score, notes = _evaluate(rate_email, subject, body, facts)
        history.append({"iteration": step, "score": score, "notes": notes})
        iterations = step
        if score > best_score:
            best_subject, best_body = subject, body
            best_score, best_notes = score, notes
        if best_score >= target_score:
            break

    return DraftResult(best_subject, best_body, best_score, best_notes,
                       iterations, history)
