"""Inbound reply handling tests (Step 06): sender parsing + classification."""
from __future__ import annotations

from src.inbound import classify_reply, parse_sender_email, suggested_reply


def test_parse_sender_email():
    assert parse_sender_email("Acme Plumbing <owner@acme.com>") == "owner@acme.com"
    assert parse_sender_email("owner@acme.com") == "owner@acme.com"
    assert parse_sender_email("Acme Plumbing") is None
    assert parse_sender_email(None) is None


def test_classify_stop():
    for text in ("please stop", "unsubscribe me", "opt out", "remove me",
                 "not interested", "no more emails"):
        assert classify_reply(text) == "STOP", text


def test_classify_price_objection():
    assert classify_reply("how much does it cost?") == "PRICE_OBJECTION"
    assert classify_reply("too expensive for us right now") == "PRICE_OBJECTION"
    assert classify_reply("what's the price?") == "PRICE_OBJECTION"


def test_classify_interested():
    assert classify_reply("yes, interested — let's talk") == "INTERESTED"
    assert classify_reply("sounds good, send more info") == "INTERESTED"
    assert classify_reply("tell me more") == "INTERESTED"


def test_classify_defaults_to_question():
    assert classify_reply("can you email me next week?") == "QUESTION"
    assert classify_reply("") == "QUESTION"


def test_llm_label_wins_over_keywords():
    assert classify_reply("no way", llm_label="INTERESTED") == "INTERESTED"
    assert classify_reply("", llm_label="price_objection") == "PRICE_OBJECTION"
    assert classify_reply("yes yes yes", llm_label="STOP") == "STOP"


def test_suggested_reply_mentions_lead():
    assert "Acme" in suggested_reply("INTERESTED", {"Name": "Acme"})
    assert "unsubscribed" in suggested_reply("STOP", {"Name": "Acme"}).lower()
