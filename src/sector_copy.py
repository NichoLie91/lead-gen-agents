"""Sector-routed first-touch copy: plumbing, HVAC, dental.

One copy module, two callers (`pipeline._draft_email` and `outreach/fix_outbound.py`),
so the pipeline and the batch sender can never drift apart again.

Why this exists
---------------
The previous version shipped ONE body for every recipient:

    "Running a {trade} business means the phone rings while you're up a ladder
     or under a sink."

with the trade guessed from a substring of the company name. On the 60-lead
sheet that produced 3 unique bodies across 53 leads, sent "up a ladder or under
a sink" to cleaners, and labelled 8 leads "home-services" because nothing in
their name matched a keyword -- a subject line that announces mass mail.

Rules this module keeps (COLD_EMAIL_PLAYBOOK.md):
- The sector comes from the lead's own `vertical` field, never from the name.
  An unknown vertical raises `UnsupportedSector` instead of guessing.
- Copy is sector-true: a plumber, an HVAC shop and a dental practice do not
  share a reason for missing calls, so they do not share sentences.
- No invented proof. There is no "11 extra bookings" line here, because there is
  no client who has ever produced one.
- Bodies are returned WITHOUT the CAN-SPAM footer; the caller runs
  `email_lint.append_footer()` then `lint_email()` before any send.
"""

from __future__ import annotations

PLUMBING = "plumbing"
HVAC = "hvac"
DENTAL = "dental"

SECTORS: tuple[str, ...] = (PLUMBING, HVAC, DENTAL)

# Exact-match routing. Keys are the vertical strings that discovery queries for
# (config/criteria.json) plus the spellings already present in stored lead rows.
VERTICAL_SECTOR: dict[str, str] = {
    # plumbing
    "plumber": PLUMBING,
    "plumbing": PLUMBING,
    "plumbing contractor": PLUMBING,
    "emergency plumber": PLUMBING,
    "drain cleaning": PLUMBING,
    "water heater": PLUMBING,
    "sewer repair": PLUMBING,
    "rooter service": PLUMBING,
    # hvac
    "hvac": HVAC,
    "hvac contractor": HVAC,
    "ac repair": HVAC,
    "air conditioning": HVAC,
    "heating repair": HVAC,
    "furnace repair": HVAC,
    # dental
    "dental": DENTAL,
    "dentist": DENTAL,
    "dental clinic": DENTAL,
    "family dentistry": DENTAL,
    "cosmetic dentistry": DENTAL,
    "pediatric dentistry": DENTAL,
    "dental implants": DENTAL,
    "emergency dental": DENTAL,
    "teeth whitening": DENTAL,
    "dental group": DENTAL,
    "orthodontist": DENTAL,
    "orthodontics": DENTAL,
    "endodontics": DENTAL,
    "periodontics": DENTAL,
    "prosthodontics": DENTAL,
    "oral surgery": DENTAL,
}

# 2-5 words, observation-based, no Re:/Fwd:, no ALL CAPS (playbook §2).
SUBJECTS: dict[str, tuple[str, ...]] = {
    PLUMBING: (
        "weekend plumbing calls",
        "the calls after 6pm",
        "who answers at 11pm",
        "missed plumbing calls",
    ),
    HVAC: (
        "your peak-season AC calls",
        "the calls after 6pm",
        "who answers at 9pm",
        "summer AC calls",
    ),
    DENTAL: (
        "your new-patient calls",
        "the calls after 5pm",
        "who answers at noon",
        "recall list follow-up",
    ),
}

# Four sentences max, one question mark, no links, no I/we opener, no banned
# phrase. Each body is a true statement about the trade, a cost, an offer of a
# read of THEIR data, and one interest CTA.
BODIES: dict[str, tuple[str, ...]] = {
    PLUMBING: (
        (
            "After-hours plumbing calls rarely leave a voicemail. "
            "They dial the next name on Google, so a burst pipe at 11pm becomes "
            "someone else's invoice. "
            "A seven day read of your own call data shows exactly how many went "
            "that way. Worth a look?"
        ),
        (
            "Voicemail is where emergency plumbing jobs quietly stop. "
            "Most callers do not wait on hold, they just redial the next plumber. "
            "A seven day read of your own call data shows how many of those you "
            "never heard about. Worth a look?"
        ),
        (
            "Saturday morning is your busiest window and the one most likely to "
            "ring out. "
            "Every call that hits voicemail on a weekend is a job another shop "
            "picks up. "
            "A seven day read of your own call data puts a number on it. "
            "Worth a look?"
        ),
    ),
    HVAC: (
        (
            "Peak-season AC calls arrive faster than any office can answer them. "
            "The ones that hit voicemail in July rarely try twice. "
            "A seven day read of your own call data shows how many booked with the "
            "shop that picked up second. Worth a look?"
        ),
        (
            "A furnace that quits at 9pm gets called twice, then someone else gets "
            "the job. "
            "Those calls never show up in your schedule, so they look like a quiet "
            "night. "
            "A seven day read of your own call data shows how many you lost "
            "without knowing. Worth a look?"
        ),
        (
            "Install quotes are easy to send and easy to lose track of. "
            "The follow-up calls happen while the crew is on a roof. "
            "A seven day read of your own call data shows where those quotes went "
            "quiet. Worth a look?"
        ),
    ),
    DENTAL: (
        (
            "New-patient calls that reach voicemail mostly do not call back, and "
            "nothing in the schedule shows it. "
            "The first appointment is the only one you get to lose. "
            "A seven day read of your own call data shows how many booked "
            "somewhere else. Worth a look?"
        ),
        (
            "The recall list keeps growing on paper while the hygiene chairs sit "
            "open. "
            "Most of those patients are not refusing, they are being reached at "
            "the wrong time. "
            "A seven day read of your own call data shows what happened after the "
            "last reminder. Worth a look?"
        ),
        (
            "An after-hours toothache is the call that decides where a family goes "
            "for years. "
            "Voicemail sends that decision to the next practice on the map. "
            "A seven day read of your own call data shows how many of those you "
            "missed. Worth a look?"
        ),
    ),
}


class UnsupportedSector(ValueError):
    """Raised when a lead's vertical is outside the three served sectors.

    Deliberately an error, not a fallback: sending a plumber's email to a
    storage unit is worse than sending nothing.
    """


def _normalise(vertical: str) -> str:
    return " ".join(str(vertical or "").lower().replace("_", " ").split())


def sector_of(vertical: str) -> str | None:
    """Map a lead's vertical to a sector, or None if it is not one of ours."""
    return VERTICAL_SECTOR.get(_normalise(vertical))


def _index(seed: str, size: int) -> int:
    """Stable pick: the same business always receives the same copy."""
    return sum(ord(c) for c in seed) % size


def draft(lead: dict) -> tuple[str, str]:
    """Return (subject, body) for this lead. Body has NO footer yet.

    Raises UnsupportedSector rather than falling back to a generic template.
    """
    sector = sector_of(lead.get("vertical", ""))
    if sector is None:
        raise UnsupportedSector(
            f"vertical {lead.get('vertical')!r} is outside "
            f"{', '.join(SECTORS)} - not drafting"
        )
    seed = str(lead.get("name") or lead.get("email") or "")
    subjects = SUBJECTS[sector]
    bodies = BODIES[sector]
    return subjects[_index(seed, len(subjects))], bodies[_index(seed + "body", len(bodies))]
