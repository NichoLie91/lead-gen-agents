"""Behavioral evals (Phase 2 of RESEARCH_PROPER_AGENTS.md).

Golden sets that pin the *behavior* of the deterministic layers:
- Telegram command routing + Gemini intent parsing (50 cases);
- Scout scoring rationale (30 cases, scored 0-100 against a rubric).

Wired into CI (`.github/workflows/ci.yml`) as `python -m src.evals.run` —
an eval failure fails the build exactly like a unit-test failure, so agent
behavior can't silently regress.
"""
