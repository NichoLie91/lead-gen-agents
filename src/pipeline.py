"""Pipeline orchestrator (spec sections 4.2, 7, 10).

Wires the agents into a stage state machine:

    discovery -> enrichment -> scoring -> pipeline (queue for the research batch)

Enrichment runs BEFORE scoring so the Reachability category sees confirmed
email/IG (the rubric grants 12 points for a confirmed email — user doc Agent 2).

- ``/stop`` flag is checked between stages (spec 4.5).
- Hard caps (50 emails/run, 15 IG DMs/24h) are enforced here, clamped in code
  so env/config cannot raise them (spec 7.5).
- ``--dry-run`` runs everything offline with mock data: no Composio, no GitHub,
  no Telegram sends.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import re
import uuid
from datetime import UTC, datetime, timedelta

from src import site_research
from src.agents.atlas import Atlas
from src.agents.composio_agent import ComposioAgent
from src.agents.crm_agent import CrmAgent
from src.agents.github_agent import GitHubAgent
from src.agents.maps_agent import MapsAgent
from src.agents.scout import Scout
from src.agents.sheets_agent import DRAFTS_HEADER, PIPELINE_HEADER, SheetsAgent
from src.approvals import ApprovalQueue
from src.core.config import Settings
from src.core.ident import lead_id
from src.core.llm import GeminiPool, LLMUsage
from src.core.logging import TelegramNotifier
from src.core.memory import memory_block
from src.core.state import StateStore
from src.draft_optimizer import draft_with_critique
from src.enrichment import enrich_leads
from src.followups import is_due
from src.inbound import parse_sender_email
from src.inbound_triage import run_inbound_triage
from src.sector_copy import draft as sector_copy_draft

log = logging.getLogger(__name__)

# Hard caps: clamped in code regardless of config (spec 7.5).
EMAIL_CAP_ABSOLUTE = 50
IG_CAP_ABSOLUTE = 15

MODES = ("full", "discovery", "enrichment", "outreach-email", "outreach-ig",
         "followups", "inbound", "report", "batch", "purge")

# Drafts tab policy: a phone screenful of pending work, everything else moved to
# DraftsArchive. Without this the queue grew by one row per draft forever and
# nobody could tell which ten were today's.
DRAFTS_MAX_ROWS = 20
DRAFTS_STALE_DAYS = 21
ARCHIVE_MAX_ROWS = 1000


def _draft_created(row: list) -> str:
    """The Created cell of a Drafts-tab row ('' when missing)."""
    i = DRAFTS_HEADER.index("Created")
    return str(row[i] or "").strip() if i < len(row) else ""


def partition_drafts(rows: list[list], today: str, keep: int = DRAFTS_MAX_ROWS,
                     stale_days: int = DRAFTS_STALE_DAYS) -> tuple[list[list], list[list]]:
    """Split pending draft rows into (what the tab shows, what gets archived).

    Newest first by Created. A row is archived when it is older than
    ``stale_days`` or when ``keep`` newer rows push it out.
    """
    try:
        cutoff = (datetime.fromisoformat(today)
                  - timedelta(days=stale_days)).date().isoformat()
    except ValueError:
        cutoff = ""
    ordered = sorted(rows, key=lambda r: _draft_created(r) or "0000", reverse=True)
    kept: list[list] = []
    archived: list[list] = []
    for i, row in enumerate(ordered):
        created = _draft_created(row)
        if (cutoff and created and created < cutoff) or i >= keep:
            archived.append(row)
        else:
            kept.append(row)
    return kept, archived


def partition_archive(history: list[list], fresh: list[list],
                      cap: int = ARCHIVE_MAX_ROWS) -> list[list]:
    """Append to the archive and drop the oldest rows past ``cap``."""
    combined = list(history) + list(fresh)
    return combined[-cap:] if len(combined) > cap else combined

# Modes that run discovery -> enrichment -> scoring (lead generation path).
DISCOVERY_MODES = ("full", "discovery", "enrichment", "outreach-email", "outreach-ig")

# CRM statuses the pipeline moves leads through (AI employee lifecycle).
STATUS_DRAFTED = "DRAFTED"
STATUS_CONTACTED = "CONTACTED"
STATUS_REPLIED = "REPLIED-INTERESTED"
STATUS_OBJECTION = "OBJECTION"
STATUS_UNSUBSCRIBED = "UNSUBSCRIBED"

# Vertical-specific AI-bottleneck hooks (spec 7.5 + outreach-templates.md).
# Expanded to cover ALL ICP verticals — each hook is a specific, relatable
# pain point that proves we understand their business (video framework:
# signal-based personalization > generic pitch).
HOOKS = {
    # Core verticals
    "plumber": "after-hours drain and water-heater calls are landing in voicemail",
    "hvac": "peak-season calls don't stop at 5pm and are being missed",
    "cleaning": "review follow-up and quote chasing are eating the week",
    "mechanic": "missed calls and manual estimate follow-up are costing jobs",
    "dental": "recall reminders and new-patient follow-up are manual",
    # Dental sub-verticals (all map to the dental hook)
    "dental clinic": "recall reminders and new-patient follow-up are manual",
    "dental implants": "consultation follow-up and case-acceptance calls are falling through",
    "cosmetic dentistry": "high-value case follow-up is inconsistent after consultations",
    "orthodontics": "retainer check reminders and new-patient consultations pile up",
    "pediatric dentistry": "appointment reminders and new-family onboarding are manual",
    "endodontics": "referral follow-up and post-op check-ins are time-consuming",
    "periodontics": "treatment plan follow-up and maintenance recalls are manual",
    "oral surgery": "pre-op instructions and post-surgery follow-ups are missed",
    "prosthodontics": "case presentation follow-up and lab coordination calls are manual",
    "family dentistry": "recare reminders and insurance verification calls pile up",
    "emergency dental": "after-hours emergency calls are going to voicemail",
    "teeth whitening": "consultation follow-up and seasonal promotions are manual",
    "dental group": "multi-location call routing and patient recall are inconsistent",
    # HVAC sub-verticals
    "ac repair": "emergency AC calls in summer go to voicemail after hours",
    "heating repair": "furnace breakdown calls in winter are being missed",
    "air conditioning": "seasonal install quotes and follow-ups are manual",
    # Plumbing sub-verticals
    "drain cleaning": "emergency drain calls go to voicemail on weekends",
    "water heater": "replacement quote follow-up is inconsistent",
    "sewer repair": "inspection follow-up and estimate chasing are manual",
    # Cleaning sub-verticals
    "house cleaning": "recurring booking confirmations and quote follow-ups are manual",
    "commercial cleaning": "bid follow-up and contract renewal reminders are inconsistent",
    "carpet cleaning": "seasonal promotion follow-up and quote chasing are manual",
    # Mechanic sub-verticals
    "auto repair": "estimate follow-up and service reminders are inconsistent",
    "auto body": "insurance claim follow-up and repair status updates are manual",
    "oil change": "service reminders and upsell follow-ups are missed",
    # Generic fallbacks
    "contractor": "estimate follow-up and project scheduling calls are manual",
    "roofing": "inspection follow-up and insurance claim calls are inconsistent",
    "landscaping": "seasonal booking and quote follow-up are manual",
    "pest control": "service reminders and re-treatment follow-ups are missed",
    "locksmith": "after-hours emergency calls go to voicemail",
    "electrician": "emergency calls and estimate follow-ups are inconsistent",
    "handyman": "quote follow-up and scheduling calls pile up",
    "moving": "estimate follow-up and booking confirmations are manual",
    "storage": "unit availability calls and move-in follow-ups are missed",
    "chiropractor": "patient recall and new-patient follow-up are manual",
    "physical therapy": "discharge follow-up and exercise compliance checks are manual",
    "vet": "pet owner recall and vaccination reminders are inconsistent",
    "pet grooming": "appointment reminders and rebooking follow-ups are manual",
    "tire shop": "seasonal tire change reminders and estimate follow-ups are missed",
    "glass repair": "insurance claim follow-up and scheduling calls are manual",
    "appliance repair": "service call follow-up and parts availability updates are manual",
}

# Human-writer rules from the ANCHOR job doc: banned punctuation + AI clichés.
# Enforced programmatically AFTER generation (the doc: "if you generate any
# text containing these, rewrite it immediately before outputting").
AI_TELL_PATTERNS = (
    ("em-dash", "—"), ("en-dash", "–"),
    ("cliche 'delve'", "delve"), ("cliche 'testament'", "testament"),
    ("cliche 'beacon'", "beacon"), ("cliche 'tapestry'", "tapestry"),
    ("cliche 'furthermore'", "furthermore"), ("cliche 'plethora'", "plethora"),
    ("cliche 'moreover'", "moreover"), ("cliche 'in today's world'", "in today's world"),
    ("cliche 'it is important to note'", "it is important to note"),
    ("cliche 'at the end of the day'", "at the end of the day"),
    ("cliche 'seamless'", "seamless"), ("cliche 'game-changer'", "game-changer"),
    ("cliche 'cutting-edge'", "cutting-edge"),    ("cliche 'leverage'", "leverage"), ("cliche 'unlock'", "unlock"), ("cliche 'streamline'", "streamline"),
    ("cliche 'elevate'", "elevate"), ("cliche 'revolutionize'", "revolutionize"),
    ("cliche 'synergy'", "synergy"), ("cliche 'circle back'", "circle back"),
    ("cliche 'best-in-class'", "best-in-class"),
    ("opener 'I hope this email finds you well'", "I hope this email finds you well"),
    ("opener 'I came across'", "I came across"),
)

# Cold-email skill: anti-spam trigger words, banned from body copy entirely.
SPAM_WORDS = (
    "free", "guarantee", "risk-free", "buy now", "special promotion",
    "increase revenue", "urgent", "save money", "click here", "best price",
)


# IG failure reasons mapped to the doc's outcome vocabulary.
IG_COLD_START_HINTS = (
    "cold", "must message first", "cannot message", "conversation must be initiated",
    "recipient has not messaged", "open a conversation", "not allowlisted",
    "new contact", "recipient must message",
)
IG_WINDOW_HINTS = (
    "24 hour", "24-hour", "24h", "allowed window", "messaging window",
    "window has", "outside the window", "once the recipient",
)
# Enrichment collects the @handle; INSTAGRAM_SEND_TEXT_MESSAGE needs a numeric
# PSID and the v3 catalog has no handle->ID resolver. Treat that tooling gap as
# a SKIP (doc outcome vocabulary), never a failed send.
IG_NO_PSID_HINTS = (
    "numeric instagram psid", "valid id string", "recipient[id]",
    "handle; no handle->id resolver",
)


class StopRequested(Exception):
    pass


class Pipeline:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.state = StateStore(settings.state_dir)
        self.composio = ComposioAgent(settings)
        # Two model tiers over (up to) two Gemini keys: quick judgment uses the
        # fast pool, heavier writing the pro pool — both round-robin across
        # keys to split the load (see GeminiPool). Every call is recorded for
        # the /usage dashboard command.
        self.usage = LLMUsage(settings.state_dir)
        self.llm = GeminiPool(settings, role="pro", usage=self.usage)
        self.fast_llm = GeminiPool(settings, role="fast", usage=self.usage)
        self.github = GitHubAgent(settings, self.state)
        self.sheets = SheetsAgent(self.composio, settings, self.state)
        self.crm = CrmAgent(self.sheets)
        self.approvals = ApprovalQueue(self.state)
        # Every agent carries the shared Gemini brain (falls back to
        # deterministic behavior when the key is absent). Maps gets the state
        # store so per-query yield feedback compounds across runs (Phase 3).
        self.maps = MapsAgent(self.composio, settings, llm=self.fast_llm,
                              state=self.state)
        self.atlas = Atlas(self.maps, settings)
        self.scout = Scout(settings, llm=self.fast_llm)
        self.notifier = TelegramNotifier(settings.telegram_bot_token, settings.telegram_alert_chat_id)
        # Leads an email was attempted for THIS run (sent/drafted/NEEDS_ENRICHMENT).
        # ANCHOR's IG rule: a DM is only eligible when an email attempt exists.
        # Initialized here AND reset per-run in run() so stage-level tests (and
        # any direct stage call) have a working set.
        self._email_attempted: set[str] = set()
        # Dedicated Drafts tab: WARM drafts accumulate here for visibility.
        self._drafts_rows: list[list] = []

    # ---------- main entry ----------
    async def run(self, mode: str = "full") -> dict:
        run_id = uuid.uuid4().hex[:8]
        started = datetime.now(UTC)
        report: dict = {
            "run_id": run_id, "mode": mode,
            "started_at": started.isoformat(), "status": "RUNNING",
            "metrics": {},
        }
        self.state.save_if_changed("pipeline_running", {"running": True, "run_id": run_id})
        # Leads an email was attempted for THIS run (sent/drafted/NEEDS_ENRICHMENT).
        # ANCHOR's IG rule: a DM is only eligible when an email attempt exists.
        self._email_attempted: set[str] = set()
        self._drafts_rows: list[list] = []

        try:
            scored: list[dict] = []
            if mode != "report":
                await self.sheets.ensure_sheet()  # create/reuse per-tab spreadsheets
            if mode in DISCOVERY_MODES:
                raw = await self._stage_discovery(report)
                self._check_stop()
                enriched = await self._stage_enrichment(raw, report)
                self._check_stop()
                scored = await self._stage_scoring(enriched, report)
                self._check_stop()
                await self._stage_write_score_tab(scored)

            # SENDING IS RETIRED. `full` used to draft one templated email per
            # scored lead, auto-approve every WARM one, and send it in the same
            # run -- which is why the Drafts tab grew by the whole scored list
            # every day with mail that had already gone out. The old modes stay
            # routable so an existing trigger or bookmark gets an explanation
            # instead of a send.
            if mode in ("outreach-email", "outreach-ig", "followups"):
                await self._stage_retired_senders(mode, report)
                self._check_stop()

            # Every mode that found + scored leads writes them to the Pipeline
            # tab, not just `full`. The daily schedule runs `discovery`, and
            # gating this on `full` meant the one tab every report links to sat
            # empty while the leads went to the Score tab of another file.
            if mode in DISCOVERY_MODES:
                await self._stage_pipeline(scored, report)

            if mode == "purge":
                # One-time migration: drop the old draft pile, remember who the
                # old flow already touched, and hand over to the batch flow.
                await self._stage_purge_legacy(report)
                self._check_stop()

            if mode == "batch":
                # Daily research batch: detective -> writer -> closer, 10 at a
                # time, posted to Telegram and NOT sent. It reads the Pipeline
                # tab and runs as its own scheduled job, so a broken discovery
                # provider cannot silence the delivery.
                await self._stage_research_batch(
                    await self._load_pipeline_leads(), report)
                self._check_stop()

            if mode == "inbound":
                # AI employee loop: read + classify lead replies (Step 06).
                await self._stage_inbound(report)

            # Write the dedicated Drafts tab if any drafts were created.
            if self._drafts_rows:
                await self._flush_drafts(report)

            # Persist long-term lead memory (queued), then flush ALL tab writes
            # once at the end of the run (quota-safe: reads cached, writes
            # batched, throttles retried with backoff in execute_action).
            if mode != "report":
                await self.crm.save()
            # ONE master list, rebuilt from every tab + this run's leads. Runs
            # after the CRM is queued so its statuses are visible, and before
            # flush() so it lands in the same batched write burst.
            if mode != "report":
                await self._stage_write_all_leads(scored, report)
            if mode != "report" and self.sheets.has_pending_writes():
                ok_tabs, failed_tabs = await self.sheets.flush()
                report["metrics"]["sheet_tabs_written"] = ok_tabs
                report["metrics"]["sheet_tabs_failed"] = failed_tabs
                if failed_tabs:
                    log.error("sheet flush: %d tab(s) failed to write", failed_tabs)

            report["status"] = "COMPLETED"
            if not self.settings.dry_run:
                sheet_url = self.sheets.sheet_url() if not self.settings.dry_run else ""
                await self.notifier.notify(self.build_report_text(report, sheet_url))
        except StopRequested:
            report["status"] = "STOPPED"
        except Exception as exc:
            report["status"] = "FAILED"
            report["error"] = str(exc)
            log.exception("pipeline %s failed", run_id)
            await self.notifier.notify(f"lead-gen-agents pipeline {run_id} FAILED: {exc}")
        finally:
            report["finished_at"] = datetime.now(UTC).isoformat()
            self.state.save_if_changed("last_run", report)
            self.state.save_if_changed("pipeline_running", {"running": False})
            self.github.commit_state()
        return report

    # ---------- stages ----------
    async def _stage_discovery(self, report: dict) -> list[dict]:
        raw = await self.atlas.run()
        metrics = report.setdefault("metrics", {})
        # Which Composio Maps action answered. Without this, a failover to a
        # second provider was invisible and looked like the same broken source.
        provider = self.atlas.provider_used()
        if provider:
            metrics["maps_provider"] = provider
        if not self.settings.dry_run:
            raw = self._filter_new_leads(raw)  # cross-run dedupe (spec 10)
        metrics["candidates"] = len(raw)
        return raw

    def _filter_new_leads(self, raw: list[dict]) -> list[dict]:
        """Drop leads already seen in previous runs (PII-safe sha256 registry).

        Only the hash of (name, address) is stored in ``state/dedupe.json`` so
        nothing identifiable leaks into the public repo (spec 11).
        """
        registry = self.state.load("dedupe", {"keys": []})
        seen = set(registry.get("keys", []))
        kept: list[dict] = []
        new_keys: list[str] = []
        for lead in raw:
            key = lead_id(lead.get("name", ""), lead.get("address", ""))
            if key in seen:
                continue
            kept.append(lead)
            new_keys.append(key)
        if new_keys:
            seen.update(new_keys)
            self.state.save_if_changed("dedupe", {"keys": sorted(seen)})
        return kept

    async def _stage_enrichment(self, raw: list[dict], report: dict) -> list[dict]:
        enriched = await enrich_leads(raw, self.composio, self.settings, llm=self.fast_llm)
        with_email = sum(1 for l in enriched if l.get("email"))
        report["metrics"]["with_email"] = with_email
        report["metrics"]["needs_enrichment"] = len(enriched) - with_email
        return enriched

    async def _stage_scoring(self, enriched: list[dict], report: dict) -> list[dict]:
        """Score with confirmed contact data (enrichment ran first).

        The deterministic rubric stays authoritative; Scout's Gemini brain
        adds a one-line rationale for the top leads (bounded, best-effort).
        """
        scored = self.scout.run(enriched)
        if not self.settings.dry_run:  # dry-run stays fully offline (no Gemini quota)
            # Scout's judgment overlay (autonomy): Gemini may adjust the
            # top candidates by a bounded -5..+5 before tiers are locked.
            await self.scout.apply_judgment(
                scored, int(self.settings.crit("judgment_leads", 15)))
            sem = asyncio.Semaphore(5)

            async def _rationale(lead: dict) -> None:
                async with sem:
                    lead["score"]["rationale"] = await self.scout.explain(lead, lead["score"])

            await asyncio.gather(*(_rationale(lead) for lead in scored[:10]))
        tiers: dict[str, int] = {}
        for lead in scored:
            tier = lead["score"]["tier"]
            tiers[tier] = tiers.get(tier, 0) + 1
        report["metrics"]["tiers"] = tiers
        return scored

    async def _stage_write_score_tab(self, scored: list[dict]) -> None:
        rows = [
            [lead.get("name"), lead["score"]["icp"], lead["score"]["intent"],
             lead["score"]["budget"], lead["score"]["reachability"],
             lead["score"]["timing"], lead["score"]["total"], lead["score"]["tier"]]
            for lead in scored
        ]
        await self.sheets.write_tab("Score", rows)

    async def _stage_outreach_email(self, leads: list[dict], report: dict) -> None:
        """RETIRED. Templated cold email is no longer drafted, approved or sent.

        What this stage did, and why it is gone: it wrote one templated email
        per scored lead ("Saw you guys are running a solid 4.9 star setup in
        Germantown..."), auto-approved every WARM draft it created, and then
        _stage_send_approved sent it in the same run. HOT leads went out with no
        approval at all. So every daily `full` run filled the Drafts tab and
        emptied it into Gmail in one pass, and the tab only ever showed mail
        that had already left.

        Outreach is now the research batch (_stage_research_batch): ten
        businesses a day, each email naming one defect seen on that business's
        own site, and nothing is ever sent by this system. The method stays so
        the `outreach-email` mode resolves to an explanation, not a send.
        """
        report.setdefault("metrics", {})["legacy_outreach_retired"] = True
        log.warning(
            "outreach-email is retired: %d lead(s) ignored, nothing drafted, "
            "nothing sent. Use mode=batch for the research batch.",
            len(leads),
        )

    async def _stage_send_approved(self, report: dict) -> None:
        """RETIRED. This system does not send email.

        This was the choke point: any Outreach row with status NEEDS_APPROVAL
        whose lead hash sat in the approval queue went out through Gmail. The
        old outreach stage approved its own drafts, so "needs your approval"
        was already true by the time the owner read the message.

        Drafts now live in the Drafts tab and in the Telegram batch, and the
        owner sends them from his own mailbox.
        """
        report.setdefault("metrics", {})["legacy_sender_retired"] = True

    async def _stage_followups(self, report: dict) -> None:
        """RETIRED as a sender: counts what the CRM thinks is due, sends nothing.

        The Day 3/7/14 cadence was automated off the same templated first touch,
        so with that flow gone there is no sequence left to continue. The
        research batch hands over a written day-4 and day-10 closer per business
        instead, for the owner to send himself.
        """
        metrics = report.setdefault("metrics", {})
        rows = await self.crm.load()
        due = [r for r in rows.values() if is_due(r, datetime.now(UTC).date())]
        metrics["followups_due_manual"] = len(due)
        metrics["followups_sent"] = 0
        if due:
            log.info("followups: %d due, sending is retired", len(due))

    async def _stage_inbound(self, report: dict) -> None:
        """AI employee loop: scan email, match replies to CRM leads, act.

        STOP -> auto-confirm opt-out + mark UNSUBSCRIBED.
        INTERESTED / OBJECTION / QUESTION -> escalate to the owner on Telegram
        with the reply and a suggested response (human-in-the-loop, Step 04).
        """
        if self.settings.dry_run or not self.composio.connected:
            report["metrics"]["inbound_scanned"] = 0
            return
        await self.crm.load()
        seen_data = self.state.load("inbound_seen", {"threads": []})
        seen = set(seen_data.get("threads", [])) if isinstance(seen_data, dict) else set()
        resp = await self.composio.execute_action(
            self.composio.slug("mail_list_threads"), {"query": "newer_than:3d"})
        threads = (resp.get("data") or {}).get("threads", []) if resp.get("ok") else []
        processed = notified = auto_replied = 0
        # Phase 3: one bounded LLM budget for the whole triage pass; the
        # keyword classifier is the floor when it runs out.
        triage_budget = {"left": 8}
        history_used = 0
        for thread in threads[:25]:
            tid = (thread or {}).get("id", "")
            if not tid or tid in seen:
                continue
            fetched = await self.composio.execute_action(
                self.composio.slug("mail_fetch_thread"), {"thread_id": tid})
            msgs = ((fetched.get("data") or {}).get("messages") or []) if fetched.get("ok") else []
            if not fetched.get("ok"):
                continue  # retried on the next scan (not marked seen)
            seen.add(tid)  # mark seen only after a successful fetch
            if not msgs:
                continue
            latest = msgs[-1]
            sender = parse_sender_email(latest.get("sender"))
            text = latest.get("messageText") or latest.get("preview") or ""
            lead = self.crm.find_by_email(sender) if sender else None
            if not lead:
                continue  # not a tracked lead's reply
            lid = lead["Lead ID"]
            # Phase 3 triage loop: classify (LLM label + keyword floor) ->
            # act -> escalate with CRM history. STOP stays code-enforced.
            triage = await run_inbound_triage(
                text=text, lead=lead, crm=self.crm,
                llm=self.fast_llm, budget=triage_budget)
            if triage.get("history_used"):
                history_used += 1
            kind = triage["kind"]
            self.crm.append_timeline(lid, "reply", f"[{kind}] {text[:300]}")
            self.crm.upsert(lid, name=lead.get("Name", ""))
            self.crm.set_status(lid, {
                "INTERESTED": STATUS_REPLIED,
                "PRICE_OBJECTION": STATUS_OBJECTION,
                "STOP": STATUS_UNSUBSCRIBED,
                "QUESTION": "QUESTION",
            }.get(kind, "QUESTION"))
            if triage["action"] == "auto_reply":
                if not self.settings.dry_run:
                    await self.composio.execute_action(
                        self.composio.slug("mail_reply_thread"),
                        {"thread_id": tid,
                         "message_body": triage["suggested"],
                         "recipient_email": sender})
                    auto_replied += 1
                self.crm.append_timeline(lid, "auto-reply", "opt-out confirmed")
            elif triage.get("notify"):
                title = {"INTERESTED": "🎯 Interested",
                         "PRICE_OBJECTION": "💰 Price objection",
                         "QUESTION": "❓ Question"}.get(kind, "ℹ️ Reply")
                msg = (f"{title} — {lead.get('Name')}\n{lead.get('Email')}\n\n"
                       f"Reply: {text[:300]}\n\n"
                       f"Suggested: {triage['suggested']}")
                if not self.settings.dry_run:
                    await self.notifier.notify(msg)
                    notified += 1
            processed += 1
        self.state.save_if_changed("inbound_seen", {"threads": sorted(seen)[-200:]})
        report["metrics"]["inbound_scanned"] = len(threads)
        report["metrics"]["inbound_processed"] = processed
        report["metrics"]["inbound_owner_notified"] = notified
        report["metrics"]["inbound_auto_replied"] = auto_replied
        report["metrics"]["inbound_history_used"] = history_used

    async def _stage_outreach_ig(self, leads: list[dict], report: dict) -> None:
        """RETIRED as a sender. No Instagram DM leaves this system any more.

        The DM stage fired for HOT-VERIFIED leads that had an email attempt in
        the same run, so it was part of the same automatic blast. The owner
        sends everything himself, from his own accounts.
        """
        report.setdefault("metrics", {})["legacy_ig_retired"] = True
        log.info("outreach-ig is retired: %d lead(s) ignored, no DMs sent", len(leads))

    async def _stage_pipeline(self, leads: list[dict], report: dict) -> None:
        rows = [
            [
                i,  # rank
                lead.get("name", ""),  # business_name
                lead.get("email", "") or "",  # verified_email
                lead.get("email_verification_method", "not_found"),  # email_verification_method
                lead.get("city", ""),  # city
                lead.get("state", ""),  # state
                lead.get("vertical", ""),  # trade
                lead.get("phone", ""),  # phone
                lead.get("owner_phone", "") or "",  # owner_or_manager_phone
                lead.get("website", "") or "",  # official_site
                lead.get("rating", "") or "",  # rating
                lead.get("reviews", "") or "",  # review_count
                f"https://www.instagram.com/{lead.get('instagram', '')}/" if lead.get("instagram") else "",  # instagram_profile
                lead.get("booking_signal", "unknown"),  # booking_signal
                lead.get("emergency_signal", "unknown"),  # emergency_signal
                lead.get("source_url", ""),  # source_url
                lead.get("source_evidence_text", ""),  # source_evidence_text
                lead.get("evidence_quality", "medium"),  # evidence_quality
                lead["score"]["total"],  # scout_score
                lead["score"]["tier"],  # scout_tier
                "email" if lead.get("email") and not str(lead.get("email", "")).endswith(".example") else "hold",  # recommended_channel
                "email not publicly verified" if not lead.get("email") or str(lead.get("email", "")).endswith(".example") else "",  # hold_reason
            ]
            for i, lead in enumerate(leads, start=1)
        ]
        # "Manual" not "Queued": the cadence is no longer scheduled or sent by
        # this system, and the sheet must not imply otherwise.
        followup = [
            [i, lead.get("name"), "Day 0: intro (send yourself)", "Day 4: bump",
             "Day 10: close", "Write your own for anything after that", "Manual"]
            for i, lead in enumerate(leads, start=1)
        ]
        # The Pipeline tab is the research batch's work queue, so it ACCUMULATES:
        # rewriting it with only this run's new leads wiped the queue on any run
        # that found nothing new (cross-run dedupe settles at zero once a market
        # is mined), which left the batch with nothing to inspect.
        cap = int(self.settings.crit("pipeline_pool_max", 400))
        existing = await self.sheets.read_tab("Pipeline")
        merged = self._merge_pipeline_rows(existing, rows, cap)
        await self.sheets.write_tab("Pipeline", merged)
        await self.sheets.write_tab("Followup", followup)
        report["metrics"]["pipeline_rows"] = len(rows)
        report["metrics"]["pipeline_pool"] = len(merged)

    @staticmethod
    def _merge_pipeline_rows(existing: list[list] | None, new_rows: list[list],
                             cap: int) -> list[list]:
        """New leads first, then whatever was already queued, deduped and capped.

        Keyed on lead_id(business_name, city) — the same id the research batch
        uses, because the tab carries no street address. Existing rows are
        rebuilt into PIPELINE_HEADER order first: passing a stale column order
        through would silently shift every cell one column left.
        """
        name_i = PIPELINE_HEADER.index("business_name")
        city_i = PIPELINE_HEADER.index("city")
        merged: dict[str, list] = {}
        for row in new_rows:
            merged[lead_id(str(row[name_i]), str(row[city_i]))] = list(row)

        if existing and existing[0]:
            header = [str(h).strip() for h in existing[0]]
            positions = {name: header.index(name) for name in PIPELINE_HEADER
                         if name in header}
            for row in existing[1:]:
                if not any(str(c or "").strip() for c in row):
                    continue
                rebuilt = [
                    (row[positions[name]] if name in positions and positions[name] < len(row)
                     else "")
                    for name in PIPELINE_HEADER
                ]
                name = str(rebuilt[name_i] or "").strip()
                if not name:
                    continue
                merged.setdefault(lead_id(name, str(rebuilt[city_i] or "")), rebuilt)

        rows = list(merged.values())[:cap]
        for i, row in enumerate(rows, start=1):
            row[0] = i  # rank reflects the queue order, not the old run
        return rows

    async def _stage_write_all_leads(self, leads: list[dict], report: dict) -> None:
        """Write the single deduped master list (AllLeads tab).

        Every tab contributes; the same business appears once no matter how
        many runs, tabs or verticals reached it, and the row keeps both phone
        numbers. Failures are reported, never swallowed: an empty master list
        must never look like "no leads yet".
        """
        from src.all_leads import build_all_leads, to_rows

        try:
            rows = await build_all_leads(self.sheets, leads)
        except Exception as exc:
            # A tab that could not be read must ABORT the rebuild, not shrink
            # it: writing here would replace the master list with a partial one
            # and quietly lose every lead that lived in the unreadable tab.
            log.warning("all-leads build failed, master list left untouched: %s", exc)
            report["metrics"]["all_leads_error"] = str(exc)[:200]
            return
        await self.sheets.write_tab("AllLeads", to_rows(rows))
        report["metrics"]["all_leads"] = len(rows)
        report["metrics"]["all_leads_with_email"] = sum(
            1 for r in rows if str(r.get("email") or "").strip())
        report["metrics"]["all_leads_with_owner_phone"] = sum(
            1 for r in rows if str(r.get("owner_or_manager_phone") or "").strip())
        report["metrics"]["all_leads_with_phone"] = sum(
            1 for r in rows if str(r.get("public_phone") or "").strip()
            or str(r.get("owner_or_manager_phone") or "").strip())

    # ---------- daily research batch (detective -> writer -> closer) ----------
    async def _load_pipeline_leads(self) -> list[dict]:
        """Read the Pipeline tab as lead dicts, keyed by header name."""
        rows = await self.sheets.read_tab("Pipeline")
        if not rows or not rows[0]:
            return []
        header = [str(h).strip() for h in rows[0]]
        idx = {h: i for i, h in enumerate(header)}

        def cell(row: list, name: str) -> str:
            i = idx.get(name)
            if i is None or i >= len(row) or row[i] is None:
                return ""
            return str(row[i]).strip()

        return [
            {
                "name": cell(row, "business_name"),
                "email": cell(row, "verified_email"),
                "website": cell(row, "official_site"),
                "city": cell(row, "city"),
                "vertical": cell(row, "trade"),
                # Only used to make a reviews claim truthful: the copy says
                # "though Google shows 4.9 from 80 reviews" when, and only
                # when, the sheet actually carries that number.
                "rating": cell(row, "rating"),
                "review_count": cell(row, "review_count"),
                "score": cell(row, "scout_score"),
                "tier": cell(row, "scout_tier"),
            }
            for row in rows[1:]
            if cell(row, "business_name")
        ]

    async def _stage_research_batch(self, leads: list[dict], report: dict) -> None:
        """Ten businesses: one seen problem, one email, a day-4 and day-10 closer.

        Nothing is sent. The table, the drafts and a CSV go to Telegram, the
        drafts also go to the Drafts tab, and the NEXT ten wait for `/next` (the
        owner asked to approve each batch before continuing).
        """
        metrics = report.setdefault("metrics", {})
        state = self.state.load(site_research.STATE_FILE, site_research.new_state())
        today = datetime.now(UTC).date().isoformat()
        # /next sets force_next: an explicit request beats both gates, so the
        # owner is never stuck waiting for the next cron tick.
        force = bool(state.get("force_next"))

        if state.get("awaiting_confirm") and not force:
            metrics["research_awaiting_confirm"] = True
            metrics["research_batch"] = 0
            # Logged as well as messaged: the Actions log is where "why did the
            # batch do nothing?" gets answered.
            log.info("research batch: waiting for /next after batch %s",
                     state.get("batches", 0))
            await self._notify(
                f"Batch {state.get('batches', 0)} is done and waiting. "
                "Send /next for the next 10 businesses."
            )
            return

        if state.get("last_batch_date") == today and not force:
            # One batch a day, even when the earlier one yielded nothing. Without
            # this a zero-yield morning left the gate open and the 15:00 run
            # produced a second batch of ten the same day.
            metrics["research_batch"] = 0
            metrics["research_already_ran"] = True
            log.info("research batch: one batch already ran today (%s)", today)
            await self._notify(
                "A research batch already went out today. Reply /next only if "
                "you want another ten now."
            )
            return

        pool = []
        not_eligible = 0
        for lead in leads:
            if not site_research.eligible(lead):
                not_eligible += 1
                continue
            pool.append({**lead, "id": site_research.lead_hash(lead)})

        if not pool:
            # Nothing to inspect is a different message from "nothing found":
            # without a website and an email there is no detective work to do.
            metrics["research_batch"] = 0
            log.info("research batch: %d leads, none has both a website and an "
                     "email (%d incomplete)", len(leads), not_eligible)
            await self._notify(
                f"No lead in the Pipeline tab has both a website and an email yet "
                f"({len(leads)} leads, {not_eligible} missing one or the other), "
                "so there was nothing to inspect today."
            )
            return

        batch = site_research.select_batch(pool, state, today=today)
        if not batch:
            metrics["research_batch"] = 0
            log.info("research batch: %d eligible leads, all drafted already or "
                     "waiting on a retry date (%d marked done)",
                     len(pool), len(state.get("done") or []))
            await self._notify(
                "Every lead in the Pipeline tab has either been written to "
                "already or is waiting on a retry date, so there was nothing "
                "new to inspect today."
            )
            return

        items: list[dict] = []
        skipped: list[dict] = []
        # Walk each defect's phrasings in order so ten plumbers in one batch do
        # not receive the same observation with a different name in it.
        used_variants: dict[str, int] = {}
        for lead in batch:
            problem, evidence = await site_research.inspect_site(lead["website"])
            if problem is None:
                # No readable page or no defect in the checked list: no email.
                # Writing one anyway would mean inventing what we did not see.
                reason = site_research.classify_skip(evidence)
                skipped.append({"id": lead["id"], "reason": reason,
                                "detail": evidence})
                log.info("research: skipped %s (%s)",
                         site_research.lead_hash(lead)[:8], evidence)
                continue
            variant = used_variants.get(problem.code, 0)
            used_variants[problem.code] = variant + 1
            subject, body = site_research.write_email(lead, problem, variant)
            items.append({
                **lead,
                "problem": problem,
                "evidence": evidence,
                "subject": subject,
                "body": body,
                "closers": site_research.write_closers(lead["name"], problem),
            })

        metrics["research_inspected"] = len(batch)
        metrics["research_batch"] = len(items)
        metrics["research_no_problem"] = len(skipped)
        metrics["research_not_eligible"] = not_eligible

        if not items:
            # The batch is recorded anyway: the inspected leads must not come
            # back tomorrow, or the pool never advances past them.
            self.state.save_if_changed(
                site_research.STATE_FILE,
                site_research.record_batch(state, [], skipped, today=today))
            reasons = self._skip_summary(skipped)
            await self._notify(
                "Research batch: no business in this group had a readable site "
                "with a problem I could actually see, so nothing was written. "
                f"No emails were invented.\n{reasons}"
            )
            return

        # Drafts tab: the owner still has to send these by hand. The Notes
        # column carries the evidence so a draft can be checked against the
        # site without re-reading the Telegram message.
        for i, it in enumerate(items, start=1):
            self._drafts_rows.append([
                i, it["name"], it["id"], it["email"], it["subject"], it["body"],
                it.get("score", ""), it.get("tier", ""), "NEEDS_APPROVAL",
                today, f"Manual send. {it['evidence']}"[:200],
            ])

        header = (f"Research batch {state.get('batches', 0) + 1}: "
                  f"{len(items)} emails from {len(batch)} businesses inspected, "
                  f"{len(skipped)} skipped.")
        if skipped:
            header += f"\n{self._skip_summary(skipped)}"
        table = site_research.format_table(items)
        print(header + "\n" + site_research.format_table(items, html=False))
        await self._notify(f"{header}\n{table}", html=True)
        for chunk in site_research.format_drafts(items):
            await self._notify(chunk)
        await self._notify_file(
            f"research-batch-{today}.csv", site_research.format_csv(items))
        await self._notify("Nothing above has been sent. Reply /next for the next 10.")

        self.state.save_if_changed(
            site_research.STATE_FILE,
            site_research.record_batch(state, items, skipped, today=today))

    @staticmethod
    def _skip_summary(skipped: list[dict]) -> str:
        """Why leads were skipped, in plain words."""
        counts: dict[str, int] = {}
        for skip in skipped:
            counts[skip["reason"]] = counts.get(skip["reason"], 0) + 1
        return "Skipped: " + ", ".join(
            f"{site_research.SKIP_LABELS.get(k, k)} ({v})"
            for k, v in sorted(counts.items())
        )

    async def _flush_drafts(self, report: dict) -> None:
        """Write the Drafts tab as a queue: a screenful of pending, rest archived.

        The tab is read on a phone, so it must not grow past what fits there.
        Pending rows survive; rows that are already sent/rejected, older than
        ``drafts_stale_days``, or pushed out by newer ones move to DraftsArchive
        (append-only) instead of piling up in the queue.
        """
        metrics = report.setdefault("metrics", {})
        keep_max = int(self.settings.crit("drafts_max_rows", DRAFTS_MAX_ROWS))
        stale_days = int(self.settings.crit("drafts_stale_days", DRAFTS_STALE_DAYS))

        existing = await self.sheets.read_tab("Drafts")
        pending: list[list] = []
        archive: list[list] = []
        if existing and existing[0]:
            header = [str(h).strip() for h in existing[0]]
            status_i = header.index("Status") if "Status" in header else None
            for row in existing[1:]:
                if not any(str(c or "").strip() for c in row):
                    continue
                status = (str(row[status_i] or "").strip()
                          if status_i is not None and status_i < len(row) else "")
                if status in ("NEEDS_APPROVAL", "APPROVED"):
                    pending.append(list(row))
                else:
                    archive.append(list(row))  # SENT / REJECTED / SKIPPED

        pending.extend(list(row) for row in self._drafts_rows)
        kept, pushed_out = partition_drafts(
            pending, datetime.now(UTC).date().isoformat(),
            keep=keep_max, stale_days=stale_days)
        archive.extend(pushed_out)

        for i, row in enumerate(kept, start=1):
            if row:
                row[0] = i
        await self.sheets.write_tab("Drafts", kept)

        if archive:
            previous = await self.sheets.read_tab("DraftsArchive")
            history = ([list(r) for r in previous[1:]] if previous and previous[0] else [])
            combined = partition_archive(history, archive, cap=ARCHIVE_MAX_ROWS)
            await self.sheets.write_tab("DraftsArchive", combined)
            metrics["drafts_archived"] = len(combined) - len(history)
        metrics["drafts_kept"] = len(kept)

    async def _stage_retired_senders(self, mode: str, report: dict) -> None:
        """Answer a retired sending mode with an explanation, not a send."""
        report.setdefault("metrics", {})[f"retired_{mode.replace('-', '_')}"] = True
        await self._notify(
            f"Mode {mode} is retired: this system no longer sends anything.\n\n"
            "Outreach is the research batch now: ten businesses, one problem "
            "seen on each business's own site, draft plus a day-4 and day-10 "
            "closer, all written for you to send yourself.\n"
            "Reply /next for the next ten."
        )

    async def _stage_purge_legacy(self, report: dict) -> None:
        """One-time migration off the old templated flow (mode `purge`).

        - Drafts tab: pending rows deleted. That tab was a log of mail the old
          stage had already sent, not a queue waiting for a human.
        - Outreach tab: rows still NEEDS_APPROVAL are marked REJECTED and kept
          as history; nothing in that table is sendable any more.
        - Approval queue: emptied, so no stale "approved" hash can fire.
        - Research batch: every Lead ID the old flow ever touched is marked
          done, so the new batch never writes to a business that already heard
          from the template, and the daily gate is cleared so a batch can run
          today.
        """
        metrics = report.setdefault("metrics", {})

        def col(header: list, name: str) -> int | None:
            for i, head in enumerate(header):
                if str(head).strip() == name:
                    return i
            return None

        def cell(row: list, idx: int | None) -> str:
            if idx is None or idx >= len(row) or row[idx] is None:
                return ""
            return str(row[idx]).strip()

        # 1. Drafts tab: the pile goes.
        drafts = await self.sheets.read_tab("Drafts")
        kept: list[list] = []
        removed = 0
        if drafts and drafts[0]:
            status_i = col(drafts[0], "Status")
            for row in drafts[1:]:
                if cell(row, status_i) in ("NEEDS_APPROVAL", "APPROVED"):
                    removed += 1
                else:
                    kept.append(row)
        await self.sheets.write_tab("Drafts", kept)

        # 2. Outreach tab: retire pending rows in place, remember every lead.
        outreach = await self.sheets.read_tab("Outreach")
        touched: list[str] = []
        touched_names: set[str] = set()
        retired = 0
        rows_out: list[list] = []
        if outreach and outreach[0]:
            status_i = col(outreach[0], "Status")
            lid_i = col(outreach[0], "Lead ID")
            note_i = col(outreach[0], "Notes")
            lead_i = col(outreach[0], "Lead")
            for row in outreach[1:]:
                row = list(row)
                lid = cell(row, lid_i)
                if lid:
                    touched.append(lid)
                name = cell(row, lead_i)
                if name:
                    touched_names.add(name.lower())
                if cell(row, status_i) == "NEEDS_APPROVAL":
                    if status_i is not None and status_i < len(row):
                        row[status_i] = "REJECTED"
                    if note_i is not None and note_i < len(row):
                        row[note_i] = "Templated draft retired; nothing to send"
                    retired += 1
                rows_out.append(row)
        await self.sheets.write_tab("Outreach", rows_out)

        # 3. Approval queue: nothing may stay armed.
        cleared = len(self.state.load("approvals", {}) or {})
        self.state.save_if_changed("approvals", {})

        # 4. Hand over to the batch flow: already-contacted stays already-contacted.
        #    The ids above are lead_id(name, address); the batch keys on
        #    lead_hash(name, city or address), and the Pipeline tab carries no
        #    street address. Seeding only the raw ids would therefore miss
        #    everyone and mail them a second time, so the queued Pipeline leads
        #    are matched by name against the old Outreach rows and marked with
        #    the hash the batch actually uses. Skipping beats emailing twice.
        state = self.state.load(site_research.STATE_FILE, site_research.new_state())
        pipeline_rows = await self.sheets.read_tab("Pipeline")
        batch_ids: list[str] = []
        if pipeline_rows and pipeline_rows[0]:
            p_name = col(pipeline_rows[0], "business_name")
            p_city = col(pipeline_rows[0], "city")
            for row in pipeline_rows[1:]:
                name = cell(row, p_name)
                if name and name.lower() in touched_names:
                    batch_ids.append(site_research.lead_hash(
                        {"name": name, "city": cell(row, p_city)}))
        done = list(dict.fromkeys(
            [*(state.get("done") or []), *touched, *batch_ids]))
        self.state.save_if_changed(
            site_research.STATE_FILE,
            {**site_research.new_state(), "done": done,
             "batches": state.get("batches", 0)},
        )

        metrics["purge_drafts_removed"] = removed
        metrics["purge_outreach_retired"] = retired
        metrics["purge_approvals_cleared"] = cleared
        metrics["purge_leads_remembered"] = len(done)
        await self._notify(
            "Legacy outreach purged. Nothing was sent.\n"
            f"Drafts removed: {removed}\n"
            f"Outreach rows retired: {retired}\n"
            f"Approval entries cleared: {cleared}\n"
            f"Businesses marked already contacted: {len(done)}\n\n"
            "The research batch takes over from here. Reply /next for ten now."
        )

    async def _notify(self, text: str, html: bool = False) -> None:
        """Telegram message; a dry run prints instead of sending."""
        if self.settings.dry_run:
            print(text)
            return
        if html:
            send_html = getattr(self.notifier, "notify_html", None)
            if send_html is not None and await send_html(text):
                return
            # No HTML support on this notifier: degrade to plain text rather
            # than showing the owner raw <pre> tags.
            text = text.replace("<pre>", "").replace("</pre>", "")
        await self.notifier.notify(text)

    async def _notify_file(self, filename: str, content: str) -> None:
        """Attach the batch as a document; a dry run says what it would send."""
        if self.settings.dry_run:
            print(f"[dry-run] would attach {filename} ({len(content)} bytes)")
            return
        send_doc = getattr(self.notifier, "notify_document", None)
        if send_doc is None:
            return
        await send_doc(filename, content)

    # ---------- helpers ----------
    def _check_stop(self) -> None:
        if self.github.stop_requested():
            self.github.clear_stop()
            raise StopRequested("stop flag set via Telegram /stop")

    @staticmethod
    def _hook(lead: dict) -> str:
        vertical = str(lead.get("vertical", "")).lower()
        hook = HOOKS.get(vertical)
        if not hook and not lead.get("website"):
            hook = "no website at all, so jobs are slipping away today"
        return hook or "manual follow-up is eating time"

    async def _draft_email(self, lead: dict) -> tuple[str, str]:
        """5-part B2B cold email framework (combined from 14 video deep-dive).

        Frameworks applied:
        1. CONTEXT LINE - prove research with a specific trigger (EmailAwesome 2026)
        2. PROBLEM-SOLUTION - one sentence pain, one sentence fix (PPC Framework)
        3. COST OF INACTION - make them imagine not acting (LeadPilot 30/30/50)
        4. SOCIAL PROOF - one comparable result, one sentence (Hormozi)
        5. SOFT CTA - ask for reply, not meeting (all 14 videos agree)

        Rules enforced:
        - Subject: 2-5 words, boring/internal-looking (Hormozi: "boring = gets opened")
        - Body: 40-80 words (2026 benchmark: reply rates drop above 80 words)
        - No links, no attachments, no urgency, no spam trigger words
        - CTA: yes/no question, lowest friction possible
        """
        # Sector-routed copy (plumbing / HVAC / dental). The previous version
        # carried ONE body for every recipient plus an invented "11 extra
        # bookings" proof line; both are gone. sector_copy raises
        # UnsupportedSector for anything outside the three sectors instead of
        # sending a plumber's email to a storage unit.
        subject, body = sector_copy_draft(lead)
        facts = self._lead_facts(lead)
        if self.llm.available:
            body = await self._humanize(facts, body)
        # Phase 2 evaluator-optimizer: the deterministic rubric critiques the
        # draft, the model revises against the findings (max 3 iterations),
        # and the best-scoring candidate wins. Without a key this is a no-op
        # that returns the (already lint-compliant) template draft.
        result = await draft_with_critique(
            initial_subject=subject, initial_body=body, facts=facts,
            rate_email=self.rate_email, llm=self.llm,
            memory=memory_block(self.state, "Outreach"))
        self._last_draft_result = result
        return result.subject, result.body

    @staticmethod
    def _note_draft_metrics(report: dict, result) -> None:
        """Fold one DraftResult into the run report (quality is a metric)."""
        m = report.setdefault("metrics", {})
        m["drafts_scored"] = m.get("drafts_scored", 0) + 1
        m["draft_score_sum"] = m.get("draft_score_sum", 0) + result.score
        m["draft_score_avg"] = round(m["draft_score_sum"] / m["drafts_scored"], 2)
        m["draft_revision_steps"] = m.get("draft_revision_steps", 0) \
            + max(0, result.iterations - 1)

    # ---------- ANCHOR human-writer rules (doc 4) ----------
    @staticmethod
    def _lead_facts(lead: dict) -> str:
        """PII-safe facts about THIS lead for per-lead personalization. Only
        facts already public in the lead row — never invented."""
        website = str(lead.get("website") or "").strip()
        status = str(lead.get("website_status") or "").strip()
        site = "No website at all" if not website else (
            f"Has a website{'; ' + status if status else ''}")
        facts = [
            f"Business: {lead.get('name', '')}",
            f"Category: {lead.get('vertical', '')}",
            f"City: {lead.get('city', '')}",
            f"Google rating: {lead.get('rating', '')} ({lead.get('reviews', '')} reviews)",
            site,
        ]
        if lead.get("instagram"):
            facts.append(f"Instagram active: @{lead.get('instagram')}")
        facts.append(f"Observed bottleneck: {Pipeline._hook(lead)}")
        return "\n".join(facts)

    async def _humanize(self, facts: str, draft: str) -> str:
        """Combined framework humanizer: 5-part B2B + PPC + Hormozi + 30/30/50.

        Key principles from all 14 videos:
        1. 5-Part Framework: Context Line, Problem-Solution, Cost of Inaction,
           Social Proof, Soft CTA (EmailAwesome 2026)
        2. PPC: Pain Point first, Partial Solution, then CTA (Killer Cold Emails)
        3. Signal-based personalization: Open with trigger, not "I came across"
        4. Social proof: One company, one result, one sentence. Specificity = credibility.
        5. Cost of inaction: Make them imagine not acting (optional but powerful)
        6. Low-friction CTA: Ask for reply, not meeting. "Does this resonate?"
        7. 40-80 words (2026 benchmark: reply rates drop above 80 words)
        8. No links in first email (deliverability rule)
        """
        prompt = (
            "Rewrite this cold email using the 5-part B2B framework. "
            "You are a sharp human copywriter. Structure:\n\n"
            "1. CONTEXT LINE: Open with a specific trigger about THEIR business "
            "(their rating, review count, no website, missed calls). Prove research.\n"
            "2. PROBLEM-SOLUTION: One sentence on their pain. One sentence on your fix. "
            "No features, no company history.\n"
            "3. COST OF INACTION (optional): One question making them imagine not acting. "
            "Example: 'How many jobs are you losing to missed calls this month?'\n"
            "4. SOCIAL PROOF: One comparable company, one specific result, one sentence. "
            "Example: 'A plumbing shop in Houston added 11 extra bookings in month one.'\n"
            "5. SOFT CTA: Ask for a reply, not a meeting. 'Does this apply to you?' "
            "or 'Worth a quick look?' -- never 'Book a 30-min call'.\n\n"
            "RULES:\n"
            "- 40-80 words total (2026 benchmark: reply rates drop above 80 words)\n"
            "- 3-4 sentences max\n"
            "- No links in first email\n"
            "- No urgency, no spam words\n"
            "- Pattern interrupt opener (not 'I came across' or 'I hope this finds you well')\n"
            "- Vary sentence structures so it does not sound templated\n"
            "- Keep every fact accurate\n\n"
            "STRICTLY BANNED:\n"
            "- Em-dashes, en-dashes\n"
            "- AI cliches: delve, testament, beacon, tapestry, furthermore, moreover, "
            "plethora, synergy, 'in today\'s world', 'it is important to note', "
            "'at the end of the day', 'I hope this email finds you well', 'I came across'\n"
            "- Three-part lists\n"
            "- Hype / spam words: free, guarantee, risk-free, buy now, urgent, "
            "save money, best price, increase revenue, click here\n"
            "- Starting with 'Hi [Name] team' (too formal, pattern interrupt instead)\n\n"
            f"FACTS:\n{facts}\n\nDRAFT:\n{draft}"
        )
        polished = (await self.llm.complete(prompt) or "").strip()
        if not polished:
            return self._sanitize_ai_tells(draft)
        violations = self._lint_email_rules(polished)
        if not violations:
            return polished[:1000]
        # Skill rule: rewrite immediately when the rules are violated.
        again = (await self.llm.complete(
            "Your email still violates the cold-email rules. Fix ALL of these and "
            "return the full rewritten email, keeping every fact accurate:\n"
            + "\n".join(f"- {v}" for v in violations)
            + f"\n\nFACTS:\n{facts}\n\nDRAFT:\n{polished}"
        ) or "").strip()
        if again and not self._lint_email_rules(again):
            return again[:1000]
        # Last resort: sanitize the (compliant) template draft.
        return self._sanitize_ai_tells(again or draft)

    @classmethod
    def _lint_spam_words(cls, text: str) -> list[str]:
        """Return every banned spam trigger word found (regex word boundaries,
        so 'buy now,' and 'Urgent:' still match)."""
        lowered = (text or "").lower()
        return [w for w in SPAM_WORDS
                if re.search(rf"\b{re.escape(w)}\b", lowered)]

    @classmethod
    def _count_sentences(cls, text: str) -> int:
        """Sentence count. Splits on terminal punctuation followed by
        whitespace and a capital letter so decimals like '4.8' don't count."""
        text = (text or "").strip()
        return len(re.findall(r"[.!?]+\s+[A-Z0-9]", text)) + 1

    @classmethod
    def _lint_email_rules(cls, text: str) -> list[str]:
        """Full cold-email + humanizer rule check. Returns every violation."""
        text = (text or "").strip()
        violations: list[str] = []
        words = len(text.split())
        if words > 100:
            violations.append(f"{words} words (max 100)")
        sentences = cls._count_sentences(text)
        if not 3 <= sentences <= 4:
            violations.append(f"{sentences} sentences (want 3-4)")
        if re.search(r"https?://|www\.", text):
            violations.append("contains a link")
        if "?" not in text:
            violations.append("no low-friction CTA question")
        violations.extend(cls._lint_ai_tells(text))
        for w in cls._lint_spam_words(text):
            violations.append(f"spam trigger word '{w}'")
        return violations

    @classmethod
    def rate_email(cls, subject: str, body: str, facts: str = "") -> tuple[int, list[str]]:
        """Rate a draft 1-10 against the cold-email + humanizer skills.

        Returns (score, notes). Used to gate quality and to report a concrete
        rating for each email (owner-facing metric)."""
        score = 10
        notes: list[str] = []
        body = (body or "").strip()
        words = len(body.split())
        if words > 100:
            score -= 2
            notes.append(f"{words} words (max 100)")
        sentences = cls._count_sentences(body)
        if not 3 <= sentences <= 4:
            score -= 1
            notes.append(f"{sentences} sentences (want 3-4)")
        if re.search(r"https?://|www\.", body):
            score -= 1
            notes.append("contains a link")
        if "?" not in body:
            score -= 1
            notes.append("no low-friction CTA question")
        tells = cls._lint_ai_tells(body)
        if tells:
            score -= 2
            notes.append(f"AI tells: {', '.join(tells)}")
        for w in cls._lint_spam_words(body):
            score -= 1
            notes.append(f"spam trigger word '{w}'")
        subj_words = len((subject or "").split())
        if not 2 <= subj_words <= 4:
            score -= 1
            notes.append(f"subject {subj_words} words (want 2-4)")
        # Personalization: the body must reference a concrete lead detail
        # (business name, city, or rating) so it isn't a pasted template.
        if facts:
            name = next((l.split(":", 1)[1].strip() for l in facts.splitlines()
                         if l.lower().startswith("business:")), "")
            city = next((l.split(":", 1)[1].strip() for l in facts.splitlines()
                         if l.lower().startswith("city:")), "")
            rating = next((l.split(":", 1)[1].split("(")[0].strip()
                           for l in facts.splitlines()
                           if l.lower().startswith("google rating:")), "")
            tokens = [t for t in (name, city, rating) if t]
            if not any(t in body for t in tokens):
                score -= 1
                notes.append("no concrete lead detail referenced (template-y)")
        return max(1, score), notes

    @classmethod
    def _lint_ai_tells(cls, text: str) -> list[str]:
        """Return every banned AI tell found in the text (doc 4 list)."""
        lowered = (text or "").lower()
        return [label for label, token in AI_TELL_PATTERNS if token.lower() in lowered]

    @classmethod
    def _sanitize_ai_tells(cls, text: str) -> str:
        """Last-resort cleanup: replace em/en-dashes and strip banned cliches."""
        text = (text or "").replace("—", ", ").replace("–", ", ")
        for _, token in AI_TELL_PATTERNS:
            text = re.sub(rf"\b{re.escape(token)}\b", "", text, flags=re.IGNORECASE)
        return re.sub(r"\s{2,}", " ", text).strip()

    @classmethod
    def _ig_failure_reason(cls, error: str) -> str:
        """Map a Meta/Composio DM error to the doc's outcome vocabulary."""
        lowered = (error or "").lower()
        if any(h in lowered for h in IG_COLD_START_HINTS):
            return "cold_start"
        if any(h in lowered for h in IG_WINDOW_HINTS):
            return "window"
        if any(h in lowered for h in IG_NO_PSID_HINTS):
            return "no_psid"
        return "other"

    async def _ig_halt_record(self, reason: str) -> None:
        """Write the halted record the doc requires when IG pre-flight fails."""
        previous = await self.sheets.read_tab("Outreach")
        merged = (previous[1:] if previous and previous[0] else []) + [
            [1, "Instagram", "", "", "instagram", "HALTED", "",
             "Halted", f"IG stage halted: {reason}"],
        ]
        await self.sheets.write_tab("Outreach", merged)

    @staticmethod
    def _ig_first_message(lead: dict) -> str:
        return (f"Quick one. Who texts back your missed calls after 5pm? "
                f"Saw your {lead.get('rating')} star profile in {lead.get('city')}.")

    @staticmethod
    def _outreach_row(idx: int, name: str, lead_id: str, email: str, channel: str,
                      subject: str, body: str, status: str, note: str) -> list:
        return [idx, name, lead_id, email, channel, subject, body, status, note]

    # ---------- CLI ----------
    @staticmethod
    def build_report_text(report: dict, sheet_url: str = "") -> str:
        metrics = report.get("metrics", {})
        lines = [
            f"Run {report.get('run_id', '?')} [{report.get('mode', '?')}] -> {report.get('status', '?')}",
            f"Started: {report.get('started_at', '?')}",
        ]
        if report.get("error"):
            lines.append(f"Error: {report['error']}")
        if metrics:
            lines.append(f"Candidates: {metrics.get('candidates', 0)}")
            tiers = metrics.get("tiers", {})
            if tiers:
                lines.append("Tiers: " + ", ".join(f"{k}={v}" for k, v in tiers.items()))
            lines.append(f"Emails: sent={metrics.get('emails_sent', 0)} "
                         f"drafted={metrics.get('emails_drafted', 0)} "
                         f"skipped={metrics.get('emails_skipped', 0)}")
            lines.append("IG: " + " ".join(
                f"{k}={metrics.get(k, 0)}" for k in
                ("ig_sent", "ig_skipped", "ig_failed", "ig_queued")))
            if metrics.get("ig_skipped_no_email"):
                lines.append("ℹ️ IG skips: an email attempt is required before a DM "
                             "(run 'full' or 'outreach-email' first)")
            if metrics.get("ig_halted"):
                lines.append(f"⚠️ IG stage HALTED: {metrics['ig_halted']}")
            if "research_batch" in metrics:
                note = ""
                if metrics.get("research_awaiting_confirm"):
                    note = " - waiting for /next"
                elif metrics.get("research_already_ran"):
                    note = " - already ran today"
                lines.append(
                    f"Research batch: {metrics['research_batch']} drafted "
                    f"({metrics.get('research_no_problem', 0)} skipped, nothing seen){note}"
                )
            if metrics.get("all_leads_error"):
                lines.append("⚠️ Master list NOT updated (a tab could not be read): "
                             f"{metrics['all_leads_error']}")
            elif "all_leads" in metrics:
                lines.append(
                    f"Master list: {metrics['all_leads']} leads "
                    f"({metrics.get('all_leads_with_email', 0)} with email, "
                    f"{metrics.get('all_leads_with_phone', 0)} with a phone)"
                )
            if metrics.get("sheet_tabs_failed"):
                lines.append(f"⚠️ Sheet write FAILED for {metrics['sheet_tabs_failed']} tab(s) "
                             f"({metrics.get('sheet_tabs_written', 0)} ok)")
        if sheet_url:
            lines.append(f"Sheet: {sheet_url}")
        return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="lead-gen-agents pipeline")
    parser.add_argument("--mode", default="full")
    parser.add_argument("--dry-run", action="store_true",
                        help="Run offline with mock data (no Composio/GitHub/Telegram side effects)")
    args = parser.parse_args()

    # Scheduled workflow runs pass an EMPTY --mode (inputs.mode == ''), which
    # argparse choices would reject (exit 2 — the 2026-08-05 scheduled-run
    # failure). Normalize empty/whitespace to the 'full' default; still reject
    # genuinely unknown modes so a typo can't silently run the wrong stage.
    mode = (args.mode or "").strip().lower() or "full"
    if mode not in MODES:
        parser.error(f"argument --mode: invalid choice: '{mode}' (choose from {', '.join(MODES)})")

    import os
    if args.dry_run:
        os.environ["DRY_RUN"] = "1"
    settings = Settings.load()  # merges local .env; Actions secrets already in env
    import src.core.logging as logmod
    logmod.setup_logging()

    report = asyncio.run(Pipeline(settings).run(mode))
    sheet_url = ""
    if not settings.dry_run and settings.google_sheet_id:
        sheet_url = f"https://docs.google.com/spreadsheets/d/{settings.google_sheet_id}"
    print(Pipeline.build_report_text(report, sheet_url))
    # run() swallows the exception, records status=FAILED and returns the report,
    # so the process used to exit 0 and Actions painted a failed run green (the
    # 2026-10-25 run: 918 queries, 0 leads, conclusion=success). Exit non-zero so
    # the run goes red and GitHub's failure mail fires, but only AFTER the report
    # is printed and run()'s finally block has committed state + alerted Telegram.
    if report.get("status") == "FAILED":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
