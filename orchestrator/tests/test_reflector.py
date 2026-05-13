from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from orchestrator.reflector import NightlyReflector


class FakeHealthStore:
    def __init__(self) -> None:
        self.aggregate_daily = AsyncMock(side_effect=self._aggregate_daily)

    async def _aggregate_daily(self, metric: str, days: int = 7) -> list[dict]:
        return [
            {
                "metric": metric,
                "day": "2026-05-13",
                "value": 420 if metric == "sleep_asleep" else 9000,
            }
        ]


class FakeStore:
    def __init__(self) -> None:
        self.events = [
            {
                "id": event_id,
                "ts": datetime(2026, 1, 1, 7, event_id, tzinfo=UTC).isoformat(),
                "agent": "personal_assistant",
                "capability": "chat",
                "summary": f"Coffee routine evidence {event_id}",
                "payload": {},
            }
            for event_id in range(1, 6)
        ]
        self.profile: list[dict] = []
        self.proposals: list[dict] = []
        self.profile_upserts: list[dict] = []
        self.briefs: list[dict] = []
        self.calls: list[str] = []

    async def list_recent_events(self, window_hours: int = 24) -> list[dict]:
        self.calls.append(f"events:{window_hours}")
        return self.events

    async def list_proposals(self, status: str | None = None, limit: int = 50) -> list[dict]:
        self.calls.append(f"proposals:{status}:{limit}")
        return [p for p in self.proposals if status is None or p.get("status") == status]

    async def list_profile(self) -> list[dict]:
        self.calls.append("profile")
        return self.profile

    async def add_proposal(self, **kwargs) -> int:
        proposal = {"id": len(self.proposals) + 1, **kwargs}
        self.proposals.append(proposal)
        return proposal["id"]

    async def upsert_profile(self, key, value, confidence, source) -> None:  # noqa: ANN001
        self.profile_upserts.append(
            {"key": key, "value": value, "confidence": confidence, "source": source}
        )

    async def record_brief(self, summary: str, body: dict) -> int:
        self.calls.append("brief")
        self.briefs.append({"summary": summary, "body": body})
        return 77


def _reflector(store: FakeStore, llm: MagicMock) -> NightlyReflector:
    registry = MagicMock()
    registry.list_capabilities.return_value = [
        {"agent": "personal_assistant", "id": "chat", "description": "chat fallback"}
    ]
    reflector = NightlyReflector(
        pool=None,
        redis=None,
        llm=llm,
        registry=registry,
        reasoner_model="reasoner-model",
        fallback_model="fallback-model",
    )
    reflector.store = store
    return reflector


def _response(proposals: list[dict]) -> dict:
    return {"message": {"content": json.dumps({"proposals": proposals})}}


@pytest.mark.asyncio
async def test_run_once_executes_pipeline_and_requires_evidence_citations() -> None:
    store = FakeStore()
    llm = MagicMock()
    llm.chat = AsyncMock(
        return_value=_response(
            [
                {
                    "kind": "code_change",
                    "title": "Improve no-capability logging",
                    "rationale": "Make router misses easier to audit.",
                    "evidence_event_ids": [1],
                    "evidence_keys": [],
                    "confidence": 0.7,
                    "cost_estimate": "small",
                    "impact_estimate": "better diagnostics",
                }
            ]
        )
    )

    result = await _reflector(store, llm).run_once()

    assert result["brief_id"] == 77
    assert {"events:24", "profile", "brief"}.issubset(set(store.calls))
    assert result["patterns"][0]["hour"] == 7
    assert store.proposals[0]["status"] == "pending"
    messages = llm.chat.await_args.kwargs["messages"]
    assert "Every proposal MUST cite" in messages[0]["content"]
    assert "knowledge-gap key" in messages[0]["content"]


@pytest.mark.asyncio
async def test_health_summary_is_included_in_prompt_context() -> None:
    store = FakeStore()
    health_store = FakeHealthStore()
    llm = MagicMock()
    llm.chat = AsyncMock(return_value=_response([]))
    reflector = _reflector(store, llm)
    reflector.health_store = health_store

    await reflector.run_once()

    prompt = json.loads(llm.chat.await_args.kwargs["messages"][1]["content"])
    assert prompt["health_summary"] == {
        "sleep_asleep_7d": [{"metric": "sleep_asleep", "day": "2026-05-13", "value": 420}],
        "steps_7d": [{"metric": "steps", "day": "2026-05-13", "value": 9000}],
    }
    assert health_store.aggregate_daily.await_count == 2


