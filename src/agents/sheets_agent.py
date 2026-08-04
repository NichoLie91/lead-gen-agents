"""Sheets Agent — the only writer to the Google Sheet (spec section 7.7).

Online: Composio GOOGLESHEETS_* actions with 'Tab'!A1 range formatting.
Offline/dry-run: mirrors tabs into ``state/sheet_mirror.json`` so the
pipeline can be exercised end-to-end without a Composio connection.
"""
from __future__ import annotations

import logging

from src.agents.composio_agent import ComposioAgent, ComposioNotConfigured
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
        self._sheet_id = settings.google_sheet_id
        self._mirror: dict[str, list[list]] = {}

    # ---------- lifecycle ----------
    async def ensure_sheet(self) -> str:
        """Return a usable sheet id (creates one if absent). Offline -> mirror id."""
        if self._sheet_id:
            return self._sheet_id
        if not self._composio.connected or self._settings.dry_run:
            self._sheet_id = "DRY-RUN-MIRROR"
            self._init_mirror()
            return self._sheet_id
        try:
            resp = await self._composio.execute_action(
                self._composio.slug("sheet_create"),
                {"title": self._sheet_name()},
            )
            if resp.get("ok"):
                self._sheet_id = str(resp["data"].get("spreadsheetId", ""))
                self._settings.google_sheet_id = self._sheet_id
                self._ensure_tabs()
            return self._sheet_id or "UNKNOWN"
        except ComposioNotConfigured:
            self._sheet_id = "DRY-RUN-MIRROR"
            self._init_mirror()
            return self._sheet_id

    def _sheet_name(self) -> str:
        count = self._settings.crit("target_leads", 250)
        verticals = ", ".join(self._settings.verticals[:2])
        return f"AI Lead Gen Machine — Pipeline ({count} {verticals} leads)"

    async def _ensure_tabs(self) -> None:
        for tab in TABS:
            await self._composio.execute_action(
                self._composio.slug("sheet_add_sheet"),
                {"spreadsheet_id": self._sheet_id, "sheet_name": tab},
            )

    # ---------- reads/writes ----------
    async def write_tab(self, tab: str, rows: list[list]) -> bool:
        header = HEADERS.get(tab, [])
        values = [header] + rows
        if not self._composio.connected or self._settings.dry_run:
            self._mirror[tab] = values
            self._persist_mirror()
            return True
        resp = await self._composio.execute_action(
            self._composio.slug("sheet_values_update"),
            {
                "spreadsheet_id": self._sheet_id,
                "range": f"'{tab}'!A1",
                "values": values,
            },
        )
        return bool(resp.get("ok"))

    async def read_tab(self, tab: str) -> list[list]:
        if not self._composio.connected or self._settings.dry_run:
            return self._mirror.get(tab, [])
        resp = await self._composio.execute_action(
            self._composio.slug("sheet_values_update"),  # informational only
            {"spreadsheet_id": self._sheet_id, "range": f"'{tab}'!A1:Z1000"},
        )
        data = resp.get("data") if resp.get("ok") else []
        return data.get("values", []) if isinstance(data, dict) else []

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
