from __future__ import annotations

import json

import fakeredis.aioredis
import pytest

from tools import core


@pytest.fixture
def fake_redis(monkeypatch):
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(core.publish_helper, "_redis_client", lambda: fake)
    return fake


@pytest.mark.asyncio
async def test_validate_backup_publishes_stale_backup_warning(fake_redis, monkeypatch) -> None:
    monkeypatch.setattr(
        core,
        "_validate_backup_config",
        lambda _config_path: {
            "ok": False,
            "checks": [
                {
                    "name": "nas",
                    "path": "/backups/nas",
                    "ok": False,
                    "age_hours": 72.0,
                    "max_age_hours": 36,
                    "entries": 1,
                    "glob_ok": True,
                }
            ],
        },
    )

    result = await core.validate_backup("ignored.yaml")

    assert result["ok"] is False
    rows = await fake_redis.xrange("events.system")
    payload = json.loads(rows[0][1]["payload"])
    assert payload["agent"] == "storage_backup"
    assert payload["metric"] == "backup.age_hours"
    assert payload["value"] == 72.0
    assert payload["threshold"] == 36


@pytest.mark.asyncio
async def test_summarize_storage_publishes_disk_warning(fake_redis, monkeypatch) -> None:
    monkeypatch.setattr(
        core,
        "disk_usage",
        lambda threshold_pct=85.0: {
            "items": [
                {
                    "mount": "/data",
                    "used_pct": 91.5,
                    "is_high": 91.5 >= threshold_pct,
                    "free": 100,
                    "total": 1000,
                }
            ]
        },
    )

    result = await core.summarize_storage()

    assert "above 90%" in result["summary"]
    rows = await fake_redis.xrange("events.system")
    payload = json.loads(rows[0][1]["payload"])
    assert payload["metric"] == "disk.used_pct"
    assert payload["value"] == 91.5
    assert payload["threshold"] == 90.0
