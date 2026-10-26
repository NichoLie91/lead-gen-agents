"""CLI exit-status tests.

`Pipeline.run()` catches its own exception, records status=FAILED and returns
the report (so the Telegram alert and the state commit still happen). main()
must translate that into a non-zero exit: on 2026-10-25 a run where every
discovery query failed (918 queries, 0 leads) still exited 0, so Actions
reported success and nobody was paged.
"""
from __future__ import annotations

import pytest

import src.core.logging as logging_mod
import src.pipeline as pipeline_mod
from src.core.config import Settings


@pytest.fixture
def cli(monkeypatch, tmp_path, capsys):
    """Drive main() with a stubbed run(); returns a callable."""

    def run_with(status: str, error: str = ""):
        class FakePipeline:
            def __init__(self, settings):
                self.settings = settings

            async def run(self, mode):
                return {"run_id": "run-test", "mode": mode, "status": status,
                        "error": error or None, "metrics": {}}

            build_report_text = staticmethod(pipeline_mod.Pipeline.build_report_text)

        monkeypatch.setattr(pipeline_mod, "Pipeline", FakePipeline)
        monkeypatch.setattr(Settings, "load",
                            classmethod(lambda cls: Settings(repo_root=tmp_path,
                                                             dry_run=True)))
        monkeypatch.setattr(logging_mod, "setup_logging", lambda *a, **k: None)
        monkeypatch.setattr("sys.argv", ["pipeline", "--mode", "full"])
        return pipeline_mod.main()

    return run_with, capsys


def test_failed_run_exits_nonzero(cli):
    run_with, capsys = cli
    with pytest.raises(SystemExit) as exc:
        run_with("FAILED", "Discovery returned 0 leads from 918 queries")
    assert exc.value.code == 1
    # The report (with the real cause) is printed first so the log is useful.
    out = capsys.readouterr().out
    assert "Discovery returned 0 leads" in out


def test_completed_run_exits_zero(cli):
    run_with, capsys = cli
    run_with("COMPLETED")            # must not raise SystemExit
    assert "COMPLETED" in capsys.readouterr().out


def test_stopped_run_exits_zero(cli):
    """/stop is a deliberate halt, not a failure — the job must stay green."""
    run_with, capsys = cli
    run_with("STOPPED")
    assert "STOPPED" in capsys.readouterr().out
