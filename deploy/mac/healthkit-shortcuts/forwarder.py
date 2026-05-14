#!/usr/bin/env python3
"""Forward a JSON-shaped HealthKit snapshot from a macOS Shortcut to the
Home Intelligence orchestrator.

The companion Shortcut "HI Health Snapshot" reads recent HealthKit samples
on macOS Sonoma+ (where the Health app is built into macOS and syncs from
your iPhone via iCloud) and emits a small JSON dictionary on stdout. This
script reads that JSON, wraps it in the Health Auto Export envelope the
orchestrator expects, and POSTs it to /admin/healthkit/sync.

Why this two-stage design?
  Shortcuts can read HealthKit (it's the only free, no-Xcode path on macOS
  with the right entitlements). But Shortcuts is awkward for assembling
  nested JSON and for HTTP calls with custom headers. So we let the Shortcut
  do what it's good at (HealthKit access), and let Python do what it's
  good at (formatting + HTTP).

Stdin format (what the Shortcut emits — keep this in sync with the recipe
in README.md):

  {
    "ts":          "2026-05-14T08:00:00Z",   # snapshot capture time, ISO 8601 UTC
    "window_min":  60,                        # how far back the Shortcut looked
    "steps":       1234,                      # sum of HKQuantityTypeIdentifierStepCount
    "active_energy": 87.5,                    # sum of HKQuantityTypeIdentifierActiveEnergyBurned (kcal)
    "heart_rate":  72.4,                      # avg HKQuantityTypeIdentifierHeartRate (bpm)
    "resting_heart_rate": 58,                 # latest HKQuantityTypeIdentifierRestingHeartRate
    "hrv":         48.2,                      # latest HKQuantityTypeIdentifierHeartRateVariabilitySDNN (ms)
    "weight":      82.3,                      # latest HKQuantityTypeIdentifierBodyMass (kg)
    "blood_oxygen":97.0,                      # latest HKQuantityTypeIdentifierOxygenSaturation (%)
    "sleep_asleep_min": 412,                  # sum of asleep stages over window
    "sleep_window": {                         # optional aggregate sleep window
        "start": "2026-05-13T23:30:00Z",
        "end":   "2026-05-14T07:15:00Z",
        "asleep_min": 412
    },
    "workouts": [                             # zero or more workouts that ENDED in window
        {"type": "Walking", "start": "...", "end": "...", "duration_min": 28,
         "active_energy": 142.0, "distance_m": 2400}
    ]
  }

All keys are optional. Missing/empty values are skipped, not zeroed —
the orchestrator handles partial payloads.

Environment:
  ORCHESTRATOR_URL    e.g. http://truenas.local:8080  (required)
  HEALTHKIT_TOKEN     must match HEALTHKIT_WEBHOOK_TOKEN on TrueNAS  (required)
  MEMBER_ID           optional integer to attach the upload to a specific
                      household_members row
  REQUEST_TIMEOUT     seconds, default 30

Exit codes:
  0  success (or nothing-to-send, which is also a success)
  2  bad/missing config
  3  bad input from Shortcut
  4  retriable network or 5xx error (launchd will retry next run)
  5  permanent rejection (4xx other than 408/425/429)
"""
from __future__ import annotations

import json
import logging
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOG_PATH = Path.home() / "Library" / "Logs" / "healthkit-shortcuts.log"
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler(sys.stderr)],
)
log = logging.getLogger("healthkit-shortcuts")


# Map the friendly snapshot keys (what the Shortcut emits) to the HealthKit
# type identifiers the orchestrator's normalizer recognizes. Keep the right
# side in sync with orchestrator/health.py:_HEALTHKIT_METRICS.
_QUANTITY_MAP: dict[str, tuple[str, str]] = {
    # snapshot key -> (HK identifier, unit)
    "steps":              ("HKQuantityTypeIdentifierStepCount",                  "steps"),
    "active_energy":      ("HKQuantityTypeIdentifierActiveEnergyBurned",         "kcal"),
    "heart_rate":         ("HKQuantityTypeIdentifierHeartRate",                  "bpm"),
    "resting_heart_rate": ("HKQuantityTypeIdentifierRestingHeartRate",           "bpm"),
    "hrv":                ("HKQuantityTypeIdentifierHeartRateVariabilitySDNN",   "ms"),
    "weight":             ("HKQuantityTypeIdentifierBodyMass",                   "kg"),
    "blood_oxygen":       ("HKQuantityTypeIdentifierOxygenSaturation",           "%"),
    "vo2_max":            ("HKQuantityTypeIdentifierVO2Max",                     "mL/kg/min"),
}


def _getenv(key: str, default: str | None = None, *, required: bool = False) -> str:
    value = os.environ.get(key, default)
    if required and not value:
        log.error("missing required env var: %s", key)
        sys.exit(2)
    return value or ""


