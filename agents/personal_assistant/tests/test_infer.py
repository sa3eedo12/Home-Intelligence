from __future__ import annotations

import json
from typing import Any

import pytest

from tools import infer as infer_tool


class _FakeStore:
    def __init__(self) -> None:
        self.window_minutes = None

    async def recall_recent(self, window_minutes: int = 60, agent: str | None = None) -> dict:
        self.window_minutes = window_minutes
        return {
            "items": [
                {
                    "agent": "washer",
                    "capability": "cycle_complete",
                    "summary": "Washer completed a hot cycle after bed linens were stripped",
                    "payload": {"room": "bedroom"},
                }
            ]
        }


class _FakeLLM:
    def __init__(self, content: dict[str, Any]) -> None:
        self.content = content
        self.calls = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        return {"message": {"content": json.dumps(self.content)}}


@pytest.mark.asyncio
async def test_infer_returns_confirmation_action(monkeypatch) -> None:
    store = _FakeStore()
    llm = _FakeLLM(
        {
            "inference": "You probably washed bed sheets.",
            "confidence": 0.78,
            "clarifying_question": "",
            "proposed_action": {
                "agent": "knowledge_notes",
                "capability": "record_event",
                "payload": {
                    "agent": "personal_assistant",
                    "capability": "inferred_event",
                    "summary": "Washed bed sheets",
                    "payload": {"source": "infer"},
                },
            },
        }
    )

    async def _fake_store() -> _FakeStore:
        return store

    monkeypatch.setattr(infer_tool, "_event_store", _fake_store)
    monkeypatch.setattr(infer_tool, "_llm", lambda: llm)

    result = await infer_tool.infer("what was the last wash cycle?")

    assert result["needs_confirmation"] is True
    assert result["confidence"] == 0.78
    assert result["proposed_action"]["agent"] == "knowledge_notes"
    assert result["proposed_action"]["capability"] == "record_event"
    assert store.window_minutes == 24 * 60
    assert llm.calls[0]["response_format"] == "json"


@pytest.mark.asyncio
async def test_infer_low_confidence_asks_clarifying_question(monkeypatch) -> None:
    store = _FakeStore()
    llm = _FakeLLM(
        {
            "inference": "",
            "confidence": 0.42,
            "clarifying_question": "Was this the bedding load or clothes?",
            "proposed_action": None,
        }
    )

    async def _fake_store() -> _FakeStore:
        return store

    monkeypatch.setattr(infer_tool, "_event_store", _fake_store)
    monkeypatch.setattr(infer_tool, "_llm", lambda: llm)

    result = await infer_tool.infer("what was the last wash cycle?")

    assert result["needs_confirmation"] is False
    assert result["proposed_action"] is None
    assert result["clarifying_question"] == "Was this the bedding load or clothes?"


@pytest.mark.asyncio
async def test_infer_handles_llm_failure(monkeypatch) -> None:
    async def _fake_store() -> _FakeStore:
        return _FakeStore()

    class _BrokenLLM:
        async def chat(self, **_kwargs):
            raise RuntimeError("offline")

    monkeypatch.setattr(infer_tool, "_event_store", _fake_store)
    monkeypatch.setattr(infer_tool, "_llm", lambda: _BrokenLLM())

    result = await infer_tool.infer("infer something")

    assert result["needs_confirmation"] is False
    assert result["clarifying_question"]
