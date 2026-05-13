from __future__ import annotations

from typing import Any

import pytest

from orchestrator.observers.presence_observer import PresenceObserver


class _CapturePresence(PresenceObserver):
    def __init__(self) -> None:
        super().__init__()
        self.emitted: list[tuple[str, dict[str, Any]]] = []

    async def emit_event(self, kind: str, summary: str, payload: dict[str, Any]) -> None:
        self.emitted.append((kind, payload))


@pytest.mark.asyncio
async def test_presence_changed_emits_home_and_not_home() -> None:
    observer = _CapturePresence()
    base = {
        "entity_id": "device_tracker.saeed_phone",
        "attributes": {"friendly_name": "Saeed"},
    }

    await observer.handle(
        {**base, "old_state": "not_home", "state": "home", "ts": "2026-01-01T09:00:00+00:00"}
    )
    await observer.handle(
        {**base, "old_state": "home", "state": "home", "ts": "2026-01-01T09:01:00+00:00"}
    )
    await observer.handle(
        {**base, "old_state": "home", "state": "not_home", "ts": "2026-01-01T10:00:00+00:00"}
    )

    assert [item[0] for item in observer.emitted] == ["presence.changed", "presence.changed"]
    assert observer.emitted[0][1]["state"] == "home"
    assert observer.emitted[0][1]["person"] == "Saeed"
    assert observer.emitted[1][1]["state"] == "not_home"


@pytest.mark.asyncio
async def test_presence_filters_non_person_devices() -> None:
    """Aqara hubs, gateways, smart appliances, TVs, etc. should NOT trigger
    presence events even though they're in the device_tracker domain."""
    observer = _CapturePresence()
    noisy = [
        ("device_tracker.aqara_hub_e1_4e55", "Aqara_Hub_E1-4E55"),
        ("device_tracker.aqara_hub_m2_bbed", "Aqara-Hub-M2-BBED"),
        ("device_tracker.cloud_gateway_max", "Cloud Gateway Max"),
        ("device_tracker.samsung_dryer", "Samsung-Dryer"),
        ("device_tracker.samsung_washer", "Samsung-Washer"),
        ("device_tracker.samsung", "Samsung TV"),
        ("device_tracker.oled_g8", "OLED G8"),
        ("device_tracker.ringdoorbell_dc", "RingDoorbell-dc"),
        ("device_tracker.unifi_default_48_e1_e9_93_2e_99", ""),
        ("device_tracker.anker", "Anker"),
        ("device_tracker.espressif", "espressif"),
        ("device_tracker.raspberry_pi_3", "Raspberry Pi 3"),
        ("device_tracker.express_7_2", "Express 7"),
    ]
    for eid, name in noisy:
        await observer.handle({
            "entity_id": eid, "old_state": "not_home", "state": "home",
            "ts": "2026-01-01T09:00:00+00:00",
            "attributes": {"friendly_name": name},
        })
    assert observer.emitted == [], (
        f"non-person devices fired presence events: {[e[1]['entity_id'] for e in observer.emitted]}"
    )


@pytest.mark.asyncio
async def test_presence_keeps_real_people() -> None:
    """Phones/laptops/watches without non-person keywords should still fire."""
    observer = _CapturePresence()
    real = [
        ("device_tracker.saeeds_iphone", "Saeed's iPhone"),
        ("device_tracker.judes_laptop", "Judes-Laptop"),
        ("device_tracker.iphone_8", "iPhone"),
        ("person.saeed", "Saeed"),
    ]
    for eid, name in real:
        await observer.handle({
            "entity_id": eid, "old_state": "not_home", "state": "home",
            "ts": "2026-01-01T09:00:00+00:00",
            "attributes": {"friendly_name": name},
        })
    assert len(observer.emitted) == len(real), (
        f"only got {len(observer.emitted)} of {len(real)}: "
        f"{[e[1]['entity_id'] for e in observer.emitted]}"
    )


@pytest.mark.asyncio
async def test_presence_explicit_allowlist_via_env(monkeypatch) -> None:
    """When PRESENCE_ALLOWLIST is set, only those entities fire."""
    monkeypatch.setenv(
        "PRESENCE_ALLOWLIST",
        "device_tracker.saeed_phone,device_tracker.judes_laptop",
    )
    observer = _CapturePresence()
    for eid in [
        "device_tracker.saeed_phone",
        "device_tracker.judes_laptop",
        "device_tracker.random_other_phone",  # not in allowlist
    ]:
        await observer.handle({
            "entity_id": eid, "old_state": "not_home", "state": "home",
            "ts": "2026-01-01T09:00:00+00:00",
            "attributes": {"friendly_name": eid.split(".")[1]},
        })
    fired = {e[1]["entity_id"] for e in observer.emitted}
    assert fired == {"device_tracker.saeed_phone", "device_tracker.judes_laptop"}


@pytest.mark.asyncio
async def test_presence_strict_when_at_least_one_member_linked(monkeypatch) -> None:
    """If household_members.attributes.tracker_entity_ids is set for any
    member, the observer enters STRICT mode: only those entities fire,
    even if other entities would have passed the keyword heuristic."""
    monkeypatch.delenv("PRESENCE_ALLOWLIST", raising=False)
    observer = _CapturePresence()
    # Simulate a member-link cache populated from the DB
    observer._member_links = {"device_tracker.saeeds_iphone": "Saeed"}
    observer._member_links_ts = 9e18  # never expires for this test
    base_attrs = {"friendly_name": "Some Device"}

    # Linked entity: fires
    await observer.handle({
        "entity_id": "device_tracker.saeeds_iphone",
        "old_state": "not_home", "state": "home", "ts": "x",
        "attributes": base_attrs,
    })
    # Unlinked entity that WOULD have passed the keyword heuristic: blocked
    await observer.handle({
        "entity_id": "device_tracker.judes_laptop",
        "old_state": "not_home", "state": "home", "ts": "y",
        "attributes": base_attrs,
    })
    fired = [(e[1]["entity_id"], e[1]["person"]) for e in observer.emitted]
    assert fired == [("device_tracker.saeeds_iphone", "Saeed")]
    # Person field should come from the linked household_member.name, not friendly
    assert observer.emitted[0][1]["household_member_linked"] is True


@pytest.mark.asyncio
async def test_presence_uses_member_name_not_device_name(monkeypatch) -> None:
    observer = _CapturePresence()
    observer._member_links = {"device_tracker.judes_laptop": "Judith"}
    observer._member_links_ts = 9e18
    await observer.handle({
        "entity_id": "device_tracker.judes_laptop",
        "old_state": "not_home", "state": "home", "ts": "z",
        "attributes": {"friendly_name": "Judes-Laptop"},
    })
    assert observer.emitted[0][1]["person"] == "Judith"
