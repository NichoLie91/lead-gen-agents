"""Bounded observe-reason-act loop for runtime agents (Phase 1 of
RESEARCH_PROPER_AGENTS.md).

Shape (Anthropic, "Building Effective Agents"): agents are LLMs using tools
based on environmental feedback in a loop. This module is that loop, once,
shared by every runtime agent — Lead Agent first (Phase 1), Enrichment and
Inbound later (Phase 3).

Safety model — the code floor holds regardless of what the model does:
- steps are hard-capped (`max_steps`);
- every tool call goes through ToolRegistry.dispatch (call budgets, defensive
  parsing, never raises);
- every Gemini call goes through GeminiPool (failover chain, usage recording,
  deterministic fallback when no key is available);
- the result carries a full step trace so run reports can show the work, and
  `save_plan`/`save_lessons` callbacks persist plan + lessons via StateStore
  (memory that survives the ephemeral Actions job).

Deterministic degradation (house rule: no key => pipeline still runs): with
`brain.available()` false, run() returns immediately without calling Gemini.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from src.core.tools import ToolRegistry

# The loop prompt: role + task + observations + tool contract + stop rules.
_PROMPT_TMPL = """You are {role}, part of the lead-gen-agents system.

{memory}TASK
{task}

TOOL RESULTS SO FAR
{observations}

TOOLS
{tools}

RULES
1. Call ONE tool per turn with a JSON object of arguments. Use the
   `final_answer` tool when you can finish without another call.
2. Tool results arrive as {{"ok": true, "result": ...}} or {{"ok": false,
   "error": "..."}}. On an error, either change your approach or finish —
   never retry the identical call.
3. Never invent data. If a fact is unknown, say so in the final answer
   (the system marks gaps NEEDS_ENRICHMENT / NEEDS_VERIFICATION instead of
   guessing, and so do you).
4. Stop as soon as the task is done. Remaining step budget: {steps_left}.
5. Final answers are plain text the owner (or another agent) will read:
   lead with the outcome, then the specifics, then anything blocked.
