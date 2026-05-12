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
    import tools.events as _events
    import tools.registry as _registry

    clear_tools()
    importlib.reload(_core)
    importlib.reload(_events)
    importlib.reload(_registry)
    expected = {
        "index_path",
        "search",
        "summarize",
        "ask",
        "list_indexed",
        "forget_path",
        "record_event",
        "recall_recent",
        "search_events",
        "things.list",
        "things.put",
        "things.forget",
        "things.confirm",
        "habits.list",
        "habits.put",
        "habits.forget",
        "habits.confirm",
        "preferences.list",
        "preferences.put",
        "preferences.forget",
        "routines.list",
        "routines.put",
        "routines.forget",
    }
    assert expected.issubset(set(list_tools()))
