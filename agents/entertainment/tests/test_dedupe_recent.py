from __future__ import annotations

from tools.core import _recent_exclusion


def test_recent_watches_excluded_from_recommendations() -> None:
    rows = [{"title": "Dune"}, {"title": "Arrival"}, {"title": None}]
    excluded = _recent_exclusion(rows)
    assert "dune" in excluded
    assert "arrival" in excluded
