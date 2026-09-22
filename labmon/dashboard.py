"""Portable Grafana dashboard for the common Agent /metrics schema."""


def resource_dashboard(web_url="/"):
    definitions = [
        ("CPU · 全机占用", "labmon_cpu_percent", "percent", "{{server}}"),
        ("内存 · 已用比例", "labmon_memory_percent", "percent", "{{server}}"),
        ("GPU · 利用率", "labmon_gpu_utilization_percent", "percent", "{{server}} / GPU {{id}}"),
        ("GPU · 已用显存", "labmon_gpu_memory_used_bytes", "bytes", "{{server}} / GPU {{id}}"),
        ("SSH · TCP 连接数", "labmon_ssh_tcp_connections", "short", "{{server}}"),
        ("磁盘 · 剩余空间", "labmon_disk_free_bytes", "bytes", "{{server}} / {{volume}}"),
        ("进程 · CPU 占用前十（整机百分比）", "topk(10, labmon_process_cpu_percent)", "percent", "{{server}} / {{process}} · {{process_id}}"),
        ("网络 · 接收速率", "labmon_network_receive_bytes_per_second", "Bps", "{{server}}"),
        ("网络 · 发送速率", "labmon_network_transmit_bytes_per_second", "Bps", "{{server}}"),
    ]
    panels = []
    for i, (title, expr, unit, legend) in enumerate(definitions):
        panels.append({"id": i + 1, "title": title, "type": "timeseries",
            "gridPos": {"h": 8, "w": 12, "x": (i % 2) * 12, "y": (i // 2) * 8},
            "datasource": {"type": "prometheus", "uid": "lab-prometheus"},
            "targets": [{"refId": "A", "expr": expr, "legendFormat": legend}],
            "fieldConfig": {"defaults": {"unit": unit, "min": 0, "custom": {"drawStyle": "line", "lineWidth": 2, "fillOpacity": 8, "spanNulls": False}}, "overrides": []},
            "options": {"tooltip": {"mode": "multi"}, "legend": {"displayMode": "list", "placement": "bottom"}}})
    return {"uid": "lab-resources", "title": "实验室服务器 · 资源趋势", "schemaVersion": 39,
            "version": 2, "refresh": "10s", "timezone": "browser", "time": {"from": "now-1h", "to": "now"},
            "editable": False, "tags": ["lab-monitor"], "panels": panels,
            "links": [{"title": "返回任务看板", "url": web_url, "type": "link"}]}
