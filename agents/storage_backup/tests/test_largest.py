from __future__ import annotations

from pathlib import Path

from tools.core import largest_files


def test_largest_files_orders_and_limits(tmp_path: Path) -> None:
    small = tmp_path / "small.bin"
    large = tmp_path / "large.bin"
    small.write_bytes(b"a" * 10)
    large.write_bytes(b"a" * 100)

    result = largest_files(str(tmp_path), limit=1)
    assert len(result["items"]) == 1
    assert result["items"][0]["path"].endswith("large.bin")
