from __future__ import annotations

import time
import uuid
from collections.abc import Callable

import structlog
from fastapi import FastAPI, Request

_STRUCTLOG_CONFIGURED = False


def _configure_structlog() -> None:
    global _STRUCTLOG_CONFIGURED
    if _STRUCTLOG_CONFIGURED:
        return
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.add_log_level,
            structlog.processors.JSONRenderer(),
        ],
    )
    _STRUCTLOG_CONFIGURED = True


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    _configure_structlog()
    return structlog.get_logger(name)


def add_request_id_middleware(app: FastAPI) -> None:
    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next: Callable):
        request_id = request.headers.get("x-request-id", str(uuid.uuid4()))
        start = time.perf_counter()
        response = await call_next(request)
        response.headers["x-request-id"] = request_id
        response.headers["x-process-time-ms"] = f"{(time.perf_counter() - start) * 1000:.2f}"
        return response
