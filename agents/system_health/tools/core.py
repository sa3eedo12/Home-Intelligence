from __future__ import annotations

import json
import math
import os
from asyncio import create_subprocess_exec
from collections.abc import Sequence
from statistics import mean, pstdev
from typing import Any

import docker
import psutil
from home_agents_sdk import tool
from redis.asyncio import Redis


def _hwmon_temps() -> list[dict[str, Any]]:
    root = "/host/sys/class/hwmon"
    if not os.path.exists(root):
        root = "/sys/class/hwmon"
    results: list[dict[str, Any]] = []
    for name in os.listdir(root) if os.path.exists(root) else []:
        p = os.path.join(root, name, "temp1_input")
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    milli = int(f.read().strip())
                results.append({"sensor": name, "temp_c": round(milli / 1000.0, 1)})
            except Exception:
                continue
    return results


def _container_client() -> docker.DockerClient | None:
    try:
        return docker.from_env()
    except Exception:
        return None


@tool("top_processes")
def top_processes(sort_by: str = "cpu", limit: int = 5) -> dict[str, Any]:
    rows = []
    for proc in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]):
        info = proc.info
        rows.append(
            {
                "pid": info.get("pid"),
                "name": info.get("name") or "unknown",
                "cpu_pct": float(info.get("cpu_percent") or 0.0),
                "mem_pct": float(info.get("memory_percent") or 0.0),
            }
        )
    key = "mem_pct" if sort_by == "memory" else "cpu_pct"
    rows.sort(key=lambda r: r[key], reverse=True)
    return {"items": rows[:limit]}


@tool("container_status")
def container_status() -> dict[str, Any]:
    client = _container_client()
    if client is None:
        return {"items": [], "note": "docker socket unavailable"}
    items: list[dict[str, Any]] = []
    for c in client.containers.list(all=True):
        attrs = c.attrs or {}
        restart_count = attrs.get("RestartCount", 0)
        try:
            logs = c.logs(tail=50).decode("utf-8", errors="ignore").splitlines()[-50:]
        except Exception:
            logs = []
        items.append(
            {
                "name": c.name,
                "status": c.status,
                "restart_count": restart_count,
                "logs": logs,
            }
        )
    return {"items": items}


@tool("restart_container", side_effects=True)
def restart_container(name: str) -> dict[str, Any]:
    client = _container_client()
    if client is None:
        return {"ok": False, "error": "docker unavailable"}
    container = client.containers.get(name)
    container.restart(timeout=10)
    return {"ok": True, "name": name}


@tool("gpu_status")
async def gpu_status() -> dict[str, Any]:
    try:
        proc = await create_subprocess_exec("rocm-smi", "--json", stdout=-1, stderr=-1)
        stdout, stderr = await proc.communicate()
    except FileNotFoundError:
        return {"ok": False, "error": "rocm-smi not available"}

    if proc.returncode != 0:
        return {"ok": False, "error": stderr.decode("utf-8", errors="ignore")}
    try:
        return {"ok": True, "data": json.loads(stdout.decode("utf-8", errors="ignore"))}
    except json.JSONDecodeError:
        return {"ok": True, "raw": stdout.decode("utf-8", errors="ignore")}


def _read_modules() -> str:
    """Return the contents of /proc/modules (or the host-mounted version)."""
    for path in ("/host/proc/modules", "/proc/modules"):
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    return fh.read()
            except OSError:
                continue
    return ""


