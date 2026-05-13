from __future__ import annotations

import fnmatch
import hashlib
import os
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psutil
import yaml
from home_agents_sdk import tool

from . import publish_helper


@tool("disk_usage")
def disk_usage(threshold_pct: float = 85.0) -> dict[str, Any]:
    items = []
    for part in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except Exception:
            continue
        items.append(
            {
                "mount": part.mountpoint,
                "used_pct": float(usage.percent),
                "is_high": float(usage.percent) >= threshold_pct,
                "total": usage.total,
                "used": usage.used,
                "free": usage.free,
            }
        )
    return {"items": items}


def _iter_files(root: str, max_seconds_per_dir: float = 2.0):
    """Yield files recursively with a per-directory time budget to avoid long scans stalling."""
    stack = [root]
    while stack:
        current = stack.pop()
        start = time.monotonic()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    if time.monotonic() - start > max_seconds_per_dir:
                        break
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
                    elif entry.is_file(follow_symlinks=False):
                        yield entry.path, entry.stat(follow_symlinks=False)
        except (PermissionError, FileNotFoundError, NotADirectoryError):
            continue


@tool("largest_files")
def largest_files(path: str | None = None, limit: int = 20) -> dict[str, Any]:
    roots = [
        p.strip()
        for p in os.getenv("STORAGE_SCAN_ROOTS", "/mnt,/host-home").split(",")
        if p.strip()
    ]
    scan_roots = [path] if path else roots
    files: list[dict[str, Any]] = []
    for root in scan_roots:
        for fpath, st in _iter_files(root):
            mtime = datetime.fromtimestamp(st.st_mtime, tz=UTC)
            files.append(
                {
                    "path": fpath,
                    "size": st.st_size,
                    "mtime": mtime.isoformat(),
                    "age_hours": round((datetime.now(UTC) - mtime).total_seconds() / 3600, 2),
                }
            )
    files.sort(key=lambda x: x["size"], reverse=True)
    return {"items": files[:limit]}


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


@tool("find_duplicates")
def find_duplicates(path: str | None = None) -> dict[str, Any]:
    roots = (
        [path]
        if path
        else [
            p.strip()
            for p in os.getenv("STORAGE_SCAN_ROOTS", "/mnt,/host-home").split(",")
            if p.strip()
        ]
    )
    by_size: dict[int, list[str]] = defaultdict(list)
    for root in roots:
        for fpath, st in _iter_files(root):
            by_size[st.st_size].append(fpath)

    groups = []
    reclaimable = 0
    for size, paths in by_size.items():
        if size <= 0 or len(paths) < 2:
            continue
        by_hash: dict[str, list[str]] = defaultdict(list)
        for p in paths:
            try:
                by_hash[_sha256(p)].append(p)
            except (PermissionError, FileNotFoundError, OSError):
                continue
        for h, hp in by_hash.items():
            if len(hp) < 2:
                continue
            reclaimable += size * (len(hp) - 1)
            groups.append({"hash": h, "size": size, "files": hp})

    groups.sort(key=lambda g: g["size"] * len(g["files"]), reverse=True)
    return {"groups": groups, "reclaimable_bytes": reclaimable}


def _validate_backup_config(config_path: str) -> dict[str, Any]:
    config_file = Path(config_path)
    if not config_file.exists():
        return {"ok": False, "error": f"missing config: {config_path}"}
    cfg = yaml.safe_load(config_file.read_text(encoding="utf-8")) or {}
    checks = []
    all_ok = True
    for backup in cfg.get("backups", []):
        bpath = Path(backup["path"])
        max_age_hours = int(backup.get("max_age_hours", 36))
        globs = backup.get("expect_globs", [])
        if not bpath.exists() or not bpath.is_dir():
            checks.append(
                {
                    "name": backup.get("name"),
                    "path": str(bpath),
                    "ok": False,
                    "error": "missing path",
                }
            )
            all_ok = False
            continue

        entries = list(bpath.iterdir())
        mtimes = []
        for entry in entries:
            try:
                mtimes.append(entry.stat().st_mtime)
            except (FileNotFoundError, PermissionError, OSError):
                continue
        latest_mtime = max(mtimes, default=0)
        age_hours = (time.time() - latest_mtime) / 3600 if latest_mtime else 99999
        glob_ok = all(any(fnmatch.fnmatch(e.name, g) for e in entries) for g in globs)
        ok = bool(entries) and age_hours <= max_age_hours and glob_ok
        checks.append(
            {
                "name": backup.get("name"),
                "path": str(bpath),
                "ok": ok,
                "age_hours": round(age_hours, 2),
                "max_age_hours": max_age_hours,
                "entries": len(entries),
                "glob_ok": glob_ok,
            }
        )
        all_ok = all_ok and ok
    return {"ok": all_ok, "checks": checks}


