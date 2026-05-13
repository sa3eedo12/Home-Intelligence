from __future__ import annotations

import json

import fakeredis.aioredis
import pytest

from tools import doorbell, notify_helper, scenes


@pytest.fixture
def fake_redis(monkeypatch):
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(notify_helper, "_redis_client", lambda: fake)
    return fake


@pytest.mark.asyncio
async def test_scene_activation_publishes_notification(fake_redis, monkeypatch) -> None:
    class _FakeHaClient:
        async def list_states(self, domain=None):  # noqa: ANN001
            assert domain == "scene"
            return [
                {
                    "entity_id": "scene.movie_night",
                    "attributes": {"friendly_name": "Movie Night"},
                }
            ]

        async def call_service(self, domain, service, data):  # noqa: ANN001
            assert (domain, service, data) == (
                "scene",
                "turn_on",
                {"entity_id": "scene.movie_night"},
            )
            return []

    monkeypatch.setattr(scenes, "get_ha_client", lambda: _FakeHaClient())

    result = await scenes.set_scene("Movie Night")

    assert result["ok"] is True
    rows = await fake_redis.xrange("notify.outbound")
    payload = json.loads(rows[0][1]["payload"])
    assert payload["text"] == "Scene activated: Movie Night"
    assert payload["topic"] == "home.scene"
    assert payload["capability"] == "set_scene"


@pytest.mark.asyncio
async def test_doorbell_summary_publishes_notification(fake_redis, monkeypatch) -> None:
    class _FakeHaClient:
        async def get_camera_snapshot(self, entity_id):  # noqa: ANN001
            assert entity_id == "camera.front_door"
            return b"image"

    async def _fake_detect_objects(_image, classes):  # noqa: ANN001
        assert "cat" in classes
        return [{"class": "cat", "score": 0.9}]

    monkeypatch.setattr(doorbell, "get_ha_client", lambda: _FakeHaClient())
    monkeypatch.setattr(doorbell.vision, "detect_objects", _fake_detect_objects)

    result = await doorbell.summarize_event("doorbell_motion")

    assert "cat" in result["summary"]
    rows = await fake_redis.xrange("notify.outbound")
    payload = json.loads(rows[0][1]["payload"])
    assert payload["topic"] == "doorbell.event"
    assert payload["capability"] == "doorbell.summarize_event"
    assert payload["severity"] == "info"
