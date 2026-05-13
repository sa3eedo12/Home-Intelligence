#!/usr/bin/env python3
"""Forward Health Auto Export JSON files from iCloud Drive to the Home
Intelligence orchestrator on the LAN.

Reads JSON files from WATCH_DIR, POSTs each to
$ORCHESTRATOR_URL/admin/healthkit/sync with the X-Health-Token header, then
moves the file to processed/ on success or failed/ on a non-retriable error.
Network and 5xx errors leave the file in place so the next run retries.

Designed to be invoked every minute by launchd. Idempotent and safe to run
overlapping (lock file prevents concurrent runs).

Environment:
  ORCHESTRATOR_URL    e.g. http://truenas.local:8080  (required)
  HEALTHKIT_TOKEN     must match HEALTHKIT_WEBHOOK_TOKEN on TrueNAS  (required)
  WATCH_DIR           folder where Health Auto Export drops JSON files (required)
  MEMBER_ID           optional integer to attach the upload to a specific
                      household_members row
  REQUEST_TIMEOUT     seconds, default 60
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

LOG_PATH = Path.home() / "Library" / "Logs" / "healthkit-bridge.log"
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("healthkit-bridge")


def _getenv(key: str, default: str | None = None, *, required: bool = False) -> str:
    value = os.environ.get(key, default)
    if required and not value:
        log.error("missing required env var: %s", key)
        sys.exit(2)
    return value or ""


def _post(url: str, payload: bytes, *, token: str, timeout: int) -> tuple[int, str]:
    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Health-Token": token,
            "User-Agent": "home-intelligence-healthkit-bridge/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        return exc.code, body


def _is_retriable(status: int) -> bool:
    # network errors are handled separately; here we only see HTTP status codes
    return status >= 500 or status in (408, 425, 429)


def _process_file(
    path: Path,
    *,
    url: str,
    token: str,
    member_id: str,
    processed_dir: Path,
    failed_dir: Path,
    timeout: int,
) -> None:
    if path.suffix.lower() != ".json":
        return
    if path.name.startswith("."):
        return  # iCloud sync placeholders or hidden files
    try:
        payload = path.read_bytes()
    except FileNotFoundError:
        return
    if not payload.strip():
        log.warning("skipping empty file: %s", path.name)
        _move(path, failed_dir, suffix=".empty")
        return
    try:
        json.loads(payload)
    except json.JSONDecodeError as exc:
        log.warning("skipping invalid JSON %s: %s", path.name, exc)
        _move(path, failed_dir, suffix=".badjson")
        return

    target = url.rstrip("/") + "/admin/healthkit/sync"
    if member_id:
        target += "?" + urllib.parse.urlencode({"member_id": member_id})

    started = time.monotonic()
    try:
        status, body = _post(target, payload, token=token, timeout=timeout)
    except urllib.error.URLError as exc:
        log.warning(
            "network error on %s (%s) - leaving for retry",
            path.name,
            exc.reason,
        )
        return
    except TimeoutError:
        log.warning("timeout on %s - leaving for retry", path.name)
        return

    elapsed_ms = int((time.monotonic() - started) * 1000)
    if 200 <= status < 300:
        log.info(
            "uploaded %s (%d bytes) in %d ms: %s",
            path.name,
            len(payload),
            elapsed_ms,
            body[:200],
        )
        _move(path, processed_dir)
    elif _is_retriable(status):
        log.warning(
            "retriable error on %s status=%d body=%s", path.name, status, body[:200]
        )
    else:
        log.error("rejected %s status=%d body=%s", path.name, status, body[:500])
        _move(path, failed_dir, suffix=f".http{status}")


def _move(path: Path, dest_dir: Path, *, suffix: str = "") -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    new_name = f"{stamp}-{path.stem}{suffix}{path.suffix}"
    target = dest_dir / new_name
    shutil.move(str(path), str(target))


def _acquire_lock(lock_path: Path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fp = lock_path.open("w")
    try:
        fcntl.flock(fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.info("another bridge run is in progress; exiting")
        sys.exit(0)
    return fp


def main() -> int:
    url = _getenv("ORCHESTRATOR_URL", required=True)
    token = _getenv("HEALTHKIT_TOKEN", required=True)
    watch_raw = _getenv("WATCH_DIR", required=True)
    member_id = _getenv("MEMBER_ID", "")
    timeout = int(_getenv("REQUEST_TIMEOUT", "60") or "60")

    watch_dir = Path(os.path.expanduser(watch_raw))
    if not watch_dir.is_dir():
        log.error("WATCH_DIR is not a directory: %s", watch_dir)
        return 2

    processed_dir = watch_dir / "processed"
    failed_dir = watch_dir / "failed"
    lock_fp = _acquire_lock(Path.home() / "Library" / "Caches" / "healthkit-bridge.lock")

    files = sorted(p for p in watch_dir.iterdir() if p.is_file())
    if not files:
        return 0
    log.info("scanning %d file(s) in %s", len(files), watch_dir)
    for path in files:
        _process_file(
            path,
            url=url,
            token=token,
            member_id=member_id,
            processed_dir=processed_dir,
            failed_dir=failed_dir,
            timeout=timeout,
        )
    lock_fp.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
