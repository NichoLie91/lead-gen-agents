"""The master list: one row per business, both phone numbers, no duplicates."""
from __future__ import annotations

import asyncio

from src.agents.sheets_agent import ALL_LEADS_HEADER
from src.all_leads import build_all_leads, merge_rows, to_rows
from src.enrichment import find_owner_phone


def _row(**kw) -> dict:
    return {h: kw.get(h, "") for h in ALL_LEADS_HEADER}


# ---------- owner / manager phone extraction ----------

def test_owner_phone_needs_person_context():
    text = ("Call the shop on (713) 555-0100. Ask for Dave, the owner, "
            "on his mobile (832) 555-0199.")
    assert find_owner_phone(text, "(713) 555-0100") == "(832) 555-0199"


def test_owner_phone_ignores_the_public_number():
    text = "Owner: Dave. Phone: (713) 555-0100"
    assert find_owner_phone(text, "(713) 555-0100") == ""


def test_owner_phone_is_blank_without_owner_wording():
    """A second number with no owner/manager context is not the owner's line."""
    text = "Shop (713) 555-0100 | Warehouse (713) 555-0177"
    assert find_owner_phone(text, "(713) 555-0100") == ""


def test_owner_phone_empty_text():
    assert find_owner_phone("", "555") == ""


# ---------- dedupe ----------

def test_same_phone_is_one_lead_even_with_a_different_name():
    rows = [_row(business_name="Blue Creek Plumbing", public_phone="(713) 555-0100"),
            _row(business_name="Blue Creek Plumbing Co",
                 public_phone="713-555-0100")]
    assert len(merge_rows(rows)) == 1


def test_same_email_is_one_lead():
    rows = [_row(business_name="Shop A", email="info@shop.test", city="Houston"),
            _row(business_name="Shop A", email="INFO@shop.test", city="Houston")]
    assert len(merge_rows(rows)) == 1


def test_same_name_and_city_is_one_lead():
    rows = [_row(business_name="Dental Smiles", city="Houston"),
            _row(business_name="dental smiles", city="houston")]
    assert len(merge_rows(rows)) == 1


def test_same_name_in_two_cities_stays_two_leads():
    rows = [_row(business_name="Dental Smiles", city="Houston"),
            _row(business_name="Dental Smiles", city="Dallas")]
    assert len(merge_rows(rows)) == 2


def test_name_only_rows_collapse_into_one():
    """Outreach/CRM rows arrive with a name and nothing else — three copies
    of the same shop must not survive as three rows (found in a live run)."""
    rows = [_row(business_name="Anderson Cleaning", sales_status=""),
            _row(business_name="Anderson Cleaning", sales_status="SKIP"),
            _row(business_name="Anderson Cleaning", sales_status="SKIP")]
    assert len(merge_rows(rows)) == 1


def test_a_name_only_row_joins_the_single_city_row():
    rows = [_row(business_name="Blue Creek Plumbing", city="Houston",
                 public_phone="(713) 555-0100"),
            _row(business_name="Blue Creek Plumbing", sales_status="CONTACTED")]
    merged = merge_rows(rows)
    assert len(merged) == 1
    assert merged[0]["sales_status"] == "CONTACTED"


def test_a_name_only_row_will_not_guess_between_two_cities():
    rows = [_row(business_name="Dental Smiles", city="Houston",
                 public_phone="(713) 555-0100"),
            _row(business_name="Dental Smiles", city="Dallas",
                 public_phone="(214) 555-0100"),
            _row(business_name="Dental Smiles", sales_status="SKIP")]
    merged = merge_rows(rows)
    assert len(merged) == 3, "ambiguous name stays separate instead of taking a city's data"


def test_nameless_rows_are_dropped():
    assert merge_rows([_row(), _row(city="Houston")]) == []


def test_different_phone_and_name_is_two_leads():
    rows = [_row(business_name="Shop A", public_phone="(713) 555-0100"),
            _row(business_name="Shop B", public_phone="(832) 555-0100")]
    assert len(merge_rows(rows)) == 2


# ---------- merging ----------

def test_merge_fills_blanks_and_keeps_the_best_score():
    first = _row(business_name="Blue Creek", public_phone="(713) 555-0100",
                 scout_score=62, first_seen="2026-10-01", last_seen="2026-10-01",
                 sources="pipeline")
    second = _row(business_name="Blue Creek", public_phone="(713) 555-0100",
                  email="info@bluecreek.test", owner_or_manager_phone="(832) 555-0199",
                  scout_score=88, first_seen="2026-10-20", last_seen="2026-10-20",
                  sources="discovery")
    merged = merge_rows([first, second])[0]

    assert merged["email"] == "info@bluecreek.test"
    assert merged["owner_or_manager_phone"] == "(832) 555-0199"
    assert merged["scout_score"] == 88
    assert merged["first_seen"] == "2026-10-01"   # first time we ever saw it
    assert merged["last_seen"] == "2026-10-20"    # most recent sighting
    assert merged["sources"] == "discovery,pipeline"


