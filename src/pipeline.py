"""Pipeline orchestrator (spec sections 4.2, 7, 10).

Wires the 6 agents into a stage state machine:

    discovery -> enrichment -> scoring -> outreach-email -> outreach-ig -> pipeline

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
from datetime import UTC, datetime
from typing import ClassVar

from src.agents.atlas import Atlas
from src.agents.composio_agent import ComposioAgent
from src.agents.crm_agent import CrmAgent
from src.agents.github_agent import GitHubAgent
from src.agents.maps_agent import MapsAgent
from src.agents.scout import Scout
from src.agents.sheets_agent import SheetsAgent
from src.approvals import ApprovalQueue
from src.core.config import Settings
from src.core.ident import lead_id
from src.core.llm import GeminiPool, LLMUsage
from src.core.logging import TelegramNotifier
from src.core.state import StateStore
from src.enrichment import enrich_leads
from src.followups import FOLLOWUP_INTERVALS_DAYS, build_followup_body, is_due, next_interval_days
from src.inbound import classify_reply, parse_sender_email, suggested_reply

log = logging.getLogger(__name__)

# Hard caps: clamped in code regardless of config (spec 7.5).
EMAIL_CAP_ABSOLUTE = 50
IG_CAP_ABSOLUTE = 15

MODES = ("full", "discovery", "enrichment", "outreach-email", "outreach-ig",
         "followups", "inbound", "report")

# Modes that run discovery -> enrichment -> scoring (lead generation path).
DISCOVERY_MODES = ("full", "discovery", "enrichment", "outreach-email", "outreach-ig")

# CRM statuses the pipeline moves leads through (AI employee lifecycle).
STATUS_DRAFTED = "DRAFTED"
STATUS_CONTACTED = "CONTACTED"
STATUS_REPLIED = "REPLIED-INTERESTED"
STATUS_OBJECTION = "OBJECTION"
STATUS_UNSUBSCRIBED = "UNSUBSCRIBED"

# Vertical-specific AI-bottleneck hooks (spec 7.5 + outreach-templates.md).
HOOKS = {
    "plumber": "after-hours drain and water-heater calls are landing in voicemail",
    "hvac": "peak-season calls don't stop at 5pm and are being missed",
    "cleaning": "review follow-up and quote chasing are eating the week",
    "mechanic": "missed calls and manual estimate follow-up are costing jobs",
    "dental": "recall reminders and new-patient follow-up are manual",
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
    # Cold-email skill: short, boring, internal-looking subject lines (2-4
    # words), varied per lead so no two emails sound identical (spintax
    # variation). Keyed by vertical; defaults to the generic pool.
    SUBJECT_POOL: ClassVar[dict[str, tuple[str, ...]]] = {
        "plumber": ("After-hours calls", "Missed calls", "Weekend calls", "Booking follow-up"),
        "hvac": ("After-hours calls", "Peak-season calls", "Missed calls", "Booking follow-up"),
        "cleaning": ("Quote follow-up", "Review replies", "New clients", "Booking follow-up"),
        "mechanic": ("Missed calls", "Estimate follow-up", "Service reminders", "Booking follow-up"),
        "dental": ("Recall reminders", "New-patient follow-up", "No-show follow-up", "Booking follow-up"),
        "default": ("Missed calls", "Quick question", "Follow up", "After-hours calls"),
    }

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
        # deterministic behavior when the key is absent).
        self.maps = MapsAgent(self.composio, settings, llm=self.fast_llm)
        self.atlas = Atlas(self.maps, settings)
        self.scout = Scout(settings, llm=self.fast_llm)
        self.notifier = TelegramNotifier(settings.telegram_bot_token, settings.telegram_alert_chat_id)

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

            if mode in ("full", "outreach-email"):
                await self._stage_outreach_email(scored, report)
                self._check_stop()
                # Send drafts the owner approved via Telegram (Step 04).
                await self._stage_send_approved(report)
                self._check_stop()

            if mode in ("full", "outreach-ig"):
                await self._stage_outreach_ig(scored, report)
                self._check_stop()

            if mode == "full":
                await self._stage_pipeline(scored, report)

            if mode == "followups":
                # AI employee loop: Day 3/7/14 cadence on the CRM (Step 06).
                await self._stage_followups(report)

            if mode == "inbound":
                # AI employee loop: read + classify lead replies (Step 06).
                await self._stage_inbound(report)

            # Persist long-term lead memory (queued), then flush ALL tab writes
            # once at the end of the run (quota-safe: reads cached, writes
            # batched, throttles retried with backoff in execute_action).
            if mode != "report":
                await self.crm.save()
            if mode != "report" and self.sheets.has_pending_writes():
                ok_tabs, failed_tabs = await self.sheets.flush()
                report["metrics"]["sheet_tabs_written"] = ok_tabs
                report["metrics"]["sheet_tabs_failed"] = failed_tabs
                if failed_tabs:
                    log.error("sheet flush: %d tab(s) failed to write", failed_tabs)

            report["status"] = "COMPLETED"
            if not self.settings.dry_run:
                sheet_url = (f"https://docs.google.com/spreadsheets/d/{self.settings.google_sheet_id}"
                             if self.settings.google_sheet_id else "")
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
        if not self.settings.dry_run:
            raw = self._filter_new_leads(raw)  # cross-run dedupe (spec 10)
        report.setdefault("metrics", {})["candidates"] = len(raw)
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
        """Send HOT leads now; queue WARM leads as drafts awaiting approval.

        Drafts are registered in the PII-safe approval queue (lead_id hashes)
        and stored in the private Outreach sheet (status NEEDS_APPROVAL); the
        owner approves them in Telegram and _stage_send_approved sends them.
        """
        await self.crm.load()
        cap = min(int(self.settings.crit("emails_per_run_max", EMAIL_CAP_ABSOLUTE)), EMAIL_CAP_ABSOLUTE)
        sent, drafted, skipped, bounced = 0, 0, 0, 0
        new_rows: list[list] = []
        for idx, lead in enumerate(leads, start=1):
            lid = lead_id(lead.get("name", ""), lead.get("address", ""))
            name = lead.get("name", "")
            email = lead.get("email")
            tier = lead["score"]["tier"]
            # Every lead counts as an email attempt (sent / drafted / NEEDS
            # ENRICHMENT) for the IG eligibility rule (doc 4).
            self._email_attempted.add(lid)
            if not email:
                skipped += 1
                new_rows.append(self._outreach_row(idx, name, lid, email, "email",
                                                   "", "", "SKIP", "NEEDS_ENRICHMENT"))
                continue
            subject, body = await self._draft_email(lead)
            if tier == "HOT-VERIFIED" and sent < cap:
                outcome = "SENT (dry-run)" if self.settings.dry_run else "SENT"
                note = "Hot - auto-sent"
                if not self.settings.dry_run and self.composio.connected:
                    resp = await self.composio.gmail_send_email(to=email, subject=subject, body=body)
                    if resp.get("ok"):
                        outcome = "SENT"
                    else:
                        # Doc: failed sends are marked BOUNCED in the sheet.
                        outcome = "BOUNCED"
                        note = f"BOUNCED: {resp.get('error', '')[:150]}"
                        bounced += 1
                sent += 1
                new_rows.append(self._outreach_row(idx, name, lid, email, "email",
                                                   subject, body, outcome, note))
                self.crm.upsert(lid, name=name, email=email,
                                instagram=lead.get("instagram", ""), tier=tier)
                self.crm.set_status(lid, STATUS_CONTACTED)
                self.crm.set_last_contact(lid)
                self.crm.schedule_followup(lid, FOLLOWUP_INTERVALS_DAYS[0])
                self.crm.append_timeline(lid, "email-sent", subject)
            elif tier == "WARM":
                self.approvals.register(lid)
                self.crm.upsert(lid, name=name, email=email,
                                instagram=lead.get("instagram", ""), tier=tier,
                                status=STATUS_DRAFTED)
                self.crm.append_timeline(lid, "drafted", subject)
                drafted += 1
                new_rows.append(self._outreach_row(idx, name, lid, email, "email",
                                                   subject, body, "NEEDS_APPROVAL", "Needs review (Warm)"))
            else:
                skipped += 1
                new_rows.append(self._outreach_row(idx, name, lid, email, "email",
                                                   "", "", "SKIP", tier))
        # Carry forward the previous Outreach table so approved drafts and sent
        # history survive between runs, then append this run's rows.
        previous = await self.sheets.read_tab("Outreach")
        merged = (previous[1:] if previous and previous[0] else []) + new_rows
        await self.sheets.write_tab("Outreach", merged)
        report["metrics"]["emails_sent"] = sent
        report["metrics"]["emails_bounced"] = bounced
        report["metrics"]["emails_drafted"] = drafted
        report["metrics"]["emails_skipped"] = skipped

    async def _stage_send_approved(self, report: dict) -> None:
        """Send WARM drafts the owner approved via Telegram (Step 04).

        Reads the Outreach table (current-run queued rows or last flushed
        table), sends rows whose lead_id is approved, marks rejections, and
        requeues the table so nothing is lost.
        """
        await self.crm.load()
        raw = await self.sheets.read_tab("Outreach")
        if not raw or len(raw) < 2:
            return
        header = [str(h).strip() for h in raw[0]]
        col = {name: i for i, name in enumerate(header)}
        approved = self.approvals.approved()
        rejected = self.approvals.rejected()
        sent = failed = rejected_count = 0
        for row in raw[1:]:
            lid = row[col["Lead ID"]] if col.get("Lead ID") is not None and col["Lead ID"] < len(row) else ""
            status = row[col["Status"]] if col.get("Status") is not None and col["Status"] < len(row) else ""
            if status != "NEEDS_APPROVAL" or not lid:
                continue
            if lid in rejected:
                row[col["Status"]] = "REJECTED"
                rejected_count += 1
                continue
            if lid not in approved:
                continue
            email = row[col["Email"]] if col.get("Email") is not None and col["Email"] < len(row) else ""
            subject = row[col["Subject"]] if col.get("Subject") is not None and col["Subject"] < len(row) else ""
            body = row[col["Body"]] if col.get("Body") is not None and col["Body"] < len(row) else ""
            outcome = "FAILED: no email on draft"
            if email and self.settings.dry_run:
                outcome = "SENT"                      # dry-run simulates the send
            elif email and self.composio.connected:
                resp = await self.composio.gmail_send_email(to=email, subject=subject, body=body)
                outcome = "SENT" if resp.get("ok") else f"FAILED: {resp.get('error', '')}"
            elif email:
                outcome = "FAILED: gmail not connected"
            if outcome == "SENT":
                row[col["Status"]] = "SENT"
                if col.get("Send Date") is not None and col["Send Date"] < len(row):
                    row[col["Send Date"]] = datetime.now(UTC).date().isoformat()
                sent += 1
                self._email_attempted.add(lid)  # an approved send is an email attempt
                self.crm.set_status(lid, STATUS_CONTACTED)
                self.crm.set_last_contact(lid)
                self.crm.schedule_followup(lid, FOLLOWUP_INTERVALS_DAYS[0])
                self.crm.append_timeline(lid, "approved+sent", subject)
            else:
                failed += 1
        await self.sheets.write_tab("Outreach", raw[1:])
        report["metrics"]["emails_approved_sent"] = sent
        report["metrics"]["emails_approved_failed"] = failed
        report["metrics"]["emails_rejected"] = rejected_count

    async def _stage_followups(self, report: dict) -> None:
        """AI employee loop: send due Day 3/7/14 follow-ups from the CRM."""
        await self.crm.load()
        rows = await self.crm.load()
        due = [r for r in rows.values() if is_due(r, datetime.now(UTC).date())]
        cap = min(int(self.settings.crit("emails_per_run_max", EMAIL_CAP_ABSOLUTE)), EMAIL_CAP_ABSOLUTE)
        sent = failed = 0
        for row in due:
            if sent >= cap:
                report["metrics"]["followups_capped"] = len(due) - sent
                break
            lid = row["Lead ID"]
            name = row.get("Name") or "the business"
            sent_n = int(row.get("Follow-ups Sent") or 0)
            subject = f"Quick question for {name}"[:50]
            body = build_followup_body(row, sent_n)
            if self.fast_llm.available:
                polished = await self.fast_llm.complete(
                    "Rewrite this short follow-up to sound human: no em-dashes, "
                    "no AI cliches, under 900 chars. "
                    f"Original:\n\n{body}"
                )
                if polished:
                    body = polished[:900]
            ok = False
            if self.settings.dry_run:
                ok = True
            elif self.composio.connected and row.get("Email"):
                resp = await self.composio.gmail_send_email(
                    to=row["Email"], subject=subject, body=body)
                ok = resp.get("ok", False)
            if ok:
                self.crm.record_followup_sent(lid)
                nxt = next_interval_days(sent_n + 1)
                if nxt is not None:
                    self.crm.schedule_followup(lid, nxt)
                else:
                    self.crm.set_status(lid, "LOST")  # cadence exhausted -> cold
                self.crm.append_timeline(lid, "followup", f"step {sent_n + 1}/3: {subject}")
                sent += 1
            else:
                failed += 1
        report["metrics"]["followups_sent"] = sent
        report["metrics"]["followups_due"] = len(due)
        report["metrics"]["followups_failed"] = failed

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
            llm_label = ""
            if self.fast_llm.available:
                llm_label = await self.fast_llm.complete(
                    "Classify this lead's reply as exactly one of: INTERESTED, "
                    "PRICE_OBJECTION, STOP, QUESTION. Reply with only the label.\n"
                    f"Lead: {lead.get('Name')}\nReply: {text[:800]}"
                )
            kind = classify_reply(text, llm_label)
            self.crm.append_timeline(lid, "reply", f"[{kind}] {text[:300]}")
            self.crm.upsert(lid, name=lead.get("Name", ""))
            self.crm.set_status(lid, {
                "INTERESTED": STATUS_REPLIED,
                "PRICE_OBJECTION": STATUS_OBJECTION,
                "STOP": STATUS_UNSUBSCRIBED,
                "QUESTION": "QUESTION",
            }.get(kind, "QUESTION"))
            if kind == "STOP":
                if not self.settings.dry_run:
                    await self.composio.execute_action(
                        self.composio.slug("mail_reply_thread"),
                        {"thread_id": tid,
                         "message_body": suggested_reply("STOP", lead),
                         "recipient_email": sender})
                    auto_replied += 1
                self.crm.append_timeline(lid, "auto-reply", "opt-out confirmed")
            else:
                title = {"INTERESTED": "🎯 Interested",
                         "PRICE_OBJECTION": "💰 Price objection",
                         "QUESTION": "❓ Question"}.get(kind, "ℹ️ Reply")
                msg = (f"{title} — {lead.get('Name')}\n{lead.get('Email')}\n\n"
                       f"Reply: {text[:300]}\n\n"
                       f"Suggested: {suggested_reply(kind, lead)}")
                if not self.settings.dry_run:
                    await self.notifier.notify(msg)
                    notified += 1
            processed += 1
        self.state.save_if_changed("inbound_seen", {"threads": sorted(seen)[-200:]})
        report["metrics"]["inbound_scanned"] = len(threads)
        report["metrics"]["inbound_processed"] = processed
        report["metrics"]["inbound_owner_notified"] = notified
        report["metrics"]["inbound_auto_replied"] = auto_replied

    async def _stage_outreach_ig(self, leads: list[dict], report: dict) -> None:
        cap = min(int(self.settings.crit("ig_dms_per_24h_max", IG_CAP_ABSOLUTE)), IG_CAP_ABSOLUTE)

        # Pre-flight (doc 4): if Meta flagged/restricted the connected account,
        # the ENTIRE Instagram stage halts and a halted record is written.
        # Unknown status fails OPEN (never halt on a missing tool).
        if not self.settings.dry_run and self.composio.connected:
            status = await self.composio.ig_account_status()
            if status and status.get("restricted"):
                reason = str(status.get("reason") or "Instagram account flagged by Meta")
                report["metrics"]["ig_halted"] = reason
                await self._ig_halt_record(reason)
                log.warning("instagram stage halted: %s", reason)
                return

        sent = skipped = failed = queued = 0
        no_email = 0
        new_rows: list[list] = []
        for idx, lead in enumerate(leads, start=1):
            lid = lead_id(lead.get("name", ""), lead.get("address", ""))
            name = lead.get("name", "")
            tier = lead["score"]["tier"]
            ig = lead.get("instagram") or ""
            ig_verified = lead.get("ig_status") == "VERIFIED" or bool(ig)
            if tier != "HOT-VERIFIED" or not ig_verified:
                skipped += 1
                new_rows.append(self._outreach_row(
                    idx, name, lid, "", "instagram", "", "",
                    "Skipped — not eligible", f"tier={tier} ig={'yes' if ig else 'no'}"))
                continue
            # Doc: DM eligible ONLY when an email attempt was made this run.
            if lid not in self._email_attempted:
                skipped += 1
                no_email += 1
                new_rows.append(self._outreach_row(
                    idx, name, lid, "", "instagram", "", "",
                    "Skipped — no email attempt this run",
                    "IG eligibility requires an email attempt in this run"))
                continue
            message = self._ig_first_message(lead)
            if sent >= cap:
                queued += 1
                new_rows.append(self._outreach_row(
                    idx, name, lid, "", "instagram", "", message,
                    "Queued (cap hit)", f"{cap}/24h reached"))
                continue
            status, note = "SENT", "Hot - DM sent"
            if not self.settings.dry_run and self.composio.connected:
                resp = await self.composio.ig_send_dm(recipient_id=ig, message=message)
                if not resp.get("ok"):
                    err = str(resp.get("error", ""))[:200]
                    reason = self._ig_failure_reason(err)
                    if reason == "cold_start":
                        status, note = "Skipped — IG cold-start", err or "recipient must message first"
                        skipped += 1
                    elif reason == "window":
                        status, note = "Skipped — IG 24h window", err or "outside Meta's 24h window"
                        skipped += 1
                    elif reason == "no_psid":
                        status, note = "Skipped — IG no PSID", err or "handle needs resolving to a numeric PSID"
                        skipped += 1
                    else:
                        status, note = "Failed", err or "unknown IG error"
                        failed += 1
                else:
                    sent += 1
            else:
                sent += 1  # dry-run simulates the send
            new_rows.append(self._outreach_row(
                idx, name, lid, "", "instagram", "", message, status, note))

        # Carry forward the Outreach table so IG outcomes are recorded, then
        # append this run's IG rows (doc: outcomes must be written).
        previous = await self.sheets.read_tab("Outreach")
        merged = (previous[1:] if previous and previous[0] else []) + new_rows
        await self.sheets.write_tab("Outreach", merged)
        report["metrics"]["ig_sent"] = sent
        report["metrics"]["ig_skipped"] = skipped
        report["metrics"]["ig_failed"] = failed
        report["metrics"]["ig_queued"] = queued
        report["metrics"]["ig_skipped_no_email"] = no_email

    async def _stage_pipeline(self, leads: list[dict], report: dict) -> None:
        rows = [
            [i, lead.get("name"), lead.get("vertical"), lead.get("city"),
             lead.get("phone", ""), lead.get("email", ""), lead.get("website", ""),
             lead.get("website_status", ""), f"@{lead.get('instagram', '')}" if lead.get("instagram") else "",
             lead.get("rating"), lead.get("reviews"), lead.get("email_status", ""),
             lead["score"]["tier"], self._hook(lead), lead["score"]["total"],
             "Discovery", "Enrich + outreach", ""]
            for i, lead in enumerate(leads, start=1)
        ]
        followup = [
            [i, lead.get("name"), "Day 0: intro", "Day 3: bump",
             "Day 7: case study", "Day 14: final ask", "Queued"]
            for i, lead in enumerate(leads, start=1)
        ]
        await self.sheets.write_tab("Pipeline", rows)
        await self.sheets.write_tab("Followup", followup)
        report["metrics"]["pipeline_rows"] = len(rows)

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

    @classmethod
    def _pick_subject(cls, lead: dict) -> str:
        """2-4 word, boring, internal-looking subject (cold-email skill).

        Picks deterministically from the vertical's pool (hashed on the lead
        name) so every lead gets a varied subject and the same lead always
        gets the same one across runs (spintax variation).
        """
        vertical = str(lead.get("vertical") or "default").lower()
        pool = cls.SUBJECT_POOL.get(vertical, cls.SUBJECT_POOL["default"])
        key = lead.get("name") or lead.get("address") or ""
        return pool[sum(ord(c) for c in key) % len(pool)]

    async def _draft_email(self, lead: dict) -> tuple[str, str]:
        """Cold-email skill: <100 words, 3-4 sentences, personalized opening,
        one low-friction open-ended CTA, no links, no spam trigger words.

        The template only carries the skeleton; the humanizer pass (below)
        rewrites it per-lead so it reads like a human wrote it, then the
        skill rules are enforced programmatically.
        """
        name = lead.get("name", "the business")
        subject = self._pick_subject(lead)
        facts = self._lead_facts(lead)
        rating = lead.get("rating") or "4"
        hook = self._hook(lead)
        city = lead.get("city", "your city")
        body = (
            f"Hi {name} team. I noticed your {rating} star profile in {city} and "
            f"one thing stood out: {hook}. I build custom AI systems for "
            f"{lead.get('vertical', 'service')} businesses, scoped to your workflow "
            f"and only charged if they pay for themselves. Worth a brief chat next "
            f"week?"
        )
        if self.llm.available:
            body = await self._humanize(facts, body)
        return subject, body

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
        """Humanizer skill: Gemini rewrite, then ENFORCE both skill rule sets.

        Enforced programmatically (never trust the model): under 100 words,
        3-4 sentences, no spam trigger words, no links, personalized opening,
        low-friction CTA question, no AI tells. On any violation, one targeted
        rewrite; sanitize the template as last resort.
        """
        prompt = (
            "Rewrite this cold outreach email as a sharp human copywriter. "
            "Write like a busy human executive, not a marketing bot. Personalize "
            "it with the FACTS below: open by referencing ONE concrete detail "
            "about this specific business (its rating, no website, no online "
            "booking) so it can't be a pasted template. Rules: 3-4 sentences, "
            "under 100 words total. Short, punchy sentences under 10 words where "
            "possible. Active voice. No links, no attachments, no urgency. End "
            "with a single low-friction, open-ended CTA question like 'Worth a "
            "brief chat next week?' or 'Open to exploring this?' - never a booking "
            "link or a hard pitch. STRICTLY NO em-dashes, no AI cliches (delve, "
            "testament, beacon, tapestry, furthermore, moreover, plethora, synergy, "
            "'in today's world', 'it is important to note', 'at the end of the day', "
            "'I hope this email finds you well', 'I came across'), no three-part "
            "lists, no hype, no spam words (free, guarantee, risk-free, buy now, "
            "urgent, save money, best price). Vary your vocabulary and sentence "
            "structures so it doesn't sound like a template. Keep every fact accurate.\n\n"
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


if __name__ == "__main__":
    main()
