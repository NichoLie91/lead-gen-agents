"""Maps Agent — owns every Google Maps discovery call (spec section 3, 7.2).

Offline mode (no Composio key / dry-run) returns a deterministic mock
candidate pool so the pipeline can be exercised end-to-end without network.
"""
from __future__ import annotations

import logging

from src.agents.composio_agent import ComposioAgent
from src.core.config import Settings

log = logging.getLogger(__name__)

# Business name generator for offline testing. Names are deterministically
# generated so every (metro, vertical) pair gets its OWN business name — a
# single name must never repeat across metros, because that would legitimately
# trip the in-pool chain detector (spec 7.2) and zero out the whole pool.
_NAME_PREFIXES = [
    "Anderson", "Blue Creek", "Harvest", "Summit", "Northstar", "Bright",
    "Fresh Start", "City", "Liberty", "Golden", "Apex", "Reliable",
    "Prime", "True", "Crown", "Legacy", "Iron", "Clear", "Value", "Cornerstone",
]
_VERTICAL_TITLES = {
    "plumber": "Plumbing", "hvac": "Cooling & Heating", "cleaning": "Cleaning",
    "mechanic": "Auto Repair", "dental": "Dental",
}


def _business_name(metro_idx: int, vertical: str, taken: set[str]) -> str:
    title = _VERTICAL_TITLES.get(vertical, vertical.title())
    base = (metro_idx * 7 + list(_VERTICAL_TITLES).index(vertical) * 3)
    i = 0
    while True:
        prefix = _NAME_PREFIXES[(base + i) % len(_NAME_PREFIXES)]
        name = f"{prefix} {title}"
        if name not in taken:
            taken.add(name)
            return name
        i += 1

# Special cases that exercise the exclusion filters (only injected once, so
# they never trip the in-pool chain rule themselves).
_CHAIN_FAMILY = "Dental Smiles"   # same name in the first 3 metros -> in-pool chain
_SPECIAL_CASES = {
    "Houston": [  # plumber specials, only in Houston
        {"name": "Roto-Rooter Houston", "vertical": "plumber", "open_state": "Open"},
        {"name": "Permanently Closed Plumbing", "vertical": "plumber", "open_state": "Permanently closed"},
    ],
    "Tampa": [   # no-contact special, only in Tampa
        {"name": "NoContact Service Co", "vertical": "cleaning", "open_state": "Open", "no_contact": True},
    ],
}


def _deterministic(seed: str, lo: int, hi: int) -> int:
    import hashlib

    digest = int(hashlib.sha256(seed.encode()).hexdigest(), 16)
    return lo + digest % (hi - lo + 1)


def _slug(name: str) -> str:
    return "".join(c for c in name.lower() if c.isalnum())[:24]


