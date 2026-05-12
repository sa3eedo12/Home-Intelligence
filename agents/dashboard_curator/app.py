from __future__ import annotations

from home_agents_sdk.agent_base import build_app
from home_agents_sdk.telemetry import get_logger

from tools import core  # noqa: F401

logger = get_logger("dashboard_curator")
app = build_app("dashboard_curator", manifest_path="manifest.yaml")
