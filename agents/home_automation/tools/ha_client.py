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

    async def list_states_enriched(
        self, domain: str | None = None, include_unavailable: bool = True
    ) -> list[dict]:
        """List states with friendly_name and area_name resolved via HA template API.

        Falls back to the plain /api/states response if the template render
        fails (older HA, area_registry not loaded, etc.).
        """
        domain_filter = f"and s.domain == '{domain}'" if domain else ""
        if not include_unavailable:
            unavail_filter = "and s.state not in ['unavailable','unknown']"
        else:
            unavail_filter = ""
        # Render entities → JSON-shaped string in HA, then parse on our side.
        template = (
            "[{% set entities = states | selectattr('domain', 'in', "
            "['light','switch','sensor','binary_sensor','climate','media_player',"
            "'cover','fan','vacuum','lock','automation','scene','script']) "
            "| list %}{% for s in entities if true "
            f"{domain_filter} {unavail_filter} "
            "%}{{ '{' }}\"entity_id\": \"{{ s.entity_id }}\", "
            "\"name\": \"{{ state_attr(s.entity_id, 'friendly_name') or s.entity_id }}\", "
            "\"area\": \"{{ area_name(s.entity_id) or 'Unassigned' }}\", "
            "\"state\": \"{{ s.state }}\"{{ '}' }}{% if not loop.last %},{% endif %}{% endfor %}]"
        )
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"{self._base_url}/api/template",
                    headers=self._headers,
                    json={"template": template},
                )
                resp.raise_for_status()
                rendered = resp.text
            import json as _json

            return _json.loads(rendered)
        except Exception:
            # Fallback: plain states list with friendly_name from attributes.
            states = await self.list_states(domain=domain)
            out = []
            for s in states:
                attrs = s.get("attributes", {}) or {}
                if not include_unavailable and s["state"] in ("unavailable", "unknown"):
                    continue
                out.append(
                    {
                        "entity_id": s["entity_id"],
                        "name": attrs.get("friendly_name") or s["entity_id"],
                        "area": "Unassigned",
                        "state": s["state"],
                    }
                )
            return out

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

    async def get_history_since(
        self, entity_ids: list[str], since_iso: str
    ) -> list[list[dict]]:
        """Fetch state history since a specific ISO-8601 timestamp.

        Returns the raw HA history shape: a list of per-entity state lists.
        """
        filter_ids = ",".join(entity_ids)
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{self._base_url}/api/history/period/{since_iso}",
                headers=self._headers,
                params={"filter_entity_id": filter_ids, "minimal_response": "true"},
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
