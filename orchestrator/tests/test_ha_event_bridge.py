from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from orchestrator.ha_event_bridge import (
    EVENTS_HOME,
    HaEventBridge,
    _ws_url_for,
    build_from_env,
)


class FakeRedis:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str], dict[str, Any]]] = []
        self.fail_next = False

    async def xadd(
        self,
        name: str,
        fields: dict[str, str],
        *,
        maxlen: int | None = None,
        approximate: bool = True,
    ) -> str:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("redis went away")
        self.calls.append((name, fields, {"maxlen": maxlen, "approximate": approximate}))
        return f"{len(self.calls)}-0"


class FakeWs:
    def __init__(self, frames: list[str]) -> None:
        self._incoming: asyncio.Queue[str] = asyncio.Queue()
        for frame in frames:
            self._incoming.put_nowait(frame)
        self.sent: list[dict[str, Any]] = []
        self.closed = False
        self._stalled = asyncio.Event()

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))

    async def recv(self) -> str:
        if self._incoming.empty():
            # Block until cancelled by stop() — mimics a real socket waiting for events
            await self._stalled.wait()
        return await self._incoming.get()

    async def close(self) -> None:
        self.closed = True
        self._stalled.set()


@asynccontextmanager
async def _ws_factory(ws: FakeWs) -> AsyncIterator[FakeWs]:
    try:
        yield ws
    finally:
        await ws.close()


def _ws_connector(ws: FakeWs):
    def factory(url: str):
        return _ws_factory(ws)

    return factory


def _state_changed_frame(entity_id: str, *, state: str = "running") -> str:
    return json.dumps(
        {
            "id": 1,
            "type": "event",
            "event": {
                "event_type": "state_changed",
                "data": {
                    "entity_id": entity_id,
                    "old_state": {"state": "idle", "attributes": {}},
                    "new_state": {"state": state, "attributes": {"friendly_name": "Washer"}},
                },
                "time_fired": "2026-05-13T19:00:00Z",
            },
        }
    )


def _auth_handshake_frames() -> list[str]:
    return [
        json.dumps({"type": "auth_required", "ha_version": "2026.5.0"}),
        json.dumps({"type": "auth_ok"}),
        json.dumps({"id": 1, "type": "result", "success": True, "result": None}),
    ]


def test_build_from_env_disabled_when_token_missing(monkeypatch) -> None:
    monkeypatch.delenv("HA_URL", raising=False)
    monkeypatch.delenv("HA_TOKEN", raising=False)
    bridge = build_from_env(redis=FakeRedis())
    assert bridge.status.enabled is False


def test_build_from_env_enabled_when_both_set(monkeypatch) -> None:
    monkeypatch.setenv("HA_URL", "http://homeassistant.local:8123")
    monkeypatch.setenv("HA_TOKEN", "abc")
    bridge = build_from_env(redis=FakeRedis())
    assert bridge.status.enabled is True


def test_ws_url_translation() -> None:
    assert _ws_url_for("http://homeassistant.local:8123") == "ws://homeassistant.local:8123/api/websocket"
    assert _ws_url_for("https://ha.example.com/") == "wss://ha.example.com/api/websocket"
    assert _ws_url_for("homeassistant.local:8123") == "homeassistant.local:8123/api/websocket"


async def test_disabled_bridge_is_a_no_op() -> None:
    bridge = HaEventBridge(redis=FakeRedis(), ha_url="", ha_token="")
    assert bridge.status.enabled is False
    await bridge.start()
    assert bridge._task is None
    await bridge.stop()


