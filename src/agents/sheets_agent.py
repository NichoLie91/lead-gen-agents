"""Sheets Agent — the only writer to Google Sheets (spec section 7.7).

ONE SPREADSHEET, MANY TABS. Every logical tab (Pipeline / Score / Outreach /
Followup / CRM / Drafts / DraftsArchive / AllLeads) lives in a tab of a single
spreadsheet, so the whole lead database is one link. Tabs are created with
GOOGLESHEETS_ADD_SHEET and rewritten as clear-range + GOOGLESHEETS_BATCH_UPDATE.

The previous design gave each tab its OWN spreadsheet because the v3 catalog
was believed to have no add-tab action; it does (GOOGLESHEETS_ADD_SHEET), and
seven separate files made "where is lead X?" unanswerable. ensure_sheet()
adopts GOOGLE_SHEET_ID as the single file, adds any missing tab, and seeds a
new tab from the old per-tab spreadsheet the first time it is created.

QUOTA DESIGN (googleapis 100 req/min read quota — the pipeline used to trip
"Quota exceeded for Read requests per minute"):
- Reads are cached in memory for the lifetime of the run: a tab is fetched
  from the API at most ONCE per run; repeat reads hit the cache.
- Writes are QUEUED (``write_tab``) and only sent to the API by ``flush()``,
  called once at the end of a run — never mid-run per stage.
- Per-tab API failures (clear/update) go through ComposioAgent.execute_action,
  which already retries throttles with 2-5s+ exponential backoff; a small
  inter-tab delay further smooths request bursts.
- Caching is intentionally in-memory ONLY: lead PII must never be written into
  the public repo (spec 11), so no local cache file for sheet contents.

READ FAILURES ARE NOT EMPTY TABS. A throttled or failed read raises
``SheetReadError`` instead of returning ``[]``. Returning ``[]`` used to make
the AllLeads builder believe a tab had gone blank and rebuild the master list
without it — silently dropping every lead that tab held (seen live: 90 of one
run's 108 fresh leads vanished from the master list after a 429).

Offline/dry-run: mirrors tabs into ``state/sheet_mirror.json`` so the pipeline
can be exercised end-to-end without a Composio connection.
"""
from __future__ import annotations

import asyncio
import logging

from src.agents.composio_agent import ComposioAgent
from src.core.config import Settings
from src.core.state import StateStore

log = logging.getLogger(__name__)

TABS = ("Pipeline", "Score", "Outreach", "Drafts", "Followup", "CRM",
        "DraftsArchive", "AllLeads")

SCORING_SUMMARY_HEADER = ["Metric", "Value"]

PIPELINE_HEADER = [
    "rank", "business_name", "verified_email", "email_verification_method",
    "city", "state", "trade", "phone", "owner_or_manager_phone",
    "official_site", "rating",
    "review_count", "instagram_profile", "booking_signal", "emergency_signal",
    "source_url", "source_evidence_text", "evidence_quality",
    "scout_score", "scout_tier", "recommended_channel", "hold_reason",
]

# ONE master list: every lead this system has ever found, from every tab,
# with both phone numbers, the email and the score. Deduped by phone, email
# then name+city, and accumulated across runs (never rewritten with only the
# current run's leads).
ALL_LEADS_HEADER = [
    "lead_id", "business_name", "trade", "city", "state",
    "public_phone", "owner_or_manager_phone", "email", "email_status",
    "website", "rating", "review_count", "scout_score", "scout_tier",
    "sales_status", "sources", "first_seen", "last_seen",
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
    # Sent/rejected/stale drafts leave the Drafts queue for here, so the queue
    # stays a screenful. Same schema, append-only.
    "DraftsArchive": DRAFTS_HEADER,
    "AllLeads": ALL_LEADS_HEADER,
}

# Pause between live tab writes so bursts of clear+update calls stay well under
# the per-minute quota (a couple of seconds at most for all 4 tabs).
INTER_WRITE_DELAY_SEC = 1.0


class SheetReadError(RuntimeError):
    """A tab could not be read. NEVER treat this as "the tab is empty".

    Callers that rebuild or merge accumulated data (AllLeads, CRM, Pipeline)
    must let this propagate: an empty answer would overwrite real rows.
    """


