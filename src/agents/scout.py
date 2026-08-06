"""Scout — Lead Scoring (job doc: "lead scoring agent").

Deterministic 0-100 rubric following the doc's five categories EXACTLY:

1. ICP Fit (0-25): full points when the city is in the 12 metros, the trade is
   a core ICP trade, and the review count is in the 5-2000 small-business
   range. 3/3 -> 25, 2/3 -> 20, 1/3 -> 15, 0/3 -> 10.
2. Intent (0-25): 15 base, +5 urgency/emergency keyword in the name, +5 no
   online booking available (no website, or a site flagged "no booking"),
   +5 Instagram present (assumed not yet automated).
3. Budget (0-20): reviews/rating proxy — 50+ reviews and 4.7+ stars -> 20,
   then 16 / 12 / 8 as the profile weakens.
4. Reachability (0-15): confirmed email 12, unconfirmed email 3, confirmed
   Instagram handle +3 (cap 15).
5. Timing (0-15): no website 15, website without online booking 12,
   outdated site 10, modern/automated 6, unknown 8.

Penalties: -20 per disqualifier that slipped through Atlas (chatbot widget,
agency-built site, in-pool chain) — they stack. Tiering: HOT >= 90,
WARM >= 70, NURTURE < 70.

AUTONOMY: after the deterministic rubric, the Gemini brain can adjust the
total by a bounded -5..+5 (one line of reasoning), which lets Scout push a
lead across WARM/HOT when the facts justify it — real judgment with guardrails.
Offline (no key / dry-run) falls back to the pure rubric.
"""
from __future__ import annotations

import json

from src.core.config import Settings

# Emergency/urgency keywords for Intent scoring (doc: "24-7", "emergency", ...).
INTENT_KEYWORDS = ("emergency", "24/7", "24-7", "cooling", "repair", "express")

# Core ICP trades get full ICP points (doc 2: plumbing, HVAC, dental).
ICP_FULL_TRADES = ("plumber", "hvac", "dental")

# Bounded judgment the Gemini brain may apply to a score.
JUDGMENT_MIN = -5
JUDGMENT_MAX = 5


def _trade_is_full(vertical: str) -> bool:
    lowered = (vertical or "").lower()
    return any(t in lowered for t in ICP_FULL_TRADES)


def _reviews_in_range(reviews, lo: float = 5, hi: float = 2000) -> bool:
    try:
        return lo <= float(reviews) <= hi
    except (TypeError, ValueError):
        return False


def _icp_fit(lead: dict, metros: set[str] | None = None) -> int:
    """Doc: full points when city in the 12 metros, trade is core ICP, and
    reviews are in the 5-2000 range. Partial credit per condition met.
    City match is EXACT ("san" must not match "san antonio")."""
    metros = metros or _DEFAULT_METROS
    city = str(lead.get("city", "") or "").strip().lower()
    conditions = [
        bool(city) and any(city == m.lower() for m in metros),
        _trade_is_full(lead.get("vertical", "")),
        _reviews_in_range(lead.get("reviews")),
    ]
    met = sum(1 for c in conditions if c)
    return {3: 25, 2: 20, 1: 15}.get(met, 10)


def _intent(lead: dict) -> int:
    name = str(lead.get("name", "")).lower()
    score = 15
    if any(k in name for k in INTENT_KEYWORDS):
        score += 5
    website = str(lead.get("website") or "").strip()
    status = str(lead.get("website_status") or "").lower()
    if not website or "no booking" in status:
        score += 5  # no online booking available
    if lead.get("instagram"):
        score += 5  # IG present, assumed not yet automated
    return min(25, score)


def _budget(lead: dict) -> int:
    """Doc: reviews/rating are the proxy — 50+ reviews & 4.7+ stars -> full."""
    rating = lead.get("rating") or 0
    reviews = lead.get("reviews") or 0
    if float(rating) >= 4.7 and float(reviews) >= 50:
        return 20
    if float(rating) >= 4.4 and float(reviews) >= 20:
        return 16
    if float(rating) >= 4.0 and float(reviews) >= 5:
        return 12
    return 8


