from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SHARED = str(_REPO_ROOT / "shared")
if _SHARED not in sys.path:
    sys.path.insert(0, _SHARED)
for _name in list(sys.modules):
    if _name == "home_agents_sdk" or _name.startswith("home_agents_sdk."):
        del sys.modules[_name]
