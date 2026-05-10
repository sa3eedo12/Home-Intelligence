from __future__ import annotations

import os
from typing import Any

from home_agents_sdk.telemetry import get_logger
from home_agents_sdk.tools import tool

from .ha_client import get_ha_client

logger = get_logger("home_automation.ha_mcp_client")

# ── Internal helpers ──────────────────────────────────────────────────────────


def _mcp_url() -> str:
    """Return the HA MCP server SSE endpoint.

    Defaults to ``{HA_URL}/mcp_server/sse`` so no extra config is needed when
    the ``mcp_server`` integration is enabled in Home Assistant.  Override with
    ``HA_MCP_URL`` for non-standard deployments.
    """
    ha_url = os.getenv("HA_URL", "http://homeassistant.local:8123").rstrip("/")
    return os.getenv("HA_MCP_URL", f"{ha_url}/mcp_server/sse")


def _ha_token() -> str:
    return os.getenv("HA_TOKEN", "")


async def _list_mcp_tools() -> dict[str, Any]:
    """Open a short-lived SSE session to the HA MCP server and list its tools.

    Returns a dict with keys ``ok``, ``tools`` (list), and optionally ``error``.
    Fails gracefully when the HA MCP server integration is not configured.
    """
    try:
        # Deferred import so the module is importable when mcp is not installed.
        from mcp import ClientSession  # type: ignore[import-untyped]
        from mcp.client.sse import sse_client  # type: ignore[import-untyped]
    except ImportError:
        return {"ok": False, "error": "mcp package not installed"}

    url = _mcp_url()
    headers = {"Authorization": f"Bearer {_ha_token()}"}

    try:
        async with sse_client(url=url, headers=headers) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()
                tools = [
                    {
                        "name": t.name,
                        "description": t.description or "",
                        # camelCase matches the MCP JSON Schema spec and the mcp
                        # library's Tool dataclass attribute name.
                        "input_schema": getattr(t, "inputSchema", {}),
                    }
                    for t in result.tools
                ]
                return {"ok": True, "tools": tools, "tool_count": len(tools)}
    except Exception as exc:
        logger.warning("ha_mcp_list_tools_failed", error=str(exc))
        return {"ok": False, "error": str(exc)}


def _build_improvement_suggestions(
    automations: list[dict[str, Any]],
    domain_counts: dict[str, int],
    mcp_tool_names: set[str],
) -> list[str]:
    """Derive a short list of human-readable improvement suggestions."""
    suggestions: list[str] = []

    # Disabled automations
    disabled = [a for a in automations if a.get("state") == "off"]
    if disabled:
        names = ", ".join(a["name"] for a in disabled[:3])
        extra = f" (and {len(disabled) - 3} more)" if len(disabled) > 3 else ""
        suggestions.append(
            f"{len(disabled)} automation(s) are currently disabled — "
            f"review or remove: {names}{extra}."
        )

    # Lights without automations
    if domain_counts.get("light", 0) > 3 and domain_counts.get("automation", 0) < 2:
        suggestions.append(
            "You have several lights but few automations — consider adding time-based "
            "or presence-triggered automations."
        )

    # Sensors without automations
    if domain_counts.get("sensor", 0) > 5 and domain_counts.get("automation", 0) < 3:
        suggestions.append(
            "Multiple sensors found but few automations — threshold-based alerts "
            "(e.g. temperature, humidity) could add value."
        )

    # MCP-advertised integrations worth highlighting
    _MCP_HINTS: dict[str, str] = {
        "HassListAddItem": (
            "Shopping list integration is active in HA — the agent can manage your grocery list."
        ),
        "HassMediaNext": (
            "Media player integration is active — the agent can control playback."
        ),
        "HassTodoListAddItem": (
            "To-do list integration is active — the agent can create and manage tasks."
        ),
    }
    for tool_name, hint in _MCP_HINTS.items():
        if tool_name in mcp_tool_names:
            suggestions.append(hint)

    return suggestions


# ── Exported tool ─────────────────────────────────────────────────────────────


@tool("ha.introspect")
async def ha_introspect() -> dict[str, Any]:
    """Discover all HA capabilities via the MCP server, list existing automations,
    count entities by domain, and surface configuration improvement suggestions.

    Connects to the HA ``mcp_server`` SSE endpoint to enumerate every tool HA
    currently exposes (which changes dynamically as integrations are added or
    removed).  Falls back gracefully when the MCP server integration is not
    enabled — REST-based data is always returned.
    """
    ha = get_ha_client()

    # 1. MCP introspection — discover HA's live tool set
    mcp_data = await _list_mcp_tools()
    mcp_tool_names: set[str] = {t["name"] for t in mcp_data.get("tools", [])}

    # 2. Existing automations via REST
    automation_error: str | None = None
    automations: list[dict[str, Any]] = []
    try:
        states = await ha.list_states(domain="automation")
        automations = [
            {
                "entity_id": s["entity_id"],
                "name": s.get("attributes", {}).get("friendly_name", s["entity_id"]),
                "state": s["state"],  # "on" / "off"
                "last_triggered": s.get("attributes", {}).get("last_triggered"),
            }
            for s in states
        ]
    except Exception as exc:
        automation_error = str(exc)
        logger.warning("ha_introspect_automations_failed", error=str(exc))

    # 3. Entity counts by domain
    domain_counts: dict[str, int] = {}
    try:
        all_states = await ha.list_states()
        for state in all_states:
            domain = state["entity_id"].split(".")[0]
            domain_counts[domain] = domain_counts.get(domain, 0) + 1
    except Exception as exc:
        logger.warning("ha_introspect_states_failed", error=str(exc))

    # 4. Improvement suggestions
    suggestions = _build_improvement_suggestions(automations, domain_counts, mcp_tool_names)

    return {
        "mcp": mcp_data,
        "automations": automations,
        "automation_count": len(automations),
        "automation_error": automation_error,
        "domain_counts": domain_counts,
        "entity_count": sum(domain_counts.values()),
        "improvement_suggestions": suggestions,
    }
