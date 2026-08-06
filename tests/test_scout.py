"""Scout scoring tests — worked examples from the scoring rubric docs.

The rubric now follows the "lead scoring agent" job doc EXACTLY:
ICP = city/trade/review-range, Intent = urgency+booking+IG signals,
Budget = reviews/rating proxy, Reachability = email 12 / IG 3,
Timing = website-state buckets. Judgment overlay is bounded -5..+5.
"""

import asyncio

from src.agents.scout import Scout, pick_top, score_lead


def _lead(**overrides):
    base = {
        "name": "Test Business", "rating": 4.5, "reviews": 50,
        "vertical": "plumber", "phone": "555-0100", "website": "",
        "email": "x@example.com", "instagram": "handle", "city": "Houston",
    }
    base.update(overrides)
    return base


def test_worked_example_warm_plumber():
    """Lifetime Plumbing, Houston, 4.9*/99, basic Wix site, no IG -> Warm."""
    lead = _lead(name="Lifetime Plumbing", rating=4.9, reviews=99,
                 website="https://lifetime.example", website_status="old/basic",
                 email="x@example.com", instagram="")
    result = score_lead(lead)
    assert result["icp"] == 25          # Houston + plumber + 99 reviews in range
    assert result["intent"] == 15       # no urgency keyword, site exists, no IG
    assert result["budget"] == 20       # 50+ reviews & 4.7+ stars -> full proxy
    assert result["reachability"] == 12  # confirmed email, no IG
    assert result["timing"] == 10       # outdated/basic website
    assert result["total"] == 82
    assert result["tier"] == "WARM"


def test_worked_example_hot_dental():
    """Emergency Dentist, 5*/85, website w/o booking, IG, no email -> Warm."""
    lead = _lead(name="Emergency Dentist", rating=5.0, reviews=85, vertical="dental",
                 website="https://dentist.example", website_status="no booking",
                 instagram="dentistig", email="")
    result = score_lead(lead)
    assert result["icp"] == 25          # Houston + dental + 85 reviews
    assert result["intent"] == 25       # "emergency" + no online booking + IG
    assert result["budget"] == 20       # 5.0*/85 -> full proxy
    assert result["reachability"] == 3  # no email, confirmed IG only
    assert result["timing"] == 12       # website but no online booking
    assert result["total"] == 85
    assert result["tier"] == "WARM"     # 85 < 90 (doc threshold)


def test_penalty_per_disqualifier():
    lead = _lead(_chain=True)
    result = score_lead(lead)
    assert result["penalties"] == 20
    lead2 = _lead(_chain=True, _chatbot=True)
    result2 = score_lead(lead2)
    assert result2["penalties"] == 40


def test_nurture_discarded():
    # Low rating + no contact channels + outside review range -> NURTURE.
    lead = _lead(rating=3.5, reviews=3, phone="", email="", instagram="", city="")
    result = score_lead(lead)
    assert result["tier"] == "NURTURE"
    assert result["total"] < 70


def test_budget_is_reviews_rating_proxy():
    assert score_lead(_lead(rating=4.8, reviews=60))["budget"] == 20
    assert score_lead(_lead(rating=4.5, reviews=30))["budget"] == 16
    assert score_lead(_lead(rating=4.2, reviews=10))["budget"] == 12
    assert score_lead(_lead(rating=3.9, reviews=2))["budget"] == 8


def test_reachability_confirmed_vs_unconfirmed():
    confirmed = _lead(email="x@example.com", instagram="")
    assert score_lead(confirmed)["reachability"] == 12
    unconfirmed = _lead(email="x@example.com", email_status="NEEDS_ENRICHMENT",
                        instagram="")
    assert score_lead(unconfirmed)["reachability"] == 3
    with_ig = _lead(email="x@example.com", instagram="ig")
    assert score_lead(with_ig)["reachability"] == 15


def test_timing_website_state_buckets():
    assert score_lead(_lead(website=""))["timing"] == 15            # no website
    assert score_lead(_lead(website="https://x.example",
                            website_status="no booking"))["timing"] == 12
    assert score_lead(_lead(website="https://x.example",
                            website_status="old/basic"))["timing"] == 10
    assert score_lead(_lead(website="https://x.example",
                            website_status="modern/automated"))["timing"] == 6
    assert score_lead(_lead(website="https://x.example",
                            website_status=""))["timing"] == 8       # unknown


