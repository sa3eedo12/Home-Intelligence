from __future__ import annotations

import importlib
import sys
from pathlib import Path

from home_agents_sdk.tools import clear_tools, list_tools


def test_manifest_and_tools_consistent() -> None:
    agent_dir = Path(__file__).parent.parent
    if str(agent_dir) not in sys.path:
        sys.path.insert(0, str(agent_dir))

    import tools.core as _core

    clear_tools()
    importlib.reload(_core)
    expected = {
        "scan",
        "container_status",
        "restart_container",
        "top_processes",
        "gpu_status",
        "anomaly_check",
        "suggest_optimizations",
    }
    assert expected.issubset(set(list_tools()))
