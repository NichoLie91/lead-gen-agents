"""CRM Agent tests (Step 06): lead memory over the private sheet (dry-run)."""
from __future__ import annotations

import asyncio

from src.agents.composio_agent import ComposioAgent
from src.agents.crm_agent import CrmAgent
from src.agents.sheets_agent import SheetsAgent
from src.core.config import Settings
from src.core.state import StateStore


def make(tmp_path) -> tuple[CrmAgent, StateStore]:
    settings = Settings(dry_run=True, repo_root=tmp_path)
    state = StateStore(tmp_path / "state")
    sheets = SheetsAgent(ComposioAgent(settings), settings, state)
    return CrmAgent(sheets), state


def test_upsert_and_find_by_email(tmp_path):
    crm, _ = make(tmp_path)
    asyncio.run(crm.load())
    crm.upsert("lid1", name="Acme Plumbing", email="owner@acme.com", tier="WARM")
    found = crm.find_by_email("OWNER@acme.com")   # case-insensitive match
    assert found is not None
    assert found["Lead ID"] == "lid1"
    assert crm.find_by_email("nobody@else.com") is None


def test_timeline_and_save_roundtrip(tmp_path):
    crm, _ = make(tmp_path)
    asyncio.run(crm.load())
    crm.upsert("lid1", name="Acme")
    crm.append_timeline("lid1", "email-sent", "subject here")
    timeline = crm.timeline("lid1")
    assert len(timeline) == 1
    assert timeline[0]["event"] == "email-sent"
    assert timeline[0]["detail"] == "subject here"
    asyncio.run(crm.save())
    rows = asyncio.run(crm._sheets.read_tab("CRM"))
    assert any(r and r[0] == "lid1" for r in rows)


def test_followup_scheduling_and_counter(tmp_path):
    crm, _ = make(tmp_path)
    asyncio.run(crm.load())
    crm.upsert("lid1")
    crm.schedule_followup("lid1", 3)
    row = crm.find_by_lead_id("lid1")
    assert row["Next Follow-up"].startswith("2")   # a date like 2026-08-08
    crm.record_followup_sent("lid1")
    assert row["Follow-ups Sent"] == "1"


def test_status_updates(tmp_path):
    crm, _ = make(tmp_path)
    asyncio.run(crm.load())
    crm.upsert("lid1")
    crm.set_status("lid1", "CONTACTED")
    assert crm.find_by_lead_id("lid1")["Status"] == "CONTACTED"


def test_save_without_load_preserves_existing_rows(tmp_path):
    """Regression: discovery/enrichment/outreach-ig modes never call load();
    their save() must not clobber the CRM with a header-only write."""
    crm1, _ = make(tmp_path)
    asyncio.run(crm1.load())
    crm1.upsert("lid1", name="Acme Plumbing", email="owner@acme.com")
    asyncio.run(crm1.save())

    # Fresh agent over the same state dir, WITHOUT load() (simulates a
    # discovery-mode run) -> save must preserve the existing lead memory.
    # Real runs always call ensure_sheet() first, which loads the mirror.
    crm2, _ = make(tmp_path)
    asyncio.run(crm2._sheets.ensure_sheet())
    asyncio.run(crm2.save())
    rows = asyncio.run(crm2._sheets.read_tab("CRM"))
    assert any(r and r[0] == "lid1" for r in rows)
    assert crm2.find_by_lead_id("lid1") is not None
