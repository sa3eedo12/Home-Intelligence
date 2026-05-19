from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path

import yaml
from home_agents_sdk.tools import clear_tools


def test_manifest_optional_types_are_quoted():
    manifest_path = Path(__file__).parent.parent / "manifest.yaml"
    manifest_text = manifest_path.read_text(encoding="utf-8")
    assert not re.search(
        r":\s*(string\?|integer\?|boolean\?|object\?|array\?|number\?)(\s*[},]|$)",
        manifest_text,
    )
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    capabilities = {cap["id"]: cap for cap in manifest["capabilities"]}
    assert capabilities["list_entities"]["inputs"]["domain"] == "string?"
    assert capabilities["doorbell.snapshot"]["inputs"]["entity_id"] == "string?"
    assert capabilities["doorbell.summarize_event"]["inputs"]["entity_id"] == "string?"


def test_manifest_and_tools_consistent():
    """All manifest capabilities must have @tool registrations."""
    # Ensure the agent root is on sys.path so bare `tools.*` imports work.
    agent_dir = Path(__file__).parent.parent
    if str(agent_dir) not in sys.path:
        sys.path.insert(0, str(agent_dir))

    from home_agents_sdk.tools import list_tools

    import tools.anomaly as _anomaly
    import tools.appliance as _appliance
    import tools.area as _area
    import tools.climate as _climate
    import tools.core as _core
    import tools.cover as _cover
    import tools.doorbell as _doorbell
    import tools.ev as _ev
    import tools.ha_mcp_client as _ha_mcp
    import tools.lights_control as _lights
    import tools.lock as _lock
    import tools.scenes as _scenes
    import tools.suggest as _suggest
    import tools.vacuum as _vacuum

    # Clear stale registrations then force every module to re-run its @tool
    # decorators.  This makes the test order-independent regardless of what
    # other tests imported beforehand.
    clear_tools()
    for mod in (
        _core,
        _scenes,
        _doorbell,
        _anomaly,
        _suggest,
        _ha_mcp,
        _appliance,
        _area,
        _lights,
        _climate,
        _cover,
        _lock,
        _vacuum,
        _ev,
    ):
        importlib.reload(mod)

    tools = list_tools()
    expected = [
        "list_entities",
        "get_entity_state",
        "search_entities",
        "call_service",
        "call_service_in_area",
        "list_areas",
        "set_scene",
        "list_scenes",
        "doorbell.snapshot",
        "doorbell.summarize_event",
        "doorbell.last_visitor",
        "anomaly.scan",
        "suggest_automation",
        "ha.introspect",
        "recent_appliance_activity",
        "lights_off",
        "lights_on",
        "lights_status",
        "climate_status",
        "climate_set_temperature",
        "climate_set_mode",
        "cover_status",
        "cover_open",
        "cover_close",
        "cover_set_position",
        "lock_status",
        "lock_lock",
        "lock_unlock",
        "vacuum_status",
        "vacuum_start",
        "vacuum_dock",
        "ev_status",
        "ev_start_charging",
        "ev_close_windows",
        "ev_flash_lights",
        "ev_find_car",
    ]
    for cap_id in expected:
        assert cap_id in tools, f"Missing @tool registration for capability: {cap_id}"
