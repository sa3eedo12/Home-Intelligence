from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools import core


@pytest.mark.asyncio
async def test_library_index_json_calls_upsert(monkeypatch, tmp_path: Path) -> None:
    payload = {
        "items": [
            {"kind": "movie", "title": "Inception", "year": 2010, "summary": "dreams"},
            {"kind": "show", "title": "Dark", "year": 2017, "summary": "time"},
        ]
    }
    fp = tmp_path / "library.json"
    fp.write_text(json.dumps(payload), encoding="utf-8")

    called = {"count": 0}

    async def _fake_upsert(items):
        called["count"] = len(items)
        return len(items)

    monkeypatch.setattr(core, "_upsert_media", _fake_upsert)
    result = await core.library_index(str(fp))

    assert result["ok"] is True
    assert called["count"] == 2
