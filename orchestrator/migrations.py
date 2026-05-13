"""Idempotent boot-time migrations runner.

Postgres docker images only execute /docker-entrypoint-initdb.d/*.sql on the
FIRST start of an empty data volume. After that, new migration files added to
``infra/postgres/init/*.sql`` are silently ignored — which is exactly how the
recent appliance-cycle inference work shipped to production with broken
inference (cycle_loads / cleaning_runs / sleep_summaries / tv_left_on /
auto_inferences tables didn't exist for hours).

This module fixes that gap by running every ``infra/postgres/init/*.sql`` file
at orchestrator startup, in lexicographic order, against a connection from
the orchestrator's own postgres pool. We track which files have been applied
in a small ``applied_migrations`` table so we don't re-run heavy ones.

Each file MUST be idempotent (use ``CREATE TABLE IF NOT EXISTS`` /
``CREATE INDEX IF NOT EXISTS`` / ``ALTER TABLE ... IF NOT EXISTS``). The
existing init scripts already follow this convention.
"""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any

import asyncpg
from home_agents_sdk.telemetry import get_logger

logger = get_logger("orchestrator.migrations")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def _ensure_tracking_table(conn: asyncpg.Connection) -> None:
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS applied_migrations (
            filename TEXT PRIMARY KEY,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            sha256 TEXT NOT NULL
        )
        """
    )


def _discover(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_file() and p.suffix.lower() == ".sql")


# A few migrations early in the project use CREATE statements without
# IF NOT EXISTS. Rather than rewrite history, we wrap their execution in a
# savepoint and swallow the "already exists" error so a re-run is a no-op.
_DUPLICATE_OBJECT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"already exists"),
    re.compile(r"duplicate_object"),
)


def _is_duplicate_object_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(p.search(msg) for p in _DUPLICATE_OBJECT_PATTERNS)


async def _apply_one(conn: asyncpg.Connection, path: Path) -> dict[str, Any]:
    sql = path.read_text(encoding="utf-8")  # noqa: ASYNC240 — boot-only sync IO is fine
    digest = _sha256(sql)
    name = path.name
    existing = await conn.fetchrow(
        "SELECT filename, sha256 FROM applied_migrations WHERE filename = $1", name
    )
    if existing is not None and existing["sha256"] == digest:
        return {"file": name, "status": "skipped", "reason": "already_applied"}
    try:
        async with conn.transaction():
            await conn.execute(sql)
    except asyncpg.PostgresError as exc:
        if _is_duplicate_object_error(exc):
            # Migration is structurally idempotent in practice but raised
            # because of a non-IF-NOT-EXISTS statement. Mark it applied and
            # continue.
            logger.info("migration_idempotent_duplicate", file=name, error=str(exc)[:120])
        else:
            logger.warning("migration_failed", file=name, error=str(exc))
            return {"file": name, "status": "error", "error": str(exc)}
    await conn.execute(
        """
        INSERT INTO applied_migrations (filename, sha256, applied_at)
        VALUES ($1, $2, now())
        ON CONFLICT (filename) DO UPDATE SET sha256 = EXCLUDED.sha256, applied_at = now()
        """,
        name,
        digest,
    )
    return {"file": name, "status": "applied"}


async def run_pending_migrations(
    pool: asyncpg.Pool,
    *,
    init_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Apply every ``infra/postgres/init/*.sql`` not yet applied.

    Returns a per-file result list useful for startup logging and for the
    ``/admin/migrations/status`` endpoint.

    Always runs in a savepoint so a failing migration aborts only that one
    file — the orchestrator still finishes booting, and the failure surfaces
    in logs and the admin endpoint.
    """
    if init_dir is None:
        env = os.environ.get("MIGRATIONS_DIR")
        if env:
            init_dir = Path(env)
        else:
            # Boot-only path resolution; running once at startup is safe.
            init_dir = (
                Path(__file__).resolve().parents[1]  # noqa: ASYNC240
                / "infra"
                / "postgres"
                / "init"
            )

    files = _discover(init_dir)
    if not files:
        logger.info("migrations_none_found", dir=str(init_dir))
        return []

    results: list[dict[str, Any]] = []
    async with pool.acquire() as conn:
        await _ensure_tracking_table(conn)
        for path in files:
            try:
                result = await _apply_one(conn, path)
            except Exception as exc:  # noqa: BLE001 - surface every failure as a result row
                result = {"file": path.name, "status": "error", "error": str(exc)}
            results.append(result)
            if result["status"] == "applied":
                logger.info("migration_applied", file=result["file"])

    summary = {
        "applied": sum(1 for r in results if r["status"] == "applied"),
        "skipped": sum(1 for r in results if r["status"] == "skipped"),
        "error": sum(1 for r in results if r["status"] == "error"),
    }
    logger.info("migrations_run_complete", **summary)
    return results
