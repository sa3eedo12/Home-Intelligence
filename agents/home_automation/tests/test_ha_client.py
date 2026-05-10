from __future__ import annotations

import httpx
import pytest
import respx

from tools.ha_client import HAClient


@pytest.fixture
def client():
    return HAClient("http://homeassistant.local:8123", "test-token")


@respx.mock
@pytest.mark.asyncio
async def test_get_state(client):
    respx.get("http://homeassistant.local:8123/api/states/light.living_room").mock(
        return_value=httpx.Response(
            200,
            json={"entity_id": "light.living_room", "state": "on", "attributes": {}},
        )
    )
    state = await client.get_state("light.living_room")
    assert state["entity_id"] == "light.living_room"
    assert state["state"] == "on"


@respx.mock
@pytest.mark.asyncio
async def test_list_states_filtered_by_domain(client):
    all_states = [
        {"entity_id": "light.room", "state": "on", "attributes": {}},
        {"entity_id": "switch.fan", "state": "off", "attributes": {}},
    ]
    respx.get("http://homeassistant.local:8123/api/states").mock(
        return_value=httpx.Response(200, json=all_states)
    )
    lights = await client.list_states(domain="light")
    assert len(lights) == 1
    assert lights[0]["entity_id"] == "light.room"


@respx.mock
@pytest.mark.asyncio
async def test_call_service(client):
    respx.post("http://homeassistant.local:8123/api/services/light/turn_off").mock(
        return_value=httpx.Response(200, json=[])
    )
    result = await client.call_service("light", "turn_off", {"entity_id": "light.living_room"})
    assert result == []


@respx.mock
@pytest.mark.asyncio
async def test_auth_header_sent(client):
    route = respx.get("http://homeassistant.local:8123/api/states/sensor.temp").mock(
        return_value=httpx.Response(
            200,
            json={"entity_id": "sensor.temp", "state": "22.5", "attributes": {}},
        )
    )
    await client.get_state("sensor.temp")
    assert route.called
    request = route.calls[0].request
    assert request.headers.get("Authorization") == "Bearer test-token"
