from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.router import Router


class StaticSafety:
    def __init__(self, tier: str) -> None:
        self.tier = tier
        self.calls: list[tuple[str, str, dict]] = []

    def explain(self, agent: str, capability: str, inputs: dict | None = None) -> dict:
        self.calls.append((agent, capability, inputs or {}))
        return {
            "tier": self.tier,
            "matched_rule": {"agent": agent, "capability": capability},
            "reason": f"{self.tier} reason",
        }


class FakeProposalStore:
    def __init__(self) -> None:
        self.proposals: list[dict] = []

    async def add_proposal(self, **kwargs) -> int:
        self.proposals.append(kwargs)
        return len(self.proposals)


def _npu_response(agent: str = "home_automation", capability: str = "call_service") -> dict:
    return {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "agent": agent,
                            "capability": capability,
                            "inputs": {"domain": "light", "service": "turn_on"},
                            "needs_confirmation": False,
                            "reason": "turn on light",
                        }
                    )
                }
            }
        ]
    }


def _router(tier: str) -> tuple[Router, MagicMock, FakeProposalStore]:
    npu = MagicMock()
    npu.chat = AsyncMock(return_value=_npu_response())
    registry = MagicMock()
    registry.list_capabilities = MagicMock(
        return_value=[
            {"agent": "home_automation", "id": "call_service", "description": "call service"}
        ]
    )
    registry.get_capability = MagicMock(return_value={"description": "call service"})
    registry.semantic_search = AsyncMock(return_value=[])
    registry.dispatch = AsyncMock(return_value={"ok": True, "result": {"done": True}})
    store = FakeProposalStore()
    router = Router(
        npu=npu,
        registry=registry,
        router_model="router-model",
        llm=None,
        safety=StaticSafety(tier),
        proposal_store=store,  # type: ignore[arg-type]
    )
    return router, registry, store


@pytest.mark.asyncio
async def test_never_short_circuits_with_refusal_reply() -> None:
    router, registry, store = _router("never")

    result = await router.handle("unlock the front door", "user1")

    assert "I won't do that automatically" in result["reply"]
    registry.dispatch.assert_not_awaited()
    assert store.proposals == []


@pytest.mark.asyncio
async def test_suggest_autonomous_writes_proposal_without_dispatch() -> None:
    router, registry, store = _router("suggest")

    result = await router.handle("turn on the lamp", "system", autonomous=True)

    assert result["proposal"]["id"] == 1
    assert store.proposals[0]["kind"] == "suggested_action"
    assert store.proposals[0]["status"] == "pending"
    registry.dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_suggest_user_initiated_dispatches_as_before() -> None:
    router, registry, store = _router("suggest")

    result = await router.handle("turn on the lamp", "user1", autonomous=False)

    assert "done" in result["reply"]
    registry.dispatch.assert_awaited_once_with(
        "home_automation", "call_service", {"domain": "light", "service": "turn_on"}
    )
    assert store.proposals == []


@pytest.mark.asyncio
async def test_auto_dispatches_as_before() -> None:
    router, registry, store = _router("auto")

    result = await router.handle("turn on the lamp", "user1")

    assert "done" in result["reply"]
    registry.dispatch.assert_awaited_once()
    assert store.proposals == []
