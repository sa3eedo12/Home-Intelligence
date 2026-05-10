from __future__ import annotations

from typing import Any


async def detect_objects(
    image_bytes: bytes, classes: list[str] | None = None
) -> list[dict[str, Any]]:
    """Placeholder for PR 2/3 vision integration.

    TODO:
    - Call Lemonade vision endpoint for VISION_MODEL when available.
    - Optionally support local ONNX Runtime inference fallback.
    """
    _ = image_bytes
    _ = classes
    return []
