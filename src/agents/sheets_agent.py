"""Sheets Agent — the only writer to the Google Sheet (spec section 7.7).

Online: Composio GOOGLESHEETS_* actions. Verified against the live v3
catalog (2026-08): the googlesheets toolkit has NO add-tab action, so each
logical tab (Pipeline / Score / Outreach / Followup) lives in its OWN
spreadsheet, created via GOOGLESHEETS_SHEET_FROM_JSON with the tab's header
row. Tabs are rewritten as clear-range + GOOGLESHEETS_BATCH_UPDATE.

QUOTA DESIGN (googleapis 100 req/min default read quota — the pipeline used to
trip "Quota exceeded for Read requests per minute"):
- Reads are cached in memory for the lifetime of the run: a tab is fetched
  from the API at most ONCE per run; repeat reads hit the cache.
- Writes are QUEUED (``write_tab``) and only sent to the API by ``flush()``,
  called once at the end of a run — never mid-run per stage.
- Per-tab API failures (clear/update) go through ComposioAgent.execute_action,
  which already retries throttles with 2-5s+ exponential backoff; a small
  inter-tab delay further smooths request bursts.
- Caching is intentionally in-memory ONLY: lead PII must never be written into
  the public repo (spec 11), so no local cache file for sheet contents.

Offline/dry-run: mirrors tabs into ``state/sheet_mirror.json`` so the
pipeline can be exercised end-to-end without a Composio connection.
"""
from __future__ import annotations

import asyncio
import logging

from src.agents.composio_agent import ComposioAgent
from src.core.config import Settings
from src.core.state import StateStore

log = logging.getLogger(__name__)

TABS = ("Pipeline", "Score", "Outreach", "Drafts", "Followup", "CRM")

SCORING_SUMMARY_HEADER = ["Metric", "Value"]

PIPELINE_HEADER = [
    "rank", "business_name", "verified_email", "email_verification_method",
    "city", "state", "trade", "phone", "official_site", "rating",
    "review_count", "instagram_profile", "booking_signal", "emergency_signal",
    "source_url", "source_evidence_text", "evidence_quality",
    "scout_score", "scout_tier", "recommended_channel", "hold_reason",
]
SCORE_HEADER = ["Lead", "ICP (0-25)", "Intent (0-25)", "Budget (0-20)",
                "Reachability (0-15)", "Timing (0-15)", "Total", "Tier"]
# "Lead ID" is the PII-safe sha256 (src/core/ident.py) so approvals match
# drafts without exposing lead data in the repo; Email/Subject/Body live only
# in this private sheet.
OUTREACH_HEADER = ["#", "Lead", "Lead ID", "Email", "Channel", "Subject",
                   "Body", "Status", "Send Date"]
# Dedicated Drafts tab: holds WARM drafts awaiting approval before they
# move to Outreach for sending. Separates "pending" from "sent/rejected".
DRAFTS_HEADER = ["#", "Lead", "Lead ID", "Email", "Subject", "Body",
                 "Score", "Tier", "Status", "Created", "Notes"]
FOLLOWUP_HEADER = ["#", "Lead", "Step 1", "Step 2", "Step 3", "Step 4", "Status"]

# Long-term lead memory (Step 06). Timeline holds a JSON array of
# {ts, event, detail} entries — the per-lead conversation log. All of it stays
# in the private sheet; the public repo only ever sees lead_id hashes.
CRM_HEADER = [
    "Lead ID", "Name", "Email", "Instagram", "Tier", "Status", "Last Contact",
    "Next Follow-up", "Follow-ups Sent", "Last Reply", "Timeline", "Notes",
]
CRM_STATUSES = (
    "NEW", "DRAFTED", "CONTACTED", "REPLIED-INTERESTED", "OBJECTION",
    "QUESTION", "UNSUBSCRIBED", "WON", "LOST",
)

HEADERS = {
    "Pipeline": PIPELINE_HEADER,
    "Score": SCORE_HEADER,
    "Outreach": OUTREACH_HEADER,
    "Drafts": DRAFTS_HEADER,
    "Followup": FOLLOWUP_HEADER,
    "CRM": CRM_HEADER,
}

# Pause between live tab writes so bursts of clear+update calls stay well under
# the per-minute quota (a couple of seconds at most for all 4 tabs).
INTER_WRITE_DELAY_SEC = 1.0


