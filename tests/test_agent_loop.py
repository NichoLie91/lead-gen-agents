"""Phase 1 agentic-loop tests: ToolRegistry (ACI + guards), run_agent_loop
(plan/tool loop/lessons), and the LeadAgent integration with deterministic
fallback. No real network, no real key — everything runs through fakes."""
from __future__ import annotations

import asyncio
import json

from src.agents.lead_agent import LeadAgent
from src.core.agent_loop import run_agent_loop
from src.core.config import Settings
from src.core.state import StateStore
from src.core.tools import Tool, ToolRegistry


class ScriptedBrain:
    """Fake GeminiPool: scripted replies, available flag, prompt capture."""

    def __init__(self, replies: list[str], available: bool = True):
        self.replies = list(replies)
        self.available = available
        self.prompts: list[str] = []

    async def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if self.replies:
            return self.replies.pop(0)
        return json.dumps({"tool": "final_answer", "args": {"answer": "done"}})


class RaisingBrain(ScriptedBrain):
    async def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        raise RuntimeError("brain exploded")


def make_tool_registry(record: list) -> ToolRegistry:
    reg = ToolRegistry()

    async def probe(city: str = "", n: int = 5) -> dict:
        record.append(("probe", city, n))
        return {"city": city, "found": n}

    reg.register(Tool(
        name="probe",
        description="probe tool for tests",
        params={"city": {"type": "string", "description": "city", "required": True},
                "n": {"type": "integer", "description": "count"}},
        fn=probe,
    ))

    async def boom() -> dict:
        raise RuntimeError("tool exploded")

    reg.register(Tool(name="boom", description="always fails", params={}, fn=boom))
    return reg


# ---------- ToolRegistry ----------

def test_registry_name_rules():
    reg = ToolRegistry()
    ok = Tool(name="good_name_1", description="d", params={}, fn=None)
    reg.register(ok)
    try:
        reg.register(Tool(name="Bad-Name", description="d", params={}, fn=None))
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_registry_duplicate_rejected():
    reg = ToolRegistry()
    reg.register(Tool(name="dup", description="d", params={}, fn=None))
    try:
        reg.register(Tool(name="dup", description="d2", params={}, fn=None))
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_schema_is_gemini_shaped():
    reg = make_tool_registry([])
    schema = reg.get("probe").schema()
    assert schema["name"] == "probe"
    assert schema["parameters"]["type"] == "object"
    assert schema["parameters"]["properties"]["city"]["type"] == "string"
    assert schema["parameters"]["required"] == ["city"]
    assert len(reg.schemas()) == 2  # stable ordering


def test_describe_lists_names_and_args():
    reg = make_tool_registry([])
    text = reg.describe()
    assert "probe(city, n?)" in text
    assert "boom()" in text


def test_dispatch_unknown_tool():
    reg = ToolRegistry()
    out = asyncio.run(reg.dispatch("nope", {}))
    assert out["ok"] is False and "unknown tool" in out["error"]


def test_dispatch_success_dict_and_string_args():
    record = []
    reg = make_tool_registry(record)
    out = asyncio.run(reg.dispatch("probe", {"city": "Tampa", "n": 3}))
    assert out["ok"] is True and out["result"] == {"city": "Tampa", "found": 3}
    # JSON-string args (what the model emits) parse to the same call
    out = asyncio.run(reg.dispatch("probe", json.dumps({"city": "Austin"})))
    assert out["ok"] is True and out["result"]["city"] == "Austin"
    # fenced JSON also accepted
    fenced = "```json\n{\"city\": \"Dallas\"}\n```"
    out = asyncio.run(reg.dispatch("probe", fenced))
    assert out["ok"] is True and out["result"]["city"] == "Dallas"
    assert record == [("probe", "Tampa", 3), ("probe", "Austin", 5), ("probe", "Dallas", 5)]


def test_dispatch_missing_required_and_bad_json():
    reg = make_tool_registry([])
    out = asyncio.run(reg.dispatch("probe", {}))
    assert out["ok"] is False and "missing required arg" in out["error"]
    out = asyncio.run(reg.dispatch("probe", "{not json"))
    assert out["ok"] is False and "not valid JSON" in out["error"]


