"""SheetsAgent quota-safe behavior tests: queued writes, read cache, flush.

All tests run in dry-run/mirror mode (no Composio connection), which is what
the quota fix relies on for offline correctness; the live path differs only in
that execute_action performs the API calls with retry/backoff.
"""
from __future__ import annotations

import asyncio

from src.agents.composio_agent import ComposioAgent
from src.agents.sheets_agent import HEADERS, SheetReadError, SheetsAgent
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


class FakeComposio:
    """One spreadsheet that starts with a single Pipeline tab."""

    connected = True

    def __init__(self, tabs=("Pipeline",), legacy_rows=None):
        self.tabs = list(tabs)
        self.created: list[str] = []
        self.written: list[tuple[str, list]] = []
        self.legacy_rows = legacy_rows or {}

    def slug(self, name: str) -> str:
        return name

    async def execute_action(self, slug: str, args: dict) -> dict:
        if slug == "sheet_get_info":
            return {"ok": True, "data": {"sheets":
                    [{"properties": {"title": t}} for t in self.tabs]}}
        if slug == "sheet_add_tab":
            title = args["properties"]["title"]
            self.created.append(title)
            self.tabs.append(title)
            return {"ok": True, "data": {}}
        if slug == "sheet_values_update":
            self.written.append((args["sheet_name"], args["values"]))
            return {"ok": True, "data": {}}
        if slug == "sheet_read":
            return {"ok": True, "data": {"values": self.legacy_rows.get(
                args["spreadsheet_id"], [])}}
        return {"ok": False, "error": "unexpected call"}


def test_ensure_sheet_creates_every_tab_in_one_spreadsheet(tmp_path):
    # ONE file: the tabs that used to live in seven spreadsheets are added as
    # tabs of the spreadsheet GOOGLE_SHEET_ID already points at.
    fake = FakeComposio()
    settings = Settings(dry_run=False, repo_root=tmp_path)
    state = StateStore(tmp_path / "state")
    state.save("sheet_ids", {"Pipeline": "sid-pipeline"})
    settings.google_sheet_id = "sid-pipeline"
    sheets = SheetsAgent(fake, settings, state)

    assert asyncio.run(sheets.ensure_sheet()) == "sid-pipeline"
    assert fake.created == ["Score", "Outreach", "Drafts", "Followup", "CRM",
                            "DraftsArchive", "AllLeads"]
    for tab in ("Pipeline", "Score", "CRM", "AllLeads"):
        assert sheets._tab_sheet_id(tab) == "sid-pipeline", tab
    saved = state.load("sheet_ids", {})
    assert set(saved.values()) == {"sid-pipeline"}
    assert state.load("sheet_unified", "") == "sid-pipeline"


def test_ensure_sheet_migrates_rows_from_the_old_per_tab_files(tmp_path):
    legacy = {"sid-old-score": [["Lead", "Total"], ["Blue Creek", 82]]}
    fake = FakeComposio(legacy_rows=legacy)
    settings = Settings(dry_run=False, repo_root=tmp_path)
    state = StateStore(tmp_path / "state")
    state.save("sheet_ids", {"Pipeline": "sid-pipeline", "Score": "sid-old-score"})
    settings.google_sheet_id = "sid-pipeline"
    sheets = SheetsAgent(fake, settings, state)

    asyncio.run(sheets.ensure_sheet())

    seeded = [values for tab, values in fake.written if tab == "Score"]
    assert seeded, "old Score rows must be copied into the new tab"
    written_rows = [row for values in seeded for row in values]
    # Rows are re-mapped into the new tab's header order (the old file had a
    # short "Lead | Total" header), and the old header row is not carried over.
    total_i = HEADERS["Score"].index("Total")
    blue = next(row for row in written_rows if row[0] == "Blue Creek")
    assert str(blue[total_i]) == "82"
    assert sum(1 for row in written_rows if row[0] == "Lead") == 1, \
        "exactly one header row, no duplicated header inside the data"
    assert state.load("sheet_ids_legacy", {}).get("Score") == "sid-old-score"


def test_ensure_sheet_is_idempotent(tmp_path):
    fake = FakeComposio(tabs=("Pipeline", "Score", "Outreach", "Drafts",
                              "Followup", "CRM", "DraftsArchive", "AllLeads"))
    settings = Settings(dry_run=False, repo_root=tmp_path)
    state = StateStore(tmp_path / "state")
    settings.google_sheet_id = "sid-pipeline"
    sheets = SheetsAgent(fake, settings, state)

    asyncio.run(sheets.ensure_sheet())
    asyncio.run(sheets.ensure_sheet())
    assert fake.created == [], "an existing tab must never be re-created"


def test_a_failed_read_raises_and_is_not_cached(tmp_path):
    # Regression: a throttled read returned [] and the AllLeads builder then
    # rebuilt the master list without that tab, silently dropping its leads.
    class Throttled(FakeComposio):
        async def execute_action(self, slug: str, args: dict) -> dict:
            if slug == "sheet_read":
                return {"ok": False, "error": "429 Quota exceeded for Read requests"}
            return await super().execute_action(slug, args)

    settings = Settings(dry_run=False, repo_root=tmp_path)
    state = StateStore(tmp_path / "state")
    settings.google_sheet_id = "sid-pipeline"
    sheets = SheetsAgent(Throttled(), settings, state)
    sheets._sheet_ids = {"Pipeline": "sid-pipeline"}

    try:
        asyncio.run(sheets.read_tab("Pipeline"))
    except SheetReadError:
        pass
    else:
        raise AssertionError("a failed read must not look like an empty tab")
    assert sheets.read_cache_size() == 0, "the failure must not be cached as []"


def test_non_strict_read_returns_empty_for_display_callers(tmp_path):
    class Throttled(FakeComposio):
        async def execute_action(self, slug: str, args: dict) -> dict:
            if slug == "sheet_read":
                return {"ok": False, "error": "429 Quota exceeded"}
            return await super().execute_action(slug, args)

    settings = Settings(dry_run=False, repo_root=tmp_path)
    state = StateStore(tmp_path / "state")
    settings.google_sheet_id = "sid-pipeline"
    sheets = SheetsAgent(Throttled(), settings, state)
    sheets._sheet_ids = {"Pipeline": "sid-pipeline"}
    assert asyncio.run(sheets.read_tab("Pipeline", strict=False)) == []
