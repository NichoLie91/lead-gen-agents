"""Telegram bot command parsing & formatting tests (spec 5.1)."""
from src.bot.commands import build_help, format_status, is_allowed, parse_command


def test_parse_single_word():
    assert parse_command("/run") == ("/run", "")
    assert parse_command("/help") == ("/help", "")


def test_parse_multiword_longest_match():
    assert parse_command("/reject all") == ("/reject all", "")
    assert parse_command("/reject") == ("/reject", "")
    assert parse_command("/send all email") == ("/send all email", "")
    assert parse_command("/send all instagram") == ("/send all instagram", "")


def test_parse_with_extra_text():
    cmd, args = parse_command("/run tonight")
    assert cmd == "/run"
    assert args == "tonight"


def test_parse_unknown():
    assert parse_command("/nonsense") == ("", "")
    assert parse_command("hello there") == ("", "")
    assert parse_command(None) == ("", "")


def test_allow_list():
    assert is_allowed(123, []) is True            # empty list == open bot
    assert is_allowed(123, [456]) is False
    assert is_allowed(456, [456, 789]) is True


def test_help_lists_all_commands():
    help_text = build_help()
    for cmd in ("/run", "/status", "/stop", "/approve", "/reject all",
                "/send all email", "/send all instagram", "/sheet", "/id"):
        assert cmd in help_text


def test_format_status_no_run():
    text = format_status(None, "", False)
    assert "/run" in text


def test_format_status_with_report():
    report = {
        "run_id": "abc123", "mode": "full", "status": "COMPLETED",
        "started_at": "2026-08-05T00:00:00+00:00",
        "metrics": {
            "candidates": 12, "tiers": {"HOT-VERIFIED": 3, "WARM": 5},
            "emails_sent": 3, "emails_drafted": 5, "emails_skipped": 4,
            "ig_sent": 0, "ig_skipped": 12,
        },
    }
    text = format_status(report, "https://sheets.example/1", False)
    assert "abc123" in text and "COMPLETED" in text
    assert "candidates: 12" in text.lower() or "Candidates: 12" in text
    assert "sheets.example" in text


def test_format_status_running():
    text = format_status(None, "", True)
    assert "RUNNING" in text
