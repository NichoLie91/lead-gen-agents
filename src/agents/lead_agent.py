"""The Lead Agent — ONE Gemini brain that owns every Telegram reply and
delegates tasks to the six-agent team.

Architecture (spec: AI employees / Step 06):
    Telegram -> LeadAgent.handle_message() -> Gemini brain
        -> whitelisted command    -> delegate() assigns work to the six agents
        -> plain-English request  -> classify_intent() (Gemini) -> delegate()
        -> question/chat          -> Gemini answers conversationally

The six agents it manages:
    1. Atlas        — lead discovery (Google Maps, via the pipeline's Atlas)
    2. Scout        — lead scoring rubric + Gemini rationale
    3. Enrichment   — contact finding + verification (email / Instagram)
    4. Outreach     — Gemini-drafted emails + IG DMs, approval flow
    5. Followups    — Day 3/7/14 cadence on the CRM
    6. Inbound      — reads lead replies, classifies, escalates

Every agent has a Gemini brain (see Atlas/MapsAgent, Scout, enrichment,
pipeline outreach/followups/inbound). Heavy stage runs execute on GitHub
Actions via GitHubAgent; lightweight work (status, drafts, approvals, CRM)
runs right here.

SAFETY (spec 5): Gemini can only ever *suggest* one of the whitelisted
commands (src/bot/commands.py) — it can never invent actions. The prompt is
PII-safe: only run metrics + draft counts, never lead data (spec 11).
"""
from __future__ import annotations

import logging

from src.agents.github_agent import GitHubAgent
from src.approvals import ApprovalQueue
from src.bot.ai import RUN_MODES, classify_intent
from src.bot.commands import build_help, format_status, parse_command
from src.core.config import Settings
from src.core.llm import GeminiClient
from src.core.state import StateStore

log = logging.getLogger(__name__)

# The six-agent team the Lead Agent manages (roster is fed to Gemini so it can
# reason about delegation and answer questions about the system).
TEAM = (
    {"name": "Atlas", "role": "Lead Discovery",
     "capabilities": "finds small local businesses via Google Maps (vertical x metro queries)"},
    {"name": "Scout", "role": "Lead Scoring",
     "capabilities": "scores ICP/intent/budget/reachability/timing 0-100 and tiers HOT/WARM/NURTURE"},
    {"name": "Enrichment", "role": "Contact Research",
     "capabilities": "finds + verifies emails and Instagram handles (web search + website fetch)"},
    {"name": "Outreach", "role": "Outreach",
     "capabilities": "drafts Gemini-personalized emails/IG DMs, queues WARM drafts for approval, sends approved sends"},
    {"name": "Followups", "role": "Follow-up Cadence",
     "capabilities": "sends Day 3/7/14 follow-ups from the CRM, polishes copy with Gemini"},
    {"name": "Inbound", "role": "Inbound Replies",
     "capabilities": "scans email for lead replies, classifies (INTERESTED/OBJECTION/STOP/QUESTION), escalates"},
)

# Which agents own each whitelisted command (delegation map). Heavy stages run
# on GitHub Actions; light ones run in-process.
COMMAND_OWNERS = {
    "/run": ("Atlas", "Scout", "Enrichment", "Outreach"),
    "/send all email": ("Outreach",),
    "/send all instagram": ("Outreach",),
    "/approve": ("Outreach",),
    "/reject": ("Outreach",),
    "/reject all": ("Outreach",),
    "/list drafts": ("Outreach",),
    "/followups": ("Followups",),
    "/inbound": ("Inbound",),
    "/status": ("Atlas", "Scout", "Enrichment", "Outreach"),
    "/stop": ("Outreach", "Followups"),
    "/sheet": ("Enrichment",),
    "/help": (),
    "/id": (),
}


def team_roster() -> str:
    """PII-safe one-liner describing the six agents (fed to Gemini)."""
    return "\n".join(
        f"- {m['name']} ({m['role']}): {m['capabilities']}" for m in TEAM
    )


