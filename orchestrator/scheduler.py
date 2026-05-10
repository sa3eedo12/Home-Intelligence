from __future__ import annotations

from apscheduler.schedulers.asyncio import AsyncIOScheduler


def build_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="Asia/Dubai")
    # Jobs added in PR 4
    return scheduler
