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
    """Phones/watches without non-person keywords should still fire.
    Note: PCs / laptops / desktops are NEVER people (HARD_NON_PERSON_SUBSTRINGS),
    even when they appear in device_tracker.* — see hard-block test below."""
    observer = _CapturePresence()
    real = [
        ("device_tracker.saeeds_iphone", "Saeed's iPhone"),
        ("device_tracker.judiths_phone", "Judith's Phone"),
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
        "device_tracker.saeed_phone,device_tracker.judiths_phone",
    )
    observer = _CapturePresence()
    for eid in [
        "device_tracker.saeed_phone",
        "device_tracker.judiths_phone",
        "device_tracker.random_other_phone",  # not in allowlist
    ]:
        await observer.handle({
            "entity_id": eid, "old_state": "not_home", "state": "home",
            "ts": "2026-01-01T09:00:00+00:00",
            "attributes": {"friendly_name": eid.split(".")[1]},
        })
    fired = {e[1]["entity_id"] for e in observer.emitted}
    assert fired == {"device_tracker.saeed_phone", "device_tracker.judiths_phone"}


@pytest.mark.asyncio
async def test_presence_strict_when_at_least_one_member_linked(monkeypatch) -> None:
    """If household_members.attributes.tracker_entity_ids is set for any
    member, the observer enters STRICT mode: only those entities fire,
    even if other entities would have passed the keyword heuristic."""
    monkeypatch.delenv("PRESENCE_ALLOWLIST", raising=False)
    observer = _CapturePresence()
    observer._member_links = {"device_tracker.saeeds_iphone": "Saeed"}
    observer._member_links_ts = 9e18  # never expires for this test
    base_attrs = {"friendly_name": "Some Device"}

    # Linked entity: fires
    await observer.handle({
        "entity_id": "device_tracker.saeeds_iphone",
        "old_state": "not_home", "state": "home", "ts": "x",
        "attributes": base_attrs,
    })
    # Unlinked phone that would normally pass: blocked by strict mode
    await observer.handle({
        "entity_id": "device_tracker.guests_phone",
        "old_state": "not_home", "state": "home", "ts": "y",
        "attributes": base_attrs,
    })
    fired = [(e[1]["entity_id"], e[1]["person"]) for e in observer.emitted]
    assert fired == [("device_tracker.saeeds_iphone", "Saeed")]
    assert observer.emitted[0][1]["household_member_linked"] is True


@pytest.mark.asyncio
async def test_presence_uses_member_name_not_device_name(monkeypatch) -> None:
    observer = _CapturePresence()
    observer._member_links = {"device_tracker.judiths_phone": "Judith"}
    observer._member_links_ts = 9e18
    await observer.handle({
        "entity_id": "device_tracker.judiths_phone",
        "old_state": "not_home", "state": "home", "ts": "z",
        "attributes": {"friendly_name": "Judith's Phone"},
    })
    assert observer.emitted[0][1]["person"] == "Judith"


@pytest.mark.asyncio
async def test_presence_hard_blocks_pcs_and_laptops_even_in_allowlist(monkeypatch) -> None:
    """Regression: PCs/laptops/desktops fire 'home/away' as their WiFi
    flaps when sleeping, polluting presence. They must NEVER count as
    people, even when added to a household_member's tracker_entity_ids
    (because users do this thinking it'll help) or to the env allowlist."""
    monkeypatch.setenv("PRESENCE_ALLOWLIST", "device_tracker.judes_laptop")
    observer = _CapturePresence()
    # Also link it as a member tracker — the user might try this
    observer._member_links = {"device_tracker.judes_laptop": "Judith"}
    observer._member_links_ts = 9e18
    for eid in [
        "device_tracker.saeed_pc",
        "device_tracker.judes_laptop",
        "device_tracker.someone_macbook",
        "device_tracker.host_imac",
        "device_tracker.workstation_desktop",
    ]:
        await observer.handle({
            "entity_id": eid, "old_state": "not_home", "state": "home",
            "ts": "x", "attributes": {"friendly_name": eid.split(".")[1]},
        })
    assert observer.emitted == [], (
        f"PC/laptop entities should be hard-blocked: got "
        f"{[e[1]['entity_id'] for e in observer.emitted]}"
    )


