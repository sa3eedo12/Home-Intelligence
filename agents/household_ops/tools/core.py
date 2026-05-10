from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta
from typing import Any

import asyncpg
import dateparser
from home_agents_sdk import tool

_POOL: asyncpg.Pool | None = None

_WEEKDAY = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


async def _pool() -> asyncpg.Pool:
    global _POOL
    if _POOL is None:
        database_url = os.getenv(
            "DATABASE_URL", "postgresql://agents:changeme@postgres:5432/agents"
        )
        _POOL = await asyncpg.create_pool(database_url, min_size=1, max_size=5)
    return _POOL


def _parse_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    dt = dateparser.parse(
        raw,
        settings={
            "TIMEZONE": os.getenv("USER_TZ", "Asia/Dubai"),
            "RETURN_AS_TIMEZONE_AWARE": True,
            "PREFER_DATES_FROM": "future",
        },
    )
    return dt


def _split_items(raw: str) -> list[str]:
    return [p.strip() for p in raw.replace("\n", ",").split(",") if p.strip()]


def _next_due(current_due: datetime | None, recurrence: str, completed_at: datetime) -> datetime:
    base = current_due or completed_at
    if recurrence == "daily":
        return base + timedelta(days=1)
    if recurrence.startswith("weekly:"):
        tokens = [p.strip().lower() for p in recurrence.split(":", 1)[1].split(",") if p.strip()]
        targets = sorted(_WEEKDAY[t] for t in tokens if t in _WEEKDAY)
        if targets:
            current = base.weekday()
            for t in targets:
                if t > current:
                    return base + timedelta(days=(t - current))
            return base + timedelta(days=(7 - current + targets[0]))
    return base + timedelta(days=1)


