from __future__ import annotations

from tools.core import _zscore_anomaly


def test_zscore_anomaly_detects_threshold_cross() -> None:
    normal = [10.0, 11.0, 9.5, 10.5, 10.1, 10.3]
    assert _zscore_anomaly(normal, 25.0, 2.0) is True
    assert _zscore_anomaly(normal, 10.4, 2.0) is False
