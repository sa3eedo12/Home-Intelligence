"""ChoreStore — recurring household chore templates + completion log.

Cadence-based: each template has a name, category, cadence_days, and
optional auto-detect entity (so a Roomba dock event can close the
"vacuum living room" chore without the user pressing anything).

`due` is computed from MAX(chore_log.completed_at) + cadence_days, so
the truth is always the log; the template just declares what should
happen and how often. Skipping a manual mark for a few days simply
makes the chore overdue rather than corrupting state.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import asyncpg


# Seed templates that get installed on first boot if the table is empty.
# Chosen to cover the common-case household: cleaning, laundry, kitchen,
# bathroom, plants, trash. User edits / adds via dashboard or Telegram.
DEFAULT_TEMPLATES: list[dict[str, Any]] = [
    {"name": "Vacuum the house", "category": "cleaning", "cadence_days": 7, "grace_days": 2,
     "auto_detect_kind": "vacuum",
     "description": "Run the vacuum across the whole apartment."},
    {"name": "Mop kitchen and bathrooms", "category": "cleaning", "cadence_days": 14, "grace_days": 3,
     "description": "Wet mop kitchen and both bathrooms."},
    {"name": "Deep clean the bathroom", "category": "bathroom", "cadence_days": 14, "grace_days": 3,
     "description": "Scrub shower, toilet, sink, mirror, replenish products."},
    {"name": "Change bed sheets", "category": "bedroom", "cadence_days": 7, "grace_days": 2,
     "description": "Strip and wash bed sheets, swap with a clean set."},
    {"name": "Swap pillowcases", "category": "bedroom", "cadence_days": 4, "grace_days": 1,
     "description": "Quick pillowcase swap mid-week."},
    {"name": "Swap bath towels", "category": "bathroom", "cadence_days": 5, "grace_days": 2,
     "description": "Send used bath towels to laundry, hang fresh ones."},
    {"name": "Swap kitchen towels", "category": "kitchen", "cadence_days": 7, "grace_days": 2,
     "description": "Replace kitchen dish towels."},
    {"name": "Water the plants", "category": "plants", "cadence_days": 3, "grace_days": 1,
     "description": "Water indoor plants and balcony pots."},
    {"name": "Take out the trash", "category": "trash", "cadence_days": 3, "grace_days": 1,
     "description": "Empty kitchen bin and take bag to building chute."},
    {"name": "Wipe down kitchen counters", "category": "kitchen", "cadence_days": 2, "grace_days": 1,
     "description": "Wipe stove, counters, table. Quick reset."},
    {"name": "Laundry load", "category": "laundry", "cadence_days": 4, "grace_days": 2,
     "auto_detect_kind": "washer",
     "description": "Wash + dry one load."},
    {"name": "Iron shirts", "category": "laundry", "cadence_days": 14, "grace_days": 5,
     "description": "Iron the week's shirts in one go."},
    {"name": "Clean out the fridge", "category": "kitchen", "cadence_days": 14, "grace_days": 5,
     "description": "Toss expired stuff, wipe shelves."},
    {"name": "Car wash", "category": "errands", "cadence_days": 21, "grace_days": 7,
     "description": "Exterior wash, vacuum inside if needed."},
    {"name": "Wash bathroom mats", "category": "bathroom", "cadence_days": 14, "grace_days": 5,
     "description": "Toss bathroom and entrance mats in the wash."},
]


@dataclass(slots=True)
class ChoreStatus:
    """A chore's computed state at a point in time."""
    template_id: int
    name: str
    category: str
    cadence_days: int
    grace_days: int
    auto_detect_kind: str | None
    auto_detect_entity: str | None
    last_done_at: datetime | None
    last_done_by: int | None
    next_due_on: date
    days_overdue: int                 # negative = days until due, positive = days late
    status: str                       # 'overdue' | 'due_today' | 'soon' | 'recent'
    description: str | None


