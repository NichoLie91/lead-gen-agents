"""Follow-up cadence (Step 06) — Day 3 / 7 / 14 automation.

Pure logic (no I/O) so it is unit-testable: interval math, due-date checks and
template bodies. The pipeline stage sends the emails and updates the CRM.
"""
from __future__ import annotations

from datetime import UTC, date, datetime

# Days after initial contact for each follow-up in the cadence.
FOLLOWUP_INTERVALS_DAYS = (3, 7, 14)
MAX_FOLLOWUPS = len(FOLLOWUP_INTERVALS_DAYS)

FOLLOWUP_STEP_LABELS = {
    0: "Day 3: bump",
    1: "Day 7: case study",
    2: "Day 14: final ask",
}

# Statuses that are eligible for follow-ups (someone is listening).
ELIGIBLE_STATUSES = ("CONTACTED", "REPLIED-INTERESTED", "QUESTION")


def next_interval_days(followups_sent: int) -> int | None:
    """Days until the next follow-up, or None when the cadence is exhausted."""
    if followups_sent >= MAX_FOLLOWUPS:
        return None
    return FOLLOWUP_INTERVALS_DAYS[followups_sent]


def is_due(row: dict, today: date | None = None) -> bool:
    """True when this CRM row needs a follow-up sent now."""
    today = today or datetime.now(UTC).date()
    if row.get("Status") not in ELIGIBLE_STATUSES:
        return False
    sent = _as_int(row.get("Follow-ups Sent"))
    if sent >= MAX_FOLLOWUPS:
        return False
    raw_next = (row.get("Next Follow-up") or "").strip()
    if not raw_next:
        return True  # contacted but never scheduled -> due immediately
    try:
        return date.fromisoformat(raw_next) <= today
    except ValueError:
        return True


def build_followup_body(lead: dict, step_index: int) -> str:
    """Template body for the given follow-up step (Gemini can polish it)."""
    name = lead.get("Name") or "the business"
    city = lead.get("City") or "your city"
    if step_index == 0:  # Day 3 bump
        return (
            f"Hi {name} team — circling back on my note from a few days ago. "
            f"I help {city} service businesses automate the follow-up busywork "
            f"that slips through the cracks. I don't know if the timing is "
            f"right, but if a 15-minute call would help, I'll make it easy."
        )
    if step_index == 1:  # Day 7 case study
        return (
            f"Hi {name} — one example of what I mean. A {city} plumbing "
            f"business cut missed-call losses by automating after-hours "
            f"follow-up, and the first build paid for itself in the first "
            f"month. I can send the numbers if useful.\n\n"
            f"Still happy to compare notes this week."
        )
    # Day 14 final ask
    return (
        f"Hi {name} — last note from me. If the timing's off, no problem at "
        f"all: I'll close the loop here. If you do want a second opinion on "
        f"the missed-call problem, my calendar's open this week."
    )


def _as_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
