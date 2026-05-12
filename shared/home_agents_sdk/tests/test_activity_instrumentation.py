from __future__ import annotations

import json
from pathlib import Path

import fakeredis.aioredis
import pytest
from fastapi.testclient import TestClient

from home_agents_sdk.agent_base import ACTIVITY_STREAM, build_app
from home_agents_sdk.tools import clear_tools, tool


def _write_manifest(tmp_path: Path, agent: str, capability: str = "ping") -> Path:
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        f"""
agent: {agent}
version: 0.1.0
capabilities:
  - id: {capability}
    description: test capability
    side_effects: false
""".strip(),
        encoding="utf-8",
    )
    return manifest


def test_invoke_succeeds_when_redis_url_unset(monkeypatch, tmp_path: Path) -> None:
    """Without REDIS_URL, /invoke must still work and never crash on telemetry."""
    monkeypatch.delenv("REDIS_URL", raising=False)
    clear_tools()

    @tool("ping")
    def ping() -> dict[str, str]:
        return {"pong": "ok"}

    app = build_app("test-agent", str(_write_manifest(tmp_path, "test-agent")))

    with TestClient(app) as client:
        resp = client.post("/invoke", json={"capability": "ping", "payload": {}})

    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "result": {"pong": "ok"}, "error": None}


def test_invoke_publishes_activity_to_redis(monkeypatch, tmp_path: Path) -> None:
    """When REDIS_URL is set and reachable, /invoke publishes started+ok events."""
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setenv("REDIS_URL", "redis://fake:6379/0")

    def _from_url(_url: str, **_kw):
        return fake

    monkeypatch.setattr("home_agents_sdk.agent_base.Redis.from_url", staticmethod(_from_url))

    clear_tools()

    @tool("ping")
    def ping() -> dict[str, str]:
        return {"pong": "ok"}

    app = build_app("activity-agent", str(_write_manifest(tmp_path, "activity-agent")))

    with TestClient(app) as client:
        resp = client.post("/invoke", json={"capability": "ping", "payload": {}})
    assert resp.status_code == 200

    import asyncio

    async def _read() -> list[dict]:
        raw = await fake.xrange(ACTIVITY_STREAM)
        return [json.loads(fields["payload"]) for _msg_id, fields in raw]

    events = asyncio.get_event_loop().run_until_complete(_read())
    statuses = [e["status"] for e in events]
    assert "started" in statuses
    assert "ok" in statuses
    ok_events = [e for e in events if e["status"] == "ok"]
    assert ok_events[-1]["agent"] == "activity-agent"
    assert ok_events[-1]["capability"] == "ping"
    assert ok_events[-1]["duration_ms"] >= 0


def test_invoke_publishes_error_status_on_failure(monkeypatch, tmp_path: Path) -> None:
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setenv("REDIS_URL", "redis://fake:6379/0")
    monkeypatch.setattr(
        "home_agents_sdk.agent_base.Redis.from_url", staticmethod(lambda *_a, **_kw: fake)
    )

    clear_tools()

    @tool("explode")
    def explode() -> dict[str, str]:
        raise ValueError("boom")

    app = build_app("err-agent", str(_write_manifest(tmp_path, "err-agent", "explode")))

    with TestClient(app) as client:
        resp = client.post("/invoke", json={"capability": "explode", "payload": {}})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "boom" in (body["error"] or "")

    import asyncio

    async def _read() -> list[dict]:
        raw = await fake.xrange(ACTIVITY_STREAM)
        return [json.loads(fields["payload"]) for _msg_id, fields in raw]

    events = asyncio.get_event_loop().run_until_complete(_read())
    error_events = [e for e in events if e["status"] == "error"]
    assert len(error_events) == 1
    assert error_events[0]["error"] == "boom"


def test_invoke_silently_tolerates_redis_xadd_failure(monkeypatch, tmp_path: Path) -> None:
    """If publishing fails after connect, /invoke must still return ok."""

    class FlakyRedis:
        async def ping(self) -> bool:
            return True

        async def xadd(self, *args, **kwargs):  # noqa: ANN001, ANN002
            raise RuntimeError("redis exploded")

        async def aclose(self) -> None:
            return None

    monkeypatch.setenv("REDIS_URL", "redis://fake:6379/0")
    monkeypatch.setattr(
        "home_agents_sdk.agent_base.Redis.from_url",
        staticmethod(lambda *_a, **_kw: FlakyRedis()),
    )

    clear_tools()

    @tool("ping")
    def ping() -> dict[str, str]:
        return {"pong": "ok"}

    app = build_app("flaky-agent", str(_write_manifest(tmp_path, "flaky-agent")))

    with TestClient(app) as client:
        resp = client.post("/invoke", json={"capability": "ping", "payload": {}})

    assert resp.status_code == 200
    assert resp.json()["ok"] is True


@pytest.fixture(autouse=True)
def _reset_tools_between_tests():
    yield
    clear_tools()