class LeadAgent:
    """The single brain behind every Telegram reply; delegates to the six agents."""

    def __init__(self, settings: Settings, state: StateStore,
                 github: GitHubAgent | None = None,
                 llm: GeminiClient | None = None):
        self.settings = settings
        self.state = state
        self.github = github or GitHubAgent(settings, state)
        self.llm = llm or GeminiClient(settings.gemini_api_key, settings.gemini_model)
        self.approvals = ApprovalQueue(state)

    # ---------- brain: what Gemini knows ----------
    def _intent_context(self) -> str:
        """PII-safe context for Gemini: last-run metrics, running state, draft
        count and the six-agent roster. Never lead data (spec 11)."""
        report = self.state.load("last_run")
        running = bool(self.state.get("pipeline_running", "running", False))
        head = format_status(report, self._sheet_url(), running)
        try:
            pending = len(self.approvals.pending())
        except Exception:
            pending = 0
        return (f"{head}\nDrafts awaiting approval: {pending}\n\n"
                f"Six-agent team you manage:\n{team_roster()}")

    def _sheet_url(self) -> str:
        if self.settings.google_sheet_id:
            return f"https://docs.google.com/spreadsheets/d/{self.settings.google_sheet_id}"
        return "No live sheet yet (set GOOGLE_SHEET_ID or run the pipeline once)."

    # ---------- delegation: assign a command to the six agents ----------
    async def delegate(self, command: str, args: str, user_id: int) -> str:
        """Execute one whitelisted command by assigning it to the owning
        agent(s). Shared by the exact-slash fast path AND the Gemini brain."""
        owners = COMMAND_OWNERS.get(command, ())
        if owners:
            log.info("lead agent: %s -> %s", command, ", ".join(owners))
        if command == "/help":
            return build_help()
        if command == "/id":
            return f"Your Telegram user ID: {user_id}"
        if command == "/status":
            report = self.state.load("last_run")
            return format_status(report, self._sheet_url(),
                                 bool(self.state.get("pipeline_running", "running", False)))
        if command == "/sheet":
            return self._sheet_url()
        if command == "/run":
            if await self.github.pipeline_in_progress():
                return "A pipeline run is already in progress. Use /status or /stop."
            mode = (args or "full").strip().lower()
            if mode not in RUN_MODES:  # custom modes (e.g. "run discovery") route here
                mode = "full"
            return await self.github.trigger_pipeline(mode)
        if command == "/send all email":
            return await self.github.trigger_pipeline("outreach-email")
        if command == "/send all instagram":
            return await self.github.trigger_pipeline("outreach-ig")
        if command == "/stop":
            self.github.set_stop()
            return "Stop flag set. The running pipeline will halt between stages."
        if command == "/list drafts":
            return await self._list_drafts()
        if command == "/approve":
            return self._approve_drafts(args)
        if command == "/reject":
            return self._reject_drafts(args)
        if command == "/reject all":
            return self._reject_drafts("all")
        if command == "/inbound":
            return await self.github.trigger_pipeline("inbound")
        if command == "/followups":
            return await self.github.trigger_pipeline("followups")
        return "Unknown command. Send /help."

    # ---------- approval flow (Outreach agent, Step 04) ----------
    def _approve_drafts(self, args: str) -> str:
        if args and args.lower() != "all":
            if self.approvals.decide(args, "approved"):
                return f"✅ Approved draft {args[:8]}. It sends on the next /send all email."
            return f"No pending draft matches '{args[:16]}'. Try /list drafts."
        count = self.approvals.decide_all("approved")
        if count:
            return f"✅ Approved {count} draft(s). They send on the next /send all email."
        return "No drafts waiting for approval. WARM drafts appear after an outreach run."

    def _reject_drafts(self, args: str) -> str:
        if args and args.lower() != "all":
            if self.approvals.decide(args, "rejected"):
                return f"🚫 Rejected draft {args[:8]}."
            return f"No pending draft matches '{args[:16]}'. Try /list drafts."
        count = self.approvals.decide_all("rejected")
        if count:
            return f"🚫 Rejected {count} draft(s)."
        return "No drafts waiting for approval."

    async def _list_drafts(self) -> str:
        pending = self.approvals.pending()
        if not pending:
            return "No drafts waiting for approval. WARM leads become drafts after an outreach run."
        names: dict[str, str] = {}
        try:
            # Enrich with lead names from the private sheet (bot has Composio creds).
            from src.agents.composio_agent import ComposioAgent
            from src.agents.sheets_agent import SheetsAgent

            sheets = SheetsAgent(ComposioAgent(self.settings), self.settings, self.state)
            raw = await sheets.read_tab("Outreach")
            if raw and raw[0]:
                header = [str(h).strip() for h in raw[0]]
                lid_i = header.index("Lead ID") if "Lead ID" in header else None
                name_i = header.index("Lead") if "Lead" in header else None
                for row in raw[1:]:
                    if lid_i is not None and lid_i < len(row):
                        names[row[lid_i]] = row[name_i] if (name_i is not None and name_i < len(row)) else ""
        except Exception as exc:
            log.warning("list drafts sheet lookup failed: %s", exc)
        lines = ["Drafts awaiting approval:"]
        for lid in pending[:20]:
            lines.append(f"• `{lid[:8]}`  {names.get(lid, '')}")
        lines.append("\n/approve all — or /approve <id> · /reject <id> · /reject all")
        return "\n".join(lines)

    # ---------- conversation entry point (owns EVERY reply) ----------
    async def handle_message(self, text: str, user_id: int) -> str:
        """Exact slash commands take the deterministic fast path; everything
        else (plain English, questions, unknown /commands) goes through the
        Gemini brain — both routed here, through this single agent."""
        command, args = parse_command(text)
        if command:
            return await self.delegate(command, args, user_id)
        return await self._handle_free_text(text, user_id)

    async def _handle_free_text(self, text: str, user_id: int = 0) -> str:
        if not self.llm.available:
            return ("I only understand the listed /commands right now (no Gemini "
                    "key configured) — send /help to see them.")
        intent = await classify_intent(self.llm, text, self._intent_context())
        if intent.get("action") == "unavailable":
            return ("⚠️ Gemini is busy right now (temporary API hiccup). "
                    "Try again in a minute, or use a /command directly.")
        if not intent:
            return ("I couldn't map that to an action. Send /help, or rephrase — "
                    "e.g. \"run the pipeline\", \"approve all drafts\", "
                    "\"how did the last run go?\"")
        if intent.get("action") == "command":
            return await self.delegate(intent["command"], intent.get("args", ""), user_id)
        return intent.get("text") or "👍"
