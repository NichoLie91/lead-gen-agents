"""CRM Agent — persistent long-term lead memory (Step 06).

Lives entirely in the PRIVATE Google Sheet (``CRM`` tab) via SheetsAgent's
quota-safe read-cache + queued writes. In-memory during a run, written once at
flush. The public repo never sees lead data — only ``lead_id`` hashes.

Row model (CRM_HEADER order):
    Lead ID | Name | Email | Instagram | Tier | Status | Last Contact |
    Next Follow-up | Follow-ups Sent | Last Reply | Timeline | Notes
Timeline is a JSON list of {"ts", "event", "detail"} — the conversation log.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

from src.agents.sheets_agent import CRM_HEADER, SheetsAgent
from src.enrichment import normalize_email

log = logging.getLogger(__name__)

CRM_TAB = "CRM"


class CrmAgent:
    def __init__(self, sheets: SheetsAgent):
        self._sheets = sheets
        self._rows: dict[str, dict] = {}   # lead_id -> row dict (header-keyed)
        self._loaded = False

    # ---------- read (once per run, via SheetsAgent cache) ----------
    async def load(self) -> dict[str, dict]:
        if self._loaded:
            return self._rows
        raw = await self._sheets.read_tab(CRM_TAB)
        self._rows = {}
        if raw and raw[0]:
            header = [str(h).strip() for h in raw[0]]
            for line in raw[1:]:
                if not line or not line[0]:
                    continue
                row = dict(zip(header, line))
                if row.get("Lead ID"):
                    self._rows[row["Lead ID"]] = row
        self._loaded = True
        return self._rows

    # ---------- mutations (in-memory; save() queues the write) ----------
    def upsert(self, lead_id: str, *, name: str = "", email: str = "",
               instagram: str = "", tier: str = "", status: str = "NEW",
               notes: str = "") -> dict:
        row = self._rows.setdefault(lead_id, {h: "" for h in CRM_HEADER})
        row["Lead ID"] = lead_id
        if name:
            row["Name"] = name
        if email:
            row["Email"] = email
        if instagram:
            row["Instagram"] = instagram
        if tier:
            row["Tier"] = tier
        if status:
            row["Status"] = status
        if notes:
            row["Notes"] = notes
        return row

    def set_status(self, lead_id: str, status: str) -> None:
        if lead_id in self._rows:
            self._rows[lead_id]["Status"] = status

    def set_last_contact(self, lead_id: str, ts: str | None = None) -> None:
        if lead_id in self._rows:
            self._rows[lead_id]["Last Contact"] = ts or _now_iso()

    def schedule_followup(self, lead_id: str, days: int) -> None:
        """Set Next Follow-up = today + days (date-only, for due comparisons)."""
        if lead_id in self._rows:
            self._rows[lead_id]["Next Follow-up"] = _today_plus(days)

    def record_followup_sent(self, lead_id: str) -> None:
        if lead_id in self._rows:
            row = self._rows[lead_id]
            row["Follow-ups Sent"] = str(int(row.get("Follow-ups Sent") or 0) + 1)
            self.set_last_contact(lead_id)

    def append_timeline(self, lead_id: str, event: str, detail: str = "") -> None:
        if lead_id not in self._rows:
            return
        entry = {"ts": datetime.now(UTC).isoformat(timespec="seconds"),
                 "event": event, "detail": detail[:500]}
        timeline = self.timeline(lead_id)
        timeline.append(entry)
        self._rows[lead_id]["Timeline"] = json.dumps(timeline, separators=(",", ":"))

    def timeline(self, lead_id: str) -> list[dict]:
        row = self._rows.get(lead_id, {})
        raw = row.get("Timeline") or "[]"
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, list) else []
        except (json.JSONDecodeError, TypeError):
            return []

    # ---------- lookups ----------
    def find_by_email(self, email: str) -> dict | None:
        want = normalize_email(email)
        if not want:
            return None
        for row in self._rows.values():
            if normalize_email(row.get("Email") or "") == want:
                return row
        return None

    def find_by_lead_id(self, lead_id: str) -> dict | None:
        return self._rows.get(lead_id)

    # ---------- write-back ----------
    async def save(self) -> None:
        """Queue the full CRM rewrite; SheetsAgent.flush() writes it once.

        Lazy-loads first: modes that never touch the CRM (discovery, enrichment,
        outreach-ig) must NOT clobber it with a header-only write — without the
        load, _rows is empty and the existing lead memory would be wiped.
        """
        if not self._loaded:
            await self.load()
        rows = [
            [row.get(h, "") for h in CRM_HEADER]
            for row in self._rows.values()
        ]
        await self._sheets.write_tab(CRM_TAB, rows)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _today_plus(days: int) -> str:
    return (datetime.now(UTC).date() + timedelta(days=days)).isoformat()
