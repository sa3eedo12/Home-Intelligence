from home_agents_sdk.schemas import Capability, Event, InvokeRequest, InvokeResponse, Manifest, Task


def test_schema_roundtrip() -> None:
    manifest = Manifest(
        agent="home-automation",
        capabilities=[
            Capability(id="toggle_light", description="Toggle a light", side_effects=True)
        ],
    )
    dump = manifest.model_dump()
    loaded = Manifest.model_validate(dump)
    assert loaded == manifest


def test_runtime_schemas() -> None:
    req = InvokeRequest(capability="toggle_light", payload={"entity_id": "light.kitchen"})
    resp = InvokeResponse(ok=True, result={"status": "done"})
    evt = Event(stream="alerts", event_type="created", payload={"severity": "high"})
    task = Task(id="t1", kind="reminder", payload={"text": "buy milk"}, priority=5)

    assert req.payload["entity_id"] == "light.kitchen"
    assert resp.ok is True
    assert evt.event_type == "created"
    assert task.priority == 5
