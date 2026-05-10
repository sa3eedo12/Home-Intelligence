from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class ToolSpec:
    id: str
    side_effects: bool
    fn: Callable[..., Any]


_TOOLS: dict[str, ToolSpec] = {}


def tool(id: str, side_effects: bool = False) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        _TOOLS[id] = ToolSpec(id=id, side_effects=side_effects, fn=fn)
        return fn

    return decorator


def get_tool(tool_id: str) -> ToolSpec | None:
    return _TOOLS.get(tool_id)


def list_tools() -> dict[str, ToolSpec]:
    return dict(_TOOLS)


def clear_tools() -> None:
    _TOOLS.clear()
