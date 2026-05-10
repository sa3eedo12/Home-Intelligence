from __future__ import annotations

from pathlib import Path

from tools.core import find_duplicates


def test_find_duplicates_detects_known_dupes(tmp_path: Path) -> None:
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    c = tmp_path / "c.txt"
    a.write_text("same", encoding="utf-8")
    b.write_text("same", encoding="utf-8")
    c.write_text("different", encoding="utf-8")

    result = find_duplicates(str(tmp_path))
    assert result["groups"]
    assert any(len(group["files"]) >= 2 for group in result["groups"])
