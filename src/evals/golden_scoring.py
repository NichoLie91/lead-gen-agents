"""Golden scoring-rationale cases (Phase 2): Scout must be *explainable*.

Each case pins the deterministic rubric's total, tier, category breakdown,
AND the rationale text (scout.scoring_rationale) that must be producible.
The Gemini judgment overlay is out of scope (bounded ±5, separately tested);
these pin the deterministic floor every scored lead must be able to explain.

Numbers below are hand-computed against src/agents/scout.py:
  icp: 3/3 conditions -> 25, 2/3 -> 20, 1/3 -> 15, 0 -> 10
       (in-metro city EXACT match, core trade, reviews 5-2000)
  intent: 15 base, +5 urgency kw, +5 no-website/no-booking, +5 IG (cap 25)
  budget: 4.7+ & 50+ -> 20; 4.4+ & 20+ -> 16; 4.0+ & 5+ -> 12; else 8
  reachability: verified email 12 (unverified 3), +3 IG, cap 15
  timing: no site 15, no-booking 12, old/basic 10, modern/automated 6, unk 8
  penalties: -20 per disqualifier (_chatbot/_agency_built/_chain)
"""
from __future__ import annotations


def _lead(**over) -> dict:
    base = {
        "name": "Mock Biz", "address": "1 Main St", "vertical": "plumber",
        "city": "Houston", "rating": 4.8, "reviews": 100, "website": "",
        "instagram": "", "email": None, "email_status": "NEEDS_ENRICHMENT",
        "_chatbot": False, "_agency_built": False, "_chain": False,
    }
    base.update(over)
    return base


