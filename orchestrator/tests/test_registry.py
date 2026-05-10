from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import respx

from orchestrator.registry import CapabilityRegistry

MANIFEST = {
    "agent": "home_automation",
    "version": "0.2.0",
    "capabilities": [
        {"id": "list_entities", "description": "List HA entities.", "side_effects": False},
        {"id": "call_service", "description": "Call HA service.", "side_effects": True},
    ],
}


def _make_registry(agent_urls=None):
    if agent_urls is None:
        agent_urls = {"home_automation": "http://home_automation:8000"}

    qdrant = MagicMock()
    qdrant.get_collection = AsyncMock(return_value=MagicMock())
    qdrant.create_collection = AsyncMock()
    qdrant.upsert = AsyncMock()
    qdrant.search = AsyncMock(return_value=[])

    embedder = MagicMock()
    embedder.embed = AsyncMock(return_value=[0.1] * 1024)

    return CapabilityRegistry(agent_urls=agent_urls, qdrant=qdrant, embedder=embedder)


@pytest.mark.asyncio
@respx.mock
async def test_bootstrap_fetches_manifest():
    respx.get("http://home_automation:8000/manifest").mock(
        return_value=httpx.Response(200, json=MANIFEST)
    )
    registry = _make_registry()
    await registry.bootstrap()
    assert "home_automation" in registry._manifests


@pytest.mark.asyncio
@respx.mock
async def test_bootstrap_embeds_capabilities():
    respx.get("http://home_automation:8000/manifest").mock(
        return_value=httpx.Response(200, json=MANIFEST)
    )
    registry = _make_registry()
    await registry.bootstrap()
    registry._qdrant.upsert.assert_called()
    assert registry.capability_counts()["home_automation"] == 2


@pytest.mark.asyncio
async def test_bootstrap_handles_unavailable():
    """bootstrap() gracefully handles agent that is unreachable."""
    registry = _make_registry({"bad_agent": "http://unreachable:9999"})
    # No exception should propagate
    await registry.bootstrap()
    assert "bad_agent" not in registry._manifests


@pytest.mark.asyncio
@respx.mock
async def test_agents_returns_registered():
    respx.get("http://home_automation:8000/manifest").mock(
        return_value=httpx.Response(200, json=MANIFEST)
    )
    registry = _make_registry()
    await registry.bootstrap()
    assert "home_automation" in registry.agents()


@pytest.mark.asyncio
@respx.mock
async def test_dispatch_posts_to_agent():
    respx.get("http://home_automation:8000/manifest").mock(
        return_value=httpx.Response(200, json=MANIFEST)
    )
    respx.post("http://home_automation:8000/invoke").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": []})
    )
    registry = _make_registry()
    await registry.bootstrap()
    result = await registry.dispatch("home_automation", "list_entities", {})
    assert result["ok"] is True
