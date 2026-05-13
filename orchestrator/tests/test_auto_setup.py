from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from orchestrator.auto_setup import (
    _classify_thing,
    _normalize_name,
    _person_entity_for,
    apply_proposal,
    discover_proposal,
)


def test_classify_thing_recognizes_canonical_appliance_states() -> None:
    assert _classify_thing("sensor.washer_machine_state", "Washer") == "appliance.washer"
    assert _classify_thing("sensor.dryer_machine_state", "Dryer") == "appliance.dryer"
    assert _classify_thing("sensor.washer_job_state", "Washer Job") == "appliance.washer"
    # Sub-entities that aren't canonical state should NOT be auto-adopted
    assert _classify_thing("sensor.washer_power", "Washer Power") is None
    assert _classify_thing("switch.washer_bubble_soak", "Bubble") is None


def test_classify_thing_recognizes_domains() -> None:
    assert _classify_thing("vacuum.saeeds_deebot", "Deebot") == "device.vacuum"
    assert _classify_thing("lock.front_door", "Front") == "device.lock"
    assert _classify_thing("climate.bedroom", "Bedroom AC") == "device.climate"
    assert _classify_thing("camera.front_door", "Front Door") == "device.camera"
    assert _classify_thing("light.kitchen", "Kitchen") == "device.light"


def test_classify_thing_distinguishes_tv_from_monitor() -> None:
    assert _classify_thing("media_player.samsung_tv", "Samsung TV") == "device.tv"
    assert _classify_thing("media_player.34_odyssey_oled_g8", "OLED G8") == "device.monitor"


def test_classify_thing_recognizes_motion_sensors() -> None:
    assert _classify_thing("binary_sensor.bedroom_motion", "Bedroom motion") == "sensor.motion"
    assert _classify_thing("binary_sensor.office_occupancy", "Office occupancy") == "sensor.motion"


def test_classify_thing_returns_none_for_uninteresting() -> None:
    assert _classify_thing("button.identify", "Identify") is None
    assert _classify_thing("update.foo", "Update") is None
    assert _classify_thing("automation.refresh", "Refresh") is None


def test_normalize_name_strips_punctuation_and_case() -> None:
    assert _normalize_name("Saeed") == "saeed"
    assert _normalize_name("Saeed's iPhone") == "saeedsiphone"
    assert _normalize_name("Judes-Laptop") == "judeslaptop"


def test_person_entity_for_finds_exact_and_partial_matches() -> None:
    states = [
        {"entity_id": "person.saeed", "attributes": {"friendly_name": "Saeed"}},
        {
            "entity_id": "device_tracker.saeeds_iphone",
            "attributes": {"friendly_name": "Saeed's iPhone"},
        },
        {"entity_id": "device_tracker.saeed_pc", "attributes": {"friendly_name": "Saeed PC"}},
        {
            "entity_id": "device_tracker.judes_laptop",
            "attributes": {"friendly_name": "Judes-Laptop"},
        },
        {"entity_id": "device_tracker.aqara_hub", "attributes": {"friendly_name": "Aqara Hub"}},
    ]
    result = _person_entity_for("Saeed", states)
    assert "person.saeed" in result
    # person.saeed should come first (highest score + person.* preference)
    assert result[0] == "person.saeed"
    # Saeed's gear is included
    assert "device_tracker.saeeds_iphone" in result
    assert "device_tracker.saeed_pc" in result
    # Other people's gear is not
    assert "device_tracker.judes_laptop" not in result
    # Random hubs are not
    assert "device_tracker.aqara_hub" not in result


@pytest.mark.asyncio
async def test_discover_proposal_skips_already_adopted(monkeypatch) -> None:
    # Make _ha_states return a fixed list
    async def fake_states():
        return [
            {
                "entity_id": "vacuum.deebot",
                "attributes": {"friendly_name": "Deebot"},
                "state": "docked",
            },
            {
                "entity_id": "sensor.washer_machine_state",
                "attributes": {"friendly_name": "Washer"},
                "state": "stop",
            },
            {
                "entity_id": "person.saeed",
                "attributes": {"friendly_name": "Saeed"},
                "state": "home",
            },
        ]

    monkeypatch.setattr("orchestrator.auto_setup._ha_states", fake_states)

    graph = AsyncMock()
    graph.list_things = AsyncMock(return_value=[
        # vacuum.deebot is already adopted via attributes.entity_id
        {"id": 1, "type": "device.vacuum", "friendly_name": "Deebot",
         "attributes": {"entity_id": "vacuum.deebot"}},
    ])
    graph.list_members = AsyncMock(return_value=[
        {"id": 2, "name": "Saeed", "role": "adult", "attributes": {}},
    ])

    proposal = await discover_proposal(knowledge_graph=graph)
    eids = {t["entity_id"] for t in proposal["things_to_adopt"]}
    assert "vacuum.deebot" not in eids, "should skip already-adopted"
    assert "sensor.washer_machine_state" in eids
    # Member trackers populated
    assert "Saeed" in proposal["trackers_by_member"]
    assert "person.saeed" in proposal["trackers_by_member"]["Saeed"]


@pytest.mark.asyncio
async def test_apply_proposal_adopts_things_and_links_members() -> None:
    graph = AsyncMock()
    graph.list_things = AsyncMock(return_value=[])
    graph.list_members = AsyncMock(return_value=[
        {"id": 2, "name": "Saeed", "role": "adult", "telegram_chat_id": 123,
         "allergies": [], "dietary_restrictions": [], "sleep_time": None, "wake_time": None,
         "attributes": {"existing_key": "preserved"}},
    ])
    graph.put_thing = AsyncMock(return_value={"id": 99})
    graph.put_member = AsyncMock(return_value={"id": 2})

    proposal = {
        "things_to_adopt": [
            {"entity_id": "vacuum.deebot", "type": "device.vacuum", "friendly_name": "Deebot"},
        ],
        "trackers_by_member": {
            "Saeed": ["person.saeed", "device_tracker.saeeds_iphone"],
        },
    }
    result = await apply_proposal(proposal=proposal, knowledge_graph=graph)
    assert result["adopted_count"] == 1
    assert result["adopted"] == ["vacuum.deebot"]
    assert len(result["member_updates"]) == 1

    # Verify put_thing was called correctly
    call = graph.put_thing.call_args
    assert call.kwargs["type"] == "device.vacuum"
    assert call.kwargs["ha_entity_ids"] == ["vacuum.deebot"]

    # Verify put_member preserved existing attributes AND added trackers
    call = graph.put_member.call_args
    attrs = call.kwargs["attributes"]
    assert attrs["existing_key"] == "preserved"
    assert attrs["tracker_entity_ids"] == ["person.saeed", "device_tracker.saeeds_iphone"]


@pytest.mark.asyncio
async def test_apply_proposal_idempotent_for_already_adopted() -> None:
    graph = AsyncMock()
    graph.list_things = AsyncMock(return_value=[
        {"id": 1, "type": "device.vacuum", "attributes": {"entity_id": "vacuum.deebot"}},
    ])
    graph.list_members = AsyncMock(return_value=[])
    graph.put_thing = AsyncMock()

    result = await apply_proposal(
        proposal={
            "things_to_adopt": [
                {"entity_id": "vacuum.deebot", "type": "device.vacuum", "friendly_name": "Deebot"},
            ],
            "trackers_by_member": {},
        },
        knowledge_graph=graph,
    )
    assert result["adopted_count"] == 0
    assert result["skipped_already_adopted"] == 1
    graph.put_thing.assert_not_called()