# ── Per-member authority — person.* trumps device_tracker.* ─────────────


@pytest.mark.asyncio
async def test_per_member_authority_only_person_entity_fires() -> None:
    """REGRESSION (Saeed): with 5 trackers linked (person.saeed +
    saeeds_iphone + saeeds_iphone_2 + saeed_pc + saeed_sp11), every
    individual device_tracker flap was emitting 'Saeed is now home/
    not_home' even though person.saeed (the consolidated state) hadn't
    changed. Result: bogus welcome-home messages, missed real arrivals.

    With the per-member authority gate, only person.saeed is allowed
    to emit; the other trackers stay silent.
    """
    observer = _CapturePresence()
    observer._member_links = {
        "person.saeed": "Saeed",
        "device_tracker.saeeds_iphone": "Saeed",
        "device_tracker.saeed_sp11": "Saeed",
    }
    observer._authoritative_entity_for_member = {"Saeed": "person.saeed"}
    observer._member_links_ts = 9e18

    # Authoritative entity transitions → emit
    await observer.handle({
        "entity_id": "person.saeed",
        "old_state": "not_home", "state": "home",
        "ts": "x", "attributes": {"friendly_name": "Saeed"},
    })

    # Non-authoritative trackers transition → suppress
    await observer.handle({
        "entity_id": "device_tracker.saeeds_iphone",
        "old_state": "home", "state": "not_home",
        "ts": "x", "attributes": {"friendly_name": "Saeed iPhone"},
    })
    await observer.handle({
        "entity_id": "device_tracker.saeed_sp11",
        "old_state": "home", "state": "not_home",
        "ts": "x", "attributes": {"friendly_name": "Saeed SP11"},
    })

    assert len(observer.emitted) == 1
    assert observer.emitted[0][1]["entity_id"] == "person.saeed"
    assert observer.emitted[0][1]["state"] == "home"


@pytest.mark.asyncio
async def test_no_authority_means_all_trackers_can_emit() -> None:
    """When a member has NO person.* in their tracker list, every
    individual device_tracker remains authoritative — backwards-
    compatible with the simpler setups."""
    observer = _CapturePresence()
    observer._member_links = {
        "device_tracker.jude_phone": "Jude",
    }
    observer._authoritative_entity_for_member = {}  # no person.* linked
    observer._member_links_ts = 9e18

    await observer.handle({
        "entity_id": "device_tracker.jude_phone",
        "old_state": "not_home", "state": "home",
        "ts": "x", "attributes": {"friendly_name": "Jude Phone"},
    })

    assert len(observer.emitted) == 1
    assert observer.emitted[0][1]["person"] == "Jude"


@pytest.mark.asyncio
async def test_authority_gate_does_not_block_unrelated_members() -> None:
    """A non-authoritative tracker for member A doesn't block a
    legitimate transition for member B."""
    observer = _CapturePresence()
    observer._member_links = {
        "person.saeed": "Saeed",
        "device_tracker.saeeds_iphone": "Saeed",
        "device_tracker.jude_phone": "Jude",
    }
    observer._authoritative_entity_for_member = {"Saeed": "person.saeed"}
    observer._member_links_ts = 9e18

    # Saeed's iphone (not authoritative) → suppress
    await observer.handle({
        "entity_id": "device_tracker.saeeds_iphone",
        "old_state": "home", "state": "not_home",
        "ts": "x", "attributes": {"friendly_name": "Saeed iPhone"},
    })
    # Jude's phone (no person.* linked → authoritative by default) → emit
    await observer.handle({
        "entity_id": "device_tracker.jude_phone",
        "old_state": "not_home", "state": "home",
        "ts": "x", "attributes": {"friendly_name": "Jude Phone"},
    })

    assert [e[1]["person"] for e in observer.emitted] == ["Jude"]