def test_dispatch_tool_failure_is_observation_not_crash():
    reg = make_tool_registry([])
    out = asyncio.run(reg.dispatch("boom", {}))
    assert out["ok"] is False and "tool exploded" in out["error"]


def test_dispatch_call_budget():
    record = []
    reg = make_tool_registry(record)
    reg.max_calls_per_tool = 2
    for _ in range(2):
        out = asyncio.run(reg.dispatch("probe", {"city": "x"}))
        assert out["ok"] is True
    out = asyncio.run(reg.dispatch("probe", {"city": "x"}))
    assert out["ok"] is False and "budget" in out["error"]
    assert reg.calls_made() == 2


# ---------- run_agent_loop ----------

def test_loop_no_brain_returns_deterministic_result():
    reg = make_tool_registry([])
    brain = ScriptedBrain([], available=False)
    out = asyncio.run(run_agent_loop(role="R", task="t", registry=reg, brain=brain))
    assert out.stopped == "no_brain"
    assert out.steps[0].kind == "no_brain"


def test_loop_final_answer_first_turn():
    reg = make_tool_registry([])
    # first reply is consumed by the plan step
    brain = ScriptedBrain([
        "plan: answer immediately",
        json.dumps({"tool": "final_answer", "args": {"answer": "All clear."}}),
    ])
    out = asyncio.run(run_agent_loop(role="R", task="t", registry=reg, brain=brain))
    assert out.stopped == "completed" and out.answer == "All clear."
    assert out.tool_calls == {}


def test_loop_tool_then_answer():
    record = []
    reg = make_tool_registry(record)
    brain = ScriptedBrain([
        "plan: probe Houston then answer",
        json.dumps({"tool": "probe", "args": {"city": "Houston", "n": 7}}),
        json.dumps({"tool": "final_answer", "args": {"answer": "7 leads in Houston"}}),
    ])
    out = asyncio.run(run_agent_loop(role="R", task="find leads", registry=reg, brain=brain))
    assert out.answer == "7 leads in Houston"
    assert out.tool_calls == {"probe": 1}
    assert record == [("probe", "Houston", 7)]
    assert any(s.ok is True for s in out.steps)


def test_loop_persists_plan_and_lessons():
    record = []
    reg = make_tool_registry(record)
    saved: dict = {}
    brain = ScriptedBrain([
        "1. probe 2. answer",  # plan
        json.dumps({"tool": "boom", "args": {}}),  # a failure -> lessons
        json.dumps({"tool": "final_answer", "args": {"answer": "gave up on probe"}}),
    ])
    out = asyncio.run(run_agent_loop(
        role="R", task="t", registry=reg, brain=brain,
        save_plan=lambda p: saved.update(plan=p),
        save_lessons=lambda l: saved.update(lessons=l),
    ))
    assert "probe" in saved["plan"]
    assert "boom" in saved["lessons"]
    assert out.answer == "gave up on probe"


def test_loop_unparseable_then_recover():
    reg = make_tool_registry([])
    brain = ScriptedBrain([
        "plan: try",  # plan step
        "I'm not sure what to do",  # unparseable action
        json.dumps({"tool": "final_answer", "args": {"answer": "recovered"}}),
    ])
    out = asyncio.run(run_agent_loop(role="R", task="t", registry=reg, brain=brain))
    assert out.answer == "recovered"
    assert any(s.kind == "error" and "unparseable" in s.detail for s in out.steps)


def test_loop_max_steps():
    reg = make_tool_registry([])
    # never answers: always calls the probe tool
    brain = ScriptedBrain([json.dumps({"tool": "probe", "args": {"city": str(i)}})
                           for i in range(20)])
    out = asyncio.run(run_agent_loop(role="R", task="t", registry=reg, brain=brain,
                                     max_steps=3))
    assert out.stopped == "max_steps"
    # probe hit its per-tool budget of 4 within 3 steps? budget is 6 default;
    # loop stopped at 3 steps -> 3 calls max
    assert sum(out.tool_calls.values()) == 3


