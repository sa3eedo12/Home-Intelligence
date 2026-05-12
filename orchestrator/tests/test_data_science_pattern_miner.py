from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator.data_science.pattern_miner import PatternMiner
from tests.data_science_fakes import FakePool


class PatternConn:
    def __init__(self, rows: list[dict]) -> None:
        self.fetch = AsyncMock(return_value=rows)
        self.fetchrow = AsyncMock(return_value=None)
        self.execute = AsyncMock(return_value="UPDATE 1")


def _cluster_rows() -> list[dict]:
    start = datetime(2026, 5, 11, 7, 5, tzinfo=UTC)
    rows = []
    for index in range(4):
        rows.append(
            {
                "id": index + 1,
                "ts": start + timedelta(minutes=index * 10),
                "agent": "home_automation",
                "capability": "coffee_started",
                "summary": "Coffee machine started",
                "payload": {},
            }
        )
    rows.append(
        {
            "id": 99,
            "ts": datetime(2026, 5, 12, 20, tzinfo=UTC),
            "agent": "system_health",
            "capability": "check",
            "summary": "Noise",
            "payload": {},
        }
    )
    return rows


@pytest.mark.asyncio
async def test_pattern_miner_stores_high_purity_cluster_as_habit() -> None:
    conn = PatternConn(_cluster_rows())
    graph = SimpleNamespace(
        list_habits=AsyncMock(return_value=[]), put_habit=AsyncMock(return_value={"id": 7})
    )
    event_log = SimpleNamespace(record_event=AsyncMock())

    result = await PatternMiner(FakePool(conn), graph, event_log_store=event_log).run()

    assert result["stored"] == 1
    assert len(result["candidates"]) == 1
    candidate = result["candidates"][0]
    assert candidate["subject"] == "home_automation.coffee_started"
    assert candidate["support_count"] == 4
    assert candidate["evidence_event_ids"] == [1, 2, 3, 4]
    graph.put_habit.assert_awaited_once()
    habit_kwargs = graph.put_habit.await_args.kwargs
    assert habit_kwargs["source"] == "pattern_miner"
    assert habit_kwargs["pattern"]["attributes"]["support"] == 4
    event_log.record_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_pattern_miner_increments_existing_habit_support() -> None:
    conn = PatternConn(_cluster_rows())
    graph = SimpleNamespace(
        list_habits=AsyncMock(
            return_value=[
                {
                    "id": 7,
                    "subject": "home_automation.coffee_started",
                    "pattern": {"attributes": {"support": 2}},
                    "confidence": 0.4,
                    "source": "pattern_miner",
                }
            ]
        ),
        patch_row=AsyncMock(return_value={"id": 7}),
    )

    result = await PatternMiner(FakePool(conn), graph).run()

    assert result["stored"] == 1
    graph.patch_row.assert_awaited_once()
    patch = graph.patch_row.await_args.args[2]
    assert patch["pattern"]["attributes"]["support"] == 6