"""


@dataclass
class Step:
    """One observation from the loop, for the trace and the run report."""

    step: int
    kind: str  # "tool_call" | "error" | "final" | "max_steps" | "no_brain"
    detail: str = ""
    ok: bool | None = None


@dataclass
class LoopResult:
    answer: str
    steps: list[Step] = field(default_factory=list)
    tool_calls: dict = field(default_factory=dict)
    stopped: str = "completed"  # completed | max_steps | no_brain | error

    @property
    def trace(self) -> str:
        return "\n".join(f"  {s.step:>2} {s.kind:<9} {s.detail}"[:200] for s in self.steps)


async def run_agent_loop(
    *,
    role: str,
    task: str,
    registry: ToolRegistry,
    brain: Any,
    max_steps: int = 8,
    save_plan: Callable[[str], None] | None = None,
    save_lessons: Callable[[str], None] | None = None,
    memory: str = "",
) -> LoopResult:
    """Run the bounded loop. Never raises; failures come back as a result.

    `brain` is the pro GeminiPool (or any object exposing `available()`,
    `complete(prompt) -> str`, matching src/core/llm.GeminiPool).
    `save_plan`/`save_lessons` receive the plan and the lessons text and
    should persist them (StateStore keys, per Anthropic's plan-to-memory
    lesson) — best-effort, never fatal.
    """
    steps: list[Step] = []
    if not _brain_available(brain):
        steps.append(Step(0, "no_brain", "LLM unavailable — deterministic fallback; loop skipped"))
        return LoopResult(
            answer="Brain unavailable; no agentic loop ran (deterministic fallback).",
            steps=steps,
            stopped="no_brain",
        )

    # The model declares its plan up front (transparency principle), and the
    # caller can persist it before any tool runs.
    plan = ""
    try:
        plan = await brain.complete(
            f"Task: {task}\n"
            "Write a 3-line plan: what you will do, which tools in"
            f" {registry.names()} you will use, what the final answer looks like."
        ) or ""
    except Exception as exc:
        steps.append(Step(0, "error", f"plan step failed: {exc!r}"))
    if plan and save_plan is not None:
        try:
            save_plan(plan)
        except Exception as exc:  # plan persistence is best-effort
            steps.append(Step(0, "error", f"plan persistence failed: {exc!r}"))

    observations: list[str] = []
    for step_no in range(1, max_steps + 1):
        prompt = _PROMPT_TMPL.format(
            role=role,
            task=task,
            memory=f"{memory.strip()}\n\n" if memory and memory.strip() else "",
            observations="\n".join(observations) or "(none yet)",
            tools=registry.describe() or "(no tools registered)",
            steps_left=max_steps - step_no + 1,
        )
        try:
            raw = await brain.complete(prompt)
        except Exception as exc:
            steps.append(Step(step_no, "error", f"brain.complete failed: {exc!r}"))
            return LoopResult(
                answer="Agent loop stopped: the brain call failed.",
                steps=steps,
                tool_calls=dict(registry.calls),
                stopped="error",
            )
        action = _parse_action(raw)
        if action is None:
            observations.append(
                f"step {step_no}: unparseable action {raw!r:.120} — "
                'reply with {"tool": name, "args": {...}} or {"tool": "final_answer", "args": {"answer": "..."}}'
            )
            steps.append(Step(step_no, "error", "unparseable action"))
            continue

        name, args = action
        if name == "final_answer":
            answer = str(args.get("answer", "")).strip()
            steps.append(Step(step_no, "final", (answer or "(empty)")[:160]))
            _persist_lessons(save_lessons, task, steps, answer)
            return LoopResult(answer=answer, steps=steps, tool_calls=dict(registry.calls))

        result = await registry.dispatch(name, args)
        if result.get("ok"):
            observations.append(f"step {step_no} {name} -> OK: {json.dumps(result['result'])[:600]}")
            steps.append(Step(step_no, "tool_call", f"{name}: ok", ok=True))
        else:
            observations.append(f"step {step_no} {name} -> ERROR: {result.get('error', '')[:300]}")
            steps.append(Step(step_no, "tool_call", f"{name}: {result.get('error', '')[:120]}", ok=False))

    steps.append(Step(max_steps, "max_steps", f"step budget of {max_steps} exhausted"))
    _persist_lessons(save_lessons, task, steps, "")
    return LoopResult(
        answer="Step budget exhausted before a final answer. Partial trace attached.",
        steps=steps,
        tool_calls=dict(registry.calls),
        stopped="max_steps",
    )


def _parse_action(raw: Any) -> tuple[str, dict] | None:
    """Parse the model's action: JSON dict (bare or fenced) with a `tool` key.

    Returns (tool_name, args_dict) or None if unparseable. Never raises.
    """
    text = raw if isinstance(raw, str) else json.dumps(raw)
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.removeprefix("json")
        text = text.strip()
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("tool"), str):
        return None
    args = data.get("args", {})
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = {"answer": args}
    if not isinstance(args, dict):
        args = {}
    return data["tool"], args


def _brain_available(brain: Any) -> bool:
    """Duck-typed availability: GeminiPool/FakeLLM expose `available` as an
    attribute; other brains may expose a method. Never raises."""
    try:
        avail = brain.available
        return bool(avail() if callable(avail) else avail)
    except Exception:
        return False


def _persist_lessons(save_lessons, task: str, steps: list[Step], answer: str) -> None:
    """Write lessons if the run hit an error pattern worth remembering."""
    errors = [s for s in steps if s.kind == "error" or s.ok is False]
    if not errors or save_lessons is None:
        return
    lessons = "; ".join(s.detail[:80] for s in errors[:3])
    try:
        save_lessons(f"task={task[:80]} | errors={lessons} | outcome={answer[:80] or 'budget exhausted'}")
    except Exception as exc:  # lessons persistence is best-effort
        logging.getLogger(__name__).warning("lessons persistence failed: %s", exc)