class SheetsAgent:
    def __init__(self, composio: ComposioAgent, settings: Settings, state: StateStore):
        self._composio = composio
        self._settings = settings
        self._state = state
        # ONE spreadsheet id shared by every tab. ``_sheet_ids`` maps
        # tab -> that same id and is kept (and persisted) because the rest of
        # the system, and the report, ask "which spreadsheet holds tab X?".
        self._sheet_ids: dict[str, str] = {}
        self._sheet_id = settings.google_sheet_id
        stored = state.load("sheet_ids", {})
        if isinstance(stored, dict):
            self._sheet_ids = {k: v for k, v in stored.items() if v}
        unified = state.load("sheet_unified", "") or ""
        if unified:
            self._sheet_id = unified
        elif self._sheet_ids:
            # Legacy state: tabs lived in separate files. Adopt the Pipeline
            # file (the one every report has linked) as the single file.
            self._sheet_id = self._sheet_ids.get("Pipeline") or self._sheet_id
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
        """Return the single spreadsheet id, creating/backfilling tabs as needed.

        Idempotent. On a legacy install (one spreadsheet per tab) it adopts the
        Pipeline file, adds every missing tab, seeds each new tab with the rows
        from that tab's old spreadsheet, then records the single id so later
        runs are pure verification.
        """
        if not self._composio.connected or self._settings.dry_run:
            self._sheet_id = "DRY-RUN-MIRROR"
            self._init_mirror()
            return self._sheet_id

        # Seeder ids: where each tab's rows lived before the consolidation.
        # Kept in state/yaml-free form so a half-finished migration can be
        # re-run (tabs that already exist are simply left alone).
        legacy = dict(self._sheet_ids)
        stored_legacy = self._state.load("sheet_ids_legacy", {})
        if isinstance(stored_legacy, dict) and stored_legacy:
            legacy = {k: v for k, v in stored_legacy.items() if v}
        unified = (self._state.load("sheet_unified", "") or "").strip()
        if not unified:
            unified = (self._settings.google_sheet_id or "").strip()
        if not unified:
            unified = legacy.get("Pipeline", "")
        if not unified:
            unified = await self._create_spreadsheet()
            if not unified:
                log.error("could not create the lead spreadsheet")
                return ""

        self._sheet_id = unified
        titles = await self._tab_titles(unified)
        if titles is None:
            # Could not list tabs: keep whatever ids we had rather than risk
            # creating duplicate tabs. flush() reports tabs it cannot write.
            log.warning("could not list tabs in %s; skipping tab backfill", unified)
            self._sheet_ids = {tab: unified for tab in TABS}
            self._state.save("sheet_ids", self._sheet_ids)
            self._state.save("sheet_unified", unified)
            self._settings.google_sheet_id = unified
            return unified

        for tab in TABS:
            if tab in titles:
                continue
            if not await self._add_tab(unified, tab):
                continue
            # Seeding writes the header itself (header + carried-over rows in
            # one write at A1); a tab with nothing to carry over just gets the
            # header. Writing the header first and the body second would make
            # the body land on top of row 1 unless the API appends instead.
            if not await self._seed_tab(unified, tab, legacy.get(tab, "")):
                await self._write_header(unified, tab)
            await asyncio.sleep(INTER_WRITE_DELAY_SEC)  # smooth the burst

        self._sheet_ids = {tab: unified for tab in TABS}
        self._state.save("sheet_ids", self._sheet_ids)
        self._state.save("sheet_unified", unified)
        if legacy and any(v != unified for v in legacy.values()):
            # Keep the old ids so nothing is orphaned without a record of it.
            self._state.save("sheet_ids_legacy", legacy)
        self._settings.google_sheet_id = unified
        return unified

    def _sheet_name(self) -> str:
        count = self._settings.crit("target_leads", 250)
        verticals = ", ".join(self._settings.verticals[:2])
        return f"AI Lead Gen Machine — Pipeline ({count} {verticals} leads)"

    async def _create_spreadsheet(self) -> str:
        resp = await self._composio.execute_action(
            self._composio.slug("spreadsheet_create"), {"title": self._sheet_name()}
        )
        if not resp.get("ok"):
            log.error("create spreadsheet failed: %s", str(resp.get("error", ""))[:200])
            return ""
        data = resp.get("data") or {}
        for holder in (data, data.get("response_data") or {}):
            if not isinstance(holder, dict):
                continue
            sid = holder.get("spreadsheetId") or holder.get("spreadsheet_id") or ""
            if sid:
                return str(sid)
        log.error("create spreadsheet returned no id")
        return ""

    async def _tab_titles(self, spreadsheet_id: str) -> set[str] | None:
        """Tab names in the spreadsheet, or None when the API did not answer."""
        resp = await self._composio.execute_action(
            self._composio.slug("sheet_get_info"), {"spreadsheet_id": spreadsheet_id}
        )
        if not resp.get("ok"):
            return None
        data = resp.get("data") or {}
        sheets = data.get("sheets") if isinstance(data, dict) else None
        if not sheets and isinstance(data, dict):
            inner = data.get("response_data") or {}
            if isinstance(inner, dict):
                sheets = inner.get("sheets")
        if not isinstance(sheets, list):
            return None
        titles: set[str] = set()
        for sheet in sheets:
            props = (sheet or {}).get("properties") or {}
            title = props.get("title")
            if title:
                titles.add(str(title).strip())
        return titles

    async def _add_tab(self, spreadsheet_id: str, tab: str) -> bool:
        """Create one empty tab. The title MUST go under `properties`:
        GOOGLESHEETS_ADD_SHEET silently ignores it anywhere else and the tab
        becomes "Sheet<N>"."""
        resp = await self._composio.execute_action(
            self._composio.slug("sheet_add_tab"),
            {"spreadsheetId": spreadsheet_id, "properties": {"title": tab}},
        )
        if not resp.get("ok"):
            log.warning("add tab %s failed: %s", tab, str(resp.get("error", ""))[:200])
            return False
        return True

    async def _write(self, spreadsheet_id: str, tab: str,
                     values: list[list]) -> bool:
        resp = await self._composio.execute_action(
            self._composio.slug("sheet_values_update"),
            {"spreadsheet_id": spreadsheet_id, "sheet_name": tab,
             "values": self._sanitize_rows(values)},
        )
        if not resp.get("ok"):
            log.warning("write to tab %s failed: %s", tab,
                        str(resp.get("error", ""))[:200])
            return False
        return True

    async def _write_header(self, spreadsheet_id: str, tab: str) -> None:
        await self._write(spreadsheet_id, tab, [HEADERS.get(tab, [])])

    async def _seed_tab(self, spreadsheet_id: str, tab: str, legacy_id: str) -> bool:
        """Copy a tab's rows over from its old standalone spreadsheet (once).

        Returns True when it wrote a header + carried-over rows, False when
        there was nothing to carry over (the caller then writes a bare header).
        """
        if not legacy_id or legacy_id == spreadsheet_id:
            return False
        resp = await self._composio.execute_action(
            self._composio.slug("sheet_read"),
            {"spreadsheet_id": legacy_id, "ranges": [f"'{tab}'!A1:ZZ"]},
        )
        if not resp.get("ok"):
            log.warning("could not read legacy %s tab (%s); starting empty",
                        tab, str(resp.get("error", ""))[:150])
            return False
        rows = self._rows_from(resp.get("data"))
        if len(rows) <= 1:
            return False  # header only: nothing worth carrying over
        # Rebuild every row in HEADER order from the old file's own header, and
        # drop the old header row: the new tab already has its header written,
        # so copying verbatim would leave a second header inside the data.
        old_header = [str(h).strip() for h in rows[0]]
        positions = {name: old_header.index(name) for name in HEADERS.get(tab, [])
                     if name in old_header}
        body: list[list] = []
        for line in rows[1:]:
            if not line or not any(str(c or "").strip() for c in line):
                continue
            if not positions:
                body.append(list(line))
                continue
            body.append([(line[positions[name]]
                          if positions.get(name) is not None
                          and positions[name] < len(line) else "")
                         for name in HEADERS.get(tab, [])])
        if not body:
            return False
        if await self._write(spreadsheet_id, tab, [HEADERS.get(tab, [])] + body):
            log.info("seeded tab %s with %d row(s) from the old spreadsheet",
                     tab, len(body))
            return True
        return False

    def _tab_sheet_id(self, tab: str) -> str:
        return self._sheet_ids.get(tab, self._sheet_id)

    # ---------- reads (cached: at most one API read per tab per run) ----------
    @staticmethod
    def _rows_from(data) -> list[list]:
        """Rows out of a GOOGLESHEETS_BATCH_GET response, whatever its shape.

        One requested range comes back as a dict; several come back as a list
        of {range, values} blocks (we request one range, so take the first).
        """
        if not isinstance(data, dict):
            return []
        value_ranges = data.get("valueRanges") or data.get("values")
        if isinstance(value_ranges, dict):
            return value_ranges.get("values", []) or []
        if isinstance(value_ranges, list):
            blocks = [b for b in value_ranges if isinstance(b, dict)]
            if blocks:
                return blocks[0].get("values", []) or []
            return [r for r in value_ranges if isinstance(r, list)]
        return []

    async def read_tab(self, tab: str, strict: bool = True) -> list[list]:
        """Read a whole tab. Raises ``SheetReadError`` when the read fails.

        ``strict=False`` is for display-only callers (a chat reply): they get an
        empty list instead of an exception. Anything that merges or rebuilds
        accumulated rows must stay strict.
        """
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
                if strict:
                    raise SheetReadError(f"no spreadsheet holds tab {tab}")
                return []
            # ALWAYS name the tab. With several tabs in one file, BATCH_GET
            # without a range answers with the FIRST tab only — which reads as
            # "every tab holds the Pipeline header" and would have merged the
            # wrong rows into every tab in the system.
            resp = await self._composio.execute_action(
                self._composio.slug("sheet_read"),
                {"spreadsheet_id": sid, "ranges": [f"'{tab}'!A1:ZZ"]},
            )
            if not resp.get("ok"):
                err = str(resp.get("error", ""))[:200]
                # Do NOT cache a failure as an empty tab.
                if strict:
                    raise SheetReadError(f"read of tab {tab} failed: {err}")
                log.warning("read of tab %s failed (%s); treating as empty", tab, err)
                return []
            rows = self._rows_from(resp.get("data"))
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
            # Clear whole columns, not A1:Z1000: the old fixed window left the
            # rows past 1000 behind on every rewrite (a master list that grows
            # past 999 leads would have kept resurrecting deleted rows).
            clear = await self._composio.execute_action(
                self._composio.slug("sheet_clear"),
                {"spreadsheet_id": sid, "range": f"'{tab}'!A:AZ"},
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
