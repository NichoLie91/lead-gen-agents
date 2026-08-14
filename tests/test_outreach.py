"""ANCHOR (Outreach) compliance tests — the doc's hard rules, enforced in code.

Covers the human-writer rule enforcement (doc 4: "if you generate any text
containing these banned words or punctuation marks, rewrite it immediately"),
the IG outcome vocabulary (cold-start / 24h window / failed), and the
per-lead personalization facts. The IG first-message rule (single low-pressure
question, never a pitch) is enforced here too.
"""
from src.pipeline import Pipeline

# ---------- human-writer rules (doc 4) ----------

def test_lint_detects_banned_punctuation_and_cliches():
    tells = Pipeline._lint_ai_tells("In today's world, we must delve deeper — truly.")
    assert "em-dash" in tells
    assert any("delve" in t for t in tells)
    assert any("in today's world" in t for t in tells)


def test_has_opt_out_guard():
    assert Pipeline._has_opt_out('Reply "stop" to opt out.') is True
    assert Pipeline._has_opt_out("reply stop to opt out") is True
    assert Pipeline._has_opt_out("no opt out here") is False


def test_lint_passes_human_copy():
    clean = ("Hi Blue Creek team. I noticed your 4.8 star profile in Tampa "
             "but no website at all. Who answers your after-hours calls? "
             "Reply stop to opt out.")
    assert Pipeline._lint_ai_tells(clean) == []


def test_sanitize_strips_banned_tells():
    out = Pipeline._sanitize_ai_tells(
        "We leverage synergy — truly — at the end of the day.")
    assert "—" not in out
    assert "leverage" not in out
    assert "at the end of the day" not in out


# ---------- IG outcome vocabulary (doc 4) ----------

def test_ig_failure_reason_mapping():
    assert Pipeline._ig_failure_reason(
        "The recipient must message you first") == "cold_start"
    assert Pipeline._ig_failure_reason(
        "Cannot message: open a conversation before sending") == "cold_start"
    assert Pipeline._ig_failure_reason(
        "outside the 24 hour allowed window") == "window"
    assert Pipeline._ig_failure_reason(
        "messaging window has closed for this user") == "window"
    assert Pipeline._ig_failure_reason("rate limited, try later") == "other"


def test_ig_failure_reason_no_psid_tooling_gap():
    # Enrichment collects the @handle; the DM tool needs a numeric PSID and
    # there is no handle->ID resolver — a tooling gap, counted as a SKIP.
    assert Pipeline._ig_failure_reason(
        "recipient_id must be a numeric Instagram PSID, got 'bluecreek'") == "no_psid"
    assert Pipeline._ig_failure_reason(
        'Param recipient[id] must be a valid ID string (e.g., "123")') == "no_psid"


# ---------- per-lead personalization facts (doc 4: never a pasted template) ----------

def test_lead_facts_carry_concrete_details():
    facts = Pipeline._lead_facts({
        "name": "Blue Creek Plumbing", "vertical": "plumber", "city": "Tampa",
        "rating": 4.8, "reviews": 120, "website": "", "instagram": "bluecreek",
    })
    assert "Blue Creek Plumbing" in facts
    assert "Tampa" in facts
    assert "4.8" in facts and "120" in facts
    assert "No website at all" in facts
    assert "@bluecreek" in facts


def test_lead_facts_note_website_status():
    facts = Pipeline._lead_facts({
        "name": "Apex HVAC", "vertical": "hvac", "city": "Phoenix",
        "rating": 4.6, "reviews": 40,
        "website": "https://apex.example", "website_status": "no booking",
    })
    assert "no booking" in facts
    assert "No website at all" not in facts


# ---------- IG first message (doc 4: single question, never a pitch) ----------

def test_ig_first_message_is_question_not_pitch():
    msg = Pipeline._ig_first_message({"rating": 4.8, "city": "Tampa"})
    assert "?" in msg                         # it is a single question
    assert msg.count("?") == 1               # and only one question
    assert "I sell" not in msg
    assert "I build" not in msg
    assert "custom AI" not in msg
    assert "4.8" in msg and "Tampa" in msg  # acknowledges something specific


def test_ig_first_message_mentions_no_website_observation():
    msg = Pipeline._ig_first_message({"rating": 4.8, "city": "Tampa"})
    assert "missed calls" in msg.lower()  # the observation-and-question format
