"""Scout scoring tests — worked examples from the scoring rubric docs."""

from src.agents.scout import pick_top, score_lead


def _lead(**overrides):
    base = {
        "name": "Test Business", "rating": 4.5, "reviews": 50,
        "vertical": "plumber", "phone": "555-0100", "website": "",
        "email": "x@example.com", "instagram": "handle",
    }
    base.update(overrides)
    return base


def test_worked_example_warm_plumber():
    """Lifetime Plumbing, Houston, 4.9*/99, basic Wix site, no IG -> Warm."""
    lead = _lead(name="Lifetime Plumbing", rating=4.9, reviews=99,
                 website="https://lifetime.example", website_status="old/basic",
                 email="x@example.com", instagram="")
    result = score_lead(lead)
    assert result["icp"] == 25          # rating >= 4.5 and reviews >= 20
    assert result["intent"] == 20       # no emergency keyword
    assert result["budget"] == 15       # plumber
    assert result["reachability"] == 10  # phone + email, no IG
    assert result["timing"] == 12       # old/basic website
    assert result["total"] == 82
    assert result["tier"] == "WARM"


def test_worked_example_hot_dental():
    """Emergency Dentist, 5*/85, website, IG, no email -> Hot."""
    lead = _lead(name="Emergency Dentist", rating=5.0, reviews=85, vertical="dental",
                 website="https://dentist.example", website_status="no booking",
                 instagram="dentistig", email="")
    result = score_lead(lead)
    assert result["icp"] == 25
    assert result["intent"] == 25       # "emergency" in name
    assert result["budget"] == 20       # dental
    assert result["reachability"] == 10  # phone + IG, no email
    assert result["timing"] == 8        # no booking widget
    assert result["total"] == 88
    assert result["tier"] == "WARM"     # 88 < 90 (user doc threshold)


def test_penalty_per_disqualifier():
    lead = _lead(_chain=True)
    result = score_lead(lead)
    assert result["penalties"] == 20
    lead2 = _lead(_chain=True, _chatbot=True)
    result2 = score_lead(lead2)
    assert result2["penalties"] == 40


def test_nurture_discarded():
    # Low rating + no contact channels at all -> max 15+20+15+0+15 = 65 -> NURTURE.
    lead = _lead(rating=3.5, reviews=3, phone="", email="", instagram="")
    result = score_lead(lead)
    assert result["tier"] == "NURTURE"
    assert result["total"] < 70


def test_pick_top_balance_and_excludes_nurture():
    leads = []
    for i in range(30):
        leads.append({**_lead(name=f"B{i}", vertical="plumber" if i % 2 else "hvac"),
                      "score": score_lead(_lead(name=f"B{i}", vertical="plumber" if i % 2 else "hvac"))})
    leads.append({"name": "NurtureOne", "score": score_lead(
        _lead(name="NurtureOne", rating=3.0, phone="", email="", instagram=""))})
    picked = pick_top(leads, target=10, per_vertical_cap_pct=50)
    assert len(picked) == 10
    names = {l["name"] for l in picked}
    assert "NurtureOne" not in names
    from collections import Counter
    verticals = Counter(l["vertical"] for l in picked)
    assert max(verticals.values()) <= 5  # 50% of 10
