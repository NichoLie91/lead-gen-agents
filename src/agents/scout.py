"""Scout — Lead Scoring (spec section 7.4).

Deterministic 0-100 rubric (no LLM): ICP Fit (25), Intent (25), Budget (20),
Reachability (15), Timing (15), minus 20 per disqualifier that slipped through
Atlas. Tiers: HOT >= 90, WARM >= 70, NURTURE < 70 (configurable).
"""
from __future__ import annotations

from src.core.config import Settings

# Emergency/urgency keywords for Intent scoring.
INTENT_KEYWORDS = ("emergency", "24/7", "24-7", "cooling", "repair", "express")

# Vertical -> Budget points (spec scoring-rubric).
BUDGET_BY_VERTICAL = {
    "dental": 20, "hvac": 18, "plumber": 15, "electrician": 15,
    "roofer": 15, "cleaning": 15, "mechanic": 15, "auto repair": 15,
}


def _budget(vertical: str, default: int = 15) -> int:
    for key, value in BUDGET_BY_VERTICAL.items():
        if key in (vertical or "").lower():
            return value
    return default


def score_lead(lead: dict, hot: int = 90, warm: int = 70) -> dict:
    """Return {icp, intent, budget, reachability, timing, penalties, total, tier}."""
    rating = lead.get("rating") or 0
    reviews = lead.get("reviews") or 0
    vertical = lead.get("vertical", "")

    # ICP Fit (0-25)
    if rating >= 4.5 and reviews >= 20:
        icp = 25
    elif rating >= 4.0:
        icp = 20
    else:
        icp = 15

    # Intent (0-25)
    name = str(lead.get("name", "")).lower()
    intent = 25 if any(k in name for k in INTENT_KEYWORDS) else 20

    # Budget (0-20)
    budget = _budget(vertical)

    # Reachability (0-15): +5 phone, +5 email, +5 instagram
    reachability = 0
    if lead.get("phone"):
        reachability += 5
    if lead.get("email"):
        reachability += 5
    if lead.get("instagram"):
        reachability += 5

    # Timing (0-15)
    website = str(lead.get("website") or "").strip()
    status = str(lead.get("website_status") or "").lower()
    if not website:
        timing = 15
    elif "old" in status or "basic" in status:
        timing = 12
    elif "no booking" in status:
        timing = 8
    else:
        timing = 5

    # Penalties: -20 per disqualifier that slipped through (spec 7.4).
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
    }


def pick_top(
    scored: list[dict],
    target: int,
    per_vertical_cap_pct: int = 40,
) -> list[dict]:
    """Pick top-N balanced across verticals, preferring no-website first then
    fewer reviews (spec 7.4). NURTURE leads are never included."""
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
        self._llm = llm  # Scout's Gemini brain (optional; None -> no rationale)

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

    def run(self, leads: list[dict]) -> list[dict]:
        hot = int(self._settings.crit("hot_threshold", 90))
        warm = int(self._settings.crit("warm_threshold", 70))
        target = int(self._settings.crit("target_leads", 250))
        per_cap = int(self._settings.crit("per_vertical_cap_pct", 40))

        scored: list[dict] = []
        for lead in leads:
            if "_mock" in lead:  # offline mocks carry hidden flags for filters
                pass
            result = score_lead(lead, hot=hot, warm=warm)
            scored.append({**lead, "score": result})
        return pick_top(scored, target, per_cap)
