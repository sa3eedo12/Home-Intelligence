from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Capability(BaseModel):
    id: str
    description: str
    side_effects: bool = False


class Manifest(BaseModel):
    agent: str
    version: str = "0.1.0"
    capabilities: list[Capability] = Field(default_factory=list)


class InvokeRequest(BaseModel):
    capability: str
    payload: dict[str, Any] = Field(default_factory=dict)


class InvokeResponse(BaseModel):
    ok: bool
    result: Any = None
    error: str | None = None


class Event(BaseModel):
    stream: str
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Task(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    kind: str
    payload: dict[str, Any] = Field(default_factory=dict)
    priority: int = 0
