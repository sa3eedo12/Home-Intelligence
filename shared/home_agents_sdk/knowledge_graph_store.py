"""Knowledge graph store — property-graph CRUD + traversal helpers.

Models the household as a graph of typed nodes (person, device, area,
preference, routine, …) connected by typed edges (OWNS, LOCATED_IN,
PREFERS, OBSERVED_AT, …). Every node and edge carries JSONB attributes
for flexible per-type metadata plus a confidence score so the routine
inference engine can mark beliefs vs facts.

External references: every node can carry an ``external_ref`` (e.g.
``household_members:2``) that links back to the source-of-truth row in
another table. The upsert helpers use ``(type, external_ref)`` as the
natural key so re-seeding from the source tables is idempotent — change
a person's sleep_time in household_members, run the seeder again,
the corresponding kg_nodes row gets updated in place.

Soft delete via ``deleted_at`` — the graph is more useful as an
append-mostly historical record than as a hard-edit live snapshot,
since the routine inference looks at "what relationships did we
believe at time T?".

Storage is plain Postgres (no Neo4j dependency). Schema lives in
infra/postgres/init/23_knowledge_graph.sql.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import asyncpg


def _coerce_attrs(value: Any) -> str:
    """Render attributes to the JSON string asyncpg expects for jsonb."""
    if value is None:
        return "{}"
    if isinstance(value, str):
        return value
    return json.dumps(value, default=str, ensure_ascii=False)


def _row_to_node(row: asyncpg.Record | dict | None) -> dict[str, Any] | None:
    if row is None:
        return None
    d = dict(row)
    attrs = d.get("attributes")
    if isinstance(attrs, str):
        try:
            d["attributes"] = json.loads(attrs)
        except json.JSONDecodeError:
            d["attributes"] = {}
    elif attrs is None:
        d["attributes"] = {}
    return d


def _row_to_edge(row: asyncpg.Record | dict | None) -> dict[str, Any] | None:
    if row is None:
        return None
    d = dict(row)
    attrs = d.get("attributes")
    if isinstance(attrs, str):
        try:
            d["attributes"] = json.loads(attrs)
        except json.JSONDecodeError:
            d["attributes"] = {}
    elif attrs is None:
        d["attributes"] = {}
    return d


class KnowledgeGraphStore:
    """CRUD + traversal over the property-graph schema."""

    def __init__(self, pool: asyncpg.Pool | None) -> None:
        self.pool = pool

    @property
    def _ready(self) -> bool:
        return self.pool is not None

    # ── Node CRUD ────────────────────────────────────────────────────

    async def upsert_node(
        self,
        *,
        type: str,
        label: str,
        attributes: dict[str, Any] | None = None,
        external_ref: str | None = None,
        source: str | None = None,
        confidence: float = 1.0,
    ) -> int | None:
        """Insert or update a node.

        If ``external_ref`` is provided, the upsert key is
        ``(type, external_ref)`` — re-seeding from source tables is
        idempotent. Without it, every call creates a new node (use
        ``find_nodes`` first if you want deduplication on label).
        """
        if not self._ready or self.pool is None:
            return None
        attrs_json = _coerce_attrs(attributes)
        async with self.pool.acquire() as conn:
            if external_ref is not None:
                row = await conn.fetchrow(
                    """
                    INSERT INTO kg_nodes(
                        type, label, attributes, external_ref, source,
                        confidence
                    )
                    VALUES ($1, $2, $3::jsonb, $4, $5, $6)
                    ON CONFLICT (type, external_ref)
                      WHERE external_ref IS NOT NULL AND deleted_at IS NULL
                      DO UPDATE SET
                        label = EXCLUDED.label,
                        attributes = EXCLUDED.attributes,
                        source = COALESCE(EXCLUDED.source, kg_nodes.source),
                        confidence = EXCLUDED.confidence,
                        updated_at = now()
                    RETURNING id
                    """,
                    type, label, attrs_json, external_ref, source, float(confidence),
                )
            else:
                row = await conn.fetchrow(
                    """
                    INSERT INTO kg_nodes(type, label, attributes, source, confidence)
                    VALUES ($1, $2, $3::jsonb, $4, $5)
                    RETURNING id
                    """,
                    type, label, attrs_json, source, float(confidence),
                )
        return int(row["id"]) if row else None

    async def get_node(self, node_id: int) -> dict[str, Any] | None:
        if not self._ready or self.pool is None:
            return None
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, type, label, attributes, external_ref, source,
                       confidence, created_at, updated_at
                FROM kg_nodes
                WHERE id = $1 AND deleted_at IS NULL
                """,
                int(node_id),
            )
        return _row_to_node(row)

    async def find_nodes(
        self,
        *,
        type: str | None = None,
        label: str | None = None,
        external_ref: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Filter nodes by any combination of type / label (exact, case-
        insensitive) / external_ref."""
        if not self._ready or self.pool is None:
            return []
        clauses: list[str] = ["deleted_at IS NULL"]
        params: list[Any] = []
        if type is not None:
            params.append(type)
            clauses.append(f"type = ${len(params)}")
        if label is not None:
            params.append(label.casefold())
            clauses.append(f"lower(label) = ${len(params)}")
        if external_ref is not None:
            params.append(external_ref)
            clauses.append(f"external_ref = ${len(params)}")
        params.append(int(limit))
        query = (
            "SELECT id, type, label, attributes, external_ref, source, "
            "confidence, created_at, updated_at "
            "FROM kg_nodes WHERE " + " AND ".join(clauses) +
            f" ORDER BY updated_at DESC LIMIT ${len(params)}"
        )
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
        return [n for r in rows if (n := _row_to_node(r)) is not None]

    async def delete_node(self, node_id: int) -> bool:
        """Soft-delete a node and cascade-soft-delete its edges."""
        if not self._ready or self.pool is None:
            return False
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "UPDATE kg_nodes SET deleted_at = now() "
                    "WHERE id = $1 AND deleted_at IS NULL",
                    int(node_id),
                )
                await conn.execute(
                    """
                    UPDATE kg_edges SET deleted_at = now()
                    WHERE (source_node_id = $1 OR target_node_id = $1)
                      AND deleted_at IS NULL
                    """,
                    int(node_id),
                )
        return True

    # ── Edge CRUD ────────────────────────────────────────────────────

    async def upsert_edge(
        self,
        *,
        source_node_id: int,
        target_node_id: int,
        rel_type: str,
        attributes: dict[str, Any] | None = None,
        source: str | None = None,
        confidence: float = 1.0,
    ) -> int | None:
        """Insert or refresh an edge keyed on (source, target, rel_type).

        Edges are directional. Model an undirected relationship as two
        upserts (A→B and B→A) when needed.
        """
        if not self._ready or self.pool is None:
            return None
        attrs_json = _coerce_attrs(attributes)
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO kg_edges(
                    source_node_id, target_node_id, rel_type, attributes,
                    source, confidence
                )
                VALUES ($1, $2, $3, $4::jsonb, $5, $6)
                ON CONFLICT (source_node_id, target_node_id, rel_type)
                  WHERE deleted_at IS NULL
                  DO UPDATE SET
                    attributes = EXCLUDED.attributes,
                    source = COALESCE(EXCLUDED.source, kg_edges.source),
                    confidence = EXCLUDED.confidence,
                    updated_at = now()
                RETURNING id
                """,
                int(source_node_id), int(target_node_id), rel_type,
                attrs_json, source, float(confidence),
            )
        return int(row["id"]) if row else None

    async def find_edges(
        self,
        *,
        source_node_id: int | None = None,
        target_node_id: int | None = None,
        rel_type: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if not self._ready or self.pool is None:
            return []
        clauses: list[str] = ["deleted_at IS NULL"]
        params: list[Any] = []
        if source_node_id is not None:
            params.append(int(source_node_id))
            clauses.append(f"source_node_id = ${len(params)}")
        if target_node_id is not None:
            params.append(int(target_node_id))
            clauses.append(f"target_node_id = ${len(params)}")
        if rel_type is not None:
            params.append(rel_type)
            clauses.append(f"rel_type = ${len(params)}")
        params.append(int(limit))
        query = (
            "SELECT id, source_node_id, target_node_id, rel_type, "
            "attributes, source, confidence, created_at, updated_at "
            "FROM kg_edges WHERE " + " AND ".join(clauses) +
            f" ORDER BY updated_at DESC LIMIT ${len(params)}"
        )
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
        return [e for r in rows if (e := _row_to_edge(r)) is not None]

    async def delete_edge(self, edge_id: int) -> bool:
        if not self._ready or self.pool is None:
            return False
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE kg_edges SET deleted_at = now() "
                "WHERE id = $1 AND deleted_at IS NULL",
                int(edge_id),
            )
        return True

    # ── Traversal helpers ────────────────────────────────────────────

    async def neighbors(
        self,
        node_id: int,
        *,
        rel_type: str | None = None,
        direction: str = "out",
    ) -> list[dict[str, Any]]:
        """Return the directly-connected nodes.

        ``direction`` is 'out' (default — follow outgoing edges),
        'in' (incoming edges), or 'both'. Filter by ``rel_type`` for a
        narrower traversal (e.g. only OWNS edges).
        """
        if not self._ready or self.pool is None:
            return []
        if direction not in {"out", "in", "both"}:
            raise ValueError(f"direction must be out/in/both, got {direction!r}")
        clauses: list[str] = ["e.deleted_at IS NULL", "n.deleted_at IS NULL"]
        params: list[Any] = [int(node_id)]
        if direction == "out":
            join_cond = "n.id = e.target_node_id"
            clauses.append("e.source_node_id = $1")
        elif direction == "in":
            join_cond = "n.id = e.source_node_id"
            clauses.append("e.target_node_id = $1")
        else:  # both
            join_cond = "(n.id = e.target_node_id OR n.id = e.source_node_id)"
            clauses.append("(e.source_node_id = $1 OR e.target_node_id = $1)")
            clauses.append("n.id <> $1")
        if rel_type is not None:
            params.append(rel_type)
            clauses.append(f"e.rel_type = ${len(params)}")
        query = (
            "SELECT DISTINCT n.id, n.type, n.label, n.attributes, "
            "n.external_ref, n.source, n.confidence, "
            "n.created_at, n.updated_at "
            "FROM kg_edges e JOIN kg_nodes n ON " + join_cond +
            " WHERE " + " AND ".join(clauses) +
            " ORDER BY n.updated_at DESC LIMIT 200"
        )
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
        return [n for r in rows if (n := _row_to_node(r)) is not None]

    # ── Convenience helpers used by other agents ────────────────────

    async def who_owns(self, device_node_id: int) -> list[dict[str, Any]]:
        """All persons with OWNS edges pointing at this device."""
        return await self.neighbors(
            device_node_id, rel_type="OWNS", direction="in"
        )

    async def what_does_person_own(
        self, person_node_id: int
    ) -> list[dict[str, Any]]:
        return await self.neighbors(
            person_node_id, rel_type="OWNS", direction="out"
        )

    async def located_in(self, node_id: int) -> dict[str, Any] | None:
        """The single area node a device/thing is located in (if any)."""
        results = await self.neighbors(
            node_id, rel_type="LOCATED_IN", direction="out"
        )
        return results[0] if results else None

    async def stats(self) -> dict[str, Any]:
        """Quick summary for the dashboard / debug pages."""
        if not self._ready or self.pool is None:
            return {"nodes": 0, "edges": 0, "by_type": {}}
        async with self.pool.acquire() as conn:
            node_count = await conn.fetchval(
                "SELECT count(*)::int FROM kg_nodes WHERE deleted_at IS NULL"
            )
            edge_count = await conn.fetchval(
                "SELECT count(*)::int FROM kg_edges WHERE deleted_at IS NULL"
            )
            by_type_rows = await conn.fetch(
                """
                SELECT type, count(*)::int AS n
                FROM kg_nodes WHERE deleted_at IS NULL
                GROUP BY type ORDER BY n DESC
                """
            )
        return {
            "nodes": int(node_count or 0),
            "edges": int(edge_count or 0),
            "by_type": {r["type"]: int(r["n"]) for r in by_type_rows},
        }
