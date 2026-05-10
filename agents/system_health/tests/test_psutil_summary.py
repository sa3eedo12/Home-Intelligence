from __future__ import annotations

from tools import core


def test_scan_returns_expected_keys(monkeypatch) -> None:
    monkeypatch.setattr(core, "container_status", lambda: {"items": []})
    data = core.scan()
    assert "metrics" in data
    for key in ("cpu_pct", "ram_pct", "swap_pct", "disk", "top_processes"):
        assert key in data["metrics"]
    assert "summary" in data
