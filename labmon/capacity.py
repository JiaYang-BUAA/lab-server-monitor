"""Conservative, point-in-time capacity advice for automation clients."""
from __future__ import annotations

from datetime import datetime, timezone
import math

GIB = 1024 ** 3


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _age(value, now):
    try:
        observed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return max(0.0, now - observed.timestamp()) if observed.tzinfo else None
    except (AttributeError, ValueError, OverflowError):
        return None


def capacity_report(server: dict, requested: dict, now: float, stale_seconds: float, active_claims: int = 0):
    """Return measurements and an advisory verdict, never a resource reservation."""
    snapshot = server.get("snapshot") or {}
    cpu = snapshot.get("cpu") or {}
    memory = snapshot.get("memory") or {}
    age = _age(server.get("last_seen"), now)
    logical = cpu.get("logical_processors")
    observed = cpu.get("observed_processors")
    used_pct = cpu.get("percent")
    usable_cores = None
    coverage_complete = (isinstance(observed, int) and not isinstance(observed, bool)
                         and observed == logical)
    if (isinstance(logical, int) and not isinstance(logical, bool) and logical > 0
            and coverage_complete and _finite(used_pct) and 0 <= used_pct <= 100):
        reserve = max(2, math.ceil(logical * 0.1))
        usable_cores = max(0, math.floor(logical * (1 - used_pct / 100)) - reserve)
    total_memory = memory.get("total_bytes")
    available_memory = memory.get("available_bytes")
    usable_memory = None
    if (_finite(total_memory) and total_memory > 0 and _finite(available_memory)
            and 0 <= available_memory <= total_memory):
        usable_memory = max(0, math.floor(available_memory - max(4 * GIB, total_memory * 0.05)))
    gpus = []
    for gpu in snapshot.get("gpus") or []:
        total, used, utilization = gpu.get("memory_total_bytes"), gpu.get("memory_used_bytes"), gpu.get("utilization_pct")
        usable = None
        if _finite(total) and total > 0 and _finite(used) and 0 <= used <= total:
            usable = max(0, math.floor(total - used - max(GIB, total * 0.05)))
        gpus.append({"id": gpu.get("id"), "name": gpu.get("name"),
                     "utilization_pct": utilization if _finite(utilization) else None,
                     "estimated_usable_memory_bytes": usable})
    reasons = []
    verdict = "likely_available"
    effective_status = ("stale" if server.get("status") == "online" and (age is None or age > stale_seconds)
                        else server.get("status"))
    if effective_status != "online" or snapshot.get("telemetry_status") == "error":
        verdict = "unknown"
        reasons.append("采样已过期或服务器不在线")
    elif not any(requested.values()):
        verdict = "not_assessed"
        reasons.append("提供所需 CPU 核数、内存或 GPU 显存后才能判断")
    else:
        unknown = False
        insufficient = False
        if requested["cpu_cores"]:
            if usable_cores is None:
                unknown = True; reasons.append("CPU 可用核数未知")
            elif usable_cores < requested["cpu_cores"]:
                insufficient = True; reasons.append("估算 CPU 余量不足")
        if requested["memory_bytes"]:
            if usable_memory is None:
                unknown = True; reasons.append("可用内存未知")
            elif usable_memory < requested["memory_bytes"]:
                insufficient = True; reasons.append("可用内存不足")
        if requested["gpu_memory_bytes"]:
            if not gpus:
                insufficient = True; reasons.append("没有检测到 GPU")
            elif any(gpu["estimated_usable_memory_bytes"] is not None and
                     gpu["estimated_usable_memory_bytes"] >= requested["gpu_memory_bytes"] and
                     gpu["utilization_pct"] is not None and gpu["utilization_pct"] <= 70 for gpu in gpus):
                pass
            elif all(gpu["estimated_usable_memory_bytes"] is not None and
                     gpu["estimated_usable_memory_bytes"] < requested["gpu_memory_bytes"] for gpu in gpus):
                insufficient = True; reasons.append("每块 GPU 的可用显存均不足")
            else:
                unknown = True; reasons.append("GPU 负载或显存余量无法确认")
        verdict = "insufficient" if insufficient else "unknown" if unknown else "likely_available"
        if verdict == "likely_available":
            reasons.append("当前采样显示有余量；启动前仍需核对任务预约和实际资源需求")
    return {"id": server["id"], "name": server["name"], "status": effective_status,
            "observed_at": server.get("last_seen"), "sample_age_seconds": round(age, 1) if age is not None else None,
            "active_jobs": len(server.get("jobs") or []), "active_claims": active_claims,
            "ssh_connections": (snapshot.get("ssh") or {}).get("tcp_connections"),
            "cpu": {"logical_processors": logical, "observed_processors": observed, "used_pct": used_pct,
                    "estimated_usable_logical_cores": usable_cores},
            "memory": {"total_bytes": total_memory, "available_bytes": available_memory,
                       "estimated_usable_bytes": usable_memory},
            "gpus": gpus, "verdict": verdict, "reasons": reasons}