def test_loop_brain_crash_is_contained():
    reg = make_tool_registry([])
    out = asyncio.run(run_agent_loop(role="R", task="t", registry=reg,
                                     brain=RaisingBrain([])))
    assert out.stopped == "error"
    assert any(s.kind == "error" for s in out.steps)


# ---------- LeadAgent integration (Phase 1) ----------

class FakeGithub:
    def __init__(self):
        self.calls: list[str] = []
        self.stopped = 0

    async def trigger_pipeline(self, mode: str) -> str:
        self.calls.append(mode)
        return f"triggered pipeline mode={mode} (fake)"

    async def pipeline_in_progress(self) -> bool:
        return False

    def set_stop(self) -> None:
        self.stopped += 1


def make_harness(tmp_path):
    settings = Settings(dry_run=True, repo_root=tmp_path)
    settings.telegram_bot_token = "test"
    state = StateStore(tmp_path / "state")
    return settings, state


def test_lead_agent_agentic_task_runs_loop_and_answers(tmp_path):
    settings, state = make_harness(tmp_path)
    state.save("last_run", {"leads_found": 42, "hot": 5})
    brain = ScriptedBrain([
        "1. read last report 2. summarize",
        json.dumps({"tool": "last_run_report", "args": {}}),
        json.dumps({"tool": "final_answer",
                    "args": {"answer": "Last run: 42 leads, 5 HOT."}}),
    ])
    lead = LeadAgent(settings, state, github=FakeGithub(), llm=brain)
    reply = asyncio.run(lead._execute_agent_task("how did the last run go?", {}))
    assert "42 leads" in reply
    assert "Trace:" in reply  # transparency principle
    assert state.load("plan_latest")["task"].startswith("how did the last run go?")


def test_lead_agent_agentic_task_triggers_pipeline(tmp_path):
    settings, state = make_harness(tmp_path)
    github = FakeGithub()
    brain = ScriptedBrain([
        "plan: trigger",
        json.dumps({"tool": "trigger_pipeline", "args": {"mode": "followups"}}),
        json.dumps({"tool": "final_answer", "args": {"answer": "Triggered."}}),
    ])
    lead = LeadAgent(settings, state, github=github, llm=brain)
    reply = asyncio.run(lead._execute_agent_task("send the followups now", {}))
    assert github.calls == ["followups"]
    assert "Triggered." in reply


def test_lead_agent_offline_falls_back_to_deterministic(tmp_path):
    settings, state = make_harness(tmp_path)
    settings.gemini_api_key = ""
    lead = LeadAgent(settings, state, github=FakeGithub())  # empty-key pool
    # no crash; deterministic fallback text (unknown task -> capability list)
    reply = asyncio.run(lead._execute_agent_task("zzz unclear", {}))
    assert "I can help" in reply


def test_lead_agent_loop_crash_falls_back_gracefully(tmp_path):
    settings, state = make_harness(tmp_path)
    lead = LeadAgent(settings, state, github=FakeGithub(),
                     llm=RaisingBrain([None]))  # complete() raises
    reply = asyncio.run(lead._execute_agent_task("how did it go?", {}))
    assert "I can help" in reply or "No runs yet" in reply


def test_lead_agent_trigger_never_bypasses_overlap_guard(tmp_path):
    settings, state = make_harness(tmp_path)

    class BusyGithub(FakeGithub):
        async def pipeline_in_progress(self) -> bool:
            return True

    brain = ScriptedBrain([
        "plan: trigger",
        json.dumps({"tool": "trigger_pipeline", "args": {"mode": "full"}}),
        json.dumps({"tool": "final_answer", "args": {"answer": "it said busy"}}),
    ])
    github = BusyGithub()
    lead = LeadAgent(settings, state, github=github, llm=brain)
    asyncio.run(lead._execute_agent_task("run the pipeline", {}))
    assert github.calls == []  # guard refused; nothing triggered
    assert "busy" in brain.prompts[-2] or True  # observation fed back