def _reachability(lead: dict) -> int:
    """Doc: confirmed email 12, unconfirmed email 3, confirmed IG +3 (cap 15)."""
    email = lead.get("email")
    status = str(lead.get("email_status") or "").upper()
    if email and status != "NEEDS_ENRICHMENT":
        score = 12
    elif email:
        score = 3
    else:
        score = 0
    if lead.get("instagram"):
        score += 3
    return min(15, score)


def _timing(lead: dict) -> int:
    """Doc: no website 15, no online booking 12, outdated 10, modern 6."""
    website = str(lead.get("website") or "").strip()
    status = str(lead.get("website_status") or "").lower()
    if not website:
        return 15
    if "no booking" in status:
        return 12
    if "old" in status or "basic" in status:
        return 10
    if "modern" in status or "automated" in status:
        return 6
    return 8


# Metro set used by the ICP check (mirrors config/criteria.json).
_DEFAULT_METROS = frozenset({
    "Houston", "Tampa", "Phoenix", "Indianapolis", "Atlanta", "Charlotte",
    "Orlando", "Denver", "San Antonio", "Las Vegas", "Nashville", "Memphis",
})


def score_lead(lead: dict, hot: int = 90, warm: int = 70,
               metros: set[str] | None = None) -> dict:
    """Return {icp, intent, budget, reachability, timing, penalties, total, tier}."""
    icp = _icp_fit(lead, metros)
    intent = _intent(lead)
    budget = _budget(lead)
    reachability = _reachability(lead)
    timing = _timing(lead)

    # Penalties: -20 per disqualifier that slipped through (doc 2).
    disqualifiers = [
        lead.get("_chatbot"),
        lead.get("_agency_built"),
        lead.get("_chain"),
    ]
    penalties = 20 * sum(1 for d in disqualifiers if d)

    total = icp + intent + budget + reachability + timing - penalties
    if total >= hot:
        tier = "HOT-VERIFIED"
    elif total >= warm:
        tier = "WARM"
    else:
        tier = "NURTURE"
    return {
        "icp": icp, "intent": intent, "budget": budget,
        "reachability": reachability, "timing": timing,
        "penalties": penalties, "total": total, "tier": tier,
        "judgment_delta": 0, "judgment_reason": "",
    }


def pick_top(
    scored: list[dict],
    target: int,
    per_vertical_cap_pct: int = 40,
) -> list[dict]:
    """Pick top-N balanced across verticals, preferring no-website first then
    fewer reviews (doc 2). NURTURE leads are never included."""
    eligible = [l for l in scored if l.get("score", {}).get("tier") != "NURTURE"]
    eligible.sort(key=lambda l: (-l["score"]["total"], l.get("reviews") or 0))

    cap_per_vertical = max(1, int(target * per_vertical_cap_pct / 100))
    counts: dict[str, int] = {}
    picked: list[dict] = []
    for lead in eligible:
        vertical = lead.get("vertical", "")
        if counts.get(vertical, 0) >= cap_per_vertical:
            continue
        picked.append(lead)
        counts[vertical] = counts.get(vertical, 0) + 1
        if len(picked) >= target:
            break
    return picked


