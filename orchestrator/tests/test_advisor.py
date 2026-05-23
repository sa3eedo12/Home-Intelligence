from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.advisor import Advisor


class FakeStore:
    def __init__(self) -> None:
        self.events = [
            {
                "id": 1,
                "agent": "observer.motion",
                "capability": "state_changed",
                "summary": "Kitchen motion after sunset",
                "payload": {},
            }
        ]
        self.dismissed: list[dict] = []
        self.proposals: list[dict] = []
        self.briefs: list[dict] = []

    async def list_recent_events(self, window_hours: int = 6) -> list[dict]:
        return self.events

    async def list_proposals(self, status: str | None = None, limit: int = 20) -> list[dict]:
        return [row for row in self.dismissed if status is None or row.get("status") == status][
            :limit
        ]

    async def add_proposal(self, **kwargs) -> int:
        self.proposals.append(kwargs)
        return len(self.proposals)

    async def record_brief(self, summary: str, body: dict) -> int:
        self.briefs.append({"summary": summary, "body": body})
        return len(self.briefs)


class FakeRedis:
    def __init__(self, override: str | None = None) -> None:
        self.override = override

    async def get(self, key: str) -> str | None:
        assert key == "policy:override:quiet"
        return self.override


class StaticSafety:
    def __init__(
        self,
        default_tier: str = "suggest",
        by_capability: dict[str, str] | None = None,
    ) -> None:
        self.default_tier = default_tier
        self.by_capability = by_capability or {}

    def explain(self, agent: str, capability: str, inputs: dict | None = None) -> dict:
        tier = self.by_capability.get(capability, self.default_tier)
        return {"tier": tier, "matched_rule": None, "reason": f"{tier} reason"}


def _llm_response(proposals: list[dict]) -> dict:
    return {"message": {"content": json.dumps({"proposals": proposals})}}


def _proposal(title: str, confidence: float, capability: str = "call_service") -> dict:
    return {
        "title": title,
        "rationale": f"Rationale for {title}",
        "agent": "home_automation",
        "capability": capability,
        "inputs": {"domain": "light", "service": "turn_on"},
        "evidence_event_ids": [1],
        "confidence": confidence,
    }


def _make_advisor(
    *,
    llm_response: dict | None = None,
    safety: StaticSafety | None = None,
    redis: FakeRedis | None = None,
    now: datetime | None = None,
    tv_payload: dict | None = None,
) -> tuple[Advisor, MagicMock, MagicMock, FakeStore]:
    llm = MagicMock()
    llm.chat = AsyncMock(return_value=llm_response or _llm_response([]))
    registry = MagicMock()
    registry.list_capabilities = MagicMock(
        return_value=[
            {"agent": "home_automation", "id": "call_service", "description": "call service"},
            {"agent": "home_automation", "id": "set_scene", "description": "set scene"},
        ]
    )
    registry.dispatch = AsyncMock(
        return_value=tv_payload
        or {"ok": True, "result": {"by_area": {"Living": [{"state": "idle"}]}}}
    )
    advisor = Advisor(
        pool=None,
        redis=redis or FakeRedis("off"),  # type: ignore[arg-type]
        llm=llm,
        registry=registry,
        safety=safety or StaticSafety(),  # type: ignore[arg-type]
        default_model="default-model",
        now_fn=lambda: now or datetime(2026, 1, 1, 8, 0, tzinfo=UTC),
    )
    store = FakeStore()
    advisor.store = store  # type: ignore[assignment]
    return advisor, registry, llm, store


@pytest.mark.asyncio
async def test_quiet_context_skips_and_records_skipped_brief() -> None:
    advisor, registry, llm, store = _make_advisor(redis=FakeRedis("on"))

    result = await advisor.run_once()

    assert result["status"] == "skipped"
    assert result["reason"] == "quiet_hours_active"
    assert store.proposals == []
    assert store.briefs[0]["body"]["status"] == "skipped"
    registry.dispatch.assert_not_awaited()
    llm.chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_dinner_context_skips(monkeypatch) -> None:
    monkeypatch.setenv("TZ", "Asia/Dubai")
    advisor, registry, llm, store = _make_advisor(
        now=datetime(2026, 1, 1, 14, 30, tzinfo=UTC)
    )

    result = await advisor.run_once()

    assert result["reason"] == "is_dinner_window"
    assert store.proposals == []
    registry.dispatch.assert_not_awaited()
    llm.chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_tv_on_context_skips() -> None:
    advisor, registry, llm, store = _make_advisor(
        tv_payload={"ok": True, "result": {"by_area": {"Living": [{"state": "playing"}]}}}
    )

    result = await advisor.run_once()

    assert result["reason"] == "is_tv_on"
    assert store.proposals == []
    registry.dispatch.assert_awaited_once_with(
        "home_automation", "list_entities", {"domain": "media_player"}
    )
    llm.chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_normal_context_with_zero_llm_proposals_returns_empty_result() -> None:
    advisor, registry, llm, store = _make_advisor(llm_response=_llm_response([]))

    result = await advisor.run_once()

    assert result["status"] == "ok"
    assert result["proposals"] == []
    assert store.proposals == []
    registry.dispatch.assert_awaited_once()
    llm.chat.assert_awaited_once()


