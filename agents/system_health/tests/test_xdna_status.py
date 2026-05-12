from __future__ import annotations

from pathlib import Path

import pytest

from tools import core


def test_xdna_status_not_present(monkeypatch, tmp_path: Path) -> None:
    fake_modules = tmp_path / "modules"
    fake_modules.write_text("amdgpu 14282752 0\nttm 106496 2 amdgpu,drm_ttm_helper\n")

    def _exists(path: str) -> bool:
        return False  # no device files at all

    monkeypatch.setattr(core.os.path, "exists", _exists)
    monkeypatch.setattr(core, "_read_modules", lambda: fake_modules.read_text())

    result = core.xdna_status()
    assert result["status"] == "not_present"
    assert result["device_present"] is False
    assert result["driver_loaded"] is False
    assert "TrueNAS" in result["message"] or "not available" in result["message"].lower()


def test_xdna_status_available(monkeypatch) -> None:
    monkeypatch.setattr(
        core.os.path,
        "exists",
        lambda p: p == "/dev/accel/accel0",
    )
    monkeypatch.setattr(
        core,
        "_read_modules",
        lambda: "amdxdna 49152 0\namdgpu 14282752 0\n",
    )
    result = core.xdna_status()
    assert result["status"] == "available"
    assert result["device_present"] is True
    assert result["driver_loaded"] is True


def test_xdna_status_driver_loaded_but_no_device(monkeypatch) -> None:
    monkeypatch.setattr(core.os.path, "exists", lambda _p: False)
    monkeypatch.setattr(core, "_read_modules", lambda: "amdxdna 49152 0\n")
    result = core.xdna_status()
    assert result["status"] == "driver_loaded"
    assert result["device_present"] is False
    assert result["driver_loaded"] is True
    assert "firmware" in result["message"].lower() or "init" in result["message"].lower()


def test_xdna_status_device_only(monkeypatch) -> None:
    monkeypatch.setattr(
        core.os.path,
        "exists",
        lambda p: p == "/dev/accel/accel0",
    )
    monkeypatch.setattr(core, "_read_modules", lambda: "amdgpu 14282752 0\n")
    result = core.xdna_status()
    assert result["status"] == "device_only"
    assert result["device_present"] is True
    assert result["driver_loaded"] is False


@pytest.mark.parametrize(
    "modules_text",
    [
        "",  # empty modules file
        "amdgpu 14282752 0\nttm 106496 2 amdgpu,drm_ttm_helper\n",  # GPU but no NPU
    ],
)
def test_xdna_status_parses_modules_text_safely(monkeypatch, modules_text: str) -> None:
    monkeypatch.setattr(core.os.path, "exists", lambda _p: False)
    monkeypatch.setattr(core, "_read_modules", lambda: modules_text)
    result = core.xdna_status()
    assert result["status"] == "not_present"
