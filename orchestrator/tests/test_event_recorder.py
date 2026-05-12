from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from orchestrator.event_recorder import EventRecorder, _summarize_activity


class _FakeStore:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.calls = []

    async def record_event(self, **kwargs):
        self.calls.append(kwargs)
        return {"ok": self.ok, "error": None if self.ok else "event_log_unavailable"}


@pytest.mark.asyncio
async def test_event_recorder_records_ok_activity() -> None:
    store = _FakeStore()
    recorder = EventRecorder(redis=None, store=store)  # type: ignore[arg-type]
    ts = datetime.now(UTC).isoformat()

    wrote = await recorder.handle_payload(
        {
            "agent": "washer",
            "capability": "cycle_complete",
            "status": "ok",
            "duration_ms": 123.4,
            "ts": ts,
        }
    )

    assert wrote is True
    assert store.calls == [
        {
            "agent": "washer",
            "capability": "cycle_complete",
            "summary": "washer.cycle_complete completed successfully in 123 ms",
            "payload": {
                "activity": {
                    "agent": "washer",
                    "capability": "cycle_complete",
                    "status": "ok",
                    "duration_ms": 123.4,
                    "ts": ts,
                }
            },
            "ts": ts,
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["started", "error", "in_progress"])
async def test_event_recorder_skips_non_ok_activity(status: str) -> None:
    store = _FakeStore()
    recorder = EventRecorder(redis=None, store=store)  # type: ignore[arg-type]

    wrote = await recorder.handle_payload(
        {"agent": "a", "capability": "c", "status": status, "duration_ms": 1.0}
    )

    assert wrote is False
    assert store.calls == []


@pytest.mark.asyncio
async def test_event_recorder_handles_malformed_stream_payload() -> None:
    store = _FakeStore()
    recorder = EventRecorder(redis=None, store=store)  # type: ignore[arg-type]

    await recorder._handle_fields({"payload": "not-json"})
    await recorder._handle_fields({"payload": json.dumps(["not", "an", "object"])})

    assert store.calls == []


def test_summarize_activity_without_duration() -> None:
    assert _summarize_activity({"agent": "a", "capability": "c"}) == "a.c completed successfully"
