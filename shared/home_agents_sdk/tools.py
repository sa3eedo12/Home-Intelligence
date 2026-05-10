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


def tool(
    tool_id: str | None = None,
    side_effects: bool = False,
    **kwargs: Any,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    resolved_tool_id = tool_id if tool_id is not None else kwargs.get("id")
    if not resolved_tool_id:
        raise ValueError("tool_id is required")

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        _TOOLS[resolved_tool_id] = ToolSpec(id=resolved_tool_id, side_effects=side_effects, fn=fn)
        return fn

    return decorator


def get_tool(tool_id: str) -> ToolSpec | None:
    return _TOOLS.get(tool_id)


def list_tools() -> dict[str, ToolSpec]:
    return dict(_TOOLS)


def clear_tools() -> None:
    _TOOLS.clear()