async def test_full_session_authenticates_subscribes_and_forwards() -> None:
    redis = FakeRedis()
    ws = FakeWs(_auth_handshake_frames() + [_state_changed_frame("sensor.washing_machine")])
    bridge = HaEventBridge(
        redis=redis,
        ha_url="http://ha.local:8123",
        ha_token="secret",
        ws_connector=_ws_connector(ws),
    )
    await bridge.start()
    for _ in range(20):
        if redis.calls:
            break
        await asyncio.sleep(0.01)
    await bridge.stop()

    assert ws.sent[0] == {"type": "auth", "access_token": "secret"}
    assert ws.sent[1] == {"id": 1, "type": "subscribe_events", "event_type": "state_changed"}

    assert len(redis.calls) == 1
    stream, fields, opts = redis.calls[0]
    assert stream == EVENTS_HOME
    assert opts["maxlen"] == 50_000 and opts["approximate"] is True
    payload = json.loads(fields["payload"])
    assert payload["type"] == "state_changed"
    assert payload["entity_id"] == "sensor.washing_machine"
    assert payload["data"]["new_state"]["state"] == "running"
    assert payload["origin"] == "ha_event_bridge"

    snap = bridge.status.snapshot()
    assert snap["events_forwarded"] == 1
    assert snap["last_event_at"] == "2026-05-13T19:00:00Z"


async def test_skips_non_state_changed_and_missing_entity() -> None:
    redis = FakeRedis()
    frames = _auth_handshake_frames() + [
        json.dumps({"id": 1, "type": "event", "event": {"event_type": "call_service"}}),
        json.dumps({
            "id": 1,
            "type": "event",
            "event": {
                "event_type": "state_changed",
                "data": {"entity_id": "", "new_state": {"state": "x"}},
                "time_fired": "2026-05-13T19:00:00Z",
            },
        }),
        _state_changed_frame("light.kitchen"),
    ]
    ws = FakeWs(frames)
    bridge = HaEventBridge(
        redis=redis,
        ha_url="http://ha.local:8123",
        ha_token="t",
        ws_connector=_ws_connector(ws),
    )
    await bridge.start()
    for _ in range(20):
        if redis.calls:
            break
        await asyncio.sleep(0.01)
    await bridge.stop()

    assert len(redis.calls) == 1
    payload = json.loads(redis.calls[0][1]["payload"])
    assert payload["entity_id"] == "light.kitchen"
    assert bridge.status.events_skipped >= 1


async def test_auth_invalid_records_error_and_reconnects(monkeypatch) -> None:
    redis = FakeRedis()
    ws = FakeWs([
        json.dumps({"type": "auth_required"}),
        json.dumps({"type": "auth_invalid", "message": "Invalid access token"}),
    ])
    bridge = HaEventBridge(
        redis=redis,
        ha_url="http://ha.local:8123",
        ha_token="bad",
        backoff_schedule=(0.05,),
        ws_connector=_ws_connector(ws),
    )
    await bridge.start()
    await asyncio.sleep(0.15)
    await bridge.stop()
    assert "Invalid access token" in (bridge.status.last_error or "")
    assert bridge.status.reconnect_attempts >= 1
    assert bridge.status.connected is False


async def test_xadd_failure_does_not_crash_session() -> None:
    redis = FakeRedis()
    redis.fail_next = True
    ws = FakeWs(_auth_handshake_frames() + [
        _state_changed_frame("sensor.dryer"),
        _state_changed_frame("sensor.washer", state="active"),
    ])
    bridge = HaEventBridge(
        redis=redis,
        ha_url="http://ha.local:8123",
        ha_token="t",
        ws_connector=_ws_connector(ws),
    )
    await bridge.start()
    for _ in range(40):
        if len(redis.calls) >= 1:
            break
        await asyncio.sleep(0.01)
    await bridge.stop()
    # First call raised (skipped), second succeeded
    assert len(redis.calls) == 1
    assert bridge.status.events_skipped >= 1
    assert bridge.status.events_forwarded == 1


async def test_invalid_json_frame_recovers_via_reconnect() -> None:
    redis = FakeRedis()
    ws_bad = FakeWs([
        json.dumps({"type": "auth_required"}),
        json.dumps({"type": "auth_ok"}),
        json.dumps({"id": 1, "type": "result", "success": True}),
        "this-is-not-json",
    ])
    bridge = HaEventBridge(
        redis=redis,
        ha_url="http://ha.local:8123",
        ha_token="t",
        backoff_schedule=(0.05,),
        ws_connector=_ws_connector(ws_bad),
    )
    await bridge.start()
    await asyncio.sleep(0.2)
    await bridge.stop()
    assert "invalid JSON" in (bridge.status.last_error or "")
    assert bridge.status.reconnect_attempts >= 1
