from __future__ import annotations

import json
import uuid
from typing import Any

import asyncpg


class WorkflowEngine:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def start(self, workflow: dict) -> str:
        wf_id = str(uuid.uuid4())
        status = "awaiting_user" if workflow.get("needs_confirmation") else "running"
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO workflows (id, status, payload, created_at, updated_at)
                VALUES ($1, $2, $3, now(), now())
                """,
                wf_id,
                status,
                json.dumps(workflow),
            )
        return wf_id

    async def _fetch_payload(self, conn: asyncpg.Connection, workflow_id: str) -> dict:
        row = await conn.fetchrow("SELECT payload FROM workflows WHERE id = $1", workflow_id)
        if row is None:
            raise KeyError(f"Workflow not found: {workflow_id}")
        return json.loads(row["payload"])

    async def resume(self, workflow_id: str, user_choice: dict) -> dict:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT payload FROM workflows WHERE id = $1 FOR UPDATE", workflow_id
                )
                if row is None:
                    raise KeyError(f"Workflow not found: {workflow_id}")
                await conn.execute(
                    "UPDATE workflows SET status = 'running', updated_at = now() WHERE id = $1",
                    workflow_id,
                )
                return json.loads(row["payload"])

    async def mark_done(self, workflow_id: str, result: Any) -> None:
        async with self.pool.acquire() as conn:
            payload = await self._fetch_payload(conn, workflow_id)
            payload["result"] = result
            await conn.execute(
                "UPDATE workflows SET status = 'done', payload = $1,"
                " updated_at = now() WHERE id = $2",
                json.dumps(payload),
                workflow_id,
            )

    async def mark_failed(self, workflow_id: str, error: str) -> None:
        async with self.pool.acquire() as conn:
            payload = await self._fetch_payload(conn, workflow_id)
            payload["error"] = error
            await conn.execute(
                "UPDATE workflows SET status = 'failed', payload = $1,"
                " updated_at = now() WHERE id = $2",
                json.dumps(payload),
                workflow_id,
            )

    async def list_pending(self) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, status, payload FROM workflows"
                " WHERE status IN ('running', 'awaiting_user')"
            )
        return [
            {"id": str(r["id"]), "status": r["status"], "payload": json.loads(r["payload"])}
            for r in rows
        ]
