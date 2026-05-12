from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator.data_science.maintenance import MaintenanceJob
from tests.data_science_fakes import FakePool


class MaintenanceConn:
    def __init__(self) -> None:
        self.execute = AsyncMock(side_effect=["VACUUM", "INSERT 0 4", "DELETE 4", "DELETE 2"])


@pytest.mark.asyncio
async def test_maintenance_trims_archives_vacuums_and_prunes() -> None:
    redis = SimpleNamespace(xtrim=AsyncMock(return_value=1))
    conn = MaintenanceConn()
    event_log = SimpleNamespace(record_event=AsyncMock())

    result = await MaintenanceJob(FakePool(conn), redis, event_log_store=event_log).run()

    assert result["status"] == "ok"
    assert result["archived_rows"] == 4
    assert result["deleted_event_rows"] == 4
    assert result["deleted_brief_rows"] == 2
    assert result["reclaimed_space_estimate_bytes"] > 0
    assert redis.xtrim.await_count == 5
    assert conn.execute.await_count == 4
    event_log.record_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_maintenance_continues_when_redis_unavailable() -> None:
    conn = MaintenanceConn()
    event_log = SimpleNamespace(record_event=AsyncMock())

    result = await MaintenanceJob(FakePool(conn), None, event_log_store=event_log).run()

    assert result["status"] == "partial"
    assert result["archived_rows"] == 4
    assert result["errors"][0]["step"] == "redis_trim"
