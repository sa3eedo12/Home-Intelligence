from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from orchestrator.reflector import NightlyReflector


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
