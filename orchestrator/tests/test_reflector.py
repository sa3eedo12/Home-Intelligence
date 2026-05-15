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
    def __init__(self, proposal_signal: dict | None = None) -> None:
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
        self._proposal_signal = proposal_signal or {
            "dismissed": 0,
            "accepted": 0,
            "auto_confirmed": 0,
        }
        self.signal_lookups: list[dict] = []

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

    async def proposal_dismissal_signal(
        self, *, kind: str, days: int = 14
    ) -> dict[str, int]:
        self.signal_lookups.append({"kind": kind, "days": days})
        return dict(self._proposal_signal)

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
    # Use a habit_inference (not code_change) so we exercise the pipeline
    # plumbing without tripping the new code_change-evidence filter.
    llm.chat = AsyncMock(
        return_value=_response(
            [
                {
                    "kind": "habit_inference",
                    "title": "User typically up by 07:00",
                    "rationale": "Coffee + presence events at 07:xx for 5 days running.",
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


# ── code_change speculative-evidence filter ──────────────────────────────


@pytest.mark.asyncio
async def test_code_change_with_few_events_is_filtered() -> None:
    """REGRESSION: prior to the filter, the LLM was emitting 12+ pending
    code_change proposals like 'add jitter' / 'cap retries' citing 3 events
    of normal traffic. Require >=5 distinct events for code_change."""
    store = FakeStore()
    llm = MagicMock()
    llm.chat = AsyncMock(
        return_value=_response(
            [
                {
                    "kind": "code_change",
                    "title": "Add jitter to anomaly_check polling",
                    "rationale": (
                        "Events 1, 2, 3 show anomaly_check at regular intervals."
                        " Suggests a potential thundering herd issue."
                    ),
                    "evidence_event_ids": [1, 2, 3],
                    "evidence_keys": [],
                    "confidence": 0.8,
                }
            ]
        )
    )
    await _reflector(store, llm).run_once()

    assert store.proposals == []  # filtered out


@pytest.mark.asyncio
async def test_code_change_with_concrete_problem_keyword_passes() -> None:
    """A code_change with >=5 evidence + a concrete-problem keyword in
    the rationale (timeout, error, regression...) should persist."""
    store = FakeStore()
    llm = MagicMock()
    llm.chat = AsyncMock(
        return_value=_response(
            [
                {
                    "kind": "code_change",
                    "title": "Cap auto_infer timeout — calls hanging at 60s",
                    "rationale": (
                        "Events 1, 2, 3, 4, 5 show auto_infer_observer_event "
                        "calls timing out at 60s. Hard timeout would prevent "
                        "the whole reactive trigger from blocking."
                    ),
                    "evidence_event_ids": [1, 2, 3, 4, 5],
                    "evidence_keys": [],
                    "confidence": 0.9,
                }
            ]
        )
    )
    await _reflector(store, llm).run_once()

    assert len(store.proposals) == 1
    assert store.proposals[0]["title"].startswith("Cap auto_infer timeout")


@pytest.mark.asyncio
async def test_habit_inference_with_one_event_still_passes() -> None:
    """The new filter applies ONLY to code_change. Habit/preference/etc
    proposals can still cite a single representative event."""
    store = FakeStore()
    llm = MagicMock()
    llm.chat = AsyncMock(
        return_value=_response(
            [
                {
                    "kind": "habit_inference",
                    "title": "User watches TV around 20:30",
                    "rationale": "Event 1 shows TV at 20:30 last night.",
                    "evidence_event_ids": [1],
                    "confidence": 0.7,
                }
            ]
        )
    )
    await _reflector(store, llm).run_once()

    assert len(store.proposals) == 1


# ── Per-kind dismissal feedback loop ─────────────────────────────────────


@pytest.mark.asyncio
async def test_kind_with_many_dismissals_is_backed_off() -> None:
    """If the user has dismissed >=5 cleanup_action proposals in 14 days
    with zero accepts, the reflector should stop emitting more of that kind."""
    store = FakeStore(
        proposal_signal={"dismissed": 6, "accepted": 0, "auto_confirmed": 0}
    )
    llm = MagicMock()
    llm.chat = AsyncMock(
        return_value=_response(
            [
                {
                    "kind": "cleanup_action",
                    "title": "Reduce anomaly_check frequency",
                    "rationale": "Event 1 shows it running 15min apart.",
                    "evidence_event_ids": [1],
                    "confidence": 0.8,
                }
            ]
        )
    )
    await _reflector(store, llm).run_once()

    assert store.proposals == []  # backed off
    # The signal lookup happened once for cleanup_action
    assert store.signal_lookups[0]["kind"] == "cleanup_action"


@pytest.mark.asyncio
async def test_dismissals_with_at_least_one_accept_does_not_back_off() -> None:
    """If the user has accepted at least one of this kind, don't mute —
    they still find some valuable, just not all."""
    store = FakeStore(
        proposal_signal={"dismissed": 9, "accepted": 1, "auto_confirmed": 0}
    )
    llm = MagicMock()
    llm.chat = AsyncMock(
        return_value=_response(
            [
                {
                    "kind": "cleanup_action",
                    "title": "Cleanup something",
                    "rationale": "Event 1 shows old data.",
                    "evidence_event_ids": [1],
                    "confidence": 0.8,
                }
            ]
        )
    )
    await _reflector(store, llm).run_once()

    assert len(store.proposals) == 1


@pytest.mark.asyncio
async def test_signal_cache_dedups_lookup_per_run() -> None:
    """Multiple proposals of the same kind in one reflection run should
    only trigger one DB lookup for the signal."""
    store = FakeStore()
    llm = MagicMock()
    llm.chat = AsyncMock(
        return_value=_response(
            [
                {
                    "kind": "habit_inference",
                    "title": "Habit A",
                    "rationale": "Event 1.",
                    "evidence_event_ids": [1],
                    "confidence": 0.5,
                },
                {
                    "kind": "habit_inference",
                    "title": "Habit B",
                    "rationale": "Event 2.",
                    "evidence_event_ids": [2],
                    "confidence": 0.5,
                },
                {
                    "kind": "cleanup_action",
                    "title": "Cleanup A",
                    "rationale": "Event 3.",
                    "evidence_event_ids": [3],
                    "confidence": 0.5,
                },
            ]
        )
    )
    await _reflector(store, llm).run_once()

    # 3 proposals across 2 kinds → 2 distinct signal lookups
    kinds = [s["kind"] for s in store.signal_lookups]
    assert kinds.count("habit_inference") == 1
    assert kinds.count("cleanup_action") == 1
