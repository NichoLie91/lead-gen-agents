"""Follow-up cadence tests (Step 06): Day 3/7/14 scheduling logic."""
from __future__ import annotations

from datetime import date

from src.followups import (
    FOLLOWUP_INTERVALS_DAYS,
    build_followup_body,
    is_due,
    next_interval_days,
)


def test_interval_math():
    assert FOLLOWUP_INTERVALS_DAYS == (3, 7, 14)
    assert next_interval_days(0) == 3
    assert next_interval_days(1) == 7
    assert next_interval_days(2) == 14
    assert next_interval_days(3) is None  # cadence exhausted


def test_is_due_date_window():
    today = date(2026, 8, 5)
    row = {"Status": "CONTACTED", "Follow-ups Sent": "1", "Next Follow-up": "2026-08-05"}
    assert is_due(row, today) is True
    row["Next Follow-up"] = "2026-08-06"
    assert is_due(row, today) is False
    row["Next Follow-up"] = ""
    assert is_due(row, today) is True  # never scheduled -> due


def test_is_due_gates_on_status():
    row = {"Status": "NEW", "Follow-ups Sent": "0", "Next Follow-up": ""}
    assert is_due(row) is False
    row["Status"] = "UNSUBSCRIBED"
    assert is_due(row) is False
    row["Status"] = "REPLIED-INTERESTED"
    assert is_due(row) is True


def test_is_due_stops_at_max_followups():
    row = {"Status": "CONTACTED", "Follow-ups Sent": "3", "Next Follow-up": ""}
    assert is_due(row) is False


def test_bodies_include_name_and_opt_out():
    body = build_followup_body({"Name": "Acme Plumbing", "City": "Houston"}, 0)
    assert "Acme Plumbing" in body
    assert "stop" in body
    assert "stop" in build_followup_body({}, 1)
    assert "stop" in build_followup_body({}, 2)