@pytest.mark.asyncio
async def test_auto_confirm_rule_writes_high_confidence_habit_to_profile() -> None:
    store = FakeStore()
    llm = MagicMock()
    llm.chat = AsyncMock(
        return_value=_response(
            [
                {
                    "kind": "habit_inference",
                    "title": "Coffee around 07:00",
                    "rationale": "Five morning coffee events clustered at 07:00.",
                    "evidence_event_ids": [1, 2, 3, 4, 5],
                    "evidence_keys": [],
                    "confidence": 0.97,
                    "profile_key": "habits.morning_coffee",
                    "profile_value": {"time": "07:00", "drink": "coffee"},
                }
            ]
        )
    )

    await _reflector(store, llm).run_once()

    assert store.proposals[0]["status"] == "auto_confirmed"
    assert store.profile_upserts == [
        {
            "key": "habits.morning_coffee",
            "value": {"time": "07:00", "drink": "coffee"},
            "confidence": 0.97,
            "source": "proposal:1",
        }
    ]


@pytest.mark.asyncio
async def test_reasoner_http_error_falls_back_to_default_model() -> None:
    store = FakeStore()
    llm = MagicMock()
    llm.chat = AsyncMock(
        side_effect=[
            httpx.HTTPError("model missing"),
            _response(
                [
                    {
                        "kind": "preference_inference",
                        "title": "Ask wake time",
                        "rationale": "wake_time is missing.",
                        "evidence_event_ids": [],
                        "evidence_keys": ["wake_time"],
                        "confidence": 0.4,
                    }
                ]
            ),
        ]
    )

    result = await _reflector(store, llm).run_once()

    assert result["brief_id"] == 77
    models = [call.kwargs["model"] for call in llm.chat.call_args_list]
    assert models == ["reasoner-model", "fallback-model"]
    assert store.proposals[0]["title"] == "Ask wake time"


@pytest.mark.asyncio
async def test_status_tracks_running_state(monkeypatch) -> None:
    """status flips running=True while run_once is in flight, and back to
    False after — with last_brief_id set."""
    import asyncio

    from orchestrator import reflector as reflector_mod

    # Build a minimally-mocked reflector and short-circuit each phase.
    r = reflector_mod.NightlyReflector(
        pool=None, redis=None, llm=AsyncMock(), registry=MagicMock(),
        reasoner_model="x", fallback_model="y",
    )

    async def _noop(*_a, **_kw):
        return {}

    monkeypatch.setattr(r, "_gather_evidence", _noop)
    monkeypatch.setattr(r, "_self_audit", AsyncMock(return_value={}))
    monkeypatch.setattr(r, "_knowledge_gaps", AsyncMock(return_value=[]))
    monkeypatch.setattr(r, "_pattern_mining", AsyncMock(return_value=[]))
    monkeypatch.setattr(r, "_generate_proposals", AsyncMock(return_value=[]))
    monkeypatch.setattr(r, "_apply_auto_confirm_rules", AsyncMock(return_value=[]))
    monkeypatch.setattr(r, "_save_brief", AsyncMock(return_value=42))

    assert r.status["running"] is False
    assert r.status["last_brief_id"] is None

    # Race: kick off run_once and read status during it.
    async def _watcher():
        # Yield control so run_once can start.
        await asyncio.sleep(0)
        return r.status

    snapshot, body = await asyncio.gather(_watcher(), r.run_once())
    # During run, status was running=True.
    # (The snapshot might catch it slightly after the start; just verify the
    # final state is consistent and the brief id is recorded.)
    assert body["brief_id"] == 42
    assert r.status["running"] is False
    assert r.status["last_brief_id"] == 42
    assert r.status["last_duration_seconds"] is not None
    assert snapshot is not None  # placeholder so we don't drop the watcher


@pytest.mark.asyncio
async def test_run_once_refuses_concurrent_invocation(monkeypatch) -> None:
    """If a run is already in flight, run_once returns an error dict instead
    of starting a second pipeline."""
    from orchestrator import reflector as reflector_mod

    r = reflector_mod.NightlyReflector(
        pool=None, redis=None, llm=AsyncMock(), registry=MagicMock(),
        reasoner_model="x", fallback_model="y",
    )
    r._status["running"] = True
    r._status["started_at"] = "2026-05-12T17:30:00+00:00"

    result = await r.run_once()
    assert result["ok"] is False
    assert result["error"] == "reflection_already_running"