# (case_id, lead, expected) — missing keys are not asserted.
GOLDEN_SCORING = [
    # 1. No-website plumber, core metro: 25+20+20+0+15 = 80 -> WARM.
    ("hot-no-website-plumber", _lead(), {
        "total": 80, "tier": "WARM",
        "icp": 25, "intent": 20, "budget": 20, "reachability": 0, "timing": 15,
        "penalties": 0,
        "rationale_must_include": [
            "ICP 25/25: 3/3 conditions met",
            "Intent 20/25: no urgency keyword",
            "Budget 20/20: 4.8 stars + 100 reviews",
            "Reachability 0/15: no email, no instagram",
            "Timing 15/15: no website at all",
            "Penalties 0: none",
            "Tier WARM: total 80",
        ],
    }),
    # 2. Same + verified email: reachability 12 -> 92 >= 90 -> HOT-VERIFIED.
    ("hot-with-verified-email", _lead(email="info@mock.example",
                                      email_status="VERIFIED"), {
        "total": 92, "tier": "HOT-VERIFIED", "reachability": 12,
        "rationale_must_include": [
            "Reachability 12/15: verified email, no instagram",
        ],
    }),
    # 3. Modern site + IG + verified email:
    #    25 + (15+5 IG, no no-website bonus) 20 + 20 + 15 + 6 = 86 -> WARM.
    ("warm-modern-site-with-ig", _lead(website="https://mock.example",
                                       website_status="modern",
                                       instagram="mockbiz",
                                       email="info@mock.example",
                                       email_status="VERIFIED"), {
        "total": 86, "tier": "WARM",
        "icp": 25, "intent": 20, "budget": 20, "reachability": 15, "timing": 6,
        "rationale_must_include": [
            "Intent 20/25: no urgency keyword, has online booking signal, instagram present",
            "Reachability 15/15: verified email + instagram",
            "Timing 6/15: modern site with online booking",
        ],
    }),
    # 4. Out-of-metro city: icp 20 -> 20+20+20+0+15 = 75 WARM.
    ("warm-out-of-metro", _lead(city="Smalltown"), {
        "total": 75, "tier": "WARM", "icp": 20,
        "rationale_must_include": [
            "ICP 20/25: 2/3 conditions met",
            "city Smalltown NOT in metro set",
        ],
    }),
    # 5. Non-core trade (cleaning): icp 20 -> 75 WARM.
    ("warm-non-core-trade", _lead(vertical="cleaning"), {
        "total": 75, "tier": "WARM", "icp": 20,
        "rationale_must_include": ["trade cleaning not core ICP"],
    }),
    # 6. Emergency keyword: intent 25 -> 25+25+20+0+15 = 85 WARM.
    ("hot-emergency-keyword", _lead(name="Emergency Plumbing Co"), {
        "total": 85, "tier": "WARM", "intent": 25,
        "rationale_must_include": [
            "Intent 25/25: urgency keyword 'emergency' in name",
        ],
    }),
    # 7. Chatbot disqualifier: 80-20 = 60 NURTURE.
    ("warm-chatbot-penalty", _lead(_chatbot=True), {
        "total": 60, "tier": "NURTURE", "penalties": 20,
        "rationale_must_include": [
            "Penalties -20: chatbot",
        ],
    }),
    # 8. All three disqualifiers: 80-60 = 20 NURTURE.
    ("nurture-all-disqualifiers", _lead(_chatbot=True, _agency_built=True,
                                        _chain=True), {
        "total": 20, "tier": "NURTURE", "penalties": 60,
        "rationale_must_include": [
            "Penalties -60: chatbot + agency-built site + in-pool chain",
            "Tier NURTURE: total 20",
        ],
    }),
    # 9. 4.5 stars / 25 reviews -> budget 16: 25+20+16+0+15 = 76 WARM.
    ("warm-weak-budget", _lead(rating=4.5, reviews=25), {
        "total": 76, "tier": "WARM", "budget": 16,
        "rationale_must_include": ["Budget 16/20: moderate profile"],
    }),
    # 10. 3.9 stars / 10 reviews -> budget 8: 25+20+8+0+15 = 68 NURTURE.
    ("nurture-low-rating", _lead(rating=3.9, reviews=10), {
        "total": 68, "tier": "NURTURE", "budget": 8,
        "rationale_must_include": ["Budget 8/20: weak reviews/rating profile"],
    }),
    # 11. Missing rating+reviews: city+trade still met -> icp 20 (2/3),
    #     budget 8 -> 20+20+8+0+15 = 63 NURTURE.
    ("nurture-missing-rating-data", _lead(rating=None, reviews=None), {
        "total": 63, "tier": "NURTURE", "icp": 20, "budget": 8,
        "rationale_must_include": [
            "ICP 20/25: 2/3 conditions met",
            "reviews missing (counts against ICP)",
        ],
    }),
    # 12. 2500 reviews (>2000): icp 20 -> 75 WARM.
    ("warm-too-many-reviews", _lead(reviews=2500), {
        "total": 75, "tier": "WARM", "icp": 20,
        "rationale_must_include": ["reviews 2500 NOT in range 5-2000"],
    }),
    # 13. Website with unknown age/booking signal: intent 15 (no no-website
    #     bonus), timing 8 -> 25+15+20+0+8 = 68 NURTURE.
    ("nurture-website-unknown-age", _lead(website="https://mock.example"), {
        "total": 68, "tier": "NURTURE", "timing": 8, "intent": 15,
        "rationale_must_include": ["Timing 8/15: website of unknown age"],
    }),
    # 14. Dental + verified email + IG, no site:
    #     25 + 25 + 20 + 15 + 15 = 100 HOT.
    ("hot-dental-full-contact", _lead(vertical="dental",
                                      email="front@mock.example",
                                      email_status="VERIFIED",
                                      instagram="mockdental"), {
        "total": 100, "tier": "HOT-VERIFIED", "icp": 25, "intent": 25,
        "reachability": 15, "timing": 15, "budget": 20,
        "rationale_must_include": [
            "Reachability 15/15: verified email + instagram",
        ],
    }),
    # 15. Present-but-unconfirmed email (status NEEDS_ENRICHMENT):
    #     reachability 3 -> 25+20+20+3+15 = 83 WARM.
    ("warm-unverified-email", _lead(email="info@mock.example"), {
        "total": 83, "tier": "WARM", "reachability": 3,
        "rationale_must_include": [
            "Reachability 3/15: unverified email only",
        ],
    }),
]

SCORING_RUBRIC = (
    "A scored lead must be explainable: the rationale must name the ICP, "
    "Intent, Budget, Reachability and Timing contributions, any penalties, "
    "and end with the tier decision. Scores must match the deterministic "
    "rubric exactly — no hand-waving."
)
