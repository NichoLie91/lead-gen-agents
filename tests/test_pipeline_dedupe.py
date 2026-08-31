"""Regression tests for duplicate-email prevention (the OA Plumbing 3x bug).

Two guards added to src/pipeline.py:
1. _stage_outreach_email must NOT create a second draft for a lead that already
   has an Outreach row (any status) or a DRAFTED/CONTACTED CRM status.
2. _stage_send_approved must send each approved lead AT MOST ONCE per pass —
   accumulated duplicate NEEDS_APPROVAL rows for one lead flip to SKIP instead
   of producing N identical emails.
"""
from __future__ import annotations

import asyncio

from src.core.config import Settings
from src.pipeline import Pipeline


def make(tmp_path) -> Pipeline:
    """Offline Pipeline over a tmp state dir (dry-run mirror, no network)."""
    settings = Settings(dry_run=True, repo_root=tmp_path)
    return Pipeline(settings)


def _lead(name: str, tier: str = "WARM", email: str = "owner@acme.com") -> dict:
    return {
        "name": name, "address": "123 Main St", "vertical": "plumber",
        "city": "Houston", "rating": 4.9, "reviews": 120, "email": email,
        "score": {"tier": tier, "total": 85},
    }


def test_outreach_email_skips_lead_already_in_table(tmp_path):
    """A lead that already has an Outreach row must not be drafted again."""
    p = make(tmp_path)
    asyncio.run(p.sheets.ensure_sheet())
    asyncio.run(p.crm.load())

    # First run: draft the lead normally.
    report1 = {"metrics": {}}
    asyncio.run(p._stage_outreach_email([_lead("OA Plumbing LLC")], report1))
    assert report1["metrics"]["emails_drafted"] == 1

    # Second run: same lead appears again (e.g. re-discovered after a failed
    # state push) -> must be skipped, NOT drafted a second time.
    report2 = {"metrics": {}}
    asyncio.run(p._stage_outreach_email([_lead("OA Plumbing LLC")], report2))
    assert report2["metrics"]["emails_drafted"] == 0
    assert report2["metrics"]["emails_skipped"] == 1

    rows = asyncio.run(p.sheets.read_tab("Outreach"))
    drafts = [r for r in rows[1:] if r[7] == "NEEDS_APPROVAL"]
    assert len(drafts) == 1, "same lead must only ever get one draft row"


def test_outreach_email_allows_redraft_after_outreach_cleared(tmp_path):
    """After clearing the Outreach tab, a lead with CRM CONTACTED status
    can be re-drafted. Only the Outreach tab guards against duplicates
    (the CRM guard was removed so clearing Outreach allows fresh drafts)."""
    p = make(tmp_path)
    asyncio.run(p.sheets.ensure_sheet())
    asyncio.run(p.crm.load())
    lead = _lead("Done Plumbing")
    from src.core.ident import lead_id
    lid = lead_id(lead["name"], lead["address"])
    p.crm.upsert(lid, name=lead["name"], email=lead["email"],
                 status="CONTACTED")

    report = {"metrics": {}}
    asyncio.run(p._stage_outreach_email([lead], report))
    # Should be drafted (not skipped) since Outreach tab is empty.
    assert report["metrics"]["emails_drafted"] == 1
    assert report["metrics"]["emails_skipped"] == 0


def test_send_approved_sends_each_lid_once(tmp_path):
    """Three accumulated NEEDS_APPROVAL rows for one lead = ONE email."""
    p = make(tmp_path)
    asyncio.run(p.sheets.ensure_sheet())
    asyncio.run(p.crm.load())

    # Simulate the bug: 3 identical draft rows for the same lead.
    lid = "0980048279ccc22969df5f48c7994252f1997166890d0e42d970535a4f9b8bbd"
    rows = [
        [1, "OA Plumbing LLC", lid, "info@oaplumbingllc.com", "email",
         "Quick question", "Hi team...", "NEEDS_APPROVAL", ""],
        [1, "OA Plumbing LLC", lid, "info@oaplumbingllc.com", "email",
         "Quick question", "Hi team...", "NEEDS_APPROVAL", ""],
        [1, "OA Plumbing LLC", lid, "info@oaplumbingllc.com", "email",
         "Quick question", "Hi team...", "NEEDS_APPROVAL", ""],
    ]
    asyncio.run(p.sheets.write_tab("Outreach", rows))
    p.approvals.register(lid)
    p.approvals.decide(lid, "approved")

    report = {"metrics": {}}
    asyncio.run(p._stage_send_approved(report))
    assert report["metrics"]["emails_approved_sent"] == 1, \
        "3 accumulated drafts for one approved lead must send exactly once"

    # The other two rows must now be SKIP, never SENT.
    after = asyncio.run(p.sheets.read_tab("Outreach"))
    statuses = [r[7] for r in after[1:]]
    assert statuses.count("SENT") == 1
    assert statuses.count("SKIP") == 2


def test_send_approved_skips_second_row_same_lid_in_one_pass(tmp_path):
    """Duplicate rows in the SAME table for one approved lead -> one send."""
    p = make(tmp_path)
    asyncio.run(p.sheets.ensure_sheet())
    asyncio.run(p.crm.load())
    lid = "abc123"
    rows = [
        [1, "Acme", lid, "a@b.com", "email", "S", "B", "NEEDS_APPROVAL", ""],
        [1, "Acme", lid, "a@b.com", "email", "S", "B", "NEEDS_APPROVAL", ""],
    ]
    asyncio.run(p.sheets.write_tab("Outreach", rows))
    p.approvals.register(lid)
    p.approvals.decide(lid, "approved")

    report = {"metrics": {}}
    asyncio.run(p._stage_send_approved(report))
    assert report["metrics"]["emails_approved_sent"] == 1

    after = asyncio.run(p.sheets.read_tab("Outreach"))
    statuses = [r[7] for r in after[1:]]
    assert statuses.count("SENT") == 1
    assert statuses.count("SKIP") == 1


def test_send_approved_ignores_rows_never_approved(tmp_path):
    """Rows whose lid was not approved must stay NEEDS_APPROVAL."""
    p = make(tmp_path)
    asyncio.run(p.sheets.ensure_sheet())
    asyncio.run(p.crm.load())
    rows = [
        [1, "Pending Co", "lid-pending", "p@x.com", "email", "S", "B",
         "NEEDS_APPROVAL", ""],
    ]
    asyncio.run(p.sheets.write_tab("Outreach", rows))
    # never approved -> not sent, not skipped
    report = {"metrics": {}}
    asyncio.run(p._stage_send_approved(report))
    assert report["metrics"]["emails_approved_sent"] == 0
    after = asyncio.run(p.sheets.read_tab("Outreach"))
    assert after[1][7] == "NEEDS_APPROVAL"