class SheetsAgent:
    def __init__(self, composio: ComposioAgent, settings: Settings, state: StateStore):
        self._composio = composio
        self._settings = settings
        self._state = state
        # {tab: spreadsheet_id}. ``_sheet_id`` is the primary (Pipeline) id for
        # backward-compat with settings.google_sheet_id / sheet_url().
        self._sheet_ids: dict[str, str] = {}
        self._sheet_id = settings.google_sheet_id
        stored = state.load("sheet_ids", {})
        if isinstance(stored, dict):
            self._sheet_ids = {k: v for k, v in stored.items() if v}
            if self._sheet_id and "Pipeline" not in self._sheet_ids:
                self._sheet_ids["Pipeline"] = self._sheet_id
        self._mirror: dict[str, list[list]] = {}
        # Read-once cache: {tab: rows}. Populated on first read_tab of a tab;
        # invalidated for tabs that flush() rewrites. Lives in memory only
        # (PII policy, spec 11) for the duration of one run.
        self._read_cache: dict[str, list[list]] = {}
        # Queued writes: {tab: rows} accumulated by write_tab(), applied to the
        # API once by flush() at the end of a run.
        self._pending_writes: dict[str, list[list]] = {}

    # ---------- lifecycle ----------
    async def ensure_sheet(self) -> str:
        """Return the primary (Pipeline) spreadsheet id, creating a spreadsheet
        for every tab that doesn't have one yet. Offline -> mirror id.

        No early return when the primary exists: tabs added later (e.g. CRM)
        must be backfilled on the next run, or their writes get skipped with
        "no spreadsheet for tab" in flush().
        """
        if not self._composio.connected or self._settings.dry_run:
            self._sheet_id = "DRY-RUN-MIRROR"
            self._init_mirror()
            return self._sheet_id
        for tab in TABS:
            if tab in self._sheet_ids:
                continue
            resp = await self._composio.execute_action(
                self._composio.slug("sheet_create_named"),
                {
                    "title": f"{self._sheet_name()} - {tab}",
                    "sheet_name": tab,
                    "sheet_json": [{h: "" for h in HEADERS.get(tab, [])}],
                },
            )
            if not resp.get("ok"):
                log.warning("create spreadsheet for tab %s failed: %s",
                            tab, str(resp.get("error", ""))[:150])
                continue
            data = resp.get("data") or {}
            rd = data.get("response_data") or data
            sid = rd.get("spreadsheetId") or rd.get("spreadsheet_id") or ""
            if sid:
                self._sheet_ids[tab] = sid
        if self._sheet_ids:
            self._sheet_id = self._sheet_ids.get("Pipeline",
                                                 self._sheet_ids.get("Score", ""))
            self._settings.google_sheet_id = self._sheet_id
            self._state.save("sheet_ids", self._sheet_ids)
        return self._sheet_id or "UNKNOWN"

    def _sheet_name(self) -> str:
        count = self._settings.crit("target_leads", 250)
        verticals = ", ".join(self._settings.verticals[:2])
        return f"AI Lead Gen Machine — Pipeline ({count} {verticals} leads)"

    def _tab_sheet_id(self, tab: str) -> str:
        return self._sheet_ids.get(tab, "")

    # ---------- reads (cached: at most one API read per tab per run) ----------
    async def read_tab(self, tab: str) -> list[list]:
        if tab in self._pending_writes:
            # A queued (not yet flushed) write supersedes any API data — reading
            # the sheet now would return the stale pre-write rows.
            return self._pending_writes[tab]
        if tab in self._read_cache:
            return self._read_cache[tab]
        if not self._composio.connected or self._settings.dry_run:
            rows = self._mirror.get(tab, [])
        else:
            if not self._sheet_ids:
                await self.ensure_sheet()
            sid = self._tab_sheet_id(tab)
            if not sid:
                rows = []
            else:
                resp = await self._composio.execute_action(
                    self._composio.slug("sheet_read"), {"spreadsheet_id": sid}
                )
                data = resp.get("data") if resp.get("ok") else {}
                if not isinstance(data, dict):
                    rows = []
                else:
                    value_ranges = data.get("valueRanges") or data.get("values")
                    if isinstance(value_ranges, dict):
                        rows = value_ranges.get("values", []) or []
                    elif isinstance(value_ranges, list):
                        rows = value_ranges
                    else:
                        rows = []
        self._read_cache[tab] = rows
        return rows

    def read_cache_size(self) -> int:
        """Number of tabs currently served from the in-memory read cache."""
        return len(self._read_cache)

    def has_pending_writes(self) -> bool:
        """True when write_tab queued tabs that flush() has not applied yet."""
        return bool(self._pending_writes)

    # ---------- writes (queued; flush() applies them at the end of a run) ----------
    @staticmethod
    def _sanitize_cell(value) -> str | int | float:
        """Coerce cells to types the GOOGLESHEETS_BATCH_UPDATE tool accepts.

        Composio validates every cell as ``string | integer | number``; a
        ``None`` (e.g. ``lead.get("email")`` when the key exists with a None
        value) or a bool rejects the ENTIRE tab write ("Invalid request data
        provided" -> empty sheet). Keep scalar types, blank out None, and
        stringify anything exotic.
        """
        if value is None:
            return ""
        if isinstance(value, bool):
            return "TRUE" if value else "FALSE"
        if isinstance(value, (str, int, float)):
            return value
        return str(value)

    @classmethod
    def _sanitize_rows(cls, rows: list[list]) -> list[list]:
        return [[cls._sanitize_cell(cell) for cell in row] for row in rows]

    async def write_tab(self, tab: str, rows: list[list]) -> bool:
        """Queue a full-tab rewrite. No API call happens here — flush() applies
        all queued writes once, at the end of the run (avoids quota bursts).
        Cells are sanitized so a None/bool can never kill the whole tab write.
        """
        header = HEADERS.get(tab, [])
        values = [header] + self._sanitize_rows(rows)
        self._pending_writes[tab] = values
        # Offline/dry-run: mirror immediately so read_tab sees the same data.
        if not self._composio.connected or self._settings.dry_run:
            self._mirror[tab] = values
            self._persist_mirror()
        # Invalidate any stale cached read for this tab.
        self._read_cache.pop(tab, None)
        return True

    async def flush(self) -> tuple[int, int]:
        """Apply every queued write to the live sheet; returns (ok_tabs, failed).

        Called exactly once per run, after all stages finish. Uses
        ComposioAgent.execute_action whose retry/backoff absorbs throttles;
        a small inter-tab delay keeps the burst under the per-minute quota.
        """
        pending_count = len(self._pending_writes)
        if not pending_count:
            return 0, 0
        if not self._composio.connected or self._settings.dry_run:
            self._pending_writes.clear()
            return pending_count, 0
        if not self._sheet_ids:
            await self.ensure_sheet()

        ok = 0
        failed: list[str] = []
        for idx, (tab, values) in enumerate(self._pending_writes.items()):
            sid = self._tab_sheet_id(tab)
            if not sid:
                log.warning("no spreadsheet for tab %s; skipping live write", tab)
                failed.append(tab)
                continue
            if idx:
                await asyncio.sleep(INTER_WRITE_DELAY_SEC)  # smooth the burst
            clear = await self._composio.execute_action(
                self._composio.slug("sheet_clear"),
                {"spreadsheet_id": sid, "range": f"'{tab}'!A1:Z1000"},
            )
            if not clear.get("ok"):
                log.error("clear tab %s failed: %s", tab, str(clear.get("error", ""))[:200])
                failed.append(tab)
                continue
            write = await self._composio.execute_action(
                self._composio.slug("sheet_values_update"),
                {"spreadsheet_id": sid, "sheet_name": tab, "values": values},
            )
            if not write.get("ok"):
                log.error("write tab %s failed: %s", tab, str(write.get("error", ""))[:200])
                failed.append(tab)
                continue
            ok += 1
            self._read_cache.pop(tab, None)  # what we just wrote is now current
            log.info("sheet tab %s written (%d rows)", tab, len(values))
        self._pending_writes.clear()
        return ok, len(failed)

    def sheet_url(self) -> str:
        if self._sheet_id and self._sheet_id != "DRY-RUN-MIRROR":
            return f"https://docs.google.com/spreadsheets/d/{self._sheet_id}"
        return "dry-run mirror (no live sheet)"

    # ---------- dry-run mirror ----------
    def _init_mirror(self) -> None:
        stored = self._state.load("sheet_mirror", {})
        if isinstance(stored, dict):
            self._mirror = {k: v for k, v in stored.items()}
        for tab in TABS:
            if tab not in self._mirror:
                self._mirror[tab] = [HEADERS[tab]]
        self._persist_mirror()

    def _persist_mirror(self) -> None:
        self._state.save("sheet_mirror", self._mirror)
