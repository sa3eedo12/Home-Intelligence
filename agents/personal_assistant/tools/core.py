from __future__ import annotations

import os
import re
from datetime import UTC, date, datetime, timedelta
from typing import Any

import asyncpg
import dateparser
import httpx
from home_agents_sdk import tool

_POOL: asyncpg.Pool | None = None


async def _pool() -> asyncpg.Pool:
    global _POOL
    if _POOL is None:
        database_url = os.getenv(
            "DATABASE_URL", "postgresql://agents:changeme@postgres:5432/agents"
        )
        _POOL = await asyncpg.create_pool(database_url, min_size=1, max_size=5)
    return _POOL


def _user_tz() -> str:
    return os.getenv("USER_TZ", "Asia/Dubai")


def parse_nl_datetime(value: str) -> datetime:
    parsed = dateparser.parse(
        value,
        settings={
            "TIMEZONE": _user_tz(),
            "TO_TIMEZONE": _user_tz(),
            "RETURN_AS_TIMEZONE_AWARE": True,
            "PREFER_DATES_FROM": "future",
        },
    )
    if parsed is None:
        match = re.match(
            r"^next\s+(mon|tue|wed|thu|fri|sat|sun)(?:day)?(?:\s+at\s+(.+))?$",
            value.strip(),
            re.I,
        )
        if match:
            weekday_map = {
                "mon": 0,
                "tue": 1,
                "wed": 2,
                "thu": 3,
                "fri": 4,
                "sat": 5,
                "sun": 6,
            }
            now = datetime.now().astimezone()
            target_weekday = weekday_map[match.group(1).lower()]
            days_ahead = (target_weekday - now.weekday()) % 7
            days_ahead = 7 if days_ahead == 0 else days_ahead
            candidate = now + timedelta(days=days_ahead)
            if match.group(2):
                time_part = dateparser.parse(
                    match.group(2),
                    settings={
                        "TIMEZONE": _user_tz(),
                        "TO_TIMEZONE": _user_tz(),
                        "RETURN_AS_TIMEZONE_AWARE": True,
                    },
                )
                if time_part is not None:
                    candidate = candidate.replace(
                        hour=time_part.hour,
                        minute=time_part.minute,
                        second=0,
                        microsecond=0,
                    )
            parsed = candidate
    if parsed is None:
        raise ValueError(f"Could not parse datetime from: {value}")
    return parsed


@tool("add_reminder", side_effects=True)
async def add_reminder(text: str, due_time: str, user_id: str = "default") -> dict[str, Any]:
    due_at = parse_nl_datetime(due_time)
    pool = await _pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO reminders(user_id, text, due_at, status)
            VALUES ($1, $2, $3, 'pending')
            RETURNING id, text, due_at, status
            """,
            user_id,
            text,
            due_at,
        )
    return dict(row)


@tool("list_reminders")
async def list_reminders(status: str = "pending", limit: int = 20) -> dict[str, Any]:
    pool = await _pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, text, due_at, status
            FROM reminders
            WHERE ($1 = '' OR status = $1)
            ORDER BY due_at NULLS LAST, id DESC
            LIMIT $2
            """,
            status,
            limit,
        )
    return {"items": [dict(r) for r in rows]}


@tool("cancel_reminder", side_effects=True)
async def cancel_reminder(reminder_id: int) -> dict[str, Any]:
    pool = await _pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE reminders SET status = 'cancelled' WHERE id = $1",
            reminder_id,
        )
    return {"ok": result.endswith("1")}


@tool("add_renewal", side_effects=True)
async def add_renewal(label: str, renews_on: str, lead_days: int = 14) -> dict[str, Any]:
    renew_date = parse_nl_datetime(renews_on).date()
    pool = await _pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO renewals(label, renews_on, lead_days, status)
            VALUES ($1, $2, $3, 'active')
            RETURNING id, label, renews_on, lead_days, status
            """,
            label,
            renew_date,
            lead_days,
        )
    return dict(row)


@tool("list_renewals")
async def list_renewals(within_days: int = 60) -> dict[str, Any]:
    now_local = datetime.now().astimezone().date()
    upper = now_local + timedelta(days=within_days)
    pool = await _pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, label, renews_on, lead_days, status
            FROM renewals
            WHERE status = 'active' AND renews_on <= $1
            ORDER BY renews_on ASC
            """,
            upper,
        )
    return {"items": [dict(r) for r in rows]}


