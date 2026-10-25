"""Per-query yield feedback for discovery (Phase 3 of RESEARCH_PROPER_AGENTS.md).

Atlas's Gemini query templates get real yield data: every run records how many
raw candidates each query shape produced (``state/query_yield.json``), and the
next run feeds those numbers back into the prompt that regenerates the shapes.
Discovery stops being a fixed strategy and becomes one that compounds.

PII-safe by construction: only shape strings and integer counts are stored.
All functions are defensive — a broken/corrupt yield file degrades to "no
feedback" and never breaks discovery.
"""
from __future__ import annotations

import json
import logging
import re

log = logging.getLogger(__name__)

MAX_SHAPES = 2          # keep the existing MapsAgent bound
MAX_RECORDS = 200       # yield history cap (shape -> counts list)
YIELD_PROMPT_MAX = 6    # shapes shown in the regeneration prompt

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def _normalize_shape(shape: str) -> str:
    """Whitespace-normalize + PII-strip: state/ is committed, so a shape key
    that somehow picked up an address must never reach the repo."""
    return _EMAIL_RE.sub("[redacted]", " ".join(str(shape or "").split())).strip()[:200]


def record_query_yield(state, results: dict[str, int]) -> None:
    """Append this run's per-shape candidate counts to state/query_yield.json.

    ``results`` maps query-shape -> raw candidate count. Defensive: never
    raises, never stores PII (shapes + integers only).
    """
    if not results:
        return
    try:
        data = state.load("query_yield") or {}
        if not isinstance(data, dict):
            data = {}
        history: dict = data.get("history") or {}
        for shape, count in results.items():
            key = _normalize_shape(shape)
            if not key:
                continue
            counts = history.get(key)
            if not isinstance(counts, list):
                counts = []
            try:
                counts.append(int(count))
            except (TypeError, ValueError):
                continue
            history[key] = counts[-10:]  # keep the last 10 runs per shape
        entries = list(history.items())
        while len(entries) > MAX_RECORDS:  # prune stale shapes entirely
            entries.pop(0)
        data["history"] = dict(entries)
        state.save("query_yield", data)
    except Exception as exc:
        log.debug("query yield recording failed: %s", exc)


def _avg(counts: list) -> float:
    try:
        nums = [float(c) for c in counts if isinstance(c, (int, float))]
        return sum(nums) / len(nums) if nums else 0.0
    except Exception:
        return 0.0


def build_yield_feedback(state, min_runs: int = 1) -> str:
    """The feedback text for the shape-regeneration prompt ('' when none).

    Ranks shapes by average yield so the model knows what already works.
    Shapes with fewer than ``min_runs`` observations are excluded.
    """
    try:
        data = state.load("query_yield") or {}
        history = data.get("history") if isinstance(data, dict) else None
        if not isinstance(history, dict) or not history:
            return ""
        ranked = []
        for shape, counts in history.items():
            if not isinstance(shape, str) or not isinstance(counts, list):
                continue
            if len(counts) < min_runs:
                continue
            ranked.append((round(_avg(counts), 1), shape))
        if not ranked:
            return ""
        ranked.sort(reverse=True)
        lines = [f"- avg {avg} candidates: {shape}" for avg, shape in ranked[:YIELD_PROMPT_MAX]]
        return "YIELD FEEDBACK from previous runs (candidates per query shape):\n" + "\n".join(lines)
    except Exception as exc:
        log.debug("query yield feedback failed: %s", exc)
        return ""


async def suggest_query_shapes(llm, feedback: str, verticals: list[str],
                               metros: list[str]) -> list[str]:
    """Ask Gemini for better shapes given the yield feedback. Returns only
    shapes carrying {vertical}/{city}, capped at MAX_SHAPES. Never raises;
    empty on any failure (offline, no feedback, bad reply)."""
    if not llm or not feedback:
        return []
    try:
        avail = llm.available
        if not bool(avail() if callable(avail) else avail):
            return []
    except Exception:
        return []
    prompt = (
        "You are Atlas, the lead-discovery agent. Here is how Google Maps "
        "query shapes performed recently:\n"
        f"{feedback}\n\n"
        f"Verticals: {', '.join(verticals[:8]) or 'any'}. "
        f"Metros: {', '.join(metros[:8]) or 'any'}.\n"
        "Suggest 2 NEW query templates likely to beat the average — keep what "
        "works implicitly by varying the weak ones. Use {vertical} and {city} "
        "as placeholders. Reply STRICT JSON only: a JSON array of strings."
    )
    try:
        raw = await llm.complete(prompt)
    except Exception as exc:
        log.debug("shape regeneration call failed: %s", exc)
        return []
    return _parse_shapes(raw or "")


def _parse_shapes(raw: str) -> list[str]:
    start, depth, end = raw.find("["), 0, -1
    if start == -1:
        return []
    for i in range(start, len(raw)):
        if raw[i] == "[":
            depth += 1
        elif raw[i] == "]":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end == -1:
        return []
    try:
        shapes = json.loads(raw[start:end])
    except json.JSONDecodeError:
        return []
    out = []
    for s in shapes:
        if isinstance(s, str) and "{vertical}" in s and "{city}" in s:
            normalized = _normalize_shape(s)
            if normalized and normalized not in out:
                out.append(normalized)
    return out[:MAX_SHAPES]
