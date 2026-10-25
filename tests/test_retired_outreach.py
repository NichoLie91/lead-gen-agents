"""The old templated outreach flow is retired, and cannot come back by accident.

What the old flow did, and what these tests pin down:

- `_stage_outreach_email` wrote one templated email per scored lead, approved
  every WARM draft itself, and `_stage_send_approved` sent them in the same run.
- HOT leads were sent with no approval at all.
- The Drafts tab therefore grew by the whole scored list every day with mail
  that had already gone out, and the daily `full` cron repeated it.

Every one of those senders must now be inert, and `purge` must clear the pile
the old flow left behind without ever re-contacting a business it already
reached.
"""
from __future__ import annotations

import asyncio

from src import site_research as r
from src.core.config import Settings
from src.core.ident import lead_id
from src.pipeline import Pipeline


def make(tmp_path) -> Pipeline:
    """Offline Pipeline over a tmp state dir (dry-run mirror, no network)."""
    settings = Settings(dry_run=True, repo_root=tmp_path)
    return Pipeline(settings)


class _ComposioSpy:
    """Records any attempt to send. Nothing may ever appear here."""

    def __init__(self):
        self.sent: list[tuple] = []

    @property
    def connected(self) -> bool:
        return True

    async def gmail_send_email(self, **kwargs):
        self.sent.append(("email", kwargs))
        return {"ok": True}

    async def ig_send_dm(self, **kwargs):
        self.sent.append(("ig", kwargs))
        return {"ok": True}


def _lead(name: str, tier: str = "WARM", email: str = "owner@acme.com") -> dict:
    return {
        "name": name, "address": "123 Main St", "vertical": "plumber",
        "city": "Houston", "rating": 4.9, "reviews": 120, "email": email,
        "score": {"tier": tier, "total": 85},
    }


# ---------- the senders are inert ----------

def test_outreach_email_drafts_nothing_and_sends_nothing(tmp_path):
    p = make(tmp_path)
    p.composio = _ComposioSpy()
    asyncio.run(p.sheets.ensure_sheet())
    asyncio.run(p.crm.load())

    report = {"metrics": {}}
    asyncio.run(p._stage_outreach_email([_lead("OA Plumbing"), _lead("ACME", "HOT-VERIFIED")], report))

    assert p.composio.sent == [], "retired stage must never call a send action"
    assert report["metrics"]["legacy_outreach_retired"] is True
    assert "emails_drafted" not in report["metrics"]
    assert p._drafts_rows == []
    assert asyncio.run(p.sheets.read_tab("Outreach")) in ([[], []], [[]], None) or \
        len(asyncio.run(p.sheets.read_tab("Outreach"))) == 1, "no outreach rows written"


def test_send_approved_is_inert_even_with_approved_rows(tmp_path):
    """This was the choke point: an APPROVED hash plus a NEEDS_APPROVAL row."""
    p = make(tmp_path)
    p.composio = _ComposioSpy()
    asyncio.run(p.sheets.ensure_sheet())
    asyncio.run(p.crm.load())
    lid = lead_id("OA Plumbing", "123 Main St")
    asyncio.run(p.sheets.write_tab("Outreach", [[
        1, "OA Plumbing", lid, "info@oa.test", "email", "Quick question",
        "Hi team...", "NEEDS_APPROVAL", "",
    ]]))
    p.approvals.register(lid)
    p.approvals.decide(lid, "approved")

    report = {"metrics": {}}
    asyncio.run(p._stage_send_approved(report))

    assert p.composio.sent == []
    assert report["metrics"]["legacy_sender_retired"] is True
    after = asyncio.run(p.sheets.read_tab("Outreach"))
    assert after[1][7] == "NEEDS_APPROVAL", "the row must not be touched at all"


def test_ig_dms_are_inert(tmp_path):
    p = make(tmp_path)
    p.composio = _ComposioSpy()
    lead = {**_lead("OA Plumbing", "HOT-VERIFIED"), "instagram": "oaplumbing"}
    report = {"metrics": {}}
    asyncio.run(p._stage_outreach_ig([lead], report))
    assert p.composio.sent == []
    assert report["metrics"]["legacy_ig_retired"] is True


def test_followups_report_due_but_send_nothing(tmp_path):
    p = make(tmp_path)
    p.composio = _ComposioSpy()
    asyncio.run(p.crm.load())
    lid = lead_id("Acme", "1 Main")
    p.crm.upsert(lid, name="Acme", email="a@b.co")
    p.crm.set_status(lid, "CONTACTED")
    p.crm.schedule_followup(lid, 0)  # due today

    report = {"metrics": {}}
    asyncio.run(p._stage_followups(report))

    assert p.composio.sent == []
    assert report["metrics"]["followups_sent"] == 0
    assert report["metrics"]["followups_due_manual"] >= 1


def test_a_retired_mode_explains_itself_instead_of_sending(tmp_path):
    p = make(tmp_path)
    report = {"metrics": {}}
    asyncio.run(p._stage_retired_senders("outreach-email", report))
    assert report["metrics"]["retired_outreach_email"] is True
    assert p.notifier is not None


# ---------- purge ----------

