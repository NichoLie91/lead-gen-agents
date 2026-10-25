"""The Drafts tab stays a screenful, and the Pipeline tab stops being wiped.

Two queues the owner actually reads:

- Drafts: pending work only. Sent, rejected and stale rows move to
  DraftsArchive so the tab never grows past what fits on a phone.
- Pipeline: the research batch's source. It accumulates (new leads first,
  deduped, capped) instead of being rewritten with only the last run's finds,
  which emptied it on any run that discovered nothing new.
"""
from __future__ import annotations

import asyncio

from src.core.config import Settings
from src.pipeline import (
    DRAFTS_HEADER,
    DRAFTS_MAX_ROWS,
    PIPELINE_HEADER,
    Pipeline,
    partition_archive,
    partition_drafts,
)


def _draft(i: int, created: str, status: str = "NEEDS_APPROVAL", lead: str = "") -> list:
    row = [i, lead or f"Shop {i}", f"id{i}", "a@b.test", "Subject", "Body",
           80, "WARM", status, created, "note"]
    assert len(row) == len(DRAFTS_HEADER)
    return row


def _created(row: list) -> str:
    return row[DRAFTS_HEADER.index("Created")]


def _status(row: list) -> str:
    return row[DRAFTS_HEADER.index("Status")]


# ---------- partitioning ----------

def test_the_queue_keeps_only_a_screenful_newest_first():
    rows = [_draft(i, f"2026-10-{i:02d}") for i in range(1, 11)]
    kept, archived = partition_drafts(rows, "2026-10-11", keep=4, stale_days=99)
    assert len(kept) == 4
    assert [_created(r) for r in kept] == ["2026-10-10", "2026-10-09",
                                           "2026-10-08", "2026-10-07"]
    assert len(archived) == 6


def test_a_stale_draft_leaves_even_with_room_to_spare():
    rows = [_draft(1, "2026-09-01"), _draft(2, "2026-10-24")]
    kept, archived = partition_drafts(rows, "2026-10-25", keep=20, stale_days=21)
    assert [_created(r) for r in kept] == ["2026-10-24"]
    assert [_created(r) for r in archived] == ["2026-09-01"]


def test_a_blank_date_is_kept_rather_than_guessed_stale():
    rows = [_draft(1, ""), _draft(2, "2026-10-24")]
    kept, archived = partition_drafts(rows, "2026-10-25", keep=20, stale_days=21)
    assert len(kept) == 2
    assert archived == []


def test_partition_drafts_is_stable_for_equal_dates():
    rows = [_draft(1, "2026-10-24"), _draft(2, "2026-10-24"), _draft(3, "2026-10-24")]
    kept, _ = partition_drafts(rows, "2026-10-25", keep=2, stale_days=99)
    assert [r[1] for r in kept] == ["Shop 1", "Shop 2"]


def test_the_archive_drops_its_oldest_rows_past_the_cap():
    history = [_draft(i, "2025-01-01") for i in range(5)]
    fresh = [_draft(i, "2026-10-25") for i in range(5)]
    combined = partition_archive(history, fresh, cap=6)
    assert len(combined) == 6
    assert all(_created(r) == "2026-10-25" or r in history[-1:] for r in combined)
    assert combined[-1] in fresh, "newest rows are kept at the tail"


# ---------- the sheet write ----------

def _pipe(tmp_path) -> Pipeline:
    p = Pipeline(Settings(dry_run=True, repo_root=tmp_path))
    asyncio.run(p.sheets.ensure_sheet())
    return p


def test_sent_rows_leave_the_drafts_tab(tmp_path):
    p = _pipe(tmp_path)
    asyncio.run(p.sheets.write_tab("Drafts", [
        _draft(1, "2026-10-24", status="NEEDS_APPROVAL", lead="Pending One"),
        _draft(2, "2026-10-23", status="SENT", lead="Already Sent"),
        _draft(3, "2026-10-22", status="REJECTED", lead="Rejected One"),
    ]))
    p._drafts_rows = [_draft(4, "2026-10-25", lead="Fresh Draft")]

    report: dict = {"metrics": {}}
    asyncio.run(p._flush_drafts(report))

    drafts = asyncio.run(p.sheets.read_tab("Drafts"))
    names = [r[1] for r in drafts[1:]]
    assert names == ["Fresh Draft", "Pending One"], "newest first, nothing sent"
    archive = asyncio.run(p.sheets.read_tab("DraftsArchive"))
    assert {r[1] for r in archive[1:]} == {"Already Sent", "Rejected One"}
    assert report["metrics"]["drafts_kept"] == 2
    assert report["metrics"]["drafts_archived"] == 2


