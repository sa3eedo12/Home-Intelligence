from __future__ import annotations

from types import SimpleNamespace

import pytest

from tools import core


class _FakeEmbedder:
    async def embed(self, text: str):
        return [0.1] * 1024


class _FakeQdrant:
    def __init__(self) -> None:
        self.calls = []

    async def search(self, collection, query_vector, limit):
        self.calls.append((collection, len(query_vector), limit))
        return [
            SimpleNamespace(
                score=0.9,
                payload={
                    "path": "/tmp/note.md",
                    "chunk_index": 0,
                    "start_line": 1,
                    "end_line": 3,
                    "text": "hello",
                },
            )
        ]


@pytest.mark.asyncio
async def test_search_sends_expected_query_and_limit(monkeypatch) -> None:
    fake_q = _FakeQdrant()
    monkeypatch.setattr(core, "_qdrant", lambda: fake_q)

    async def _fake_embedder():
        return _FakeEmbedder()

    monkeypatch.setattr(core, "_embedder", _fake_embedder)

    res = await core.search("air fryer", top_k=7)

    assert fake_q.calls[0][0] == "notes"
    assert fake_q.calls[0][2] == 7
    assert res["items"][0]["path"] == "/tmp/note.md"
