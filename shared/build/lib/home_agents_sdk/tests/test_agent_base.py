from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from home_agents_sdk.agent_base import build_app
from home_agents_sdk.tools import clear_tools, tool


def test_build_app_validates_manifest_and_tools(tmp_path: Path) -> None:
    clear_tools()

    @tool("ping")
    def ping() -> dict[str, str]:
        return {"pong": "ok"}

    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        """
agent: test-agent
version: 0.1.0
capabilities:
  - id: ping
    description: Ping endpoint
    side_effects: false
""".strip(),
        encoding="utf-8",
    )

    app = build_app("test-agent", str(manifest))
    client = TestClient(app)

    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"

    invoke = client.post("/invoke", json={"capability": "ping", "payload": {}})
    assert invoke.status_code == 200
    assert invoke.json()["ok"] is True
    assert invoke.json()["result"] == {"pong": "ok"}


def test_build_app_rejects_manifest_capability_without_tool(tmp_path: Path) -> None:
    clear_tools()
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        """
agent: test-agent
version: 0.1.0
capabilities:
  - id: missing_tool
    description: Missing tool
    side_effects: false
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Missing tool registrations"):
        build_app("test-agent", str(manifest))
