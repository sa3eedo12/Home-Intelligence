from __future__ import annotations

from tools.core import _split_items


def test_shopping_split() -> None:
    assert _split_items("milk, eggs, bread") == ["milk", "eggs", "bread"]
