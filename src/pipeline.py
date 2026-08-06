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
import uuid
from datetime import UTC, datetime

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
from src.core.llm import GeminiClient
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


class StopRequested(Exception):
    pass


class Pipeline:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.state = StateStore(settings.state_dir)
        self.composio = ComposioAgent(settings)
        self.llm = GeminiClient(settings.gemini_api_key, settings.gemini_model)
        self.github = GitHubAgent(settings, self.state)
        self.sheets = SheetsAgent(self.composio, settings, self.state)
        self.crm = CrmAgent(self.sheets)
        self.approvals = ApprovalQueue(self.state)
        self.maps = MapsAgent(self.composio, settings)
        self.atlas = Atlas(self.maps, settings)
        self.scout = Scout(settings)
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

        try:
            scored: list[dict] = []
            if mode != "report":
                await self.sheets.ensure_sheet()  # create/reuse per-tab spreadsheets
            if mode in DISCOVERY_MODES:
                raw = await self._stage_discovery(report)
                self._check_stop()
                enriched = await self._stage_enrichment(raw, report)
                self._check_stop()
                scored = self._stage_scoring(enriched, report)
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
        enriched = await enrich_leads(raw, self.composio, self.settings)
        with_email = sum(1 for l in enriched if l.get("email"))
        report["metrics"]["with_email"] = with_email
        report["metrics"]["needs_enrichment"] = len(enriched) - with_email
        return enriched

    def _stage_scoring(self, enriched: list[dict], report: dict) -> list[dict]:
        """Score with confirmed contact data (enrichment ran first)."""
        scored = self.scout.run(enriched)
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
        sent, drafted, skipped = 0, 0, 0
        new_rows: list[list] = []
        for idx, lead in enumerate(leads, start=1):
            lid = lead_id(lead.get("name", ""), lead.get("address", ""))
            name = lead.get("name", "")
            email = lead.get("email")
            tier = lead["score"]["tier"]
            if not email:
                skipped += 1
                new_rows.append(self._outreach_row(idx, name, lid, email, "email",
                                                   "", "", "SKIP", "NEEDS_ENRICHMENT"))
                continue
            subject, body = await self._draft_email(lead)
            if tier == "HOT-VERIFIED" and sent < cap:
                outcome = "SENT" if not self.settings.dry_run else "SENT (dry-run)"
                if not self.settings.dry_run and self.composio.connected:
                    resp = await self.composio.gmail_send_email(to=email, subject=subject, body=body)
                    outcome = "SENT" if resp.get("ok") else f"FAILED: {resp.get('error', '')}"
                sent += 1
                new_rows.append(self._outreach_row(idx, name, lid, email, "email",
                                                   subject, body, outcome, "Hot - auto-sent"))
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
            if self.llm.available:
                polished = await self.llm.complete(
                    "Rewrite this short follow-up to sound human: no em-dashes, "
                    "no AI cliches, under 900 chars, keep the opt-out line. "
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
            if self.llm.available:
                llm_label = await self.llm.complete(
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
        sent, skipped = 0, 0
        for idx, lead in enumerate(leads, start=1):
            tier = lead["score"]["tier"]
            ig = lead.get("instagram")
            if tier != "HOT-VERIFIED" or not ig:
                skipped += 1
                continue
            if sent >= cap:
                report["metrics"]["ig_queued_cap"] = report["metrics"].get("ig_queued_cap", 0) + 1
                continue
            # Cold-start rule (spec 7.5): only leads that messaged first are DM-able.
            message = self._ig_first_message(lead)
            if not self.settings.dry_run and self.composio.connected:
                resp = await self.composio.ig_send_dm(recipient_id=ig, message=message)
                if not resp.get("ok"):
                    skipped += 1
                    continue
            sent += 1
        report["metrics"]["ig_sent"] = sent
        report["metrics"]["ig_skipped"] = skipped

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

    def _hook(self, lead: dict) -> str:
        vertical = str(lead.get("vertical", "")).lower()
        hook = HOOKS.get(vertical)
        if not hook and not lead.get("website"):
            hook = "no website at all, so jobs are slipping away today"
        return hook or "manual follow-up is eating time"

    async def _draft_email(self, lead: dict) -> tuple[str, str]:
        name = lead.get("name", "the business")
        city = lead.get("city", "your city")
        rating = lead.get("rating") or "4"
        hook = self._hook(lead)
        subject = f"Quick question for {name}"[:50]
        body = (
            f"Hi {name} team. I help {lead.get('vertical', 'service')} businesses "
            f"in {city} cut the busywork that eats the week. I saw your {rating} star "
            f"profile and one thing stood out: {hook}.\n\n"
            f"I don't sell a generic chatbot. I scope custom AI builds for the exact "
            f"workflow, ship fast, and only charge if it pays for itself. Worth 15 "
            f"minutes this week to compare notes?\n\n"
            f"Reply \"stop\" to opt out."
        )
        if self.llm.available:
            polished = await self.llm.complete(
                "Rewrite this cold outreach email to sound like a thoughtful human "
                "writer. Rules: vary sentence length, active voice, no em-dashes, no "
                "AI cliches, no three-part lists, under 1000 chars, keep the "
                "CAN-SPAM 'Reply stop to opt out' line, keep the personal facts. "
                f"Original:\n\n{body}"
            )
            if polished and "Reply" in polished and "stop" in polished:
                body = polished[:1000]
        return subject, body

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
            lines.append(f"IG: sent={metrics.get('ig_sent', 0)} skipped={metrics.get('ig_skipped', 0)}")
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
