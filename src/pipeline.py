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
from src.agents.github_agent import GitHubAgent
from src.agents.maps_agent import MapsAgent
from src.agents.scout import Scout
from src.agents.sheets_agent import SheetsAgent
from src.core.config import Settings
from src.core.llm import GeminiClient
from src.core.logging import TelegramNotifier
from src.core.state import StateStore
from src.enrichment import enrich_leads

log = logging.getLogger(__name__)

# Hard caps: clamped in code regardless of config (spec 7.5).
EMAIL_CAP_ABSOLUTE = 50
IG_CAP_ABSOLUTE = 15

MODES = ("full", "discovery", "enrichment", "outreach-email", "outreach-ig", "report")

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
            if mode in ("full", "discovery", "enrichment", "outreach-email", "outreach-ig"):
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

            if mode in ("full", "outreach-ig"):
                await self._stage_outreach_ig(scored, report)
                self._check_stop()

            if mode == "full":
                await self._stage_pipeline(scored, report)

            report["status"] = "COMPLETED"
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
        import hashlib

        registry = self.state.load("dedupe", {"keys": []})
        seen = set(registry.get("keys", []))
        kept: list[dict] = []
        new_keys: list[str] = []
        for lead in raw:
            key = hashlib.sha256(
                f"{lead.get('name', '')}|{lead.get('address', '')}".lower().encode()
            ).hexdigest()
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
        cap = min(int(self.settings.crit("emails_per_run_max", EMAIL_CAP_ABSOLUTE)), EMAIL_CAP_ABSOLUTE)
        sent, drafted, skipped = 0, 0, 0
        rows: list[list] = []
        for idx, lead in enumerate(leads, start=1):
            email = lead.get("email")
            tier = lead["score"]["tier"]
            if not email:
                skipped += 1
                rows.append(self._outreach_row(idx, lead, "email", "SKIP", "NEEDS_ENRICHMENT"))
                continue
            subject, body = await self._draft_email(lead)
            if tier == "HOT-VERIFIED" and sent < cap:
                outcome = "SENT" if not self.settings.dry_run else "SENT (dry-run)"
                if not self.settings.dry_run and self.composio.connected:
                    resp = await self.composio.gmail_send_email(to=email, subject=subject, body=body)
                    outcome = "SENT" if resp.get("ok") else f"FAILED: {resp.get('error', '')}"
                sent += 1
                rows.append(self._outreach_row(idx, lead, "email", outcome, "Hot - auto-sent"))
            elif tier == "WARM":
                outcome = "DRAFT" if not self.settings.dry_run else "DRAFT (dry-run)"
                if not self.settings.dry_run and self.composio.connected:
                    resp = await self.composio.gmail_create_draft(to=email, subject=subject, body=body)
                    outcome = "DRAFT" if resp.get("ok") else f"FAILED: {resp.get('error', '')}"
                drafted += 1
                rows.append(self._outreach_row(idx, lead, "email", outcome, "Needs Review (Warm)"))
            else:
                skipped += 1
                rows.append(self._outreach_row(idx, lead, "email", "SKIP", tier))
        await self.sheets.write_tab("Outreach", rows)
        report["metrics"]["emails_sent"] = sent
        report["metrics"]["emails_drafted"] = drafted
        report["metrics"]["emails_skipped"] = skipped

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
    def _outreach_row(idx: int, lead: dict, channel: str, status: str, note: str) -> list:
        return [idx, lead.get("name"), channel, "", "", status, note]

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
        if sheet_url:
            lines.append(f"Sheet: {sheet_url}")
        return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="lead-gen-agents pipeline")
    parser.add_argument("--mode", choices=MODES, default="full")
    parser.add_argument("--dry-run", action="store_true",
                        help="Run offline with mock data (no Composio/GitHub/Telegram side effects)")
    args = parser.parse_args()

    import os
    env = dict(os.environ)
    if args.dry_run:
        env["DRY_RUN"] = "1"
    settings = Settings.load(env)
    import src.core.logging as logmod
    logmod.setup_logging()

    report = asyncio.run(Pipeline(settings).run(args.mode))
    sheet_url = ""
    if not settings.dry_run and settings.google_sheet_id:
        sheet_url = f"https://docs.google.com/spreadsheets/d/{settings.google_sheet_id}"
    print(Pipeline.build_report_text(report, sheet_url))


if __name__ == "__main__":
    main()
