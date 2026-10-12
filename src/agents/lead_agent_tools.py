"""Lead Agent tools (Phase 1 of RESEARCH_PROPER_AGENTS.md).

Wraps the Lead Agent's real capabilities as agent-callable tools with ACI
discipline: distinct purpose, when-to-use, failure modes. Read-only tools
expose observations; the two trigger tools route through GitHubAgent (which
guards rate limits and refuses overlapping runs). Nothing here bypasses the
whitelist floor: sending still requires approval + lint; PII never enters
prompts or state.
"""

from __future__ import annotations

from src.approvals import ApprovalQueue
from src.core.agent_loop import LoopResult, run_agent_loop
from src.core.memory import load_agent_memory, record_lesson
from src.core.state import StateStore
from src.core.tools import Tool, ToolRegistry

_TRIGGERS = ("full", "discovery", "enrichment", "scoring", "outreach-email",
             "outreach-ig", "followups", "inbound", "report")


def _fmt_status_row(row: list, header: list[str]) -> str:
    name = row[header.index("Name")] if "Name" in header and header.index("Name") < len(row) else "?"
    email = row[header.index("Email")] if "Email" in header and header.index("Email") < len(row) else "?"
    return f"{name} ({email})"


def build_lead_agent_registry(
    *,
    state: StateStore,
    approvals: ApprovalQueue,
    github,          # GitHubAgent
    settings,        # Settings — used lazily for sheet access
) -> ToolRegistry:
    """Build the Lead Agent's tool registry. Every tool returns a dict and
    never raises — failures are observations the agent can reason about."""

    reg = ToolRegistry(max_calls_per_tool=4)

    async def last_run_report() -> dict:
        report = state.load("last_run")
        if not report:
            return {"status": "no runs yet — trigger one with run_pipeline"}
        running = bool(state.get("pipeline_running", "running", False))
        return {"running": running, "report": report}

    reg.register(Tool(
        name="last_run_report",
        description=(
            "Metrics from the most recent pipeline run: candidates found, tier "
            "counts (HOT/WARM/NURTURE), emails sent/drafted/blocked, errors. "
            "Use to answer any 'how did it go / status / results' question."
        ),
        params={},
        fn=last_run_report,
    ))

    async def pending_drafts() -> dict:
        count = len(approvals.pending())
        return {
            "pending": count,
            "approved": sorted(approvals.approved())[:20],
            "rejected": sorted(approvals.rejected())[:20],
            "note": "approve/reject via trigger_pipeline('outreach-email') after /approve, "
                    "or tell the owner to use /approve all",
        }

    reg.register(Tool(
        name="pending_drafts",
        description=(
            "WARM outreach drafts awaiting owner approval (hash ids only, no "
            "PII). Use for 'any drafts? / what needs review' questions."
        ),
        params={},
        fn=pending_drafts,
    ))

    async def trigger_pipeline(mode: str = "full") -> dict:
        mode = (mode or "full").strip().lower()
        if mode not in _TRIGGERS:
            return {"ok": False, "error": f"unknown mode '{mode}'. Valid: {', '.join(_TRIGGERS)}"}
        if await github.pipeline_in_progress():
            return {"ok": False, "error": "a pipeline run is already in progress — use last_run_report or wait"}
        message = await github.trigger_pipeline(mode)
        return {"ok": True, "triggered": mode, "message": message}

    reg.register(Tool(
        name="trigger_pipeline",
        description=(
            "Trigger a pipeline run on GitHub Actions. Modes: full, discovery, "
            "enrichment, scoring, outreach-email (sends approved drafts, 50/run "
            "cap), outreach-ig (15/24h cap), followups, inbound, report. NEVER "
            "trigger outreach-* without the owner's explicit ask."
        ),
        params={"mode": {"type": "string", "description": "pipeline mode to run",
                         "required": True, "enum": list(_TRIGGERS)}},
        fn=trigger_pipeline,
    ))

    async def set_stop() -> dict:
        github.set_stop()
        return {"ok": True, "note": "stop flag set; the run halts between stages"}

    reg.register(Tool(
        name="set_stop",
        description="Halt a running pipeline between stages. Use when the owner asks to stop.",
        params={},
        fn=set_stop,
    ))

    async def crm_lookup(lead_id: str = "", email: str = "") -> dict:
        lead_id = (lead_id or "").strip()
        email = (email or "").strip()
        if not lead_id and not email:
            return {"ok": False, "error": "pass lead_id (sha256 hash) or email"}
        from src.agents.composio_agent import ComposioAgent
        from src.agents.crm_agent import CrmAgent
        from src.agents.sheets_agent import SheetsAgent
        crm = CrmAgent(SheetsAgent(ComposioAgent(settings), settings, state))
        match = crm.find_by_email(email) if email else crm.find_by_lead_id(lead_id)
        if not match:
            return {"ok": True, "found": False}
        # PII-safe projection: status/timeline shape only, no raw contact data.
        return {
            "ok": True,
            "found": True,
            "status": match.get("status", ""),
            "timeline": (match.get("timeline") or [])[-5:],
            "next_followup": match.get("next_followup", ""),
        }

    reg.register(Tool(
        name="crm_lookup",
        description=(
            "Look up a lead in the CRM by lead_id hash or email; returns status "
            "and recent timeline (PII-safe projection). Use before answering "
            "questions about a specific lead's journey."
        ),
        params={
            "lead_id": {"type": "string", "description": "sha256 lead id (8+ chars ok)"},
            "email": {"type": "string", "description": "lead email address"},
        },
        fn=crm_lookup,
    ))

    return reg


async def run_lead_agent_task(
    *,
    role: str,
    task: str,
    state: StateStore,
    approvals: ApprovalQueue,
    github,
    settings,
    brain,
    max_steps: int = 8,
) -> LoopResult:
    """One bounded agentic task for the Lead Agent: plan -> tool loop ->
    answer. Persists plan + lessons via StateStore; never raises. Returns the
    full LoopResult so the caller can fall back to deterministic routing when
    the loop stopped with no_brain."""
    registry = build_lead_agent_registry(
        state=state, approvals=approvals, github=github, settings=settings)

    def _save_plan(plan: str) -> None:
        state.save("plan_latest", {"role": role, "task": task[:200], "plan": plan})

    def _save_lessons(lesson: str) -> None:
        record_lesson(state, role, lesson)

    result = await run_agent_loop(
        role=role, task=task, registry=registry, brain=brain,
        max_steps=max_steps, save_plan=_save_plan, save_lessons=_save_lessons,
        memory=load_agent_memory(state, role),
    )
    return result
