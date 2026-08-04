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
            # Chatbot/agency checks need homepage HTML; in offline mode mocks
            # mark _mock so we skip network. Online: fetch URL when present.
            if not lead.get("_mock") and lead.get("website"):
                # Deferred to enrichment to avoid blocking discovery on network.
                pass
            clean.append(lead)

        cap = int(self._settings.crit("raw_pool_cap", 250))
        return clean[:cap]