class MapsAgent:
    def __init__(self, composio: ComposioAgent, settings: Settings, llm=None):
        self._composio = composio
        self._settings = settings
        self._llm = llm  # Atlas's Gemini brain (optional; None -> template queries)

    async def _llm_query_shapes(self) -> list[str]:
        """Ask Gemini for up to 2 extra query templates for this run.

        One call per run (bounded); returns templates with {vertical}/{city}
        placeholders. Empty on failure/offline so discovery falls back to the
        configured template.
        """
        if not self._llm or not self._llm.available:
            return []
        import json

        prompt = (
            "You are Atlas, the lead-discovery agent of a local business lead "
            "generation system. Suggest 2 extra Google Maps search query "
            "templates for finding small local businesses likely to need "
            "AI/automation help (e.g. missed calls, manual follow-up). Use "
            "{vertical} and {city} as placeholders. Reply with STRICT JSON only: "
            "a JSON array of strings, e.g. [\"{vertical} near {city} that miss "
            "calls\", ...]."
        )
        raw = await self._llm.complete(prompt)
        start, depth = raw.find("["), 0
        end = -1
        if start != -1:
            for i in range(start, len(raw)):
                if raw[i] == "[":
                    depth += 1
                elif raw[i] == "]":
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
        if end == -1:
            return []
        try:
            shapes = json.loads(raw[start:end])
        except json.JSONDecodeError:
            return []
        return [s for s in shapes if isinstance(s, str)
                and "{vertical}" in s and "{city}" in s][:2]

    def build_queries(self, verticals: list[str], metros: list[str]) -> list[tuple[str, str, str]]:
        """Return [(query, vertical, metro)] for every (vertical x metro) pair."""
        shape = self._settings.crit("query_shape", "small {vertical} business {city} no website phone email")
        return [
            (shape.format(vertical=vertical, city=metro), vertical, metro)
            for vertical in verticals for metro in metros
        ]

    async def discover(
        self,
        verticals: list[str] | None = None,
        metros: list[str] | None = None,
        raw_pool_cap: int | None = None,
    ) -> list[dict]:
        verticals = verticals or self._settings.verticals
        metros = metros or self._settings.metros
        cap = raw_pool_cap or int(self._settings.crit("raw_pool_cap", 250))

        if not self._composio.connected or self._settings.dry_run:
            return self._mock_discover(verticals, metros, cap)

        pool: list[dict] = []
        queries = self.build_queries(verticals, metros)
        # Atlas's Gemini brain: fold in extra query shapes (bounded to the
        # first few pairs so we never explode the Maps call count). They are
        # INTERLEAVED right after the first base queries so they actually run
        # before the pool cap stops the loop (appending at the end would make
        # them dead code — the cap is hit long before positions 60+).
        shapes = await self._llm_query_shapes()
        if shapes:
            extra = []
            for _base, vertical, metro in queries[:3]:
                for shape in shapes:
                    extra.append((shape.format(vertical=vertical, city=metro),
                                  vertical, metro))
            queries = queries[:3] + extra + queries[3:]
        for query, vertical, metro in queries:
            results = await self._composio.search_google_maps(query, start=0)
            for item in results:
                item["vertical"] = vertical
                item["city"] = metro
                pool.append(item)
            if len(pool) >= cap:
                break
        return pool[:cap]

    # ---------- offline mock ----------
    def _mock_discover(self, verticals: list[str], metros: list[str], cap: int) -> list[dict]:
        pool: list[dict] = []
        seen: set[tuple[str, str]] = set()

        def add(lead: dict) -> bool:
            key = (str(lead.get("name", "")).lower(), str(lead.get("address", "")).lower())
            if key in seen:
                return False
            seen.add(key)
            pool.append(lead)
            return True

        taken: set[str] = set()
        for metro_idx, metro in enumerate(metros):
            for vertical in verticals:
                name = _business_name(metro_idx, vertical, taken)
                seed = f"{name}|{metro}"
                has_ig = _deterministic(seed, 0, 1) == 1
                add({
                    "name": name,
                    "address": f"{_deterministic(seed, 1, 9999)} Mock Rd, {metro}",
                    "phone": f"{_deterministic(seed, 200, 999)}-555-{_deterministic(seed, 1000, 9999)}",
                    "website": "" if _deterministic(seed, 0, 2) == 0 else f"https://{_slug(name)}.example",
                    "rating": round(_deterministic(seed, 44, 50) / 10, 1),  # 4.4-5.0
                    "reviews": _deterministic(seed, 10, 250),
                    "open_state": "Open",
                    "vertical": vertical,
                    "city": metro,
                    "_mock": True, "_closed": False, "_chain": False, "_has_ig": has_ig,
                })

            # In-pool chain family: the same name appears in the first 3 metros.
            if metros.index(metro) < 3 and "dental" in verticals:
                add({
                    "name": _CHAIN_FAMILY,
                    "address": f"{_deterministic(_CHAIN_FAMILY + metro, 1, 9999)} Mock Rd, {metro}",
                    "phone": f"{_deterministic(metro, 200, 999)}-555-{_deterministic(metro, 1000, 9999)}",
                    "website": "", "rating": 4.8, "reviews": 250,
                    "open_state": "Open", "vertical": "dental", "city": metro,
                    "_mock": True, "_closed": False, "_chain": False, "_has_ig": True,
                })

            # One-off special cases (chain regex / closed / no contact).
            for special in _SPECIAL_CASES.get(metro, []):
                if special["vertical"] not in verticals:
                    continue
                add({
                    "name": special["name"],
                    "address": f"{_deterministic(special['name'], 1, 9999)} Mock Rd, {metro}",
                    "phone": "" if special.get("no_contact") else f"{_deterministic(special['name'], 200, 999)}-555-1000",
                    "website": "", "rating": 4.5, "reviews": 60,
                    "open_state": special["open_state"], "vertical": special["vertical"],
                    "city": metro,
                    "_mock": True, "_closed": "closed" in special["open_state"].lower(),
                    "_chain": False, "_has_ig": False,
                })
            if len(pool) >= cap:
                return pool[:cap]
        return pool
