from __future__ import annotations

import json

import fakeredis.aioredis
import pytest

from tests.test_registry import _FakeKnowledgeGraph
from tools import registry


@pytest.mark.asyncio
async def test_thing_put_publishes_memory_update(monkeypatch) -> None:
    fake_graph = _FakeKnowledgeGraph()
    fake_redis = fakeredis.aioredis.FakeRedis(decode_responses=True)

    async def _fake_graph():
        return fake_graph

    monkeypatch.setattr(registry, "_knowledge_graph", _fake_graph)
    monkeypatch.setattr(registry.publish_helper, "_redis_client", lambda: fake_redis)

    result = await registry.put_thing(
        type="appliance.washer",
        friendly_name="Washer",
        attributes={"brand": "LG"},
        ha_entity_ids=["sensor.washer"],
    )

    assert result["ok"] is True
    rows = await fake_redis.xrange("memory.updates")
    payload = json.loads(rows[0][1]["payload"])
    assert payload["type"] == "registry.thing.put"
    assert payload["capability"] == "things.put"
    assert payload["entity_kind"] == "thing"
    assert payload["thing"]["friendly_name"] == "Washer"


@pytest.mark.asyncio
async def test_member_put_publishes_memory_update(monkeypatch) -> None:
    fake_graph = _FakeKnowledgeGraph()
    fake_redis = fakeredis.aioredis.FakeRedis(decode_responses=True)

    async def _fake_graph():
        return fake_graph

    monkeypatch.setattr(registry, "_knowledge_graph", _fake_graph)
    monkeypatch.setattr(registry.publish_helper, "_redis_client", lambda: fake_redis)

    result = await registry.put_member(name="Saeed", role="adult")

    assert result["ok"] is True
    rows = await fake_redis.xrange("memory.updates")
    payload = json.loads(rows[0][1]["payload"])
    assert payload["type"] == "registry.member.put"
    assert payload["capability"] == "members.put"
    assert payload["member"]["name"] == "Saeed"
