"""Phase 3 query-yield feedback tests: shapes learn from real yields."""
from __future__ import annotations

import json

from src.core.state import StateStore
from src.query_feedback import (
    MAX_SHAPES,
    build_yield_feedback,
    record_query_yield,
    suggest_query_shapes,
)


def test_record_and_feedback_roundtrip(tmp_path):
    state = StateStore(tmp_path)
    record_query_yield(state, {
        "small {vertical} business {city} no website phone email": 3,
        "{vertical} in {city} that miss calls": 12,
    })
    feedback = build_yield_feedback(state)
    assert "avg 12" in feedback
    assert "avg 3" in feedback
    # Best shape first (ranked by yield).
    assert feedback.index("miss calls") < feedback.index("small {vertical}")


def test_feedback_empty_when_no_data(tmp_path):
    state = StateStore(tmp_path)
    assert build_yield_feedback(state) == ""
    record_query_yield(state, {})  # no-op
    assert build_yield_feedback(state) == ""


def test_feedback_empty_on_corrupt_state(tmp_path):
    state = StateStore(tmp_path)
    (tmp_path / "query_yield.json").write_text("{not json", encoding="utf-8")
    assert build_yield_feedback(state) == ""


def test_record_accumulates_and_caps_history(tmp_path):
    state = StateStore(tmp_path)
    for i in range(15):
        record_query_yield(state, {"shape {vertical} {city}": i})
    data = state.load("query_yield")
    counts = data["history"]["shape {vertical} {city}"]
    assert len(counts) == 10  # per-shape cap
    assert counts[-1] == 14


def test_record_never_stores_pii(tmp_path):
    state = StateStore(tmp_path)
    record_query_yield(state, {"{vertical} {city} owner@x.example": 5})
    raw = (tmp_path / "query_yield.json").read_text(encoding="utf-8")
    # Shape keys are PII-stripped before storage (state/ is committed).
    assert "owner@x.example" not in raw
    assert "[redacted]" in raw


def test_min_runs_filters_sparse_shapes(tmp_path):
    state = StateStore(tmp_path)
    record_query_yield(state, {"a {vertical} {city}": 9})
    assert build_yield_feedback(state, min_runs=2) == ""
    assert build_yield_feedback(state, min_runs=1) != ""


def test_suggest_requires_feedback_and_available_brain():
    class FakeLLM:
        available = True

        async def complete(self, prompt):
            assert "YIELD FEEDBACK" in prompt  # feedback must reach the model
            return json.dumps(["{vertical} near {city} missing calls"])

    import asyncio

    shapes = asyncio.run(suggest_query_shapes(
        FakeLLM(), "YIELD FEEDBACK\n- avg 5: x", ["plumber"], ["Houston"]))
    assert shapes == ["{vertical} near {city} missing calls"]


def test_suggest_rejects_placeholders_missing():
    class FakeLLM:
        available = True

        async def complete(self, prompt):
            return json.dumps(["no placeholders here"])

    import asyncio

    assert asyncio.run(suggest_query_shapes(
        FakeLLM(), "fb", ["plumber"], ["Houston"])) == []


def test_suggest_caps_at_two():
    class FakeLLM:
        available = True

        async def complete(self, prompt):
            return json.dumps([f"{{vertical}} v{i} {{city}}" for i in range(5)])

    import asyncio

    shapes = asyncio.run(suggest_query_shapes(
        FakeLLM(), "fb", ["plumber"], ["Houston"]))
    assert len(shapes) == MAX_SHAPES == 2


def test_suggest_offline_brain_returns_empty():
    class OffLLM:
        available = False

        async def complete(self, prompt):
            raise AssertionError("must not be called")

    import asyncio

    assert asyncio.run(suggest_query_shapes(
        OffLLM(), "fb", ["plumber"], ["Houston"])) == []