def test_rows_come_out_in_header_order():
    rows = to_rows([_row(business_name="A", city="Houston")])
    assert rows[0][ALL_LEADS_HEADER.index("business_name")] == "A"
    assert len(rows[0]) == len(ALL_LEADS_HEADER)


# ---------- building from every tab ----------

class _Sheets:
    def __init__(self, tabs: dict[str, list[list]]):
        self._tabs = tabs
        self.written: dict[str, list] = {}

    async def read_tab(self, tab: str) -> list[list]:
        return self._tabs.get(tab, [])

    async def write_tab(self, tab: str, rows: list[list]) -> bool:
        self.written[tab] = rows
        return True


def test_build_merges_every_tab_into_one_row_per_business():
    pipeline = [
        ["rank", "business_name", "verified_email", "email_verification_method",
         "city", "state", "trade", "phone", "owner_or_manager_phone",
         "official_site", "rating", "review_count", "instagram_profile",
         "booking_signal", "emergency_signal", "source_url",
         "source_evidence_text", "evidence_quality", "scout_score",
         "scout_tier", "recommended_channel", "hold_reason"],
        [1, "Blue Creek Plumbing", "", "", "Houston", "TX", "plumber",
         "(713) 555-0100", "", "https://bluecreek.test", "4.8", "84", "", "", "",
         "", "", "high", "72", "WARM", "hold", ""],
    ]
    score = [["Lead", "ICP (0-25)", "Intent (0-25)", "Budget (0-20)",
              "Reachability (0-15)", "Timing (0-15)", "Total", "Tier"],
             ["Blue Creek Plumbing", 20, 18, 15, 12, 10, 75, "WARM"]]
    outreach = [["#", "Lead", "Lead ID", "Email", "Channel", "Subject", "Body",
                 "Status", "Send Date"],
                [1, "Blue Creek Plumbing", "abc", "info@bluecreek.test", "email",
                 "s", "b", "DRAFTED", ""]]
    crm = [["Lead ID", "Name", "Email", "Instagram", "Tier", "Status",
            "Last Contact", "Next Follow-up", "Follow-ups Sent", "Last Reply",
            "Timeline", "Notes"],
           ["abc", "Blue Creek Plumbing", "info@bluecreek.test", "", "WARM",
            "CONTACTED", "", "", "0", "", "[]", ""]]
    fresh = [{
        "name": "Blue Creek Plumbing", "city": "Houston", "state": "TX",
        "vertical": "plumber", "phone": "(713) 555-0100",
        "owner_phone": "(832) 555-0199", "email": "info@bluecreek.test",
        "website": "https://bluecreek.test", "rating": 4.8, "reviews": 84,
        "score": {"total": 80, "tier": "WARM"},
    }]
    sheets = _Sheets({"Pipeline": pipeline, "Score": score,
                      "Outreach": outreach, "Drafts": [], "CRM": crm,
                      "AllLeads": []})

    rows = asyncio.run(build_all_leads(sheets, fresh, today="2026-10-26"))

    assert len(rows) == 1, "one business, five sources, one row"
    row = rows[0]
    assert row["public_phone"] == "(713) 555-0100"
    assert row["owner_or_manager_phone"] == "(832) 555-0199"
    assert row["email"] == "info@bluecreek.test"
    assert row["scout_score"] == 80          # best of 72 / 75 / 80
    assert row["sales_status"] == "CONTACTED"  # CRM wins
    assert {"pipeline", "score", "outreach", "crm", "discovery"} <= set(
        row["sources"].split(","))


def test_build_keeps_history_from_previous_runs():
    existing = [["lead_id", "business_name", "trade", "city", "state",
                 "public_phone", "owner_or_manager_phone", "email",
                 "email_status", "website", "rating", "review_count",
                 "scout_score", "scout_tier", "sales_status", "sources",
                 "first_seen", "last_seen"],
                ["h1", "Old Timer Plumbing", "plumber", "Houston", "TX",
                 "(713) 555-0111", "", "old@timer.test", "", "", "4.6", "40",
                 "70", "WARM", "", "pipeline", "2026-09-01", "2026-09-01"]]
    sheets = _Sheets({"AllLeads": existing, "Pipeline": [], "Score": [],
                      "Outreach": [], "Drafts": [], "CRM": []})

    rows = asyncio.run(build_all_leads(sheets, [], today="2026-10-26"))
    assert len(rows) == 1
    assert rows[0]["business_name"] == "Old Timer Plumbing"
    assert rows[0]["first_seen"] == "2026-09-01"
    assert rows[0]["last_seen"] == "2026-09-01", "no new sighting, no new date"


def test_a_quiet_run_does_not_empty_the_master_list():
    existing = [["lead_id", "business_name"],
                ["h1", "Old Timer Plumbing"]]
    sheets = _Sheets({"AllLeads": existing, "Pipeline": [], "Score": [],
                      "Outreach": [], "Drafts": [], "CRM": []})
    rows = asyncio.run(build_all_leads(sheets, [], today="2026-10-26"))
    assert [r["business_name"] for r in rows] == ["Old Timer Plumbing"]