@tool("xdna_status")
def xdna_status() -> dict[str, Any]:
    """Report XDNA NPU availability.

    Returns one of three statuses:
    - `available`     : driver loaded + device file present
    - `driver_loaded` : driver loaded but device file missing (firmware
                        issue or hardware not detected)
    - `not_present`   : neither driver nor device — typical of TrueNAS
                        SCALE 25.10 which doesn't enable
                        CONFIG_DRM_ACCEL_AMDXDNA in its kernel build.
    """
    device_paths = ["/dev/accel/accel0", "/host/dev/accel/accel0"]
    device_present = any(os.path.exists(p) for p in device_paths)

    modules = _read_modules()
    driver_loaded = any(
        line.split(" ", 1)[0] == "amdxdna" for line in modules.splitlines() if line
    )

    if device_present and driver_loaded:
        status = "available"
        message = "XDNA NPU is loaded and the device file is present."
    elif driver_loaded and not device_present:
        status = "driver_loaded"
        message = (
            "amdxdna module is loaded but /dev/accel/accel0 is missing — "
            "check firmware (/lib/firmware/amdnpu/) and dmesg for init errors."
        )
    elif device_present and not driver_loaded:
        status = "device_only"
        message = (
            "Device file exists but amdxdna is not in /proc/modules — "
            "unexpected; the device may be claimed by a different driver."
        )
    else:
        status = "not_present"
        message = (
            "XDNA NPU not available. On TrueNAS SCALE 25.10 the kernel "
            "doesn't ship the amdxdna driver; this is expected."
        )
    return {
        "status": status,
        "device_present": device_present,
        "driver_loaded": driver_loaded,
        "message": message,
    }


@tool("scan")
def scan() -> dict[str, Any]:
    cpu = psutil.cpu_percent(interval=0.0)
    vm = psutil.virtual_memory()
    swap = psutil.swap_memory()

    mounts: list[dict[str, Any]] = []
    for part in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(part.mountpoint)
            mounts.append(
                {
                    "mount": part.mountpoint,
                    "used_pct": usage.percent,
                    "total": usage.total,
                    "used": usage.used,
                }
            )
        except Exception:
            continue

    top = top_processes(limit=5)["items"]
    containers = container_status()["items"]
    unhealthy = [c for c in containers if c.get("status") not in {"running", "created"}]
    payload = {
        "cpu_pct": cpu,
        "ram_pct": vm.percent,
        "swap_pct": swap.percent,
        "disk": mounts,
        "top_processes": top,
        "temperatures": _hwmon_temps(),
        "containers": unhealthy,
    }
    summary = (
        f"CPU {cpu:.1f}% | RAM {vm.percent:.1f}% | Swap {swap.percent:.1f}% | "
        f"Unhealthy containers {len(unhealthy)}"
    )
    return {"metrics": payload, "summary": summary}


def _zscore_anomaly(series: Sequence[float], value: float, threshold: float) -> bool:
    # We require at least 4 historical samples to avoid unstable z-scores on tiny windows.
    if len(series) < 4:
        return False
    sigma = pstdev(series)
    if sigma == 0:
        return False
    score = abs((value - mean(series)) / sigma)
    return bool(score > threshold and not math.isnan(score))


@tool("anomaly_check")
async def anomaly_check(metric: str = "cpu_pct", threshold: float = 2.5) -> dict[str, Any]:
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    client = Redis.from_url(redis_url, decode_responses=True)
    scan_result = scan()
    current = float(scan_result["metrics"].get(metric, 0.0))
    key = f"metrics:{metric}"
    await client.rpush(key, current)
    await client.ltrim(key, -96, -1)
    values = [float(v) for v in await client.lrange(key, 0, -1)]
    is_anomaly = _zscore_anomaly(values[:-1], current, threshold)
    return {
        "metric": metric,
        "current": current,
        "samples": len(values),
        "is_anomaly": is_anomaly,
    }


@tool("suggest_optimizations")
def suggest_optimizations() -> dict[str, Any]:
    s = scan()
    lines: list[str] = []
    if s["metrics"]["cpu_pct"] > 85:
        lines.append("High CPU detected; inspect top_processes and cap noisy workloads.")
    if s["metrics"]["ram_pct"] > 85:
        lines.append("High RAM usage; tune container memory limits or restart pressure services.")
    if s["metrics"]["containers"]:
        names = ", ".join(c["name"] for c in s["metrics"]["containers"][:3])
        lines.append(f"Containers unhealthy/restarting: {names}. Check logs and healthchecks.")
    if not lines:
        lines.append("System looks healthy. Keep observing anomaly_check trends.")
    return {"suggestions": lines}
