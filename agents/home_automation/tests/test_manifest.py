from __future__ import annotations

import sys
from pathlib import Path

from home_agents_sdk.tools import clear_tools


def test_manifest_and_tools_consistent():
    """All manifest capabilities must have @tool registrations."""
    clear_tools()

    agent_dir = Path(__file__).parent.parent
    if str(agent_dir) not in sys.path:
        sys.path.insert(0, str(agent_dir))

    from home_agents_sdk.tools import list_tools

    from tools import anomaly, core, doorbell, scenes, suggest  # noqa: F401

    tools = list_tools()
    expected = [
        "list_entities",
        "get_entity_state",
        "call_service",
        "set_scene",
        "list_scenes",
        "doorbell.snapshot",
        "doorbell.summarize_event",
        "doorbell.last_visitor",
        "anomaly.scan",
        "suggest_automation",
    ]
    for cap_id in expected:
        assert cap_id in tools, f"Missing @tool registration for capability: {cap_id}"
