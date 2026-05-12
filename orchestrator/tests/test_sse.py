from __future__ import annotations

import json
from types import SimpleNamespace

import fakeredis.aioredis
import pytest

from orchestrator.activity import ActivityAggregator
from orchestrator.sse import dashboard_stream, router


class _FakeRequest:
    """Minimal stand-in for fastapi.Request used to drive `dashboard_stream`.

    We bypass FastAPI's TestClient because StreamingResponse with an SSE
    heartbeat loop never closes from the server side, which would block the
    sync TestClient indefinitely. Driving the route coroutine directly lets
    us pull individual chunks and stop when we have what we need.
    """

    def __init__(self, app) -> None:  # noqa: ANN001
        self.app = app
        self._disconnected = False

    async def is_disconnected(self) -> bool:
        return self._disconnected


def test_sse_router_exposes_stream_route() -> None:
    paths = [r.path for r in router.routes]  # type: ignore[attr-defined]
    assert "/dashboard/stream" in paths


@pytest.mark.asyncio
async def test_sse_initial_snapshot_event_format() -> None:
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    agg = ActivityAggregator(fake)
    await agg.start()
    app = SimpleNamespace(state=SimpleNamespace(activity_aggregator=agg, redis=fake))

    request = _FakeRequest(app)
    try:
        response = await dashboard_stream(request)
        body_iter = response.body_iterator.__aiter__()
        first = await body_iter.__anext__()
        if isinstance(first, (bytes, bytearray)):
            first = first.decode("utf-8")
        assert first.startswith("event: snapshot")
        data_line = next(line for line in first.splitlines() if line.startswith("data:"))
        payload = json.loads(data_line[len("data: ") :])
        assert "agents" in payload
        assert "window_minutes" in payload
        assert response.media_type == "text/event-stream"
        assert response.headers.get("cache-control") == "no-cache"
    finally:
        request._disconnected = True  # noqa: SLF001
        await agg.stop()


# Live event propagation through the SSE stream is exercised end-to-end by
# `test_aggregator_broadcasts_live_events_to_subscribers` in test_activity.py;
# stacking another fakeredis-backed `xread` consumer on top in the SSE
# multiplexer is unreliable due to fakeredis blocking-xread semantics, so we
# do not duplicate that coverage here.