def _seed_legacy_pile(p: Pipeline) -> tuple[str, str]:
    asyncio.run(p.sheets.ensure_sheet())
    lid_sent = lead_id("Already Contacted", "1 Old St")
    lid_pending = lead_id("Never Sent", "2 Old St")
    asyncio.run(p.sheets.write_tab("Outreach", [
        [1, "Already Contacted", lid_sent, "a@x.test", "email", "S", "B", "SENT", ""],
        [2, "Never Sent", lid_pending, "b@x.test", "email", "S", "B",
         "NEEDS_APPROVAL", "Needs review (Warm)"],
    ]))
    asyncio.run(p.sheets.write_tab("Drafts", [
        [1, "Never Sent", lid_pending, "b@x.test", "S", "B", 85, "WARM",
         "NEEDS_APPROVAL", "2026-10-20", "Auto-approved WARM"],
        [2, "Never Sent", lid_pending, "b@x.test", "S", "B", 85, "WARM",
         "NEEDS_APPROVAL", "2026-10-21", "Auto-approved WARM"],
    ]))
    p.approvals.register(lid_pending)
    p.approvals.decide(lid_pending, "approved")
    return lid_sent, lid_pending


def test_purge_clears_the_draft_pile(tmp_path):
    p = make(tmp_path)
    _seed_legacy_pile(p)

    report = {"metrics": {}}
    asyncio.run(p._stage_purge_legacy(report))

    drafts = asyncio.run(p.sheets.read_tab("Drafts"))
    assert len(drafts) <= 1, "only the header may remain"
    assert report["metrics"]["purge_drafts_removed"] == 2


def test_purge_retires_pending_outreach_rows_and_keeps_history(tmp_path):
    p = make(tmp_path)
    lid_sent, _ = _seed_legacy_pile(p)

    asyncio.run(p._stage_purge_legacy({"metrics": {}}))

    rows = asyncio.run(p.sheets.read_tab("Outreach"))
    statuses = {row[1]: row[7] for row in rows[1:]}
    assert statuses["Never Sent"] == "REJECTED"
    assert statuses["Already Contacted"] == "SENT", "history must survive"
    assert lid_sent in [row[2] for row in rows[1:]]


def test_purge_disarms_the_approval_queue(tmp_path):
    p = make(tmp_path)
    _seed_legacy_pile(p)
    assert p.approvals.approved(), "seeded queue should be armed"

    asyncio.run(p._stage_purge_legacy({"metrics": {}}))

    assert p.approvals.approved() == set()
    assert p.approvals.pending() == []


def test_purge_never_recontacts_anyone_the_old_flow_touched(tmp_path):
    """The hand-over: every lead id in the Outreach tab becomes 'done' for the
    batch, and the daily gate is cleared so a batch can run today."""
    p = make(tmp_path)
    lid_sent, lid_pending = _seed_legacy_pile(p)
    p.state.save(r.STATE_FILE, {
        **r.new_state(), "batches": 3, "awaiting_confirm": True,
        "last_batch_date": "2026-10-24", "done": ["already-there"],
    })

    report = {"metrics": {}}
    asyncio.run(p._stage_purge_legacy(report))

    state = p.state.load(r.STATE_FILE)
    assert set(state["done"]) >= {lid_sent, lid_pending, "already-there"}
    assert state["awaiting_confirm"] is False
    assert state["last_batch_date"] == "", "a batch may run today"
    assert state["force_next"] is False
    assert state["batches"] == 3, "the counter is history, not a gate"
    assert report["metrics"]["purge_leads_remembered"] == 3


def test_purge_marks_queued_leads_with_the_batch_hash(tmp_path):
    """The old rows key on name|address, the batch on name|city. Seeding only
    the old ids would miss every queued lead and mail them a second time."""
    p = make(tmp_path)
    _seed_legacy_pile(p)
    from src.agents.sheets_agent import PIPELINE_HEADER
    row = [""] * len(PIPELINE_HEADER)
    row[PIPELINE_HEADER.index("business_name")] = "Never Sent"
    row[PIPELINE_HEADER.index("city")] = "Houston"
    asyncio.run(p.sheets.write_tab("Pipeline", [row]))

    asyncio.run(p._stage_purge_legacy({"metrics": {}}))

    state = p.state.load(r.STATE_FILE)
    expected = r.lead_hash({"name": "Never Sent", "city": "Houston"})
    assert expected in state["done"], "the batch must never write to this one"


def test_purge_is_idempotent(tmp_path):
    p = make(tmp_path)
    _seed_legacy_pile(p)
    asyncio.run(p._stage_purge_legacy({"metrics": {}}))
    report = {"metrics": {}}
    asyncio.run(p._stage_purge_legacy(report))
    assert report["metrics"]["purge_drafts_removed"] == 0
    assert report["metrics"]["purge_outreach_retired"] == 0


# ---------- telegram commands ----------

class _GitHub:
    def __init__(self):
        self.modes: list[str] = []

    async def trigger_pipeline(self, mode: str) -> str:
        self.modes.append(mode)
        return f"Pipeline ({mode}) triggered."


def _agent(tmp_path):
    from src.agents.lead_agent import LeadAgent

    agent = LeadAgent.__new__(LeadAgent)
    agent.state = tmp_path and __import__("src.core.state", fromlist=["StateStore"]).StateStore(tmp_path)
    agent.github = _GitHub()
    return agent


def test_send_commands_answer_without_triggering_a_run(tmp_path):
    agent = _agent(tmp_path)
    for command in ("/send all email", "/send all instagram", "/followups"):
        out = asyncio.run(agent.delegate(command, "", 0))
        assert "Sending is retired" in out
    assert agent.github.modes == [], "no pipeline run should be dispatched"


def test_purge_command_dispatches_the_migration(tmp_path):
    agent = _agent(tmp_path)
    out = asyncio.run(agent.delegate("/purge", "", 0))
    assert agent.github.modes == ["purge"]
    assert "purge" in out.lower()
