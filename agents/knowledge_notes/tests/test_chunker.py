from __future__ import annotations

from tools.core import _chunk_text


def test_chunk_overlap_and_boundaries() -> None:
    text = " ".join(f"tok{i}" for i in range(0, 1200))
    chunks = _chunk_text(text, chunk_tokens=500, overlap=80)
    assert len(chunks) >= 3
    assert chunks[0]["start_line"] == 1
    assert chunks[1]["start_line"] < chunks[0]["end_line"]
    assert chunks[-1]["end_line"] <= 1200