def _coerce_float(value: Any) -> float | None:
    """Shortcuts likes to emit numbers as strings. Accept either."""
    if value in (None, "", "null"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_iso(value: Any) -> str | None:
    if not value or not isinstance(value, str):
        return None
    return value.strip() or None


def _build_metrics_payload(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Turn the flat Shortcut snapshot into the nested format the orchestrator
    accepts (the same shape Health Auto Export emits)."""
    capture_ts = _coerce_iso(snapshot.get("ts")) or datetime.now(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    metrics: list[dict[str, Any]] = []
    for snap_key, (hk_id, unit) in _QUANTITY_MAP.items():
        qty = _coerce_float(snapshot.get(snap_key))
        if qty is None:
            continue
        metrics.append({
            "type": hk_id,
            "units": unit,
            "data": [{"date": capture_ts, "qty": qty}],
        })

    asleep_min = _coerce_float(snapshot.get("sleep_asleep_min"))
    sleep_window = snapshot.get("sleep_window") if isinstance(
        snapshot.get("sleep_window"), dict
    ) else None
    if asleep_min and asleep_min > 0:
        if sleep_window is None:
            # Synthesize a window ending at capture time so the orchestrator
            # can attach a sensible startDate / endDate for sleep aggregation.
            sleep_window = {"start": capture_ts, "end": capture_ts, "asleep_min": asleep_min}
        sleep_start = _coerce_iso(sleep_window.get("start")) or capture_ts
        sleep_end = _coerce_iso(sleep_window.get("end")) or capture_ts
        sleep_qty = _coerce_float(sleep_window.get("asleep_min")) or asleep_min
        metrics.append({
            "type": "HKCategoryTypeIdentifierSleepAnalysis",
            "data": [{
                "startDate": sleep_start,
                "endDate": sleep_end,
                "stage": "asleep",
                "qty": sleep_qty,
                "value": "asleep",
            }],
        })

    workouts: list[dict[str, Any]] = []
    raw_workouts = snapshot.get("workouts")
    if isinstance(raw_workouts, list):
        for wk in raw_workouts:
            if not isinstance(wk, dict):
                continue
            start = _coerce_iso(wk.get("start"))
            end = _coerce_iso(wk.get("end"))
            duration = _coerce_float(wk.get("duration_min"))
            if not (start and end and duration):
                continue
            normalized: dict[str, Any] = {
                "type": "HKWorkoutTypeIdentifier",
                "name": str(wk.get("type") or wk.get("name") or "Workout"),
                "start": start,
                "end": end,
                "duration": duration,
            }
            energy = _coerce_float(wk.get("active_energy"))
            if energy is not None:
                normalized["activeEnergy"] = energy
            distance = _coerce_float(wk.get("distance_m"))
            if distance is not None:
                normalized["distance"] = distance
            workouts.append(normalized)

    payload: dict[str, Any] = {"data": {"metrics": metrics}}
    if workouts:
        payload["data"]["workouts"] = workouts
    return payload


def _read_snapshot() -> dict[str, Any]:
    raw = sys.stdin.read().strip()
    if not raw:
        log.info("empty stdin — Shortcut returned nothing this run")
        sys.exit(0)
    try:
        snapshot = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.error("Shortcut emitted non-JSON output: %s | first 200 chars: %r", exc, raw[:200])
        sys.exit(3)
    if not isinstance(snapshot, dict):
        log.error("Shortcut output is not a JSON object: %r", type(snapshot).__name__)
        sys.exit(3)
    return snapshot


def _post(url: str, payload: bytes, *, token: str, timeout: int) -> tuple[int, str]:
    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Health-Token": token,
            "User-Agent": "home-intelligence-healthkit-shortcuts/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        return exc.code, body


def main() -> int:
    url = _getenv("ORCHESTRATOR_URL", required=True)
    token = _getenv("HEALTHKIT_TOKEN", required=True)
    member_id = _getenv("MEMBER_ID", "")
    timeout = int(_getenv("REQUEST_TIMEOUT", "30") or "30")

    snapshot = _read_snapshot()
    body = _build_metrics_payload(snapshot)
    if not body["data"].get("metrics") and not body["data"].get("workouts"):
        log.info("snapshot had nothing to send (no metrics or workouts)")
        return 0

    target = url.rstrip("/") + "/admin/healthkit/sync"
    if member_id:
        target += "?" + urllib.parse.urlencode({"member_id": member_id})

    encoded = json.dumps(body).encode("utf-8")
    try:
        status, resp_body = _post(target, encoded, token=token, timeout=timeout)
    except urllib.error.URLError as exc:
        log.warning("network error: %s — launchd will retry next run", exc.reason)
        return 4
    except TimeoutError:
        log.warning("timeout posting to %s — will retry next run", target)
        return 4

    if 200 <= status < 300:
        log.info(
            "uploaded %d metrics + %d workouts (%d bytes) → %s",
            len(body["data"].get("metrics", [])),
            len(body["data"].get("workouts", [])),
            len(encoded),
            resp_body[:200],
        )
        return 0
    if status >= 500 or status in (408, 425, 429):
        log.warning("retriable HTTP %d: %s", status, resp_body[:200])
        return 4
    log.error("permanent HTTP %d: %s", status, resp_body[:500])
    return 5


if __name__ == "__main__":
    sys.exit(main())
