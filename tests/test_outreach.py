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


def test_lint_passes_human_copy():
    clean = ("Hi Blue Creek team. I noticed your 4.8 star profile in Tampa "
             "but no website at all. Who answers your after-hours calls?")
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


# ---------- cold-email skill (2-4 word subjects, varied per lead) ----------

def test_subject_pool_is_short_and_varied():
    # Every subject in every pool is 2-4 words (skill rule), and the same
    # vertical has more than one option so leads don't all get identical mail.
    for vertical, pool in Pipeline.SUBJECT_POOL.items():
        assert len(pool) >= 2, f"{vertical} needs variation"
        for s in pool:
            assert 2 <= len(s.split()) <= 4, f"subject '{s}' not 2-4 words"


def test_pick_subject_is_deterministic_and_varies_across_leads():
    a = Pipeline._pick_subject({"name": "Blue Creek Plumbing", "vertical": "plumber"})
    b = Pipeline._pick_subject({"name": "Blue Creek Plumbing", "vertical": "plumber"})
    assert a == b  # same lead always gets the same subject (spintax stability)
    subjects = {Pipeline._pick_subject({"name": n, "vertical": "plumber"})
                for n in ("Alpha Plumbing", "Bravo Plumbing", "Charlie Plumbing",
                          "Delta Plumbing", "Echo Plumbing", "Foxtrot Plumbing")}
    assert len(subjects) > 1  # different leads get varied subjects


def test_pick_subject_falls_back_for_unknown_vertical():
    s = Pipeline._pick_subject({"name": "Mystery Co", "vertical": "alien"})
    assert 2 <= len(s.split()) <= 4


# ---------- humanizer skill: spam trigger words ----------

def test_lint_spam_words_detects_banned_triggers():
    got = Pipeline._lint_spam_words("Click here for a free consultation!")
    assert set(got) == {"click here", "free"}
    got = Pipeline._lint_spam_words("Urgent: buy now, save money, best price")
    assert set(got) == {"urgent", "buy now", "save money", "best price"}
    assert Pipeline._lint_spam_words("freedom is nice, not spammy") == []  # boundary


# ---------- cold-email + humanizer enforcement ----------

def test_lint_email_rules_full_check():
    good = ("Hi Blue Creek team. I noticed your 4.8 star profile in Tampa "
            "and no website at all. I build AI systems that answer those "
            "missed calls for you. Worth a brief chat next week?")
    assert Pipeline._lint_email_rules(good) == []
    bad = ("We leverage synergy to increase revenue, click here for a free "
           "ebook today, urgent! Don't miss out. It is important to note "
           "that this is a game-changer. \u2014 book now. https://example.com "
           "Best price guaranteed, save money now.")
    got = Pipeline._lint_email_rules(bad)
    joined = " ".join(got).lower()
    assert "spam trigger word" in joined
    assert "ai tells" in joined or "cliche" in joined or "em-dash" in joined
    assert "contains a link" in joined


def test_lint_email_rules_counts_sentences():
    body = ("Hi there. Your 4.8 stars in Tampa caught my eye. Worth a chat?")
    assert Pipeline._count_sentences(body) == 3
    assert Pipeline._lint_email_rules(body) == []


# ---------- rate_email (1-10 scorer against both skills) ----------

def test_rate_email_perfect_draft_scores_10():
    body = ("Hi Blue Creek team. I noticed your 4.8 star profile in Tampa and "
            "no website at all. I build AI systems that answer those missed "
            "calls for you. Worth a brief chat next week?")
    facts = ("Business: Blue Creek Plumbing\nCity: Tampa\n"
             "Google rating: 4.8 (120 reviews)")
    score, notes = Pipeline.rate_email("Missed calls", body, facts)
    assert score == 10, notes
    assert notes == []


def test_rate_email_penalizes_violations():
    body = ("We leverage synergy to increase revenue, click here now. "
            "This is a game-changer. \u2014 https://example.com Best price.")
    score, notes = Pipeline.rate_email("We leverage synergy today for revenue", body)
    assert score < 8
    joined = " ".join(notes).lower()
    assert "spam trigger word" in joined or "ai tells" in joined
    assert "contains a link" in joined
    assert "subject" in joined  # 6-word subject flagged


def test_rate_email_flags_unpersonalized_template():
    body = ("Hi there. I noticed your profile and one thing stood out. "
            "I build custom AI systems for businesses. Worth a chat?")
    facts = ("Business: Blue Creek Plumbing\nCity: Tampa\n"
             "Google rating: 4.8 (120 reviews)")
    score, notes = Pipeline.rate_email("Quick question", body, facts)
    assert score < 10
    assert any("template" in n.lower() for n in notes)
