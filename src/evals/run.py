"""Behavioral eval runner (Phase 2): fails the build when pinned behavior regresses.

Usage: python -m src.evals.run
Exit 0 = all golden cases pass; exit 1 = N failures (details printed).

Deterministic layers only — no network, no key needed, so CI can run it.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from src.evals.golden_routing import GOLDEN_ROUTING
from src.evals.golden_scoring import GOLDEN_SCORING


class _FakeGithub:
    """Minimal duck-type for LeadAgent construction; records triggers."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def trigger_pipeline(self, mode: str) -> str:
        self.calls.append(mode)
        return f"triggered pipeline mode={mode} (fake)"

    async def pipeline_in_progress(self) -> bool:
        return False

    def set_stop(self) -> None:
        pass


def _make_agent():
    from src.agents.lead_agent import LeadAgent
    from src.core.config import Settings
    from src.core.state import StateStore

    tmp = tempfile.mkdtemp(prefix="leadgen-evals-")
    settings = Settings(dry_run=True, repo_root=Path(tmp))
    settings.telegram_bot_token = "eval"
    state = StateStore(Path(tmp) / "state")
    github = _FakeGithub()
    agent = LeadAgent(settings, state, github=github)

    # Replace the heavy task methods with recorders: the keyword-routing eval
    # pins WHICH agent the router picks, not the work itself (no LLM, no IO).
    calls: list[str] = []

    def _recorder(name):
        async def _inner(params=None):
            calls.append(name)
            return f"{name} ok"
        return _inner

    for name in ("_task_discover", "_task_score", "_task_draft",
                 "_task_check", "_task_filter_leads"):
        setattr(agent, name, _recorder(name))
    return agent, github, calls


def _check_routing_case(agent, github, calls: list[str],
                        message: str, expected: str) -> tuple[bool, str]:
    """Run one golden case; returns (ok, detail-on-failure)."""
    calls.clear()
    if expected.startswith("command:"):
        from src.bot.commands import parse_command

        tail = expected[len("command:"):]
        cmd, _, args = tail.partition(":")
        got_cmd, got_args = parse_command(message)
        if got_cmd != cmd or got_args != args:
            return False, f"parse_command({message!r}) -> ({got_cmd!r}, {got_args!r}), want ({cmd!r}, {args!r})"
        return True, ""

    if expected.startswith("intent:"):
        from src.bot.ai import parse_intent_response

        got = parse_intent_response(message)
        tail = expected[len("intent:"):]
        if tail == "invalid":
            if got != {}:
                return False, f"parse_intent_response({message!r}) -> {got!r}, want {{}}"
            return True, ""
        kind = tail.split(":", 1)[0]
        if got.get("action") != kind:
            return False, f"parse_intent_response({message!r}) action={got.get('action')!r}, want {kind!r}"
        if kind == "command":
            rest = tail[len("command:"):]
            cmd, _, args = rest.partition(":")
            if got.get("command") != cmd or got.get("args", "") != args:
                return False, f"got {got!r}, want command={cmd!r} args={args!r}"
        return True, ""

    if expected.startswith("route:"):
        import asyncio

        want = expected[len("route:"):]
        asyncio.run(agent._execute_agent_task(message, {}))
        if want == "send":
            if github.calls != ["outreach-email"]:
                return False, f"router triggered {github.calls!r}, want ['outreach-email']"
            return True, ""
        if not calls or calls[0] != want:
            return False, f"router called {calls!r}, want {[want]!r}"
        return True, ""

    return False, f"unknown expected route {expected!r}"


def run_routing_evals() -> tuple[int, int]:
    agent, github, calls = _make_agent()
    passed = failed = 0
    for message, expected in GOLDEN_ROUTING:
        try:
            ok, detail = _check_routing_case(agent, github, calls, message, expected)
        except Exception as exc:  # a crash IS a failure
            ok, detail = False, f"raised {exc!r}"
        if ok:
            passed += 1
        else:
            failed += 1
            print(f"FAIL routing [{message[:60]!r}] expected {expected}: {detail}")
    return passed, failed


def run_scoring_evals() -> tuple[int, int]:
    from src.agents.scout import score_lead, scoring_rationale

    passed = failed = 0
    for case_id, lead, expected in GOLDEN_SCORING:
        try:
            score = score_lead(lead)
            rationale = scoring_rationale(score, lead)
            problems = []
            for key, want in expected.items():
                if key == "rationale_must_include":
                    for frag in want:
                        if frag not in rationale:
                            problems.append(f"rationale missing {frag!r}")
                elif score.get(key) != want:
                    problems.append(f"{key}: got {score.get(key)!r}, want {want!r}")
            ok, detail = (not problems), "; ".join(problems)
        except Exception as exc:
            ok, detail = False, f"raised {exc!r}"
        if ok:
            passed += 1
        else:
            failed += 1
            print(f"FAIL scoring [{case_id}]: {detail}")
    return passed, failed


def main() -> int:
    r_passed, r_failed = run_routing_evals()
    s_passed, s_failed = run_scoring_evals()
    total_failed = r_failed + s_failed
    print(f"Evals: routing {r_passed} passed / {r_failed} failed | "
          f"scoring {s_passed} passed / {s_failed} failed")
    if total_failed:
        print(f"::error::{total_failed} behavioral eval(s) failed")
    return 1 if total_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
