from __future__ import annotations

import json
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from tools import area
from tools.ha_client import HAClient


@pytest.fixture
def client() -> HAClient:
    return HAClient("http://homeassistant.local:8123", "test-token")


@respx.mock
@pytest.mark.asyncio
async def test_call_service_in_area_resolves_area_and_filters_domain(
    client: HAClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client.list_states_enriched = AsyncMock(
        return_value=[
            {
                "entity_id": "light.kitchen_ceiling",
                "name": "Kitchen Ceiling",
                "area": "Kitchen",
                "state": "off",
            }
        ]
    )  # type: ignore[method-assign]
    monkeypatch.setattr(area, "get_ha_client", lambda: client)

    def render_template(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert "area_entities('Kitchen')" in payload["template"]
        return httpx.Response(
            200,
            text='["light.kitchen_ceiling", "switch.kettle", "light.kitchen_strip"]',
        )

    respx.post("http://homeassistant.local:8123/api/template").mock(
        side_effect=render_template
    )
    service_route = respx.post(
        "http://homeassistant.local:8123/api/services/light/turn_on"
    ).mock(return_value=httpx.Response(200, json=[]))

    result = await area.call_service_in_area(
        "kitchen", "light", "turn_on", {"brightness_pct": 75}
    )

    assert result == {
        "area": "Kitchen",
        "domain": "light",
        "service": "turn_on",
        "target_count": 2,
        "errors": [],
    }
    assert service_route.call_count == 2
    payloads = [json.loads(call.request.content) for call in service_route.calls]
    assert payloads == [
        {"brightness_pct": 75, "entity_id": "light.kitchen_ceiling"},
        {"brightness_pct": 75, "entity_id": "light.kitchen_strip"},
    ]


@respx.mock
@pytest.mark.asyncio
async def test_list_areas_returns_template_registry(
    client: HAClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(area, "get_ha_client", lambda: client)
    respx.post("http://homeassistant.local:8123/api/template").mock(
        return_value=httpx.Response(
            200,
            text='[{"name": "Kitchen", "area_id": "kitchen"}, {"name": "Living Room"}]',
        )
    )

    result = await area.list_areas()

    assert result["count"] == 2
    assert result["areas"] == [
        {"name": "Kitchen", "area_id": "kitchen"},
        {"name": "Living Room"},
    ]
