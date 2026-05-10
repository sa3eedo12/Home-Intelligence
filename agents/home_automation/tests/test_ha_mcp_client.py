from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure the agent root is on sys.path so bare `tools.*` imports work.
_AGENT_DIR = Path(__file__).parent.parent
if str(_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_DIR))

from tools.ha_mcp_client import (  # noqa: E402
    _build_improvement_suggestions,
    _list_mcp_tools,
    ha_introspect,
)

# ── _list_mcp_tools ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_mcp_tools_import_error() -> None:
    """When the mcp package is not installed, fail gracefully."""
    with patch.dict("sys.modules", {"mcp": None, "mcp.client.sse": None}):
        result = await _list_mcp_tools()

    assert result["ok"] is False
    assert "not installed" in result["error"]


@pytest.mark.asyncio
async def test_list_mcp_tools_connection_error() -> None:
    """When the MCP server is unreachable, return ok=False with the error message."""
    mock_sse_client = MagicMock()
    mock_sse_client.return_value.__aenter__ = AsyncMock(
        side_effect=ConnectionRefusedError("refused")
    )
    mock_sse_client.return_value.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("tools.ha_mcp_client._mcp_url", return_value="http://ha.local/mcp_server/sse"),
        patch("tools.ha_mcp_client._ha_token", return_value="tok"),
        patch.dict(
            "sys.modules",
            {
                "mcp": MagicMock(),
                "mcp.client.sse": MagicMock(sse_client=mock_sse_client),
            },
        ),
    ):
        result = await _list_mcp_tools()

    assert result["ok"] is False
    assert result.get("error") is not None


@pytest.mark.asyncio
async def test_list_mcp_tools_success() -> None:
    """Happy path: MCP session lists tools and we return them structured."""
    fake_tool = MagicMock()
    fake_tool.name = "HassTurnOn"
    fake_tool.description = "Turn on a HA entity"
    fake_tool.inputSchema = {"type": "object", "properties": {"name": {"type": "string"}}}

    mock_session = AsyncMock()
    mock_session.initialize = AsyncMock()
    mock_session.list_tools = AsyncMock(return_value=MagicMock(tools=[fake_tool]))
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    mock_streams = (AsyncMock(), AsyncMock())
    mock_sse_ctx = MagicMock()
    mock_sse_ctx.__aenter__ = AsyncMock(return_value=mock_streams)
    mock_sse_ctx.__aexit__ = AsyncMock(return_value=False)

    mock_mcp = MagicMock()
    mock_mcp.ClientSession = MagicMock(return_value=mock_session)
    mock_sse_module = MagicMock()
    mock_sse_module.sse_client = MagicMock(return_value=mock_sse_ctx)

    with (
        patch("tools.ha_mcp_client._mcp_url", return_value="http://ha.local/mcp_server/sse"),
        patch("tools.ha_mcp_client._ha_token", return_value="tok"),
        patch.dict(
            "sys.modules",
            {"mcp": mock_mcp, "mcp.client.sse": mock_sse_module},
        ),
    ):
        result = await _list_mcp_tools()

    assert result["ok"] is True
    assert result["tool_count"] == 1
    assert result["tools"][0]["name"] == "HassTurnOn"
    assert result["tools"][0]["description"] == "Turn on a HA entity"


# ── _build_improvement_suggestions ────────────────────────────────────────────


def test_suggestions_disabled_automations() -> None:
    automations: list[dict[str, Any]] = [
        {"entity_id": "automation.x", "name": "Night lights", "state": "off"},
        {"entity_id": "automation.y", "name": "Morning alarm", "state": "on"},
    ]
    suggestions = _build_improvement_suggestions(automations, {}, set())
    assert any("disabled" in s for s in suggestions)
    assert any("Night lights" in s for s in suggestions)


def test_suggestions_lights_without_automations() -> None:
    suggestions = _build_improvement_suggestions(
        [], {"light": 5, "automation": 1}, set()
    )
    assert any("lights" in s.lower() for s in suggestions)


def test_suggestions_mcp_tool_hints() -> None:
    suggestions = _build_improvement_suggestions([], {}, {"HassListAddItem"})
    assert any("shopping list" in s.lower() for s in suggestions)


def test_suggestions_no_issues() -> None:
    # A well-configured setup with multiple automations and no disabled ones
    automations = [{"name": "A", "state": "on"}, {"name": "B", "state": "on"}]
    suggestions = _build_improvement_suggestions(automations, {"light": 2, "automation": 5}, set())
    assert not any("disabled" in s for s in suggestions)
    assert not any("lights" in s.lower() for s in suggestions)


# ── ha_introspect (integration with mocked dependencies) ─────────────────────


@pytest.mark.asyncio
async def test_ha_introspect_mcp_unavailable() -> None:
    """ha_introspect returns REST data even when MCP server is down."""
    mock_ha = AsyncMock()
    mock_ha.list_states = AsyncMock(
        return_value=[
            {
                "entity_id": "light.bedroom",
                "state": "off",
                "attributes": {"friendly_name": "Bedroom"},
            },
            {
                "entity_id": "automation.night_lights",
                "state": "on",
                "attributes": {"friendly_name": "Night lights"},
            },
        ]
    )

    with (
        patch("tools.ha_mcp_client.get_ha_client", return_value=mock_ha),
        patch(
            "tools.ha_mcp_client._list_mcp_tools",
            AsyncMock(return_value={"ok": False, "error": "refused"}),
        ),
    ):
        result = await ha_introspect()

    assert result["mcp"]["ok"] is False
    assert result["entity_count"] >= 0
    assert isinstance(result["improvement_suggestions"], list)


@pytest.mark.asyncio
async def test_ha_introspect_full_success() -> None:
    """ha_introspect merges MCP tool list with REST data and generates suggestions."""
    mock_ha = AsyncMock()
    mock_ha.list_states = AsyncMock(
        return_value=[
            {
                "entity_id": "light.living_room",
                "state": "on",
                "attributes": {"friendly_name": "Living room"},
            },
            {"entity_id": "light.bedroom", "state": "off", "attributes": {}},
            {"entity_id": "light.kitchen", "state": "off", "attributes": {}},
            {"entity_id": "light.hall", "state": "off", "attributes": {}},
            {
                "entity_id": "automation.welcome_home",
                "state": "off",
                "attributes": {"friendly_name": "Welcome home"},
            },
        ]
    )

    mcp_result = {
        "ok": True,
        "tools": [{"name": "HassTurnOn", "description": "Turn on", "input_schema": {}}],
        "tool_count": 1,
    }

    with (
        patch("tools.ha_mcp_client.get_ha_client", return_value=mock_ha),
        patch("tools.ha_mcp_client._list_mcp_tools", AsyncMock(return_value=mcp_result)),
    ):
        result = await ha_introspect()

    assert result["mcp"]["ok"] is True
    assert result["mcp"]["tool_count"] == 1
    # 5 entities across light + automation domains
    assert result["entity_count"] == 5
    assert result["domain_counts"]["light"] == 4
    # One automation is disabled → suggestion expected
    assert any("disabled" in s for s in result["improvement_suggestions"])
