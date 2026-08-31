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
from src.core.llm import GeminiClient, GeminiKeyState, GeminiPool, LLMUsage
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
    "/usage": (),
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
                 llm: GeminiClient | GeminiPool | None = None):
        self.settings = settings
        self.state = state
        self.github = github or GitHubAgent(settings, state)
        # Default brain is a recording pro pool so even direct constructions
        # feed the /usage dashboard (empty-key pools are available=False).
        self.llm = llm or GeminiPool(settings, role="pro",
                                     usage=LLMUsage(settings.state_dir))
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
        if command == "/list drafts" or command == "/drafts":
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
        if command == "/usage":
            return self._usage_report(args)
        return "Unknown command. Send /help."

    # ---------- dashboard: which Gemini key/model handled recent calls ----------
    def _usage_report(self, n: str = "") -> str:
        from src.core.llm import LLMUsage

        usage = LLMUsage(self.settings.state_dir)
        try:
            count = max(1, min(int(n.strip() or 20), 100))
        except ValueError:
            count = 20
        calls = usage.recent(count)
        if not calls:
            return ("No Gemini calls recorded yet. Runs and bot replies will "
                    "show up here — e.g. /usage 30.")
        lines = [f"📊 Gemini usage — last {len(calls)} calls:"]
        for call in reversed(calls):
            mark = "✅" if call.get("ok") else "⚠️"
            lines.append(f"{mark} {call.get('key')} · {call.get('model')} · "
                         f"{call.get('role')} · {call.get('ms')}ms")
        totals = usage.totals(len(calls))
        by_key = " · ".join(f"{k}={v}" for k, v in sorted(totals["by_key"].items()))
        lines.append(f"Totals: {by_key} · ok={totals['ok']} · failed={totals['failed']}")
        # Circuit-breaker status: which key is leading and which is parked.
        try:
            health = GeminiKeyState(self.settings.state_dir).health()
            if health:
                parts = []
                for key in sorted(health):
                    h = health[key]
                    if h["state"] == "healthy":
                        parts.append(f"{key} ✅ healthy")
                    else:
                        parts.append(f"{key} ⏳ cooling ({h['failures']}×429)")
                lines.append("Key status: " + " · ".join(parts))
        except Exception as exc:
            log.warning("key status lookup failed: %s", exc)
        return "\n".join(lines)

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
        """Show drafts from both the approval queue and the Drafts tab."""
        pending = self.approvals.pending()
        lines: list[str] = []

        # Show from the Drafts sheet tab.
        try:
            from src.agents.composio_agent import ComposioAgent
            from src.agents.sheets_agent import SheetsAgent

            sheets = SheetsAgent(ComposioAgent(self.settings), self.settings, self.state)
            raw = await sheets.read_tab("Drafts")
            if raw and len(raw) > 1:
                header = [str(h).strip() for h in raw[0]]
                name_i = header.index("Lead") if "Lead" in header else None
                email_i = header.index("Email") if "Email" in header else None
                score_i = header.index("Score") if "Score" in header else None
                tier_i = header.index("Tier") if "Tier" in header else None
                status_i = header.index("Status") if "Status" in header else None
                pending_rows = []
                for row in raw[1:]:
                    status = row[status_i] if (status_i is not None and status_i < len(row)) else ""
                    if status in ("NEEDS_APPROVAL", "APPROVED"):
                        pending_rows.append(row)
                if pending_rows:
                    lines.append(f"📋 Drafts tab — {len(pending_rows)} pending:")
                    for row in pending_rows[:15]:
                        name = row[name_i] if (name_i is not None and name_i < len(row)) else "?"
                        email = row[email_i] if (email_i is not None and email_i < len(row)) else "?"
                        score = row[score_i] if (score_i is not None and score_i < len(row)) else "?"
                        tier = row[tier_i] if (tier_i is not None and tier_i < len(row)) else "?"
                        lines.append(f"  • {name} ({email}) — score {score} {tier}")
                else:
                    lines.append("📋 Drafts tab: no pending drafts")
            else:
                lines.append("📋 Drafts tab: empty (run the pipeline to create drafts)")
        except Exception as exc:
            log.warning("list drafts sheet lookup failed: %s", exc)

        # Also show from the approval queue.
        if pending:
            if not lines:
                lines.append("Drafts awaiting approval:")
            for lid in pending[:10]:
                lines.append(f"• `{lid[:8]}`")

        if not lines:
            return "No drafts waiting for approval. WARM leads become drafts after an outreach run."
        lines.append("\n/approve all — or /approve <id> · /reject <id> · /reject all")
        lines.append("/send all email — send all approved drafts")
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
        if intent.get("action") == "agent":
            return await self._execute_agent_task(
                intent.get("task", ""), intent.get("params", {}))
        return intent.get("text") or "👍"

    async def _execute_agent_task(self, task: str, params: dict) -> str:
        """Execute a custom agent task that Gemini decided needs doing.

        This is the bridge between Gemini's intent brain and the six-agent
        team. Gemini describes what needs to happen; we route it to the
        right agent(s) and return the result.
        """
        task_lower = task.lower()

        # Check if this is a filtering/scoring request first (score filters
        # take priority over generic discover).
        has_score_filter = any(k in params for k in (
            "min_score", "max_score", "tier", "limit"))
        if has_score_filter or any(w in task_lower for w in (
                "filter", "95+", "above", "below", "exclude", "only")):
            return await self._task_filter_leads(params)

        # Route to the right agent based on the task description.
        if any(w in task_lower for w in ("find", "search", "discover", "maps")):
            return await self._task_discover(params)
        if any(w in task_lower for w in ("score", "rating", "rubric")):
            return await self._task_score(params)
        if any(w in task_lower for w in ("draft", "email", "write", "outreach")):
            return await self._task_draft(params)
        if any(w in task_lower for w in ("check", "list", "show", "what leads")):
            return await self._task_check(params)
        if any(w in task_lower for w in ("send", "flush", "approved")):
            return await self.github.trigger_pipeline("outreach-email")
        if any(w in task_lower for w in ("status", "report", "how")):
            return await self.delegate("/status", "", 0)
        # Fallback: describe what we can do.
        return ("I can help with that. Try these:\n"
                "• 'find plumber leads in Dallas' — Atlas searches Google Maps\n"
                "• 'score ABC Plumbing 4.8 stars 120 reviews' — Scout scores it\n"
                "• 'draft an email for a dental clinic in Atlanta' — Outreach drafts\n"
                "• 'what leads do we have in Tampa?' — check the sheets\n"
                "• 'find me 300 leads with 95+ score' — filter by score\n"
                "• 'run the pipeline' — trigger a full pipeline run\n"
                "• 'send approved emails' — flush pending drafts")

    async def _task_discover(self, params: dict) -> str:
        """Run Atlas to find leads matching the user's criteria."""
        vertical = params.get("vertical", "")
        city = params.get("city", "")
        if not vertical and not city:
            return "I need at least a vertical (plumber, hvac, etc) or a city. Example: 'find plumber leads in Dallas'"
        # Trigger a targeted pipeline run and report the result.
        mode = "discovery"
        result = await self.github.trigger_pipeline(mode)
        extras = []
        if vertical:
            extras.append(f"Vertical: {vertical}")
        if city:
            extras.append(f"City: {city}")
        if extras:
            result += "\n\n" + " | ".join(extras)
        return result

    async def _task_filter_leads(self, params: dict) -> str:
        """Filter leads from the Score tab by score, tier, city, or vertical.

        Gemini passes parameters like min_score=95, tier=HOT-VERIFIED,
        city=Dallas, vertical=plumber, limit=300. We read the Score tab,
        apply filters, and return the results.
        """
        min_score = int(params.get("min_score", 0) or 0)
        max_score = int(params.get("max_score", 100) or 100)
        tier_filter = params.get("tier", "")
        city_filter = params.get("city", "")
        vertical_filter = params.get("vertical", "")
        limit = int(params.get("limit", 50) or 50)

        try:
            from src.agents.composio_agent import ComposioAgent
            from src.agents.sheets_agent import SheetsAgent
            sheets = SheetsAgent(ComposioAgent(self.settings), self.settings, self.state)

            # Read the Score tab.
            score_rows = await sheets.read_tab("Score")
            if not score_rows or len(score_rows) < 2:
                return "No scored leads yet. Run /run first to discover and score leads."

            header = [str(h).strip() for h in score_rows[0]]
            total_i = header.index("Total") if "Total" in header else None
            tier_i = header.index("Tier") if "Tier" in header else None
            name_i = header.index("Lead") if "Lead" in header else None

            # Also read Pipeline tab for city/vertical info.
            pipeline_rows = await sheets.read_tab("Pipeline")
            pipeline_map: dict[str, dict] = {}
            if pipeline_rows and len(pipeline_rows) > 1:
                p_header = [str(h).strip() for h in pipeline_rows[0]]
                p_name_i = p_header.index("Lead") if "Lead" in p_header else None
                p_city_i = p_header.index("City-State") if "City-State" in p_header else None
                p_cat_i = p_header.index("Category") if "Category" in p_header else None
                for row in pipeline_rows[1:]:
                    name = row[p_name_i] if (p_name_i is not None and p_name_i < len(row)) else ""
                    if name:
                        pipeline_map[str(name)] = {
                            "city": str(row[p_city_i]) if (p_city_i is not None and p_city_i < len(row)) else "",
                            "vertical": str(row[p_cat_i]) if (p_cat_i is not None and p_cat_i < len(row)) else "",
                        }

            # Apply filters.
            filtered = []
            for row in score_rows[1:]:
                name = str(row[name_i]) if (name_i is not None and name_i < len(row)) else "?"
                total = int(row[total_i]) if (total_i is not None and total_i < len(row) and str(row[total_i]).isdigit()) else 0
                tier = str(row[tier_i]) if (tier_i is not None and tier_i < len(row)) else "?"

                # Score filter.
                if total < min_score:
                    continue
                if total > max_score:
                    continue
                # Tier filter.
                if tier_filter and tier.upper() != tier_filter.upper():
                    continue
                # City filter.
                info = pipeline_map.get(name, {})
                if city_filter and city_filter.lower() not in info.get("city", "").lower():
                    continue
                # Vertical filter.
                if vertical_filter and vertical_filter.lower() not in info.get("vertical", "").lower():
                    continue
                filtered.append({
                    "name": name, "score": total, "tier": tier,
                    "city": info.get("city", ""), "vertical": info.get("vertical", ""),
                })

            # Sort by score descending and apply limit.
            filtered.sort(key=lambda x: x["score"], reverse=True)
            total_found = len(filtered)
            filtered = filtered[:limit]

            if not filtered:
                filters_desc = []
                if min_score:
                    filters_desc.append(f"score >= {min_score}")
                if tier_filter:
                    filters_desc.append(f"tier={tier_filter}")
                if city_filter:
                    filters_desc.append(f"city={city_filter}")
                if vertical_filter:
                    filters_desc.append(f"vertical={vertical_filter}")
                return (f"No leads match your filters: {', '.join(filters_desc)}.\n"
                        f"Total leads in Score tab: {len(score_rows) - 1}\n"
                        "Try relaxing your filters or run /run to discover more leads.")

            # Format results.
            lines = [f"📊 Filtered leads ({total_found} total, showing top {len(filtered)}):\n"]
            for i, lead in enumerate(filtered[:20], 1):
                lines.append(
                    f"{i}. {lead['name']} — score {lead['score']} [{lead['tier']}] "
                    f"({lead['city']}, {lead['vertical']})")
            if total_found > 20:
                lines.append(f"\n... and {total_found - 20} more")
            if tier_filter:
                lines.append(f"\nFilter: tier={tier_filter}")
            if min_score:
                lines.append(f"Filter: score >= {min_score}")
            lines.append("\nTo email these: /send all email")
            lines.append("To draft emails: /run outreach-email")
            return "\n".join(lines)

        except Exception as exc:
            log.warning("filter leads failed: %s", exc)
            return f"Filter failed: {exc}. Try /status or /run first."

    async def _task_score(self, params: dict) -> str:
        """Score a specific business using Scout's rubric."""
        name = params.get("business_name", params.get("name", ""))
        rating = params.get("rating", params.get("stars", ""))
        reviews = params.get("reviews", params.get("review_count", ""))
        city = params.get("city", "")
        vertical = params.get("vertical", "")
        if not name:
            return "I need the business name. Example: 'score ABC Plumbing 4.8 stars 120 reviews in Dallas'"
        # Build a synthetic lead for Scout to score.
        from src.agents.scout import Scout
        scout = Scout(self.settings, llm=self.llm)
        lead = {
            "name": name,
            "city": city or "unknown",
            "vertical": vertical or "service",
            "rating": float(rating) if rating else 0,
            "reviews": int(reviews) if reviews else 0,
            "website": params.get("website", ""),
            "email": params.get("email", ""),
            "instagram": params.get("instagram", ""),
        }
        scored = scout.run([lead])
        if not scored:
            return "Scoring failed. Check the business details."
        s = scored[0]["score"]
        return (
            f"📊 Score for {name}:\n"
            f"ICP: {s['icp']}/25 | Intent: {s['intent']}/25 | "
            f"Budget: {s['budget']}/20 | Reachability: {s['reachability']}/15 | "
            f"Timing: {s['timing']}/15\n"
            f"Total: {s['total']}/100 → {s['tier']}"
        )

    async def _task_draft(self, params: dict) -> str:
        """Draft a personalized email for a specific business."""
        name = params.get("business_name", params.get("name", ""))
        if not name:
            return "I need the business name. Example: 'draft an email for ABC Plumbing in Dallas'"
        from src.pipeline import Pipeline
        pipe = Pipeline(self.settings)
        lead = {
            "name": name,
            "city": params.get("city", ""),
            "vertical": params.get("vertical", "service"),
            "rating": params.get("rating", "4"),
            "reviews": params.get("reviews", "dozens of"),
            "website": params.get("website", ""),
            "website_status": params.get("website_status", ""),
            "email": params.get("email", ""),
            "instagram": params.get("instagram", ""),
        }
        subject, body = await pipe._draft_email(lead)
        return (
            f"📧 Draft for {name}:\n\n"
            f"Subject: {subject}\n\n"
            f"{body}\n\n"
            f"Send with: /send all email (after approving)"
        )

    async def _task_check(self, params: dict) -> str:
        """Check what leads exist in the sheets."""
        city = params.get("city", "")
        vertical = params.get("vertical", "")
        report = self.state.load("last_run")
        if not report:
            return "No runs yet. Send /run to start the pipeline."
        metrics = report.get("metrics", {})
        tiers = metrics.get("tiers", {})
        lines = [
            f"Last run [{report.get('run_id', '?')}] — {report.get('status', '?')}",
            f"Candidates: {metrics.get('candidates', 0)}",
        ]
        if tiers:
            lines.append("Tiers: " + ", ".join(f"{k}={v}" for k, v in tiers.items()))
        lines.append(f"Emails: sent={metrics.get('emails_sent', 0)} drafted={metrics.get('emails_drafted', 0)}")
        if city:
            lines.append(f"\n(City filter: {city} — run /run discovery to find more)")
        if vertical:
            lines.append(f"(Vertical filter: {vertical})")
        lines.append(f"\nSheet: {self._sheet_url()}")
        return "\n".join(lines)
