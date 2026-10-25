"""Compounding agent memory (Phase 2 of RESEARCH_PROPER_AGENTS.md).

Failures and rejections become lessons (``state/lessons.json``, appended by
the Phase 1 loop and the pipeline), and those lessons are distilled into a
compact per-agent markdown file (``state/memory/<agent>.md``) that is injected
into that agent's context on the next run — memory that compounds instead of
an append-only log nobody reads.

House rules enforced here:
- PII never enters memory (spec 11): anything between mail-style angle
  brackets is stripped, plus an explicit email pattern, plus a hard size cap;
- memory is best-effort: every function is defensive and never raises —
  a broken memory file can never take down a run;
- PII guard safe: files live in ``state/`` but contain no lead data.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

# Sizes: lessons list cap (matches lead_agent_tools) and md byte cap.
MAX_LESSONS = 100
MAX_MEMORY_CHARS = 2000

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_ANGLE_RE = re.compile(r"<[^>\n]{4,}>")  # <name@domain>, <John Smith>, ...

# Lessons are free text from model/tool output: strip the obvious carriers.
_PII_PATTERNS = (_EMAIL_RE, _ANGLE_RE)


def sanitize_lesson(text: str, limit: int = 300) -> str:
    """PII-safe, single-line lesson text. Never raises."""
    cleaned = str(text or "")
    for pattern in _PII_PATTERNS:
        cleaned = pattern.sub("[redacted]", cleaned)
    cleaned = " ".join(cleaned.split())  # collapse whitespace/newlines
    return cleaned[:limit]


def record_lesson(state, agent: str, lesson: str) -> bool:
    """Append one sanitized lesson for ``agent`` to state/lessons.json.

    Returns True when the file changed. Never raises (a logging failure must
    not fail the run that produced the lesson).
    """
    try:
        clean = sanitize_lesson(lesson)
        if not clean:
            return False
        existing = state.load("lessons") or []
        if not isinstance(existing, list):
            existing = []
        existing.append({"agent": agent, "lesson": clean})
        state.save("lessons", existing[-MAX_LESSONS:])
        return True
    except Exception:
        return False


def _load_lessons(state) -> list[dict]:
    try:
        data = state.load("lessons") or []
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [row for row in data if isinstance(row, dict)]


def regenerate_memory_files(state) -> dict[str, str]:
    """Distill state/lessons.json into state/memory/<agent>.md files.

    Returns {agent_name: memory_text} for every agent that has lessons.
    Deduplicates identical lessons (repeated failures say it once), keeps the
    most recent ones, and caps each file at MAX_MEMORY_CHARS.
    """
    lessons = _load_lessons(state)
    by_agent: dict[str, list[str]] = {}
    for row in lessons:
        agent = str(row.get("agent") or "Lead Agent").strip() or "Lead Agent"
        lesson = sanitize_lesson(row.get("lesson") or "", 200)
        if not lesson:
            continue
        bucket = by_agent.setdefault(agent, [])
        if lesson not in bucket:  # dedupe: a repeated failure says it once
            bucket.append(lesson)

    memories: dict[str, str] = {}
    for agent, items in by_agent.items():
        lines = [f"# Lessons for {agent}", ""]
        for i, lesson in enumerate(reversed(items[-20:]), start=1):
            lines.append(f"{i}. {lesson}")
        text = "\n".join(lines)[:MAX_MEMORY_CHARS]
        memories[agent] = text
        _write_memory_file(state, agent, text)
    return memories


def _memory_path(state, agent: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", agent).strip("_") or "agent"
    return Path(str(state.root)) / "memory" / f"{safe}.md"


def _write_memory_file(state, agent: str, text: str) -> None:
    try:
        path = _memory_path(state, agent)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except Exception as exc:  # best-effort: memory files never break a run
        log.debug("memory file write failed for %s: %s", agent, exc)


def load_agent_memory(state, agent: str, max_chars: int = MAX_MEMORY_CHARS) -> str:
    """The memory block for ``agent``: regenerated from lessons when needed,
    then read from state/memory/<agent>.md. Empty string when no lessons.
    Never raises."""
    try:
        if _load_lessons(state):  # lessons changed since last regeneration?
            regenerate_memory_files(state)
        path = _memory_path(state, agent)
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")[:max_chars].strip()
    except Exception:
        return ""


def memory_block(state, agent: str) -> str:
    """Ready-to-inject context block ('' when no memory) for prompts."""
    memory = load_agent_memory(state, agent)
    if not memory:
        return ""
    return (
        "LESSONS FROM PREVIOUS RUNS (avoid repeating these mistakes):\n"
        f"{memory}"
    )


def lessons_summary(state, limit: int = 5) -> str:
    """Compact human-readable recent lessons (Telegram/report friendly)."""
    lessons = _load_lessons(state)
    if not lessons:
        return "No lessons recorded yet."
    lines = []
    for row in lessons[-limit:]:
        agent = str(row.get("agent") or "?")
        lines.append(f"- [{agent}] {row.get('lesson', '')}")
    return "\n".join(lines)


def as_json(state) -> str:
    """Debug helper: the raw lessons list as JSON (PII-safe by construction)."""
    try:
        return json.dumps(_load_lessons(state), indent=2)
    except Exception:
        return "[]"
