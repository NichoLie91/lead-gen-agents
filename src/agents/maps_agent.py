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


def _vertical_index(vertical: str) -> int:
    """Stable slot for any vertical. The 5 curated keys keep their original
    indices (mock names stay identical); anything else added to criteria.json
    (ac repair, orthodontist, ...) gets a deterministic slot from its
    codepoints -- never randomized hashing, so names are stable across runs."""
    keys = list(_VERTICAL_TITLES)
    if vertical in keys:
        return keys.index(vertical)
    return len(keys) + (sum(ord(c) for c in vertical) % 32)


def _business_name(metro_idx: int, vertical: str, taken: set[str]) -> str:
    title = _VERTICAL_TITLES.get(vertical, vertical.title())
    base = (metro_idx * 7 + _vertical_index(vertical) * 3)
    n = len(_NAME_PREFIXES)
    # Bounded: try each of the 20 prefixes once, then fall back to a
    # deterministic disambiguated name. The old `while True` hung forever when
    # a config with many verticals sharing a title exhausted all 20 prefixes.
    for i in range(n):
        prefix = _NAME_PREFIXES[(base + i) % n]
        name = f"{prefix} {title}"
        if name not in taken:
            taken.add(name)
            return name
    name = f"{_NAME_PREFIXES[base % n]} {title} #{base}-{sum(map(ord, vertical))}"
    taken.add(name)
    return name

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
    def __init__(self, composio: ComposioAgent, settings: Settings, llm=None,
                 state=None):
        self._composio = composio
        self._settings = settings
        self._llm = llm  # Atlas's Gemini brain (optional; None -> template queries)
        self._state = state  # StateStore for per-query yield feedback (optional)

    def _yield_feedback(self) -> str:
        """Yield feedback from previous runs ('' when state is unavailable)."""
        if self._state is None:
            return ""
        try:
            from src.query_feedback import build_yield_feedback

            return build_yield_feedback(self._state)
        except Exception:
            return ""

    def _record_yield(self, per_shape: dict[str, int]) -> None:
        """Persist this run's per-shape candidate counts (best-effort)."""
        if self._state is None or not per_shape:
            return
        try:
            from src.query_feedback import record_query_yield

            record_query_yield(self._state, per_shape)
        except Exception as exc:
            log.debug("query yield recording failed: %s", exc)

    async def _llm_query_shapes(self, feedback: str = "") -> list[str]:
        """Ask Gemini for up to 2 extra query templates for this run.

        One call per run (bounded); returns templates with {vertical}/{city}
        placeholders. Empty on failure/offline so discovery falls back to the
        configured template. When per-query yield feedback exists (Phase 3),
        it is folded into the prompt so shapes regenerate from real data.
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
        if feedback:
            prompt += f"\n\n{feedback}\nPrefer new shapes that could beat these averages."
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
        base_shape = self._settings.crit(
            "query_shape", "small {vertical} business {city} no website phone email")
        # Atlas's Gemini brain: fold in extra query shapes (bounded to the
        # first few pairs so we never explode the Maps call count). They are
        # INTERLEAVED right after the first base queries so they actually run
        # before the pool cap stops the loop (appending at the end would make
        # them dead code — the cap is hit long before positions 60+).
        # Phase 3: shapes regenerate against last runs' per-shape yield.
        shapes = await self._llm_query_shapes(feedback=self._yield_feedback())
        shape_by_query: dict[str, str] = {}
        if shapes:
            extra = []
            for _base, vertical, metro in queries[:3]:
                for shape in shapes:
                    formatted = shape.format(vertical=vertical, city=metro)
                    extra.append((formatted, vertical, metro))
                    shape_by_query[formatted] = shape
            queries = queries[:3] + extra + queries[3:]
        per_shape: dict[str, int] = {}
        for query, vertical, metro in queries:
            results = await self._composio.search_google_maps(query, start=0)
            shape_key = shape_by_query.get(query, base_shape)
            per_shape[shape_key] = per_shape.get(shape_key, 0) + len(results)
            for item in results:
                item["vertical"] = vertical
                item["city"] = metro
                pool.append(item)
            if len(pool) >= cap:
                break
        # Phase 3: real yield data feeds next run's shape regeneration.
        self._record_yield(per_shape)
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
                    "state": "TX",
                    "source_url": f"https://maps.example/mock/{_slug(name)}",
                    "source_evidence_text": f"Google Maps listing: {name} in {metro}",
                    "evidence_quality": "high",
                    "booking_signal": "unknown",
                    "emergency_signal": "unknown",
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
                    "state": "TX",
                    "source_url": f"https://maps.example/mock/{_slug(_CHAIN_FAMILY)}",
                    "source_evidence_text": f"Google Maps listing: {_CHAIN_FAMILY} in {metro}",
                    "evidence_quality": "high",
                    "booking_signal": "unknown",
                    "emergency_signal": "unknown",
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
                    "open_state": special["open_state"], "vertical": special["vertical"], "city": metro,
                    "state": "TX",
                    "source_url": f"https://maps.example/mock/{_slug(special['name'])}",
                    "source_evidence_text": f"Google Maps listing: {special['name']} in {metro}",
                    "evidence_quality": "medium",
                    "booking_signal": "unknown",
                    "emergency_signal": "unknown",
                    "_mock": True, "_closed": "closed" in special["open_state"].lower(),
                    "_chain": False, "_has_ig": False,
                })
            if len(pool) >= cap:
                return pool[:cap]
        return pool