@pytest.mark.asyncio
async def test_five_llm_proposals_saves_top_three_by_confidence() -> None:
    proposals = [
        _proposal("fifth", 0.1),
        _proposal("second", 0.8),
        _proposal("first", 0.9),
        _proposal("third", 0.7),
        _proposal("fourth", 0.2),
    ]
    advisor, _registry, _llm, store = _make_advisor(llm_response=_llm_response(proposals))

    result = await advisor.run_once()

    assert result["saved"] == 3
    assert [row["title"] for row in store.proposals] == ["first", "second", "third"]
    assert all(row["kind"] == "suggested_action" for row in store.proposals)
    assert result["dropped"] == 2


@pytest.mark.asyncio
async def test_auto_tier_dispatches_and_records_auto_action() -> None:
    proposal = _proposal("Set evening scene", 0.9, capability="set_scene")
    proposal["inputs"] = {"scene_name": "Evening"}
    advisor, registry, _llm, store = _make_advisor(
        llm_response=_llm_response([proposal]),
        safety=StaticSafety(by_capability={"set_scene": "auto"}),
    )
    registry.dispatch = AsyncMock(
        side_effect=[
            {"ok": True, "result": {"by_area": {"Living": [{"state": "idle"}]}}},
            {"ok": True, "scene": "scene.evening"},
        ]
    )

    result = await advisor.run_once()

    assert result["dispatched"] == 1
    assert store.proposals[0]["kind"] == "auto_action"
    assert store.proposals[0]["status"] == "auto_confirmed"
    assert registry.dispatch.await_args_list[1].args == (
        "home_automation",
        "set_scene",
        {"scene_name": "Evening"},
    )


@pytest.mark.asyncio
async def test_never_tier_proposal_is_dropped_without_dispatch() -> None:
    advisor, registry, _llm, store = _make_advisor(
        llm_response=_llm_response([_proposal("Unlock door", 0.9)]),
        safety=StaticSafety(default_tier="never"),
    )

    result = await advisor.run_once()

    assert result["saved"] == 0
    assert result["dropped"] == 1
    assert store.proposals == []
    registry.dispatch.assert_awaited_once_with(
        "home_automation", "list_entities", {"domain": "media_player"}
    )


# ── Noise filter for hallucinated reflector/advisor proposals ────


def test_is_noise_proposal_blocks_internal_cron_capability() -> None:
    from orchestrator.advisor import _is_noise_proposal

    is_noise, reason = _is_noise_proposal({
        "title": "Tweak Anomaly Detection",
        "agent": "system_health",
        "capability": "anomaly_check",
        "inputs": {"window_minutes": 30},
    })
    assert is_noise
    assert reason == "internal_cron_capability"


def test_is_noise_proposal_blocks_placeholder_argument_input() -> None:
    """The LLM invents {"argument": "monitor"} when it can't recall the
    real input fields. No tool accepts a key literally named 'argument'."""
    from orchestrator.advisor import _is_noise_proposal

    is_noise, reason = _is_noise_proposal({
        "title": "Some real-sounding action",
        "agent": "home_automation",
        "capability": "call_service",
        "inputs": {"argument": "monitor"},
    })
    assert is_noise
    assert reason == "placeholder_input_argument"


def test_is_noise_proposal_blocks_hollow_verb_titles() -> None:
    """'Optimize/Monitor/Review/Check/Validate/Summarize <thing>' titles
    are the recurring hallucinated card shape — drop them."""
    from orchestrator.advisor import _is_noise_proposal

    for title in (
        "Optimize Knowledge Notes",
        "Monitor Health Metrics",
        "Review Health Sync",
        "Check Auto Infer",
        "Validate Backups",
        "Summarize Activity",
    ):
        is_noise, reason = _is_noise_proposal({
            "title": title,
            "agent": "knowledge_notes",
            "capability": "list",   # not in internal list, so verb is sole signal
            "inputs": {"limit": 10},
        })
        assert is_noise, f"{title!r} should be flagged"
        assert reason and reason.startswith("hollow_verb:")


def test_is_noise_proposal_passes_real_user_facing_action() -> None:
    """A concrete action with real inputs and a content-bearing title
    must NOT be flagged."""
    from orchestrator.advisor import _is_noise_proposal

    is_noise, reason = _is_noise_proposal({
        "title": "Set bedroom thermostat to 21°C for tonight",
        "agent": "home_automation",
        "capability": "call_service",
        "inputs": {
            "domain": "climate",
            "service": "set_temperature",
            "entity_id": "climate.bedroom",
            "temperature": 21,
        },
    })
    assert not is_noise
    assert reason is None


def test_normalize_proposal_drops_noise_at_intake() -> None:
    """The whole intake pipeline (_normalize_proposal) must return None
    for noise so it never reaches add_proposal()."""
    from unittest.mock import MagicMock
    from orchestrator.advisor import Advisor

    advisor = Advisor.__new__(Advisor)
    # Internal noise — drops
    result = advisor._normalize_proposal({
        "title": "Optimize Auto Infer Event Processing",
        "agent": "personal_assistant",
        "capability": "auto_infer_observer_event",
        "inputs": {"argument": "optimize"},
        "confidence": 0.85,
    })
    assert result is None

    # Real proposal — passes through
    result = advisor._normalize_proposal({
        "title": "Wind down the lights",
        "agent": "home_automation",
        "capability": "call_service_in_area",
        "inputs": {"domain": "light", "area_id": "living_room"},
        "confidence": 0.7,
    })
    assert result is not None
    assert result["title"] == "Wind down the lights"
