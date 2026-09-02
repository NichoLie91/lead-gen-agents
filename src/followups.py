"""Follow-up cadence — Hormozi $100M Leads 4-email sequence.

Sequence (from examples.md + 8 video deep-dive):
  Day 3: Bump — low-friction "circling back" check-in
  Day 7: New angle + value — give something useful, not a pitch
  Day 14: Break-up — social pressure flips, highest response rate

"The break-up email often outperforms the initial outreach for response rate,
because the social pressure flips." — Hormozi examples.md

Pure logic (no I/O) so it is unit-testable.
"""
from __future__ import annotations

from datetime import UTC, date, datetime

# Days after initial contact for each follow-up in the cadence.
FOLLOWUP_INTERVALS_DAYS = (3, 7, 14)
MAX_FOLLOWUPS = len(FOLLOWUP_INTERVALS_DAYS)

FOLLOWUP_STEP_LABELS = {
    0: "Day 3: bump",
    1: "Day 7: new angle",
    2: "Day 14: break-up",
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
    """Hormozi follow-up sequence: bump -> new angle -> break-up.

    Day 3 (bump): Low-friction "circling back" — no pitch, just check if timing works.
    Day 7 (new angle): Give value, not a pitch. Share something useful.
    Day 14 (break-up): The most powerful email. Social pressure flips. "Closing my followups".
    """
    name = lead.get("Name") or "the business"
    city = lead.get("City") or "your city"
    vertical = lead.get("Vertical") or "service"
    if step_index == 0:  # Day 3 bump — Hormozi: "Wanted to bump this in case it got buried"
        return (
            f"Hey {name}, wanted to bump this in case it got buried. "
            f"Quick question — is the missed-call / after-hours follow-up "
            f"something you're actively working on, or is it on the back burner? "
            f"Either answer is fine."
        )
    if step_index == 1:  # Day 7 new angle — Hormozi: "Different angle" + give value
        return (
            f"Hey {name}, different angle. A {city} {vertical} business I work "
            f"with was losing about $4k/month to missed after-hours calls. We "
            f"automated the follow-up and they booked 11 extra jobs in the first "
            f"month. Happy to share exactly how if that's useful. "
            f"Want the breakdown?"
        )
    # Day 14 break-up — Hormozi: "Closing my followups" + social pressure flips
    return (
        f"Hey {name}, I'm closing my followups on this thread — figured you're "
        f"slammed and this isn't the right moment. If the missed-call problem "
        f"becomes a priority down the road, I'm around. Best of luck either way."
    )


def _as_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
