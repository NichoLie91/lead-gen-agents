"""Sector-routed first-touch copy: plumbing, HVAC, dental.

Two things are being protected here:

1. The sector comes from the lead's `vertical`, not from its business name.
   The old send path guessed with a substring table and mislabelled 8 of 53
   leads "home-services".
2. Nothing outside the three sectors is drafted at all. A silent generic
   fallback is the failure mode this module replaced.
"""
import asyncio
import json
from pathlib import Path

import pytest

from src.email_lint import append_footer, lint_email
from src.pipeline import Pipeline
from src.sector_copy import (
    BODIES,
    DENTAL,
    HVAC,
    PLUMBING,
    SECTORS,
    SUBJECTS,
    UnsupportedSector,
    draft,
    sector_of,
)

CRITERIA = json.loads(
    (Path(__file__).resolve().parents[1] / "config" / "criteria.json").read_text("utf-8")
)


# ---------- routing ----------

def test_routes_known_verticals():
    assert sector_of("plumber") == PLUMBING
    assert sector_of("drain cleaning") == PLUMBING
    assert sector_of("hvac") == HVAC
    assert sector_of("ac repair") == HVAC
    assert sector_of("dentist") == DENTAL
    assert sector_of("orthodontist") == DENTAL
    assert sector_of("dental implants") == DENTAL


def test_routing_is_case_and_space_insensitive():
    assert sector_of("  Dental  Clinic ") == DENTAL
    assert sector_of("AC_Repair") == HVAC


def test_retired_verticals_are_not_routed():
    for vertical in ("cleaning", "storage unit", "moving company", "painter", "alien", ""):
        assert sector_of(vertical) is None


def test_unknown_vertical_refuses_to_draft():
    # The old path returned "home-services" here and sent anyway.
    with pytest.raises(UnsupportedSector):
        draft({"name": "Mystery Co", "vertical": "storage unit"})
    with pytest.raises(UnsupportedSector):
        draft({"name": "Mystery Co", "vertical": ""})


def test_a_configured_vertical_is_either_routed_or_openly_refused():
    """Discovery can now surface trades outside the three sectors (criteria.json
    lists electricians, roofers, movers...). The contract is not "everything
    maps" — it is "nothing falls back silently": either a sector, or an explicit
    UnsupportedSector refusal."""
    for vertical in CRITERIA["verticals"]:
        sector = sector_of(vertical)
        if sector is None:
            with pytest.raises(UnsupportedSector):
                draft({"name": "Example Co", "vertical": vertical})
            continue
        assert sector in SECTORS, vertical
        draft({"name": "Example Co", "vertical": vertical})  # must not raise


# ---------- variation ----------

def test_every_sector_has_multiple_variants():
    for sector in SECTORS:
        assert len(SUBJECTS[sector]) >= 2
        assert len(BODIES[sector]) >= 2


def test_draft_is_deterministic_per_lead():
    lead = {"name": "Blue Creek Plumbing", "vertical": "plumber"}
    assert draft(lead) == draft(lead)


def test_different_leads_get_different_copy():
    leads = [{"name": n, "vertical": "plumber"} for n in
             ("Alpha Plumbing", "Bravo Plumbing", "Charlie Plumbing",
              "Delta Plumbing", "Echo Plumbing")]
    assert len({draft(ld)[0] for ld in leads}) > 1
    assert len({draft(ld)[1] for ld in leads}) > 1


# ---------- compliance (playbook §2) ----------

def test_every_subject_and_body_passes_the_linter():
    for sector in SECTORS:
        for subject in SUBJECTS[sector]:
            for body in BODIES[sector]:
                final = append_footer(body, "Example Co", CRITERIA)
                assert lint_email(subject, final, CRITERIA) == [], (subject, body[:40])
                assert Pipeline._lint_ai_tells(subject + "\n" + body) == [], subject


def test_copy_carries_no_invented_proof():
    """The old template claimed '11 extra bookings ... paying only from the
    jobs it booked'. There is no client who produced that number."""
    banned = ("11 extra", "extra bookings", "first month", "only from the jobs")
    for body in BODIES[PLUMBING] + BODIES[HVAC] + BODIES[DENTAL]:
        low = body.lower()
        for phrase in banned:
            assert phrase not in low, phrase


class _NoLLM:
    """Dry-run stand-in: no humanizer, so the template itself is asserted."""

    available = False


def _pipeline() -> Pipeline:
    pipe = Pipeline.__new__(Pipeline)
    pipe.llm = _NoLLM()
    pipe.state = None  # memory_block(None, ...) is a no-op
    return pipe


def test_pipeline_drafts_sector_copy():
    subject, body = asyncio.run(_pipeline()._draft_email(
        {"name": "Blue Creek Plumbing", "vertical": "plumber"}))
    assert subject in SUBJECTS[PLUMBING]
    assert body in BODIES[PLUMBING]


def test_pipeline_refuses_a_lead_outside_the_three_sectors():
    with pytest.raises(UnsupportedSector):
        asyncio.run(_pipeline()._draft_email(
            {"name": "Safe Storage", "vertical": "storage unit"}))


def test_sector_copy_stays_in_its_sector():
    plumber_copy = " ".join(BODIES[PLUMBING]).lower()
    dental_copy = " ".join(BODIES[DENTAL]).lower()
    hvac_copy = " ".join(BODIES[HVAC]).lower()
    # The plumbing body used to be sent verbatim to dental practices.
    assert "patient" not in plumber_copy and "hygiene" not in plumber_copy
    assert "pipe" not in dental_copy and "drain" not in dental_copy
    assert "ladder" not in plumber_copy and "ladder" not in hvac_copy
