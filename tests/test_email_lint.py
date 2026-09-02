"""Tests for src/email_lint.py — the hard guardrail #0.

Run standalone (no pytest needed):  python tests/test_email_lint.py
Or via pytest:                      pytest tests/test_email_lint.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.email_lint import (
    CTA_BANS,
    append_footer,
    lint_email,
)

CRITERIA = {
    "sender": {"physical_address": "1234 Elm St, Suite 5, Springfield, IL 62701"}
}

GOOD_BODY = (
    "Your 4.8 star profile in Tampa with 140 reviews suggests after-hours "
    "calls are going to voicemail.\n\n"
    "A Tampa plumber fixed the same gap with an AI follow-up system and added "
    "11 extra bookings in the first month, paying only from the jobs it "
    "booked.\n\n"
    "Worth a look for your shop?"
)


def _final(subject: str = "After-hours calls", body: str = GOOD_BODY,
           name: str = "Ace Plumbing") -> tuple[str, str]:
    final = append_footer(body, name, CRITERIA)
    return subject, final


# --- The happy path ----------------------------------------------------------

def test_compliant_email_passes() -> None:
    subject, final = _final()
    assert lint_email(subject, final, CRITERIA) == []


def test_footer_is_idempotent() -> None:
    once = append_footer(GOOD_BODY, "Ace Plumbing", CRITERIA)
    twice = append_footer(once, "Ace Plumbing", CRITERIA)
    assert once == twice


def test_footer_contains_address_and_optout() -> None:
    final = append_footer(GOOD_BODY, "Ace Plumbing", CRITERIA)
    assert "stop" in final.lower()
    assert "1234 Elm St" in final


# --- CAN-SPAM gates ----------------------------------------------------------

def test_empty_physical_address_blocks_everything() -> None:
    subject, final = _final()
    empty = {"sender": {"physical_address": ""}}
    violations = lint_email(subject, final, empty)
    assert any("physical_address is empty" in v for v in violations)


def test_missing_physical_address_key_blocks() -> None:
    subject, final = _final()
    violations = lint_email(subject, final, {})
    assert any("physical_address" in v for v in violations)


def test_body_without_footer_blocks() -> None:
    violations = lint_email("After-hours calls", GOOD_BODY, CRITERIA)
    assert any("footer missing" in v for v in violations)


def test_footer_with_wrong_address_blocks() -> None:
    other = {"sender": {"physical_address": "99 Other Way, Boston, MA 02101"}}
    subject, final = _final()  # built with CRITERIA address
    violations = lint_email(subject, final, other)
    assert any("does not match" in v for v in violations)


# --- Subject rules -----------------------------------------------------------

def test_long_subject_blocks() -> None:
    subject, final = _final(subject="A quick question about your plumbing business today please")
    violations = lint_email(subject, final, CRITERIA)
    assert any("Subject:" in v and "max 8" in v for v in violations)


def test_re_subject_blocks() -> None:
    subject, final = _final(subject="Re: your website")
    violations = lint_email(subject, final, CRITERIA)
    assert any("Re:/Fwd:" in v for v in violations)


def test_all_caps_subject_blocks() -> None:
    subject, final = _final(subject="MISSSED CALLS")
    violations = lint_email(subject, final, CRITERIA)
    assert any("ALL CAPS" in v for v in violations)


# --- Body structure ----------------------------------------------------------

def test_long_body_blocks() -> None:
    long_body = GOOD_BODY + " " + ("Really " * 50) + "Worth a look?"
    subject, final = _final(body=long_body)
    violations = lint_email(subject, final, CRITERIA)
    assert any("words (max 90)" in v for v in violations)


def test_two_question_marks_block() -> None:
    body = GOOD_BODY + "\n\nAlso, are you free next week?"  # 'free' also banned
    subject, final = _final(body=body)
    violations = lint_email(subject, final, CRITERIA)
    assert any("question marks" in v for v in violations)


def test_i_opener_blocks() -> None:
    body = "I build AI systems for plumbers.\n\nWorth a look?"
    subject, final = _final(body=body)
    violations = lint_email(subject, final, CRITERIA)
    assert any("opens with I/we" in v for v in violations)


def test_link_blocks() -> None:
    body = GOOD_BODY + "\n\nSee https://example.com/info"
    subject, final = _final(body=body)
    violations = lint_email(subject, final, CRITERIA)
    assert any("links banned" in v for v in violations)


def test_bare_domain_blocks() -> None:
    body = GOOD_BODY.replace("Worth a look", "See example.com for more. Worth a look")
    subject, final = _final(body=body)
    violations = lint_email(subject, final, CRITERIA)
    assert any("bare domain" in v for v in violations)


# --- CTA + banned phrases ----------------------------------------------------

def test_calendar_cta_blocks() -> None:
    body = GOOD_BODY.replace("Worth a look for your shop?", "Can we schedule a call?")
    subject, final = _final(body=body)
    violations = lint_email(subject, final, CRITERIA)
    assert any("CTA:" in v for v in violations)


def test_every_cta_ban_is_caught() -> None:
    for phrase in CTA_BANS:
        body = GOOD_BODY.replace("Worth a look for your shop?", f"{phrase.capitalize()}?")
        subject, final = _final(body=body)
        violations = lint_email(subject, final, CRITERIA)
        assert any("CTA:" in v for v in violations), phrase


def test_ai_tell_blocks() -> None:
    body = GOOD_BODY.replace("suggests", "I hope this email finds you well and suggests")
    subject, final = _final(body=body)
    violations = lint_email(subject, final, CRITERIA)
    assert any("Banned AI tell" in v for v in violations)


def test_spam_trigger_blocks() -> None:
    body = GOOD_BODY.replace("11 extra bookings", "a 100% risk-free outcome")
    subject, final = _final(body=body)
    violations = lint_email(subject, final, CRITERIA)
    assert any("Spam trigger" in v for v in violations)


def test_attachment_mention_blocks() -> None:
    body = GOOD_BODY + "\n\nAttached is our brochure."  # two ?s + attachment
    subject, final = _final(body=body)
    violations = lint_email(subject, final, CRITERIA)
    assert any("attachments" in v for v in violations)


if __name__ == "__main__":
    failures = 0
    tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_")]
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {name}: {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} passing")
    sys.exit(1 if failures else 0)
