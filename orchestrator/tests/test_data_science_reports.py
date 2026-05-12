from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator.data_science.reports import ReportGenerator
from tests.data_science_fakes import FakePool

TEST_DATA_ROOT = Path(__file__).resolve().parents[1] / ".test-data"
REPORTS_DIR = TEST_DATA_ROOT / "reports"
REPORTS_LLM_DIR = TEST_DATA_ROOT / "reports-llm"


def _file_exists(path: str) -> bool:
    return os.path.exists(path)


def _dirname(path: str) -> str:
    return os.path.dirname(path)


class ReportsConn:
    def __init__(self) -> None:
        self.fetchval = AsyncMock(side_effect=[10, 2, 1, 1, 1])
        self.fetch = AsyncMock(
            side_effect=[
                [{"agent": "home_automation", "capability": "turn_on", "count": 5}],
                [{"agent": "system_health", "capability": "anomaly_error", "count": 2}],
                [
                    {
                        "subject": "home_automation.turn_on",
                        "confidence": 0.8,
                        "source": "pattern_miner",
                    }
                ],
                [
                    {"status": "accepted", "title": "Tune washer alert"},
                    {"status": "dismissed", "title": "Ignore noisy event"},
                ],
                [{"phase": "pattern_mining", "count": 2}],
            ]
        )
        self.execute = AsyncMock(return_value="INSERT 0 1")


@pytest.mark.asyncio
async def test_weekly_report_writes_markdown_and_indexes_row(monkeypatch) -> None:
    monkeypatch.setenv("LLM_REPORT_NARRATION", "false")
    reports_dir = REPORTS_DIR
    shutil.rmtree(TEST_DATA_ROOT, ignore_errors=True)
    conn = ReportsConn()
    event_log = SimpleNamespace(record_event=AsyncMock())

    result = await ReportGenerator(
        FakePool(conn), reports_dir=reports_dir, event_log_store=event_log
    ).weekly_report()

    path = result["path"]
    try:
        assert await asyncio.to_thread(_file_exists, path)
        assert await asyncio.to_thread(_dirname, path) == str(reports_dir)
        assert "## Headline metrics" in result["markdown"]
        assert "## Knowledge graph growth" in result["markdown"]
        assert "Tune washer alert" in result["markdown"]
        conn.execute.assert_awaited_once()
        assert conn.execute.await_args.args[1] == "weekly"
        event_log.record_event.assert_awaited_once()
    finally:
        shutil.rmtree(TEST_DATA_ROOT, ignore_errors=True)


@pytest.mark.asyncio
async def test_monthly_report_can_include_llm_narrative(monkeypatch) -> None:
    monkeypatch.setenv("LLM_REPORT_NARRATION", "true")
    reports_dir = REPORTS_LLM_DIR
    shutil.rmtree(TEST_DATA_ROOT, ignore_errors=True)
    llm = SimpleNamespace(
        chat=AsyncMock(
            return_value={
                "message": {"content": "Narrative one.\n\nNarrative two.\n\nNarrative three."}
            }
        )
    )

    event_log = SimpleNamespace(record_event=AsyncMock())
    result = await ReportGenerator(
        FakePool(ReportsConn()), reports_dir=reports_dir, llm=llm, event_log_store=event_log
    ).monthly_report()

    try:
        assert result["markdown"].startswith("Narrative one.")
        llm.chat.assert_awaited_once()
    finally:
        shutil.rmtree(TEST_DATA_ROOT, ignore_errors=True)