@tool("add_appointment", side_effects=True)
async def add_appointment(
    title: str,
    starts_at: str,
    ends_at: str,
    location: str = "",
    notes: str = "",
) -> dict[str, Any]:
    start_dt = parse_nl_datetime(starts_at)
    end_dt = parse_nl_datetime(ends_at)
    pool = await _pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO appointments(title, starts_at, ends_at, location, notes)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id, title, starts_at, ends_at, location, notes
            """,
            title,
            start_dt,
            end_dt,
            location,
            notes,
        )
    return dict(row)


@tool("list_appointments")
async def list_appointments(days: int = 14) -> dict[str, Any]:
    now_utc = datetime.now(UTC)
    upper = now_utc + timedelta(days=days)
    pool = await _pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, title, starts_at, ends_at, location, notes
            FROM appointments
            WHERE starts_at >= $1 AND starts_at <= $2
            ORDER BY starts_at ASC
            """,
            now_utc,
            upper,
        )
    return {"items": [dict(r) for r in rows]}


async def _fetch_chores() -> list[str]:
    orchestrator_url = os.getenv("ORCHESTRATOR_URL", "http://orchestrator:8000")
    try:
        async with httpx.AsyncClient(timeout=4) as client:
            resp = await client.post(
                f"{orchestrator_url.rstrip('/')}/dispatch",
                json={
                    "agent": "household_ops",
                    "capability": "chores_list",
                    "inputs": {},
                },
            )
            resp.raise_for_status()
            payload = resp.json()
    except Exception:
        return []
    if not payload.get("ok"):
        return []
    items = payload.get("result", {}).get("items", [])
    return [str(i.get("title", "")) for i in items[:3] if isinstance(i, dict)]


def _cap(text: str, max_len: int = 600) -> str:
    return text if len(text) <= max_len else f"{text[: max_len - 1]}…"


@tool("morning_brief")
async def morning_brief() -> dict[str, str]:
    today = date.today()
    pool = await _pool()
    async with pool.acquire() as conn:
        reminders = await conn.fetch(
            (
                "SELECT text FROM reminders WHERE status='pending' "
                "AND due_at::date <= $1 ORDER BY due_at ASC LIMIT 3"
            ),
            today,
        )
        appointments = await conn.fetch(
            (
                "SELECT title FROM appointments "
                "WHERE starts_at::date = $1 ORDER BY starts_at ASC LIMIT 3"
            ),
            today,
        )
    chores = await _fetch_chores()
    body = (
        f"### Morning Brief ({today.isoformat()})\n"
        f"- Appointments: {', '.join(r['title'] for r in appointments) or 'None'}\n"
        f"- Due reminders: {', '.join(r['text'] for r in reminders) or 'None'}\n"
        f"- Unfinished chores: {', '.join(chores) or 'Unavailable'}"
    )
    return {"markdown": _cap(body)}


@tool("evening_recap")
async def evening_recap() -> dict[str, str]:
    today = date.today()
    tomorrow = today + timedelta(days=1)
    pool = await _pool()
    async with pool.acquire() as conn:
        appointments = await conn.fetch(
            (
                "SELECT title FROM appointments "
                "WHERE starts_at::date = $1 ORDER BY starts_at ASC LIMIT 3"
            ),
            tomorrow,
        )
        reminders = await conn.fetch(
            (
                "SELECT text FROM reminders WHERE status='pending' "
                "AND due_at::date = $1 ORDER BY due_at ASC LIMIT 3"
            ),
            today,
        )
        alerts = await conn.fetch("SELECT topic FROM alerts ORDER BY created_at DESC LIMIT 3")
    body = (
        f"### Evening Recap\n"
        f"- Tomorrow: {', '.join(r['title'] for r in appointments) or 'No appointments'}\n"
        f"- Tonight reminders: {', '.join(r['text'] for r in reminders) or 'None'}\n"
        f"- Recent alerts: {', '.join(r['topic'] for r in alerts) or 'None'}"
    )
    return {"markdown": _cap(body)}