def _backup_warning_events(result: dict[str, Any], config_path: str) -> list[dict[str, Any]]:
    if result.get("ok"):
        return []
    if result.get("error"):
        return [
            {
                "metric": "backup.config",
                "value": "missing",
                "threshold": "present",
                "summary": str(result["error"]),
                "config_path": config_path,
            }
        ]

    events: list[dict[str, Any]] = []
    for check in result.get("checks", []):
        if check.get("ok"):
            continue
        name = check.get("name") or check.get("path") or "backup"
        common = {"backup": name, "path": check.get("path"), "check": check}
        if check.get("error") == "missing path":
            events.append(
                {
                    **common,
                    "metric": "backup.path",
                    "value": "missing",
                    "threshold": "present",
                    "summary": f"Backup path missing for {name}.",
                }
            )
        elif float(check.get("age_hours") or 0.0) > float(check.get("max_age_hours") or 0.0):
            events.append(
                {
                    **common,
                    "metric": "backup.age_hours",
                    "value": check.get("age_hours"),
                    "threshold": check.get("max_age_hours"),
                    "summary": f"Backup {name} is {check.get('age_hours')} hours old.",
                }
            )
        elif int(check.get("entries") or 0) == 0:
            events.append(
                {
                    **common,
                    "metric": "backup.entries",
                    "value": 0,
                    "threshold": ">0",
                    "summary": f"Backup {name} has no files.",
                }
            )
        elif check.get("glob_ok") is False:
            events.append(
                {
                    **common,
                    "metric": "backup.expected_globs",
                    "value": "missing",
                    "threshold": "present",
                    "summary": f"Backup {name} is missing expected files.",
                }
            )
    return events


async def _publish_backup_warnings(result: dict[str, Any], config_path: str) -> None:
    for event in _backup_warning_events(result, config_path):
        await publish_helper.publish_metric_breach(severity="warn", **event)


@tool("validate_backup")
async def validate_backup(
    config_path: str = "/etc/storage_backup/backup_targets.yaml",
) -> dict[str, Any]:
    result = _validate_backup_config(config_path)
    await _publish_backup_warnings(result, config_path)
    return result


@tool("cleanup_suggestions")
def cleanup_suggestions(path: str | None = None) -> dict[str, Any]:
    largest = largest_files(path=path, limit=10)["items"]
    dupes = find_duplicates(path=path)["groups"][:5]
    candidates = [
        {
            "reason": "Large file candidate",
            "path": entry["path"],
            "size": entry["size"],
        }
        for entry in largest
        if entry["age_hours"] > 24 * 30
    ]
    for group in dupes:
        for p in group["files"][1:]:
            candidates.append({"reason": "Duplicate candidate", "path": p, "size": group["size"]})
    return {"candidates": candidates[:20], "read_only": True}


@tool("summarize_storage")
async def summarize_storage() -> dict[str, str]:
    threshold_pct = 90.0
    usage = disk_usage(threshold_pct=threshold_pct)["items"]
    high = [u for u in usage if u["is_high"]]
    msg = f"Storage mounts: {len(usage)} total; {len(high)} above {threshold_pct:.0f}%."
    if high:
        details = ", ".join(f"{u['mount']} {u['used_pct']:.1f}%" for u in high[:3])
        msg = f"{msg} High usage: {details}"
        for item in high[:5]:
            await publish_helper.publish_metric_breach(
                metric="disk.used_pct",
                value=round(float(item["used_pct"]), 1),
                threshold=threshold_pct,
                severity="warn",
                summary=f"Disk {item['mount']} is {item['used_pct']:.1f}% full.",
                mount=item["mount"],
                free=item.get("free"),
                total=item.get("total"),
            )
    return {"summary": msg[:600]}