def test_icp_partial_credit():
    # City outside the 12 metros -> 2/3 conditions -> 20.
    out_of_area = _lead(city="Boise")
    assert score_lead(out_of_area)["icp"] == 20
    # Review count outside the 5-2000 range -> 2/3 -> 20.
    bad_reviews = _lead(reviews=5000)
    assert score_lead(bad_reviews)["icp"] == 20
    # Non-ICP trade + all else right -> 2/3 -> 20.
    odd_trade = _lead(vertical="bakery")
    assert score_lead(odd_trade)["icp"] == 20


def test_icp_city_match_is_exact():
    # "san" must NOT match "san antonio" (substring false-positive).
    assert score_lead(_lead(city="San Diego"))["icp"] == 20
    assert score_lead(_lead(city="Las Vegas"))["icp"] == 25
    assert score_lead(_lead(city="san antonio"))["icp"] == 25  # case-insensitive exact


def test_pick_top_balance_and_excludes_nurture():
    leads = []
    for i in range(30):
        leads.append({**_lead(name=f"B{i}", vertical="plumber" if i % 2 else "hvac"),
                      "score": score_lead(_lead(name=f"B{i}", vertical="plumber" if i % 2 else "hvac"))})
    leads.append({"name": "NurtureOne", "score": score_lead(
        _lead(name="NurtureOne", rating=3.0, phone="", email="", instagram="", city=""))})
    picked = pick_top(leads, target=10, per_vertical_cap_pct=50)
    assert len(picked) == 10
    names = {l["name"] for l in picked}
    assert "NurtureOne" not in names
    from collections import Counter
    verticals = Counter(l["vertical"] for l in picked)
    assert max(verticals.values()) <= 5  # 50% of 10


# ---------- judgment overlay (autonomy) ----------

class _FakeSettings:
    dry_run = False

    def crit(self, key, default=None):
        return default


class _FakeLLM:
    available = True

    def __init__(self, reply):
        self._reply = reply

    async def complete(self, prompt):
        return self._reply


def _scored(name="Lead A", **overrides):
    lead = _lead(name=name, **overrides)
    return {**lead, "score": score_lead(lead)}


def test_judgment_pushes_warm_to_hot_within_bounds():
    scout = Scout(_FakeSettings(), llm=_FakeLLM(
        '{"delta": 5, "reason": "24/7 listed and phones go unanswered after hours"}'))
    # 85 -> WARM deterministically; +5 judgment -> 90 -> HOT-VERIFIED.
    scored = [_scored("Emergency Dentist", rating=5.0, reviews=85, vertical="dental",
                      website="https://x.example", website_status="no booking",
                      instagram="ig", email="")]
    assert scored[0]["score"]["tier"] == "WARM"
    asyncio.run(scout.apply_judgment(scored, n=1))
    assert scored[0]["score"]["total"] == 90
    assert scored[0]["score"]["judgment_delta"] == 5
    assert scored[0]["score"]["tier"] == "HOT-VERIFIED"


def test_judgment_ignores_out_of_range_and_bad_json():
    scout = Scout(_FakeSettings(), llm=_FakeLLM('{"delta": 99, "reason": "nope"}'))
    scored = [_scored()]
    before = scored[0]["score"]["total"]
    asyncio.run(scout.apply_judgment(scored, n=1))
    assert scored[0]["score"]["total"] == before      # out-of-range delta ignored
    scout2 = Scout(_FakeSettings(), llm=_FakeLLM("not json at all"))
    asyncio.run(scout2.apply_judgment(scored, n=1))
    assert scored[0]["score"]["total"] == before      # bad JSON ignored


def test_judgment_skipped_without_llm():
    scout = Scout(_FakeSettings(), llm=None)
    scored = [_scored()]
    before = scored[0]["score"]["total"]
    asyncio.run(scout.apply_judgment(scored, n=1))
    assert scored[0]["score"]["total"] == before
