"""SheetsAgent quota-safe behavior tests: queued writes, read cache, flush.

All tests run in dry-run/mirror mode (no Composio connection), which is what
the quota fix relies on for offline correctness; the live path differs only in
that execute_action performs the API calls with retry/backoff.
"""
from __future__ import annotations

import asyncio

from src.agents.composio_agent import ComposioAgent
from src.agents.sheets_agent import SheetsAgent
from src.core.config import Settings
from src.core.state import StateStore


def make_sheets(tmp_path) -> tuple[SheetsAgent, Settings, StateStore]:
    settings = Settings(dry_run=True, repo_root=tmp_path)
    state = StateStore(tmp_path / "state")
    composio = ComposioAgent(settings)  # no key -> connected=False -> mirror mode
    return SheetsAgent(composio, settings, state), settings, state


def test_write_tab_queues_without_api_call(tmp_path):
    sheets, _, _ = make_sheets(tmp_path)
    assert not sheets.has_pending_writes()
    asyncio.run(sheets.write_tab("Score", [["Biz A", 90]]))
    assert sheets.has_pending_writes()
    # The write is queued but already visible to readers via the mirror.
    rows = asyncio.run(sheets.read_tab("Score"))
    assert rows[0] == ["Lead", "ICP (0-25)", "Intent (0-25)", "Budget (0-20)",
                       "Reachability (0-15)", "Timing (0-15)", "Total", "Tier"]
    assert rows[1] == ["Biz A", 90]


def test_read_tab_cached_per_run(tmp_path):
    sheets, _, _ = make_sheets(tmp_path)
    asyncio.run(sheets.write_tab("Pipeline", [["Biz B", 1]]))
    asyncio.run(sheets.flush())  # pending write applied; cache invalidated
    first = asyncio.run(sheets.read_tab("Pipeline"))
    second = asyncio.run(sheets.read_tab("Pipeline"))
    assert first is second  # served from the in-memory cache, no 2nd fetch
    assert sheets.read_cache_size() >= 1


def test_read_after_queued_write_returns_pending(tmp_path):
    # A queued (unflushed) write must win over stale API/mirror data.
    sheets, _, _ = make_sheets(tmp_path)
    asyncio.run(sheets.write_tab("Score", [["Biz C", 77]]))
    rows = asyncio.run(sheets.read_tab("Score"))
    assert rows[1] == ["Biz C", 77]
    # The pending value is served without needing a flush or a cache hit.
    assert sheets.read_cache_size() == 0


def test_flush_applies_all_pending_and_clears_queue(tmp_path):
    sheets, _, _ = make_sheets(tmp_path)
    asyncio.run(sheets.write_tab("Score", [["Biz A", 90]]))
    asyncio.run(sheets.write_tab("Followup", [["Biz A", "Day 0: intro"]]))
    ok, failed = asyncio.run(sheets.flush())
    assert ok == 2 and failed == 0
    assert not sheets.has_pending_writes()


def test_flush_with_no_pending_is_noop(tmp_path):
    sheets, _, _ = make_sheets(tmp_path)
    assert asyncio.run(sheets.flush()) == (0, 0)


def test_write_tab_sanitizes_none_and_bool_cells(tmp_path):
    # Regression: a None cell (lead.get("email") with key present but value
    # None) used to reject the ENTIRE GOOGLESHEETS_BATCH_UPDATE call with
    # "Invalid request data provided" -> empty Pipeline tab in the live sheet.
    sheets, _, _ = make_sheets(tmp_path)
    asyncio.run(sheets.write_tab("Pipeline", [["Biz A", None, True, False, 1.5]]))
    rows = asyncio.run(sheets.read_tab("Pipeline"))
    assert rows[1] == ["Biz A", "", "TRUE", "FALSE", 1.5]


def test_ensure_sheet_backfills_tabs_created_later(tmp_path):
    # Regression: ensure_sheet used to early-return when the Pipeline sheet
    # existed, so tabs added later (CRM) were never created and flush() logged
    # "no spreadsheet for tab CRM; skipping live write".
    class FakeComposio:
        connected = True
        def __init__(self):
            self.created: list[str] = []
        def slug(self, name: str) -> str:
            return name
        async def execute_action(self, slug: str, args: dict) -> dict:
            if slug == "sheet_create_named":
                tab = args["sheet_name"]
                self.created.append(tab)
                return {"ok": True,
                        "data": {"response_data": {"spreadsheetId": f"sid-{tab}"}}}
            return {"ok": False, "error": "unexpected call"}

    fake = FakeComposio()
    settings = Settings(dry_run=False, repo_root=tmp_path)
    state = StateStore(tmp_path / "state")
    state.save("sheet_ids", {"Pipeline": "sid-pipeline"})
    settings.google_sheet_id = "sid-pipeline"
    sheets = SheetsAgent(fake, settings, state)

    assert asyncio.run(sheets.ensure_sheet()) == "sid-pipeline"
    # Every tab missing from the persisted ids gets backfilled.
    assert fake.created == ["Score", "Outreach", "Followup", "CRM"]
    assert sheets._tab_sheet_id("CRM") == "sid-CRM"
    assert state.load("sheet_ids", {}).get("CRM") == "sid-CRM"
