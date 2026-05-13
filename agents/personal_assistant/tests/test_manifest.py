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
    import tools.presence_inference as _presence_inference

    clear_tools()
    importlib.reload(_core)
    importlib.reload(_presence_inference)
    expected = {
        "add_reminder",
        "list_reminders",
        "cancel_reminder",
        "add_renewal",
        "list_renewals",
        "add_appointment",
        "list_appointments",
        "morning_brief",
        "evening_recap",
        "infer_presence_return",
        "confirm_presence_return",
        "recent_presence_returns",
    }
    assert expected.issubset(set(list_tools()))
