from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SHARED = ROOT / "shared"
for path in (ROOT, SHARED):
    raw = str(path)
    if raw not in sys.path:
        sys.path.insert(0, raw)
