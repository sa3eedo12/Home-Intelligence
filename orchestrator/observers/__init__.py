from __future__ import annotations

import asyncio
import json
from abc import ABC, abstractmethod
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from home_agents_sdk.bus import EventBus
from home_agents_sdk.event_log import EventLogStore
from home_agents_sdk.telemetry import get_logger

OBSERVED_STREAM = "events.observed"
ACTIVITY_STREAM = "events.activity"
OBSERVER_MAXLEN = 10000

logger = get_logger("orchestrator.observers")


class Observer(ABC):
    name: str = "observer"
    subscribed_streams: list[str] = ["events.home"]

    def __init__(self) -> None:
        self.bus: EventBus | None = None
        self.event_log_store: EventLogStore | None = None
        self.registry: Any | None = None
        self.logger = get_logger(f"orchestrator.observers.{self.name}")

    def bind(
        self,
        *,
        bus: EventBus,
        event_log_store: EventLogStore,
        registry: Any | None = None,
    ) -> None:
        self.bus = bus
        self.event_log_store = event_log_store
        self.registry = registry

    @abstractmethod
    async def handle(self, payload: dict[str, Any]) -> None:
        """Handle one decoded stream payload."""

    async def dispatch_capability(
        self,
        agent: str,
        capability: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if self.registry is None:
            return {}
        try:
            result = await self.registry.dispatch(agent, capability, payload)
        except Exception as exc:
            self.logger.warning(
                "observer_dispatch_failed",
                agent=agent,
                capability=capability,
                error=str(exc),
            )
            return {}
        if isinstance(result, dict) and "result" in result:
            nested = result.get("result")
            return nested if isinstance(nested, dict) else {}
        return result if isinstance(result, dict) else {}

    async def emit_event(self, kind: str, summary: str, payload: dict[str, Any]) -> None:
        ts = datetime.now(UTC).isoformat()
        agent = f"observer.{self.name}"
        clean_payload = _jsonable(payload)
        if self.event_log_store is not None:
            try:
                result = await self.event_log_store.record_event(
                    agent=agent,
                    capability=kind,
                    summary=summary,
                    payload=clean_payload,
                    ts=ts,
                )
                if not result.get("ok"):
                    self.logger.warning(
                        "observer_event_log_write_failed",
                        kind=kind,
                        error=result.get("error"),
                    )
            except Exception as exc:
                self.logger.warning("observer_event_log_write_failed", kind=kind, error=str(exc))

        observed_payload = {
            "agent": agent,
            "kind": kind,
            "summary": summary,
            "payload": clean_payload,
            "ts": ts,
        }
        await self._publish(OBSERVED_STREAM, observed_payload)
        await self._publish(
            ACTIVITY_STREAM,
            {
                "agent": agent,
                "capability": kind,
                "status": "ok",
                "duration_ms": 0.0,
                "ts": ts,
                "extra": {"summary": summary, "event_log_recorded": True},
            },
        )

    async def _publish(self, stream: str, payload: dict[str, Any]) -> None:
        if self.bus is None:
            return
        try:
            client = getattr(self.bus, "client", None)
            if client is not None:
                await client.xadd(
                    stream,
                    {"payload": json.dumps(payload, default=str)},
                    maxlen=OBSERVER_MAXLEN,
                    approximate=True,
                )
            else:
                await self.bus.publish(stream, payload)
        except Exception as exc:
            self.logger.warning("observer_publish_failed", stream=stream, error=str(exc))


class ObserverRunner:
    def __init__(
        self,
        *,
        bus: EventBus,
        event_log_store: EventLogStore,
        observers: Iterable[Observer],
        registry: Any | None = None,
    ) -> None:
        self._bus = bus
        self._event_log_store = event_log_store
        self._observers = list(observers)
        self._registry = registry
        self._tasks: list[asyncio.Task[None]] = []
        self._by_stream: dict[str, list[Observer]] = {}
        for observer in self._observers:
            observer.bind(bus=bus, event_log_store=event_log_store, registry=registry)
            for stream in observer.subscribed_streams or ["events.home"]:
                self._by_stream.setdefault(stream, []).append(observer)

    async def start(self) -> None:
        if self._tasks:
            return
        for stream in sorted(self._by_stream):
            self._tasks.append(
                asyncio.create_task(self._subscribe_stream(stream), name=f"observer-{stream}")
            )
        await asyncio.sleep(0)

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def _subscribe_stream(self, stream: str) -> None:
        async def _handler(payload: dict[str, Any]) -> None:
            await self.handle(stream, payload)

        try:
            await self._bus.subscribe(
                stream,
                _handler,
                group=f"orchestrator:observers:{stream.replace('.', '_')}",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("observer_subscribe_failed", stream=stream, error=str(exc))

    async def handle(self, stream: str, payload: dict[str, Any]) -> None:
        for observer in self._by_stream.get(stream, []):
            try:
                await observer.handle(payload)
            except Exception as exc:
                logger.warning(
                    "observer_handle_failed",
                    observer=observer.name,
                    stream=stream,
                    error=str(exc),
                )


def _jsonable(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        return json.loads(json.dumps(payload, default=str))
    except (TypeError, ValueError):
        return {"raw": str(payload)}
