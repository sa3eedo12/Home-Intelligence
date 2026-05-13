from __future__ import annotations

from pathlib import Path

import asyncpg
import pytest

from orchestrator.migrations import _is_duplicate_object_error, _sha256, run_pending_migrations


def test_sha256_deterministic() -> None:
    assert _sha256("hello") == _sha256("hello")
    assert _sha256("a") != _sha256("b")


def test_is_duplicate_object_error_recognizes_postgres_text() -> None:
    class FakeExc(Exception):
        pass

    assert _is_duplicate_object_error(FakeExc("relation foo already exists"))
    assert _is_duplicate_object_error(FakeExc("type duplicate_object"))
    assert not _is_duplicate_object_error(FakeExc("syntax error at or near 'X'"))


class _FakeConn:
    def __init__(self) -> None:
        self.executed: list[str] = []
        self.tracked: dict[str, str] = {}

    async def execute(self, sql: str, *args) -> None:
        self.executed.append(sql.strip()[:80])
        if sql.strip().startswith("INSERT INTO applied_migrations"):
            filename, digest = args[0], args[1]
            self.tracked[filename] = digest

    async def fetchrow(self, sql: str, *args):
        if "SELECT filename, sha256" in sql:
            name = args[0]
            if name in self.tracked:
                return {"filename": name, "sha256": self.tracked[name]}
            return None
        return None

    def transaction(self):
        class _Tx:
            async def __aenter__(self_inner):
                return self_inner

            async def __aexit__(self_inner, exc_type, exc, tb):
                return False

        return _Tx()


class _FakePool:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def acquire(self):
        conn = self._conn

        class _Acq:
            async def __aenter__(self_inner):
                return conn

            async def __aexit__(self_inner, exc_type, exc, tb):
                return False

        return _Acq()


@pytest.mark.asyncio
async def test_run_pending_applies_in_order_and_skips_already_applied(tmp_path: Path) -> None:
    init_dir = tmp_path / "init"
    init_dir.mkdir()
    (init_dir / "10_first.sql").write_text("SELECT 1;", encoding="utf-8")
    (init_dir / "20_second.sql").write_text("SELECT 2;", encoding="utf-8")

    conn = _FakeConn()
    pool = _FakePool(conn)
    results = await run_pending_migrations(pool, init_dir=init_dir)
    assert [r["status"] for r in results] == ["applied", "applied"]
    assert [r["file"] for r in results] == ["10_first.sql", "20_second.sql"]

    # Re-run: both should be skipped
    results2 = await run_pending_migrations(pool, init_dir=init_dir)
    assert [r["status"] for r in results2] == ["skipped", "skipped"]


@pytest.mark.asyncio
async def test_run_pending_marks_changed_file_for_re_apply(tmp_path: Path) -> None:
    init_dir = tmp_path / "init"
    init_dir.mkdir()
    f = init_dir / "10_x.sql"
    f.write_text("SELECT 1;", encoding="utf-8")

    conn = _FakeConn()
    pool = _FakePool(conn)
    results1 = await run_pending_migrations(pool, init_dir=init_dir)
    assert results1[0]["status"] == "applied"

    # Bump the file content; should re-apply (different sha256)
    f.write_text("SELECT 2;  -- updated", encoding="utf-8")
    results2 = await run_pending_migrations(pool, init_dir=init_dir)
    assert results2[0]["status"] == "applied"


@pytest.mark.asyncio
async def test_run_pending_returns_empty_when_no_init_dir(tmp_path: Path) -> None:
    results = await run_pending_migrations(_FakePool(_FakeConn()), init_dir=tmp_path / "missing")
    assert results == []


@pytest.mark.asyncio
async def test_run_pending_records_per_file_error_without_aborting(tmp_path: Path) -> None:
    init_dir = tmp_path / "init"
    init_dir.mkdir()
    (init_dir / "10_good.sql").write_text("SELECT 1;", encoding="utf-8")
    (init_dir / "20_bad.sql").write_text("SELECT * FROM nonexistent_table;", encoding="utf-8")
    (init_dir / "30_also_good.sql").write_text("SELECT 3;", encoding="utf-8")

    class _ConnRaisesOnBad(_FakeConn):
        async def execute(self, sql: str, *args) -> None:
            if "nonexistent_table" in sql:
                raise asyncpg.exceptions.UndefinedTableError(
                    'relation "nonexistent_table" does not exist'
                )
            await super().execute(sql, *args)

    conn = _ConnRaisesOnBad()
    pool = _FakePool(conn)
    results = await run_pending_migrations(pool, init_dir=init_dir)
    statuses = {r["file"]: r["status"] for r in results}
    assert statuses["10_good.sql"] == "applied"
    assert statuses["20_bad.sql"] == "error"
    assert statuses["30_also_good.sql"] == "applied", "later files must still run after a failure"
