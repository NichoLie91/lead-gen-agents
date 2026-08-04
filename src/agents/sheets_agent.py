"""Sheets Agent — the only writer to the Google Sheet (spec section 7.7).

Online: Composio GOOGLESHEETS_* actions. Verified against the live v3
catalog (2026-08): the googlesheets toolkit has NO add-tab action, so each
logical tab (Pipeline / Score / Outreach / Followup) lives in its OWN
spreadsheet, created via GOOGLESHEETS_SHEET_FROM_JSON with the tab's header
row. Tabs are rewritten as clear-range + GOOGLESHEETS_BATCH_UPDATE.

Offline/dry-run: mirrors tabs into ``state/sheet_mirror.json`` so the
pipeline can be exercised end-to-end without a Composio connection.
"""
from __future__ import annotations

import logging

from src.agents.composio_agent import ComposioAgent
from src.core.config import Settings
from src.core.state import StateStore

log = logging.getLogger(__name__)

TABS = ("Pipeline", "Score", "Outreach", "Followup")

PIPELINE_HEADER = [
    "#", "Lead", "Category", "City-State", "Phone", "Email", "Website",
    "Website Status", "Instagram", "Google Rating", "# Reviews", "Status",
    "Tier", "AI Bottleneck (Hook)", "Score", "Stage", "Next Step", "Value",
]
SCORE_HEADER = ["Lead", "ICP (0-25)", "Intent (0-25)", "Budget (0-20)",
                "Reachability (0-15)", "Timing (0-15)", "Total", "Tier"]
OUTREACH_HEADER = ["#", "Lead", "Channel", "Subject", "Body", "Status", "Send Date"]
FOLLOWUP_HEADER = ["#", "Lead", "Step 1", "Step 2", "Step 3", "Step 4", "Status"]

HEADERS = {
    "Pipeline": PIPELINE_HEADER,
    "Score": SCORE_HEADER,
    "Outreach": OUTREACH_HEADER,
    "Followup": FOLLOWUP_HEADER,
}


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

    # ---------- lifecycle ----------
    async def ensure_sheet(self) -> str:
        """Return the primary (Pipeline) spreadsheet id, creating one
        spreadsheet per tab on first use. Offline -> mirror id."""
        if self._sheet_id and "Pipeline" in self._sheet_ids:
            return self._sheet_id
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

    # ---------- reads/writes ----------
    async def write_tab(self, tab: str, rows: list[list]) -> bool:
        header = HEADERS.get(tab, [])
        values = [header] + rows
        if not self._composio.connected or self._settings.dry_run:
            self._mirror[tab] = values
            self._persist_mirror()
            return True
        if not self._sheet_ids:
            await self.ensure_sheet()
        sid = self._tab_sheet_id(tab)
        if not sid:
            log.warning("no spreadsheet for tab %s; skipping live write", tab)
            return False
        clear = await self._composio.execute_action(
            self._composio.slug("sheet_clear"),
            {"spreadsheet_id": sid, "range": f"'{tab}'!A1:Z1000"},
        )
        write = await self._composio.execute_action(
            self._composio.slug("sheet_values_update"),
            {"spreadsheet_id": sid, "sheet_name": tab, "values": values},
        )
        return bool(clear.get("ok") and write.get("ok"))

    async def read_tab(self, tab: str) -> list[list]:
        if not self._composio.connected or self._settings.dry_run:
            return self._mirror.get(tab, [])
        if not self._sheet_ids:
            await self.ensure_sheet()
        sid = self._tab_sheet_id(tab)
        if not sid:
            return []
        resp = await self._composio.execute_action(
            self._composio.slug("sheet_read"), {"spreadsheet_id": sid}
        )
        data = resp.get("data") if resp.get("ok") else {}
        if not isinstance(data, dict):
            return []
        value_ranges = data.get("valueRanges") or data.get("values")
        if isinstance(value_ranges, dict):
            return value_ranges.get("values", []) or []
        return value_ranges if isinstance(value_ranges, list) else []

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