def test_forty_old_drafts_collapse_to_a_screenful(tmp_path):
    p = _pipe(tmp_path)
    asyncio.run(p.sheets.write_tab(
        "Drafts", [_draft(i, "2026-10-01", lead=f"Old {i}") for i in range(40)]))
    p._drafts_rows = [_draft(100, "2026-10-25", lead="Today")]

    asyncio.run(p._flush_drafts({"metrics": {}}))

    drafts = asyncio.run(p.sheets.read_tab("Drafts"))
    assert len(drafts) - 1 == 1, "everything stale goes; only today's draft remains"
    archive = asyncio.run(p.sheets.read_tab("DraftsArchive"))
    assert len(archive) - 1 == 40


def test_the_queue_is_capped_and_renumbered(tmp_path):
    p = _pipe(tmp_path)
    asyncio.run(p.sheets.write_tab(
        "Drafts",
        [_draft(i, f"2026-10-{(i % 24) + 1:02d}", lead=f"Shop {i}")
         for i in range(35)]))
    p._drafts_rows = [_draft(200, "2026-10-25", lead="Newest")]

    asyncio.run(p._flush_drafts({"metrics": {}}))

    drafts = asyncio.run(p.sheets.read_tab("Drafts"))
    rows = drafts[1:]
    assert len(rows) == DRAFTS_MAX_ROWS
    assert [r[0] for r in rows] == list(range(1, DRAFTS_MAX_ROWS + 1))
    assert rows[0][1] == "Newest"


# ---------- pipeline queue ----------

def _pipe_row(rank: int, name: str, city: str = "Houston", score: int = 80) -> list:
    row = [""] * len(PIPELINE_HEADER)
    row[PIPELINE_HEADER.index("rank")] = rank
    row[PIPELINE_HEADER.index("business_name")] = name
    row[PIPELINE_HEADER.index("city")] = city
    row[PIPELINE_HEADER.index("scout_score")] = score
    return row


def test_the_pipeline_queue_accumulates_new_leads_first():
    existing = [PIPELINE_HEADER, _pipe_row(1, "Old One"), _pipe_row(2, "Shared Shop")]
    new = [_pipe_row(1, "Fresh Shop"), _pipe_row(2, "Shared Shop", score=91)]

    merged = Pipeline._merge_pipeline_rows(existing, new, cap=100)

    assert [r[1] for r in merged] == ["Fresh Shop", "Shared Shop", "Old One"]
    assert merged[1][PIPELINE_HEADER.index("scout_score")] == 91, "new row wins"
    assert [r[0] for r in merged] == [1, 2, 3], "rank follows the queue"


def test_an_empty_run_does_not_wipe_the_queue():
    existing = [PIPELINE_HEADER, _pipe_row(1, "Old One"), _pipe_row(2, "Old Two")]
    merged = Pipeline._merge_pipeline_rows(existing, [], cap=100)
    assert [r[1] for r in merged] == ["Old One", "Old Two"]


def test_the_queue_is_capped_and_ignores_blank_rows():
    existing = [PIPELINE_HEADER, _pipe_row(1, "A"), _pipe_row(2, ""), _pipe_row(3, "B")]
    merged = Pipeline._merge_pipeline_rows(existing, [], cap=1)
    assert [r[1] for r in merged] == ["A"]


def test_rows_are_rebuilt_into_header_order():
    """A tab whose columns were reordered must not shift every cell."""
    shuffled = list(PIPELINE_HEADER)
    shuffled.remove("city")
    shuffled.insert(1, "city")
    stale = [shuffled] + [["7", "Houston", "Legacy Shop"] +
                          [""] * (len(PIPELINE_HEADER) - 3)]
    merged = Pipeline._merge_pipeline_rows(stale, [], cap=10)
    row = merged[0]
    assert row[PIPELINE_HEADER.index("business_name")] == "Legacy Shop"
    assert row[PIPELINE_HEADER.index("city")] == "Houston"
    assert row[0] == 1
