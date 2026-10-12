"""Phase 2 compounding-memory tests: PII-safe lessons + per-agent md memory."""
from __future__ import annotations

from src.core.memory import (
    lessons_summary,
    load_agent_memory,
    memory_block,
    record_lesson,
    regenerate_memory_files,
    sanitize_lesson,
)
from src.core.state import StateStore


def test_sanitize_strips_emails_and_angle_brackets():
    out = sanitize_lesson("draft for <Owner Name> at owner@acme.com got rejected")
    assert "owner@acme.com" not in out
    assert "@" not in out
    assert "<Owner Name>" not in out
    assert "[redacted]" in out


def test_sanitize_collapses_newlines_and_caps_length():
    out = sanitize_lesson("line one\nline two\n\nline three", limit=20)
    assert "\n" not in out
    assert len(out) <= 20


def test_record_lesson_appends_and_caps(tmp_path):
    state = StateStore(tmp_path)
    for i in range(130):
        assert record_lesson(state, "Outreach", f"lesson {i}")
    lessons = state.load("lessons")
    assert len(lessons) == 100  # MAX_LESSONS
    assert lessons[-1]["lesson"] == "lesson 129"
    assert lessons[-1]["agent"] == "Outreach"


def test_record_lesson_skips_empty(tmp_path):
    state = StateStore(tmp_path)
    assert not record_lesson(state, "Outreach", "   ")
    assert state.load("lessons") is None  # nothing written


def test_regenerate_writes_per_agent_md(tmp_path):
    state = StateStore(tmp_path)
    record_lesson(state, "Outreach", "draft scored 4/10")
    record_lesson(state, "Outreach", "draft scored 4/10")  # dedupe
    record_lesson(state, "Lead Agent", "tool budget exhausted")
    memories = regenerate_memory_files(state)
    assert set(memories) == {"Outreach", "Lead Agent"}
    outreach = (tmp_path / "memory" / "Outreach.md").read_text(encoding="utf-8")
    assert "# Lessons for Outreach" in outreach
    assert outreach.count("draft scored 4/10") == 1  # deduped
    agent = (tmp_path / "memory" / "Lead_Agent.md").read_text(encoding="utf-8")
    assert "tool budget exhausted" in agent


def test_memory_block_injection_format(tmp_path):
    state = StateStore(tmp_path)
    assert memory_block(state, "Outreach") == ""  # no lessons -> no block
    record_lesson(state, "Outreach", "owner rejected 2 draft(s)")
    block = memory_block(state, "Outreach")
    assert block.startswith("LESSONS FROM PREVIOUS RUNS")
    assert "owner rejected 2 draft(s)" in block
    # Another agent's lessons must not leak into Outreach's block.
    record_lesson(state, "Scout", "other agent lesson")
    assert "other agent lesson" not in memory_block(state, "Outreach")


def test_load_agent_memory_reads_file_without_new_lessons(tmp_path):
    state = StateStore(tmp_path)
    record_lesson(state, "Outreach", "first")
    first = load_agent_memory(state, "Outreach")
    assert "first" in first
    # Lesson added for another agent does not change Outreach's file content.
    record_lesson(state, "Lead Agent", "unrelated")
    assert load_agent_memory(state, "Outreach") == first


def test_lessons_summary_lists_agent_and_lesson(tmp_path):
    state = StateStore(tmp_path)
    assert lessons_summary(state) == "No lessons recorded yet."
    record_lesson(state, "Outreach", "weak personalization")
    summary = lessons_summary(state)
    assert "[Outreach]" in summary and "weak personalization" in summary
