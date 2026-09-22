"""Prometheus gauges shared by Windows and Linux agents; unknowns are omitted."""
from datetime import datetime
import math


def render_metrics(snapshot: dict) -> bytes:
    lines = []
    types = set()

    def emit(name, value, labels=None):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            return
        if name not in types:
            lines.append(f"# TYPE {name} gauge")
            types.add(name)
        suffix = ""
        if labels:
            def escape(value):
                return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "")
            suffix = "{" + ",".join(f'{key}="{escape(value)}"' for key, value in labels.items()) + "}"
        lines.append(f"{name}{suffix} {value}")

    try:
        observed = datetime.fromisoformat(snapshot["observed_at"].replace("Z", "+00:00"))
        if observed.tzinfo:
            emit("labmon_snapshot_timestamp_seconds", observed.timestamp())
    except (KeyError, ValueError, AttributeError, TypeError):
        pass
    cpu, memory, network = (snapshot.get(key, {}) for key in ("cpu", "memory", "network"))
    emit("labmon_cpu_percent", cpu.get("percent"))
    emit("labmon_cpu_logical_processors", cpu.get("logical_processors"))
    emit("labmon_cpu_observed_processors", cpu.get("observed_processors"))
    emit("labmon_memory_percent", memory.get("percent"))
    emit("labmon_memory_total_bytes", memory.get("total_bytes"))
    emit("labmon_memory_available_bytes", memory.get("available_bytes"))
    emit("labmon_network_receive_bytes_per_second", network.get("recv_bps"))
    emit("labmon_network_transmit_bytes_per_second", network.get("sent_bps"))
    emit("labmon_ssh_tcp_connections", snapshot.get("ssh", {}).get("tcp_connections"))
    for disk in snapshot.get("disks", []):
        labels = {"volume": disk.get("name", "unknown")}
        for field, metric in (("total_bytes", "size_bytes"), ("free_bytes", "free_bytes"),
                              ("read_bps", "read_bytes_per_second"), ("write_bps", "write_bytes_per_second")):
            emit("labmon_disk_" + metric, disk.get(field), labels)
    for gpu in snapshot.get("gpus", []):
        labels = {"id": gpu.get("id", "unknown"), "name": gpu.get("name", "unknown")}
        for field, metric in (("utilization_pct", "utilization_percent"), ("memory_used_bytes", "memory_used_bytes"),
                              ("memory_total_bytes", "memory_total_bytes"), ("temperature_c", "temperature_celsius"), ("power_w", "power_watts")):
            emit("labmon_gpu_" + metric, gpu.get(field), labels)
    for process in snapshot.get("processes", []):
        labels = {"process_id": process.get("pid"), "process": process.get("name", "unknown")}
        emit("labmon_process_cpu_percent", process.get("cpu_pct"), labels)
        emit("labmon_process_memory_bytes", process.get("memory_bytes"), labels)
    return ("\n".join(lines) + "\n").encode("utf-8")
