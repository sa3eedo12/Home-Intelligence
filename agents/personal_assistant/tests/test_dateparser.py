from __future__ import annotations

from datetime import datetime

from tools.core import parse_nl_datetime


def test_parse_natural_language_dates() -> None:
    now = datetime.now().astimezone()
    for sample in ("next Friday at 3pm", "in 2 weeks", "Dec 15"):
        parsed = parse_nl_datetime(sample)
        assert parsed.tzinfo is not None
        assert parsed > now