@tool("chores_add", side_effects=True)
async def chores_add(
    title: str,
    due_at: str | None = None,
    recurrence: str | None = None,
) -> dict[str, Any]:
    due = _parse_dt(due_at)
    pool = await _pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO chores(title, due_at, recurrence, status)
            VALUES ($1, $2, $3, 'pending')
            RETURNING id, title, due_at, recurrence, status
            """,
            title,
            due,
            recurrence,
        )
    return dict(row)


@tool("chores_list")
async def chores_list(on_date: str | None = None) -> dict[str, Any]:
    day = _parse_dt(on_date).date() if on_date else None
    pool = await _pool()
    async with pool.acquire() as conn:
        if day is None:
            rows = await conn.fetch(
                "SELECT id, title, due_at, recurrence, status FROM chores "
                "WHERE status='pending' ORDER BY due_at NULLS LAST, id"
            )
        else:
            rows = await conn.fetch(
                (
                    "SELECT id, title, due_at, recurrence, status FROM chores "
                    "WHERE status='pending' AND due_at::date <= $1 "
                    "ORDER BY due_at NULLS LAST, id"
                ),
                day,
            )
    return {"items": [dict(r) for r in rows]}


@tool("chores_complete", side_effects=True)
async def chores_complete(chore_id: int) -> dict[str, Any]:
    now = datetime.now(UTC)
    pool = await _pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, title, due_at, recurrence FROM chores WHERE id = $1",
            chore_id,
        )
        if row is None:
            return {"ok": False, "error": "chore not found"}
        await conn.execute("UPDATE chores SET status='done' WHERE id=$1", chore_id)
        next_due = None
        if row["recurrence"]:
            next_due = _next_due(row["due_at"], row["recurrence"], now)
            await conn.execute(
                (
                    "INSERT INTO chores(title, due_at, recurrence, status) "
                    "VALUES ($1, $2, $3, 'pending')"
                ),
                row["title"],
                next_due,
                row["recurrence"],
            )
    return {"ok": True, "next_due": next_due.isoformat() if next_due else None}


@tool("shopping_list_add", side_effects=True)
async def shopping_list_add(items: str) -> dict[str, Any]:
    parts = _split_items(items)
    pool = await _pool()
    async with pool.acquire() as conn:
        for item in parts:
            await conn.execute("INSERT INTO shopping_list(item, checked) VALUES ($1, false)", item)
    return {"added": parts}


@tool("shopping_list_show")
async def shopping_list_show() -> dict[str, Any]:
    pool = await _pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, item, qty, checked, added_at FROM shopping_list "
            "ORDER BY checked ASC, id ASC"
        )
    return {"items": [dict(r) for r in rows]}


@tool("shopping_list_check", side_effects=True)
async def shopping_list_check(items: str) -> dict[str, Any]:
    parts = _split_items(items)
    pool = await _pool()
    async with pool.acquire() as conn:
        for item in parts:
            await conn.execute(
                "UPDATE shopping_list SET checked=true WHERE lower(item)=lower($1)",
                item,
            )
    return {"checked": parts}


@tool("shopping_list_clear", side_effects=True)
async def shopping_list_clear() -> dict[str, Any]:
    pool = await _pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM shopping_list")
    return {"ok": True}


@tool("pantry_add", side_effects=True)
async def pantry_add(item: str, qty: float, unit: str, expiry: str | None = None) -> dict[str, Any]:
    expires_on = _parse_dt(expiry).date() if expiry else None
    pool = await _pool()
    async with pool.acquire() as conn:
        updated = await conn.execute(
            """
            UPDATE pantry
            SET qty = qty + $2, unit = $3, expires_on = COALESCE($4, expires_on), updated_at = now()
            WHERE lower(item) = lower($1)
            """,
            item,
            qty,
            unit,
            expires_on,
        )
        if not updated.endswith("1"):
            await conn.execute(
                """
                INSERT INTO pantry(item, qty, unit, expires_on, updated_at)
                VALUES ($1, $2, $3, $4, now())
                """,
                item,
                qty,
                unit,
                expires_on,
            )
    return {"ok": True, "item": item}


@tool("pantry_consume", side_effects=True)
async def pantry_consume(item: str, qty: float) -> dict[str, Any]:
    pool = await _pool()
    async with pool.acquire() as conn:
        await conn.execute(
            (
                "UPDATE pantry SET qty = GREATEST(qty - $2, 0), updated_at = now() "
                "WHERE lower(item)=lower($1)"
            ),
            item,
            qty,
        )
    return {"ok": True, "item": item}


@tool("pantry_low_stock")
async def pantry_low_stock(threshold: float = 1.0) -> dict[str, Any]:
    pool = await _pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, item, qty, unit, expires_on FROM pantry WHERE qty <= $1 ORDER BY qty ASC",
            threshold,
        )
    return {"items": [dict(r) for r in rows]}


@tool("meal_plan")
async def meal_plan(days: int = 3) -> dict[str, Any]:
    prefs = [
        p.strip() for p in os.getenv("MEAL_PREFERENCES", "halal,no-pork").split(",") if p.strip()
    ]
    base_meals = [
        "grilled chicken salad",
        "lentil soup",
        "baked salmon with vegetables",
        "beef stir fry",
        "chicken shawarma bowl",
    ]
    plan = []
    for idx in range(days):
        d = date.today() + timedelta(days=idx)
        dish = base_meals[idx % len(base_meals)]
        plan.append({"plan_date": d.isoformat(), "meal": "dinner", "dish": dish})

    pool = await _pool()
    async with pool.acquire() as conn:
        for row in plan:
            await conn.execute(
                "INSERT INTO meal_plans(plan_date, meal, dish) VALUES ($1, $2, $3)",
                date.fromisoformat(row["plan_date"]),
                row["meal"],
                row["dish"],
            )
    return {"preferences": prefs, "plan": plan, "format": "json"}


@tool("meal_recipe")
async def meal_recipe(dish: str) -> dict[str, Any]:
    return {
        "dish": dish,
        "recipe": [
            "Prep ingredients.",
            "Cook protein and vegetables.",
            "Season to taste and serve warm.",
        ],
    }
