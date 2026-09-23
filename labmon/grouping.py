"""Conservatively turn a process inventory into claimable simulation jobs.

The inventory deliberately contains no command lines or working directories.
Consequently, proximity in time, username, and executable name is *not* proof
that two otherwise unrelated process trees are one simulation.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import math
import re
import time

from .estimates import iso


_NON_JOB_SOFTWARE = {"system", "unknown", "other", ""}
# Each of these process instances starts one MPI invocation. A shared service,
# shell, desktop GUI, or generic Python process is intentionally not an anchor.
_MPI_LAUNCHERS = {"hydra_pmi_proxy", "mpiexec", "mpiexec.hydra", "mpirun"}


def _timestamp(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.timestamp() if parsed.tzinfo is not None else None
        except (ValueError, OverflowError):
            pass
    return None


def _number(value):
    return value if isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value) else None


def _sum_or_unknown(values):
    values = list(values)
    return sum(values) if values and all(_number(value) is not None for value in values) else None


def _name(process):
    name = str(process.get("name") or "").replace("\\", "/").rsplit("/", 1)[-1].casefold()
    return re.sub(r"\.exe(?:#\d+)?$|#\d+$", "", name)


def _same_owner(a, b):
    first, second = a.get("owner"), b.get("owner")
    return not (first and second) or str(first).casefold() == str(second).casefold()


def _valid_parent(parent, child):
    parent_start, child_start = _timestamp(parent.get("start_time")), _timestamp(child.get("start_time"))
    return (parent_start is not None and child_start is not None and parent_start <= child_start
            and isinstance(parent.get("key"), str) and bool(parent["key"])
            and _same_owner(parent, child))


def group_jobs(host_id: str, processes: list[dict], now: float | None = None):
    """Return the existing Hub job schema, merging only evidenced relations.

    Same-software parent/child chains and direct children of a *live*, uniquely
    identified MPI invocation are inferred automatically. Disconnected trees
    remain separate; the Hub can offer an explicit, persisted manual grouping.
    """
    now = time.time() if now is None else now
    valid = {p["pid"]: p for p in processes
             if isinstance(p, dict) and isinstance(p.get("pid"), int) and not isinstance(p["pid"], bool)
             and isinstance(p.get("key"), str) and p["key"]}
    selected = {}
    for pid, process in valid.items():
        software, role = process.get("software", "unknown"), process.get("role", "unknown")
        if software in _NON_JOB_SOFTWARE or role in {"service", "system"}:
            continue
        if _timestamp(process.get("start_time")) is None:
            continue
        if software == "general" and role not in {"solver", "application", "launcher"}:
            cpu, memory = _number(process.get("cpu_pct")), _number(process.get("memory_bytes"))
            if cpu is None or cpu < 0.05 or memory is None or memory < 256 * 1024 * 1024:
                continue
        selected[pid] = process

    groups = {}
    for pid, process in selected.items():
        root, visited = process, {pid}
        while True:
            parent = selected.get(root.get("parent_pid"))
            if not parent or parent["pid"] in visited or not _valid_parent(parent, root):
                break
            if parent.get("software") != process.get("software"):
                break
            # A single CAE/COMSOL GUI (or similar application) can launch
            # multiple independent solves. It is not a per-job ancestor.
            if parent.get("role") == "application":
                break
            visited.add(parent["pid"])
            root = parent

        root_key, start = root["key"], _timestamp(root.get("start_time"))
        parent = valid.get(root.get("parent_pid"))
        if (parent and parent["pid"] not in visited and _name(parent) in _MPI_LAUNCHERS
                and _valid_parent(parent, root)):
            # Do not follow the launcher to hydra_service or a shared shell.
            root_key = f"mpi\0{parent['key']}\0{process['software']}"
            start = _timestamp(parent.get("start_time"))
        group = groups.setdefault(root_key, {"root": root, "start": start, "members": []})
        group["members"].append(process)
        if _timestamp(root.get("start_time")) < _timestamp(group["root"].get("start_time")):
            group["root"] = root

    jobs = []
    for root_key, group in groups.items():
        members, root = group["members"], group["root"]
        cpu, start = _sum_or_unknown(p.get("cpu_pct") for p in members), group["start"]
        identity = hashlib.sha256(f"{host_id}\0{root_key}".encode()).hexdigest()[:24]
        jobs.append({"id": identity, "host_id": host_id, "name": root.get("name", "计算任务"),
                     "software": root.get("software", "unknown"),
                     "state": "unknown" if cpu is None else "computing" if cpu >= 0.05 else "idle",
                     "process_count": len(members), "pids": sorted(p["pid"] for p in members),
                     "process_keys": sorted(p["key"] for p in members), "cpu_pct": cpu,
                     "memory_bytes": _sum_or_unknown(p.get("memory_bytes") for p in members),
                     "started_at": iso(start) if start is not None else None,
                     "elapsed_seconds": max(0, now - start) if start is not None else None,
                     "claim": None, "estimate": None})
    return sorted(jobs, key=lambda job: -(job["cpu_pct"] or 0))