class ChoreStore:
    def __init__(self, pool: asyncpg.Pool | None) -> None:
        self.pool = pool

    @property
    def _ready(self) -> bool:
        return self.pool is not None

    # ── Templates ────────────────────────────────────────────────

    async def seed_defaults(self) -> int:
        """Install DEFAULT_TEMPLATES if the table is empty. Idempotent —
        runs on every boot but only inserts the first time. Returns the
        number of templates inserted."""
        if not self._ready or self.pool is None:
            return 0
        async with self.pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT count(*)::int FROM chore_templates"
            )
            if count and count > 0:
                return 0
            inserted = 0
            for t in DEFAULT_TEMPLATES:
                await conn.execute(
                    """
                    INSERT INTO chore_templates(
                        name, category, cadence_days, grace_days,
                        auto_detect_kind, description
                    )
                    VALUES ($1, $2, $3, $4, $5, $6)
                    ON CONFLICT (name) DO NOTHING
                    """,
                    t["name"], t["category"], t["cadence_days"], t.get("grace_days", 1),
                    t.get("auto_detect_kind"), t.get("description"),
                )
                inserted += 1
        return inserted

    async def list_templates(
        self, *, active_only: bool = True, category: str | None = None
    ) -> list[dict[str, Any]]:
        if not self._ready or self.pool is None:
            return []
        clauses, params = [], []
        if active_only:
            clauses.append("active")
        if category:
            params.append(category)
            clauses.append(f"category = ${len(params)}")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT id, name, category, cadence_days, grace_days,
                       auto_detect_kind, auto_detect_entity, default_member_id,
                       description, active
                FROM chore_templates {where}
                ORDER BY category, name
                """,
                *params,
            )
        return [dict(r) for r in rows]

    async def upsert_template(
        self,
        *,
        name: str,
        category: str = "general",
        cadence_days: int,
        grace_days: int = 1,
        auto_detect_kind: str | None = None,
        auto_detect_entity: str | None = None,
        default_member_id: int | None = None,
        description: str | None = None,
        active: bool = True,
    ) -> int | None:
        if not self._ready or self.pool is None:
            return None
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO chore_templates(
                    name, category, cadence_days, grace_days,
                    auto_detect_kind, auto_detect_entity,
                    default_member_id, description, active
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                ON CONFLICT (name) DO UPDATE SET
                    category = EXCLUDED.category,
                    cadence_days = EXCLUDED.cadence_days,
                    grace_days = EXCLUDED.grace_days,
                    auto_detect_kind = EXCLUDED.auto_detect_kind,
                    auto_detect_entity = EXCLUDED.auto_detect_entity,
                    default_member_id = EXCLUDED.default_member_id,
                    description = EXCLUDED.description,
                    active = EXCLUDED.active,
                    updated_at = now()
                RETURNING id
                """,
                name, category, int(cadence_days), int(grace_days),
                auto_detect_kind, auto_detect_entity,
                default_member_id, description, active,
            )
        return int(row["id"]) if row else None

    # ── Completion log ───────────────────────────────────────────

    async def log_completion(
        self,
        template_id: int,
        *,
        member_id: int | None = None,
        source: str = "manual",
        note: str | None = None,
        completed_at: datetime | None = None,
        evidence_event_log_id: int | None = None,
    ) -> int | None:
        """Record a chore completion. Returns the new chore_log id."""
        if not self._ready or self.pool is None:
            return None
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO chore_log(
                    chore_template_id, completed_at, member_id,
                    source, note, evidence_event_log_id
                )
                VALUES ($1, COALESCE($2, now()), $3, $4, $5, $6)
                RETURNING id
                """,
                int(template_id), completed_at, member_id, source, note,
                evidence_event_log_id,
            )
        return int(row["id"]) if row else None

    async def log_by_name(
        self,
        name: str,
        *,
        member_id: int | None = None,
        source: str = "telegram",
        note: str | None = None,
    ) -> int | None:
        """Convenience for the Telegram path — find by exact or fuzzy
        name match, then log. Returns template id, or None if no match."""
        if not self._ready or self.pool is None:
            return None
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id FROM chore_templates
                WHERE active AND (
                    lower(name) = lower($1)
                    OR lower(name) LIKE lower($1) || '%'
                    OR lower($1) LIKE '%' || lower(name) || '%'
                )
                ORDER BY length(name) ASC LIMIT 1
                """,
                name.strip(),
            )
            if row is None:
                return None
            template_id = int(row["id"])
        await self.log_completion(
            template_id, member_id=member_id, source=source, note=note
        )
        return template_id

    # ── Status / due-today queries ───────────────────────────────

    async def list_status(
        self, *, now: datetime | None = None, include_recent: bool = True
    ) -> list[ChoreStatus]:
        """Return computed status for every active template.

        Joins each template with its most-recent log row, computes
        next_due_on, days_overdue, and bucket label. Pure SQL — runs
        fast on hundreds of templates with thousands of log entries."""
        if not self._ready or self.pool is None:
            return []
        now = now or datetime.now(UTC)
        today = now.date()
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT t.id, t.name, t.category, t.cadence_days, t.grace_days,
                       t.auto_detect_kind, t.auto_detect_entity, t.description,
                       last_log.completed_at AS last_done_at,
                       last_log.member_id AS last_done_by
                FROM chore_templates t
                LEFT JOIN LATERAL (
                    SELECT completed_at, member_id
                    FROM chore_log
                    WHERE chore_template_id = t.id
                    ORDER BY completed_at DESC LIMIT 1
                ) last_log ON true
                WHERE t.active
                ORDER BY t.category, t.name
                """
            )
        out: list[ChoreStatus] = []
        for r in rows:
            last_done = r["last_done_at"]
            if last_done is None:
                # Never done. Treat as due_today on first run so it
                # surfaces and the user can either do it or dismiss
                # the template.
                next_due = today
                days_overdue = 0
            else:
                next_due = (last_done.date() if isinstance(last_done, datetime)
                            else last_done) + timedelta(days=int(r["cadence_days"]))
                days_overdue = (today - next_due).days
            label = _bucket(days_overdue, r["grace_days"])
            if not include_recent and label == "recent":
                continue
            out.append(ChoreStatus(
                template_id=int(r["id"]),
                name=r["name"],
                category=r["category"],
                cadence_days=int(r["cadence_days"]),
                grace_days=int(r["grace_days"]),
                auto_detect_kind=r["auto_detect_kind"],
                auto_detect_entity=r["auto_detect_entity"],
                last_done_at=last_done if isinstance(last_done, datetime) else None,
                last_done_by=int(r["last_done_by"]) if r["last_done_by"] is not None else None,
                next_due_on=next_due,
                days_overdue=days_overdue,
                status=label,
                description=r["description"],
            ))
        return out

    async def history(
        self, template_id: int, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        if not self._ready or self.pool is None:
            return []
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, completed_at, member_id, source, note
                FROM chore_log WHERE chore_template_id = $1
                ORDER BY completed_at DESC LIMIT $2
                """,
                int(template_id), int(limit),
            )
        return [dict(r) for r in rows]


def _bucket(days_overdue: int, grace_days: int) -> str:
    """Bucket label from the days_overdue signed integer."""
    if days_overdue >= grace_days + 1:
        return "overdue"
    if days_overdue >= 0:
        return "due_today"
    if days_overdue >= -2:
        return "soon"
    return "recent"
