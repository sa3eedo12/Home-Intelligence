from __future__ import annotations

import ast
import json
import os
import re
from datetime import UTC, datetime
from typing import Any

import httpx

_ENTITY_ID_RE = re.compile(r"^[a-z_][a-z0-9_]*\.[A-Za-z0-9_]+$")


def _looks_like_entity_id(value: str) -> bool:
    return bool(_ENTITY_ID_RE.match(value.strip()))


def _parse_template_list(rendered: str) -> list[Any]:
    text = rendered.strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = ast.literal_eval(text)
    return parsed if isinstance(parsed, list) else []


class HAClient:
    def __init__(self, base_url: str, token: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    async def get_state(self, entity_id: str) -> dict:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{self._base_url}/api/states/{entity_id}", headers=self._headers
            )
            # HA returns 404 when the entity_id doesn't exist. The LLM
            # router frequently hallucinates plausible-sounding ids
            # (sensor.power_usage, climate.bedroom_thermostat) that don't
            # exist in this household. Returning a structured "not_found"
            # response lets the LLM apologise gracefully instead of the
            # agent throwing a 500 that surfaces as a raw HTTP error in
            # Telegram.
            if resp.status_code == 404:
                return {
                    "error": "entity_not_found",
                    "entity_id": entity_id,
                    "hint": (
                        "The Home Assistant entity does not exist. The id "
                        "may have been guessed; use list_entities to find "
                        "the correct one before calling get_entity_state."
                    ),
                }
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

    async def render_template(self, template: str) -> str:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{self._base_url}/api/template",
                headers=self._headers,
                json={"template": template},
            )
            resp.raise_for_status()
            return resp.text

    async def resolve_entity(self, name_or_id: str) -> dict | None:
        lookup = name_or_id.strip()
        if not lookup:
            return None
        if _looks_like_entity_id(lookup):
            try:
                return await self.get_state(lookup)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    return None
                raise

        states = await self.list_states()
        lookup_folded = lookup.casefold()
        for state in states:
            attrs = state.get("attributes", {}) or {}
            friendly_name = str(attrs.get("friendly_name") or "")
            if friendly_name.casefold() == lookup_folded:
                return state
        return None

    async def get_areas(self) -> list[dict]:
        template = "{{ areas() | map(attribute='name') | list }}"
        try:
            rendered = await self.render_template(template)
            areas = _parse_template_list(rendered)
        except Exception:
            return []

        normalized: list[dict] = []
        for area in areas:
            if isinstance(area, dict):
                name = area.get("name") or area.get("area_id") or area.get("id")
                if name:
                    normalized.append({**area, "name": str(name)})
            elif area not in (None, ""):
                normalized.append({"name": str(area)})
        return normalized

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
            rendered = await self.render_template(template)
            return json.loads(rendered)
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
