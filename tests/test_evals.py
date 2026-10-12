"""Tests for the Phase 2 eval infrastructure: the golden sets must pass, and
the rationale function must be injectable into agent prompts."""
from __future__ import annotations

from src.agents.scout import score_lead, scoring_rationale
from src.evals.golden_routing import GOLDEN_ROUTING
from src.evals.golden_scoring import GOLDEN_SCORING
from src.evals.run import run_routing_evals, run_scoring_evals


def test_golden_routing_set_has_expected_size():
    assert len(GOLDEN_ROUTING) >= 40
    # Every case must be well-formed (empty messages allowed: adversarial).
    for message, expected in GOLDEN_ROUTING:
        assert isinstance(message, str)
        assert expected.split(":")[0] in ("command", "intent", "route")


def test_golden_scoring_set_has_expected_size():
    assert len(GOLDEN_SCORING) == 15
    for case_id, lead, expected in GOLDEN_SCORING:
        assert case_id and lead and expected


def test_all_routing_evals_pass():
    passed, failed = run_routing_evals()
    assert failed == 0, f"{failed} routing eval(s) failed"
    assert passed == len(GOLDEN_ROUTING)


def test_all_scoring_evals_pass():
    passed, failed = run_scoring_evals()
    assert failed == 0, f"{failed} scoring eval(s) failed"
    assert passed == len(GOLDEN_SCORING)


def test_rationale_covers_every_category():
    score = score_lead({"name": "Mock Biz", "vertical": "plumber",
                        "city": "Houston", "rating": 4.8, "reviews": 100})
    rationale = scoring_rationale(score, {"name": "Mock Biz",
                                          "vertical": "plumber", "city": "Houston",
                                          "rating": 4.8, "reviews": 100})
    for line in ("ICP ", "Intent ", "Budget ", "Reachability ", "Timing ",
                 "Penalties ", "Tier "):
        assert line in rationale
    # Ends with the tier decision.
    assert rationale.strip().splitlines()[-1].startswith("Tier ")


def test_rationale_is_pii_safe():
    lead = {"name": "Owner Person", "vertical": "plumber", "city": "Houston",
            "rating": 4.8, "reviews": 100, "email": "secret@private.example",
            "instagram": "handle"}
    score = score_lead(lead)
    rationale = scoring_rationale(score, lead)
    assert "secret@private.example" not in rationale


def test_rationale_never_raises_on_garbage():
    score = score_lead({})
    out = scoring_rationale(score, {"name": None, "reviews": "not-a-number"})
    assert "Tier" in out


def test_runner_reports_totals():
    from src.evals import run as run_mod

    r_passed, r_failed = run_mod.run_routing_evals()
    s_passed, s_failed = run_mod.run_scoring_evals()
    assert r_passed == len(GOLDEN_ROUTING) and r_failed == 0
    assert s_passed == len(GOLDEN_SCORING) and s_failed == 0
