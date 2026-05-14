from __future__ import annotations

import json

import fakeredis.aioredis
import pytest

from tools import core


@pytest.mark.asyncio
async def test_anomaly_check_publishes_events_system_on_anomaly(monkeypatch) -> None:
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(core, "_redis_client", lambda: fake)
    monkeypatch.setattr(core.publish_helper, "_redis_client", lambda: fake)
    monkeypatch.setattr(core, "scan", lambda: {"metrics": {"cpu_pct": 25.0}})
    for value in [10.0, 11.0, 9.5, 10.5, 10.1, 10.3]:
        await fake.rpush("metrics:cpu_pct", value)

    # Use absolute_floor=0 so this test stays focused on the z-score detection;
    # the production default for cpu_pct adds a floor (currently 30%) so that
    # tiny baselines don't page on a small spike.
    result = await core.anomaly_check(
        metric="cpu_pct", threshold=2.0, absolute_floor=0.0
    )

    assert result["is_anomaly"] is True
    rows = await fake.xrange("events.system")
    assert len(rows) == 1
    payload = json.loads(rows[0][1]["payload"])
    assert payload["type"] == "system.metric_breach"
    assert payload["agent"] == "system_health"
    assert payload["metric"] == "cpu_pct"
    assert payload["severity"] == "warn"
    assert payload["threshold"] == 2.0


@pytest.mark.asyncio
async def test_anomaly_check_skips_low_absolute_value(monkeypatch) -> None:
    """Regression: a 3% CPU spike on an idle baseline shouldn't page the user
    even though it's statistically a >2.5σ outlier."""
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(core, "_redis_client", lambda: fake)
    monkeypatch.setattr(core.publish_helper, "_redis_client", lambda: fake)
    monkeypatch.setattr(core, "scan", lambda: {"metrics": {"cpu_pct": 3.0}})
    for value in [0.5, 0.6, 0.4, 0.5, 0.5, 0.6]:
        await fake.rpush("metrics:cpu_pct", value)

    # Default cpu_pct floor is 30% — 3% absolute should be suppressed even
    # though it's a huge z-score relative to the 0.5% baseline.
    result = await core.anomaly_check(metric="cpu_pct", threshold=2.0)

    assert result["is_anomaly"] is False
    rows = await fake.xrange("events.system")
    assert len(rows) == 0
