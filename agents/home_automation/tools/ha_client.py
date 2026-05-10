from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

import httpx


class HAClient:
    def __init__(self, base_url: str, token: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    async def get_state(self, entity_id: str) -> dict:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{self._base_url}/api/states/{entity_id}", headers=self._headers
            )
            resp.raise_for_status()
            return resp.json()

    async def list_states(self, domain: str | None = None) -> list[dict]:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(f"{self._base_url}/api/states", headers=self._headers)
            resp.raise_for_status()
            states: list[dict] = resp.json()
        if domain is not None:
            states = [s for s in states if s.get("entity_id", "").startswith(f"{domain}.")]
        return states

    async def call_service(self, domain: str, service: str, data: dict) -> Any:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{self._base_url}/api/services/{domain}/{service}",
                headers=self._headers,
                json=data,
            )
            resp.raise_for_status()
            return resp.json()

    async def get_camera_snapshot(self, entity_id: str) -> bytes:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{self._base_url}/api/camera_proxy/{entity_id}", headers=self._headers
            )
            resp.raise_for_status()
            return resp.content

    async def get_history(self, entity_ids: list[str], hours: int) -> list[dict]:
        end_time = datetime.now(UTC).isoformat()
        filter_ids = ",".join(entity_ids)
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{self._base_url}/api/history/period",
                headers=self._headers,
                params={"filter_entity_id": filter_ids, "end_time": end_time},
            )
            resp.raise_for_status()
            return resp.json()


_client: HAClient | None = None


def get_ha_client() -> HAClient:
    global _client
    if _client is None:
        url = os.getenv("HA_URL", "http://localhost:8123")
        token = os.getenv("HA_TOKEN", "")
        _client = HAClient(url, token)
    return _client
