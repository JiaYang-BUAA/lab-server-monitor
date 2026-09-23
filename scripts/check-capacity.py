"""Read the Hub's public, read-only capacity endpoint before an SSH simulation."""
from __future__ import annotations

import argparse
import json
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")


LABELS = {"likely_available": "当前估算有余量", "insufficient": "当前余量不足",
          "unknown": "无法判断", "not_assessed": "未评估"}


def parser():
    command = argparse.ArgumentParser(description="仿真前读取服务器负载；结果只是瞬时建议，不会预留资源或启动任务。")
    command.add_argument("--url", required=True, help="Hub 的 HTTPS 根地址")
    command.add_argument("--host-id", help="只查询这一台服务器；省略时列出全部")
    command.add_argument("--cpu-cores", type=int, default=0, help="预计使用的逻辑 CPU 核数")
    command.add_argument("--memory-gb", type=float, default=0, help="预计使用的 GiB 内存")
    command.add_argument("--gpu-vram-gb", type=float, default=0, help="单块 GPU 所需 GiB 显存")
    command.add_argument("--json", action="store_true", help="输出完整机器可读 JSON")
    return command


def main(argv=None):
    args = parser().parse_args(argv)
    parts = urlsplit(args.url)
    if parts.scheme != "https" or not parts.netloc or parts.username or parts.password or parts.query or parts.fragment or parts.path not in {"", "/"}:
        parser().error("--url 必须是无路径、无凭据的 HTTPS Hub 根地址")
    if not any((args.cpu_cores, args.memory_gb, args.gpu_vram_gb)):
        parser().error("至少填写一项实际资源需求")
    if args.cpu_cores < 0 or args.memory_gb < 0 or args.gpu_vram_gb < 0:
        parser().error("资源需求不能为负")
    query = {"cpu_cores": args.cpu_cores, "memory_gb": args.memory_gb, "gpu_vram_gb": args.gpu_vram_gb}
    if args.host_id:
        query["host_id"] = args.host_id
    target = args.url.rstrip("/") + "/api/capacity?" + urlencode(query)
    try:
        with urlopen(Request(target, headers={"Accept": "application/json"}), timeout=12) as response:
            report = json.load(response)
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        print("无法读取负载：" + str(exc), file=sys.stderr)
        return 4
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for server in report["servers"]:
            cpu = server["cpu"]
            memory = server["memory"]
            free_gib = memory["estimated_usable_bytes"] / 1024 ** 3 if memory["estimated_usable_bytes"] is not None else None
            age = server.get("sample_age_seconds")
            details = [f"状态 {server['status']}",
                       f"采样 {server.get('observed_at') or '未知'}" + (f"（{age:.1f} 秒前）" if age is not None else ""),
                       f"CPU {cpu['used_pct']}%" if cpu["used_pct"] is not None else "CPU 未知",
                       f"估算可用 {cpu['estimated_usable_logical_cores']} 核" if cpu["estimated_usable_logical_cores"] is not None else "核数未知",
                       f"预留余量后内存 {free_gib:.1f} GiB" if free_gib is not None else "内存未知",
                       f"活跃任务 {server['active_jobs']} 项 / 登记 {server['active_claims']} 项"]
            print(f"{server['id']} ({server['name']}): {LABELS.get(server['verdict'], server['verdict'])}；" + "，".join(details))
            for reason in server["reasons"]:
                print("  - " + reason)
        print(report["advisory"])
    verdicts = {server["verdict"] for server in report["servers"]}
    return 0 if "likely_available" in verdicts else 2 if verdicts == {"insufficient"} else 3


if __name__ == "__main__":
    raise SystemExit(main())
