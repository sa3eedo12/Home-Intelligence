"""Home Assistant WebSocket → ``events.home`` Redis stream bridge.

The orchestrator's observers (``washer_observer``, ``vacuum_observer``,
``sleep_observer``, ``presence_observer``, ``coffee_observer``) and reactive
triggers in ``reactive_triggers.yaml`` all consume the ``events.home`` Redis
stream. Without something publishing to that stream, every observer starves
and every reactive rule sits idle forever.

This module is the missing producer. It connects to the Home Assistant
WebSocket API (``{HA_URL}/api/websocket``), authenticates with
``HA_TOKEN``, subscribes to ``state_changed`` events, and forwards each
event into ``events.home`` via :py:meth:`redis.Redis.xadd` using the same
envelope as :class:`home_agents_sdk.bus.EventBus` (``{"payload":
json.dumps(...)}``).

The forwarded payload mirrors HA's event shape so existing observers'
``extract_state_change`` helper continues to work unchanged::

    {
        "type": "state_changed",
        "entity_id": "...",
        "data": {
            "entity_id": "...",
            "old_state": {...},
            "new_state": {...}
        },
        "time_fired": "...",
        "origin": "ha_event_bridge"
    }

On the FIRST successful connection (process startup), the bridge also
fetches HA's REST history for the previous ``HA_BRIDGE_REPLAY_HOURS``
hours and replays each state transition through ``events.home`` *before*
the live subscription starts. This lets observers retroactively detect a
cycle that completed while the orchestrator was down — e.g. "you washed
clothes 20 minutes ago" still produces the appliance.cycle_completed
notification.

Reconnects with exponential backoff on disconnect or auth failure. Tracks
status on ``self.status`` so an admin endpoint can surface health.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import httpx
from home_agents_sdk.telemetry import get_logger

EVENTS_HOME = "events.home"
EVENTS_HOME_MAXLEN = 50_000
DEFAULT_BACKOFF_SCHEDULE = (1.0, 2.0, 5.0, 10.0, 30.0)
DEFAULT_PING_INTERVAL = 30.0
WEBSOCKET_OPEN_TIMEOUT = 10.0
DEFAULT_REPLAY_HOURS = 6
REPLAY_HTTP_TIMEOUT = 30.0

logger = get_logger("orchestrator.ha_event_bridge")


class _RedisLike(Protocol):
    async def xadd(
        self,
        name: str,
        fields: dict[str, str],
        *,
        maxlen: int | None = ...,
        approximate: bool = ...,
    ) -> str: ...


class _WebSocketLike(Protocol):
    async def send(self, data: str) -> None: ...
    async def recv(self) -> str | bytes: ...
    async def close(self) -> None: ...


WebSocketConnector = Any  # callable returning an async context manager around a websocket


@dataclass
class HaBridgeStatus:
    enabled: bool = True
    connected: bool = False
    last_connected_at: str | None = None
    last_disconnected_at: str | None = None
    last_event_at: str | None = None
    last_error: str | None = None
    events_forwarded: int = 0
    events_skipped: int = 0
    reconnect_attempts: int = 0
    history_replayed: bool = False
    history_replay_count: int = 0
    history_replay_error: str | None = None
    history: list[str] = field(default_factory=list)

    def snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "connected": self.connected,
            "last_connected_at": self.last_connected_at,
            "last_disconnected_at": self.last_disconnected_at,
            "last_event_at": self.last_event_at,
            "last_error": self.last_error,
            "events_forwarded": self.events_forwarded,
            "events_skipped": self.events_skipped,
            "reconnect_attempts": self.reconnect_attempts,
            "history_replayed": self.history_replayed,
            "history_replay_count": self.history_replay_count,
            "history_replay_error": self.history_replay_error,
            "recent_history": list(self.history[-10:]),
        }

    def note(self, line: str) -> None:
        self.history.append(f"{datetime.now(UTC).isoformat()} {line}")
        if len(self.history) > 100:
            self.history = self.history[-100:]


def _ws_url_for(ha_url: str) -> str:
    base = ha_url.rstrip("/")
    if base.startswith("https://"):
        return "wss://" + base[len("https://") :] + "/api/websocket"
    if base.startswith("http://"):
        return "ws://" + base[len("http://") :] + "/api/websocket"
    return base + "/api/websocket"


class HaEventBridge:
    """Long-running task that forwards HA state-changed events into Redis."""

    def __init__(
        self,
        *,
        redis: _RedisLike,
        ha_url: str | None,
        ha_token: str | None,
        stream: str = EVENTS_HOME,
        maxlen: int = EVENTS_HOME_MAXLEN,
        backoff_schedule: tuple[float, ...] = DEFAULT_BACKOFF_SCHEDULE,
        ws_connector: WebSocketConnector | None = None,
        replay_hours: int = DEFAULT_REPLAY_HOURS,
        history_fetcher: Any | None = None,
    ) -> None:
        self._redis = redis
        self._ha_url = (ha_url or "").strip()
        self._ha_token = (ha_token or "").strip()
        self._stream = stream
        self._maxlen = maxlen
        self._backoff = backoff_schedule
        self._ws_connector = ws_connector
        self._replay_hours = max(0, int(replay_hours))
        self._history_fetcher = history_fetcher
        self._first_session = True
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self.status = HaBridgeStatus(
            enabled=bool(self._ha_url and self._ha_token),
        )

    async def start(self) -> None:
        if not self.status.enabled:
            logger.info(
                "ha_bridge_disabled_missing_config",
                ha_url_set=bool(self._ha_url),
                ha_token_set=bool(self._ha_token),
            )
            self.status.note("disabled: HA_URL or HA_TOKEN missing")
            return
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="ha-event-bridge")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None

    async def _run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            try:
                await self._session()
                attempt = 0  # successful session — reset backoff
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                msg = f"{type(exc).__name__}: {exc}"
                self.status.last_error = msg
                self.status.note(f"session_failed: {msg}")
                logger.warning("ha_bridge_session_failed", error=msg)
            self.status.connected = False
            self.status.last_disconnected_at = datetime.now(UTC).isoformat()
            if self._stop.is_set():
                return
            self.status.reconnect_attempts += 1
            delay = self._backoff[min(attempt, len(self._backoff) - 1)]
            attempt += 1
            self.status.note(f"reconnect in {delay}s (attempt {attempt})")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except TimeoutError:
                pass

    async def _session(self) -> None:
        connector = self._ws_connector or _default_ws_connector
        url = _ws_url_for(self._ha_url)
        logger.info("ha_bridge_connecting", url=url)
        async with connector(url) as ws:
            await self._authenticate(ws)
            await self._subscribe_state_changed(ws)
            self.status.connected = True
            self.status.last_connected_at = datetime.now(UTC).isoformat()
            self.status.last_error = None
            self.status.note("connected + subscribed state_changed")
            logger.info("ha_bridge_connected")
            if self._first_session and self._replay_hours > 0:
                await self._replay_history()
                self._first_session = False
            await self._consume(ws)

    async def _replay_history(self) -> None:
        """On first connect, hydrate observers with the last N hours of HA state.

        Synthesizes ``state_changed`` events from HA's ``/api/history/period``
        REST response. Failures here are logged and swallowed — replay is best
        effort and must never block the live subscription path.
        """
        since = datetime.now(UTC) - timedelta(hours=self._replay_hours)
        self.status.note(f"history_replay_started since={since.isoformat()}")
        logger.info("ha_bridge_history_replay_started", since=since.isoformat())
        try:
            histories = await self._fetch_history(since)
        except Exception as exc:
            msg = f"{type(exc).__name__}: {exc}"
            self.status.history_replay_error = msg
            self.status.note(f"history_replay_failed: {msg}")
            logger.warning("ha_bridge_history_replay_failed", error=msg)
            return

        replayed = 0
        for entity_history in histories:
            entries = [s for s in entity_history if isinstance(s, dict)]
            if len(entries) < 2:
                continue
            entries.sort(key=lambda s: str(s.get("last_changed") or s.get("last_updated") or ""))
            for prev, curr in zip(entries, entries[1:], strict=False):
                if prev.get("state") == curr.get("state"):
                    continue
                replayed += await self._forward_replayed_pair(prev, curr)
        self.status.history_replayed = True
        self.status.history_replay_count = replayed
        self.status.note(f"history_replay_done count={replayed}")
        logger.info("ha_bridge_history_replay_done", count=replayed)

    async def _fetch_history(self, since: datetime) -> list[list[dict[str, Any]]]:
        if self._history_fetcher is not None:
            return await self._history_fetcher(since)
        url = self._ha_url.rstrip("/") + "/api/history/period/" + since.isoformat()
        async with httpx.AsyncClient(timeout=REPLAY_HTTP_TIMEOUT) as client:
            resp = await client.get(
                url,
                headers={
                    "Authorization": f"Bearer {self._ha_token}",
                    "Content-Type": "application/json",
                },
                params={"minimal_response": "false", "significant_changes_only": "false"},
            )
            resp.raise_for_status()
            data = resp.json()
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, list)]

    async def _forward_replayed_pair(
        self, prev: dict[str, Any], curr: dict[str, Any]
    ) -> int:
        entity_id = str(curr.get("entity_id") or prev.get("entity_id") or "").strip()
        if not entity_id:
            return 0
        time_fired = str(
            curr.get("last_changed") or curr.get("last_updated") or datetime.now(UTC).isoformat()
        )
        payload = {
            "type": "state_changed",
            "entity_id": entity_id,
            "data": {
                "entity_id": entity_id,
                "old_state": prev,
                "new_state": curr,
            },
            "time_fired": time_fired,
            "origin": "ha_event_bridge_replay",
        }
        try:
            await self._redis.xadd(
                self._stream,
                {"payload": json.dumps(payload, default=str)},
                maxlen=self._maxlen,
                approximate=True,
            )
        except Exception as exc:
            logger.warning(
                "ha_bridge_replay_xadd_failed",
                error=str(exc),
                entity=entity_id,
            )
            return 0
        return 1

    async def _authenticate(self, ws: _WebSocketLike) -> None:
        msg = await self._recv_json(ws)
        if msg.get("type") != "auth_required":
            raise RuntimeError(f"unexpected first message from HA: {msg.get('type')}")
        await ws.send(json.dumps({"type": "auth", "access_token": self._ha_token}))
        msg = await self._recv_json(ws)
        if msg.get("type") == "auth_invalid":
            raise PermissionError(msg.get("message") or "HA rejected HA_TOKEN")
        if msg.get("type") != "auth_ok":
            raise RuntimeError(f"unexpected auth response: {msg.get('type')}")

    async def _subscribe_state_changed(self, ws: _WebSocketLike) -> None:
        sub_id = 1
        await ws.send(
            json.dumps(
                {
                    "id": sub_id,
                    "type": "subscribe_events",
                    "event_type": "state_changed",
                }
            )
        )
        msg = await self._recv_json(ws)
        if not (
            msg.get("type") == "result"
            and msg.get("id") == sub_id
            and msg.get("success") is True
        ):
            raise RuntimeError(f"HA refused subscribe_events: {msg}")

    async def _consume(self, ws: _WebSocketLike) -> None:
        while not self._stop.is_set():
            msg = await self._recv_json(ws)
            if msg.get("type") != "event":
                # ignore pong/result/etc. (we don't emit pings, but HA might)
                continue
            event = msg.get("event") or {}
            if event.get("event_type") != "state_changed":
                continue
            await self._forward(event)

    async def _forward(self, event: dict[str, Any]) -> None:
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        entity_id = str(data.get("entity_id") or "").strip()
        if not entity_id:
            self.status.events_skipped += 1
            return
        payload = {
            "type": "state_changed",
            "entity_id": entity_id,
            "data": data,
            "time_fired": event.get("time_fired") or datetime.now(UTC).isoformat(),
            "origin": "ha_event_bridge",
        }
        try:
            await self._redis.xadd(
                self._stream,
                {"payload": json.dumps(payload, default=str)},
                maxlen=self._maxlen,
                approximate=True,
            )
        except Exception as exc:
            logger.warning("ha_bridge_xadd_failed", error=str(exc), entity=entity_id)
            self.status.events_skipped += 1
            return
        self.status.events_forwarded += 1
        self.status.last_event_at = payload["time_fired"]

    @staticmethod
    async def _recv_json(ws: _WebSocketLike) -> dict[str, Any]:
        raw = await ws.recv()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"invalid JSON from HA: {exc}") from None
        if not isinstance(decoded, dict):
            raise RuntimeError(f"unexpected non-object frame from HA: {type(decoded).__name__}")
        return decoded


def _default_ws_connector(url: str):  # pragma: no cover - thin wrapper around websockets lib
    try:
        import websockets
    except ImportError as exc:
        raise RuntimeError(
            "ha_event_bridge requires the 'websockets' package; "
            "install with `pip install websockets`"
        ) from exc
    return websockets.connect(
        url,
        open_timeout=WEBSOCKET_OPEN_TIMEOUT,
        ping_interval=DEFAULT_PING_INTERVAL,
        max_size=8 * 1024 * 1024,
    )


def build_from_env(redis: _RedisLike) -> HaEventBridge:
    """Construct a bridge using ``HA_URL`` / ``HA_TOKEN`` environment variables."""
    replay_hours_raw = os.environ.get("HA_BRIDGE_REPLAY_HOURS", str(DEFAULT_REPLAY_HOURS))
    try:
        replay_hours = int(replay_hours_raw)
    except ValueError:
        replay_hours = DEFAULT_REPLAY_HOURS
    return HaEventBridge(
        redis=redis,
        ha_url=os.environ.get("HA_URL"),
        ha_token=os.environ.get("HA_TOKEN"),
        replay_hours=replay_hours,
    )