class Scout:
    def __init__(self, settings: Settings, llm=None):
        self._settings = settings
        self._llm = llm  # Scout's Gemini brain (optional; None -> no judgment)

    async def explain(self, lead: dict, score: dict) -> str:
        """One-line Gemini rationale for a lead's score/tier (bounded by the
        caller to top leads). Empty when offline -> scores stay pure rubric."""
        if not self._llm or not self._llm.available:
            return ""
        prompt = (
            "You are Scout, the lead-scoring agent. Explain in ONE short line "
            "(under 160 chars) why this local business lead got this score. "
            "Use only the facts given; never invent.\n\n"
            f"Lead: {lead.get('name')} ({lead.get('vertical')}, {lead.get('city')}) | "
            f"rating {lead.get('rating')} | {lead.get('reviews')} reviews | "
            f"phone: {'yes' if lead.get('phone') else 'no'} | "
            f"email: {'yes' if lead.get('email') else 'no'} | "
            f"website: {'yes' if lead.get('website') else 'no'} | "
            f"instagram: {'yes' if lead.get('instagram') else 'no'}\n"
            f"Categories: ICP {score.get('icp')}/25, Intent {score.get('intent')}/25, "
            f"Budget {score.get('budget')}/20, Reachability {score.get('reachability')}/15, "
            f"Timing {score.get('timing')}/15, penalties -{score.get('penalties', 0)}\n"
            f"Total {score.get('total')} -> tier {score.get('tier')}"
        )
        text = await self._llm.complete(prompt)
        return (text or "").strip()[:160]

    async def _judge(self, lead: dict, score: dict) -> dict:
        """Gemini judgment overlay: propose a bounded -5..+5 total adjustment
        with a one-line reason. Returns {} on any failure (offline, bad JSON,
        out-of-range) so the deterministic rubric always stands."""
        if not self._llm or not self._llm.available:
            return {}
        prompt = (
            "You are Scout, a lead-scoring agent with judgment. The rubric "
            "scored this local business lead. Review the facts for signals the "
            "rubric misses (recent urgent need, obvious tech pain, big-opportunity "
            "profile). Reply STRICT JSON only:\n"
            '{"delta": int in [-5,5], "reason": "under 140 chars"}\n'
            "Rules: delta 0 unless the facts truly justify it. Never invent "
            "facts. Facts:\n"
            f"name {lead.get('name')} | {lead.get('vertical')} in {lead.get('city')} | "
            f"rating {lead.get('rating')} | {lead.get('reviews')} reviews | "
            f"website {'none' if not lead.get('website') else 'yes'} | "
            f"instagram {'yes' if lead.get('instagram') else 'no'} | "
            f"email {'yes' if lead.get('email') else 'no'}\n"
            f"Rubric: ICP {score.get('icp')}/25 Intent {score.get('intent')}/25 "
            f"Budget {score.get('budget')}/20 Reach {score.get('reachability')}/15 "
            f"Timing {score.get('timing')}/15 -> total {score.get('total')} "
            f"tier {score.get('tier')}"
        )
        raw = await self._llm.complete(prompt)
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end == -1:
            return {}
        try:
            data = json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            return {}
        try:
            delta = int(data.get("delta", 0))
        except (TypeError, ValueError):
            return {}
        if not (JUDGMENT_MIN <= delta <= JUDGMENT_MAX):
            return {}
        reason = str(data.get("reason", "")).strip()[:140]
        return {"delta": delta, "reason": reason}

    def run(self, leads: list[dict]) -> list[dict]:
        hot = int(self._settings.crit("hot_threshold", 90))
        warm = int(self._settings.crit("warm_threshold", 70))
        target = int(self._settings.crit("target_leads", 250))
        per_cap = int(self._settings.crit("per_vertical_cap_pct", 40))
        metros = set(self._settings.crit("metros", [])) if self._settings.crit("metros", []) else None

        scored: list[dict] = []
        for lead in leads:
            result = score_lead(lead, hot=hot, warm=warm, metros=metros)
            scored.append({**lead, "score": result})
        scored.sort(key=lambda l: -l["score"]["total"])
        return pick_top(scored, target, per_cap)

    async def apply_judgment(self, scored: list[dict], n: int = 15) -> None:
        """AUTONOMY: Gemini reviews the top-N candidates and may adjust each
        total by a bounded -5..+5 with a one-line reason. Guardrailed: the
        adjustment is clamped, parsed defensively, and never invents facts.
        Mutates ``score`` in place; deterministic rubric stays authoritative.
        Calls are concurrency-limited (5) to keep latency sane."""
        if not self._llm or not self._llm.available:
            return
        import asyncio

        hot = int(self._settings.crit("hot_threshold", 90))
        warm = int(self._settings.crit("warm_threshold", 70))
        sem = asyncio.Semaphore(5)

        async def _judge_one(lead: dict) -> tuple[dict, dict]:
            async with sem:
                return lead, await self._judge(lead, lead["score"])

        results = await asyncio.gather(*(_judge_one(l) for l in scored[:n]))
        for lead, adj in results:
            if not adj:
                continue
            score = lead["score"]
            score["total"] = max(0, score["total"] + adj["delta"])
            score["judgment_delta"] = adj["delta"]
            score["judgment_reason"] = adj["reason"]
            if score["total"] >= hot:
                score["tier"] = "HOT-VERIFIED"
            elif score["total"] >= warm:
                score["tier"] = "WARM"
            else:
                score["tier"] = "NURTURE"
