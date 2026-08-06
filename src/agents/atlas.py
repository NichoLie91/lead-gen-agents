"""Atlas — Lead Discovery (spec section 7.2).

Turns the raw Google Maps pool into a clean candidate list by applying the
Stage-1 exclusion filters. Never contacts leads.
"""
from __future__ import annotations

import re
from collections import Counter

from src.agents.maps_agent import MapsAgent
from src.core.config import Settings

# Chain-name regex from the scoring rubric (case-insensitive substring match).
CHAIN_PATTERNS = [
    r"parker & sons", r"roto-rooter", r"morris-jenkins", r"rs andrews",
    r"michael & son", r"horne heating", r"moncrief", r"andy lewis",
    r"hope plumbing", r"everydayplumber", r"wyman plumbing", r"red cap plumbing",
    r"benjamin franklin", r"mr\. rooter", r"ben franklin plumbing",
    r"drain cleaning", r"american leak detection",
]

# AI chatbot widget signatures scanned in homepage HTML (spec 7.2).
CHATBOT_SIGNATURES = ["intercom", "drift", "tidio", "voiceflow", "chat-widget", "crisp.chat"]

_CLOSED_MARKERS = ("permanently closed", "closed")


def is_chain(name: str) -> bool:
    lowered = (name or "").lower()
    return any(re.search(p, lowered) for p in CHAIN_PATTERNS)


def is_closed(open_state: str) -> bool:
    return any(m in (open_state or "").lower() for m in _CLOSED_MARKERS)


def has_any_contact(lead: dict) -> bool:
    """Spec 7.2: discard when no phone AND no email AND no IG."""
    return bool(lead.get("phone") or lead.get("email") or lead.get("instagram"))


def passes_target_gate(lead: dict, rating_min: float = 4.4,
                       reviews_min: float = 5, reviews_max: float = 2000) -> bool:
    """Job doc 1: target businesses are 4.4-5 stars with an established review
    count. Leads missing rating/review data fail the gate (can't confirm
    quality); malformed values fail closed."""
    rating = lead.get("rating")
    reviews = lead.get("reviews")
    if rating is None or reviews is None:
        return False
    try:
        return (rating_min <= float(rating) <= 5.0
                and reviews_min <= float(reviews) <= reviews_max)
    except (TypeError, ValueError):
        return False


def has_chatbot(html: str) -> bool:
    lowered = (html or "").lower()
    return any(sig in lowered for sig in CHATBOT_SIGNATURES)


def dedupe(leads: list[dict]) -> list[dict]:
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for lead in leads:
        key = (str(lead.get("name", "")).lower(), str(lead.get("address", "")).lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(lead)
    return out


def flag_in_pool_chains(leads: list[dict]) -> set[str]:
    """Same business name appearing 3+ times in the pool -> chain names."""
    counts = Counter(str(l.get("name", "")).lower() for l in leads)
    return {name for name, count in counts.items() if count >= 3}


class Atlas:
    def __init__(self, maps: MapsAgent, settings: Settings):
        self._maps = maps
        self._settings = settings

    async def run(self) -> list[dict]:
        pool = await self._maps.discover()
        pool = dedupe(pool)
        chain_names = flag_in_pool_chains(pool)

        # Target profile gates (job doc: 4.4-5 stars, established local business).
        rating_min = float(self._settings.crit("rating_min", 4.4))
        reviews_min = float(self._settings.crit("reviews_min", 5))
        reviews_max = float(self._settings.crit("reviews_max", 2000))

        clean: list[dict] = []
        for lead in pool:
            if is_closed(lead.get("open_state", "")):
                continue
            if is_chain(lead.get("name", "")):
                continue
            if lead.get("name", "").lower() in chain_names:
                continue
            if not has_any_contact(lead):
                continue
            if not passes_target_gate(lead, rating_min, reviews_min, reviews_max):
                continue
            clean.append(lead)

        cap = int(self._settings.crit("raw_pool_cap", 250))
        return clean[:cap]
