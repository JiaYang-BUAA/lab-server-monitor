"""Dependency-free, read-only telemetry agents and LAN collaboration hub."""
from __future__ import annotations

import argparse
from collections import deque
import copy
from datetime import datetime, timezone
import hashlib
import hmac
import http.client
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import logging
import math
import mimetypes
from pathlib import Path
import re
import secrets
import socket
import sys
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import Request, urlopen

from .estimates import Estimator, iso
from .storage import ConflictError, MissingError, OwnershipError, Store

LOG = logging.getLogger("labmon")
COOKIE = "labmon_session"
MAX_BODY = 1024 * 1024
MAX_AGENT_RESPONSE = 8 * 1024 * 1024
EDITABLE = {"owner_name", "task_name", "expected_end", "notes", "log_path", "log_kind", "total_units"}


def timestamp(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                return parsed.timestamp()
        except (ValueError, OverflowError):
            pass
    return None


def number(value):
    return value if isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value) else None


def nullable_sum(values):
    values = list(values)
    # An incomplete total should remain unknown instead of understating load.
    return sum(values) if values and all(number(x) is not None for x in values) else None


def group_jobs(host_id: str, processes: list[dict], now: float | None = None):
    now = time.time() if now is None else now
    valid = {p["pid"]: p for p in processes if isinstance(p.get("pid"), int) and p.get("key")}
    selected = {}
    for pid, process in valid.items():
        software, role = process.get("software", "unknown"), process.get("role", "unknown")
        if software in {"system", "unknown", "other", ""} or role in {"service", "system"}:
            continue
        if timestamp(process.get("start_time")) is None:
            continue  # Still visible in snapshot.processes; cannot safely identify a task.
        if software == "general" and role not in {"solver", "application", "launcher"}:
            cpu, memory = number(process.get("cpu_pct")), number(process.get("memory_bytes"))
            if cpu is None or cpu < 0.05 or memory is None or memory < 256 * 1024 * 1024:
                continue
        selected[pid] = process
    groups = {}
    for pid, process in selected.items():
        root, visited = process, {pid}
        while True:
            parent = selected.get(root.get("parent_pid"))
            if not parent or parent["pid"] in visited:
                break
            parent_start, child_start = timestamp(parent.get("start_time")), timestamp(root.get("start_time"))
            if parent_start is None or child_start is None or parent_start > child_start:
                break  # Parent PID has been reused, or the relation is unverifiable.
            if parent.get("software") != process.get("software"):
                break
            visited.add(parent["pid"])
            root = parent
        root_key, start = root["key"], timestamp(root.get("start_time"))
        parent = valid.get(root.get("parent_pid"))
        if parent and str(parent.get("name", "")).casefold().removesuffix(".exe") == "hydra_pmi_proxy":
            parent_start = timestamp(parent.get("start_time"))
            if parent["pid"] not in visited and parent_start is not None and parent_start <= start:
                # A live per-job MPI proxy can identify sibling solver ranks.
                # Never follow its ancestors: hydra_service may serve many jobs.
                # Keep software in the identity and count only selected solvers.
                root_key = f"mpi\0{parent['key']}\0{process['software']}"
                start = parent_start
        groups.setdefault(root_key, {"root": root, "start": start, "members": []})["members"].append(process)
    jobs = []
    for root_key, group in groups.items():
        members, root = group["members"], group["root"]
        cpu = nullable_sum(p.get("cpu_pct") for p in members)
        start = group["start"]
        identity = hashlib.sha256(f"{host_id}\0{root_key}".encode()).hexdigest()[:24]
        jobs.append({"id": identity, "host_id": host_id, "name": root.get("name", "计算任务"),
                     "software": root.get("software", "unknown"),
                     "state": "unknown" if cpu is None else "computing" if cpu >= 0.05 else "idle",
                     "process_count": len(members), "pids": sorted(p["pid"] for p in members),
                     "process_keys": sorted(p["key"] for p in members), "cpu_pct": cpu,
                     "memory_bytes": nullable_sum(p.get("memory_bytes") for p in members),
                     "started_at": iso(start) if start is not None else None,
                     "elapsed_seconds": max(0, now - start) if start is not None else None,
                     "claim": None, "estimate": None})
    return sorted(jobs, key=lambda job: -(job["cpu_pct"] or 0))


def validate_claim(payload: dict, previous: dict | None = None):
    if not isinstance(payload, dict):
        raise ValueError("请求正文必须是 JSON 对象")
    result = {key: previous.get(key) for key in EDITABLE} if previous else {}
    for key in EDITABLE:
        if key in payload:
            result[key] = payload[key]
    for key, maximum, required in (("owner_name", 40, True), ("task_name", 120, True),
                                   ("notes", 500, False), ("log_path", 2048, False)):
        value = result.get(key, "")
        if not isinstance(value, str):
            raise ValueError(f"{key} 必须是文本")
        value = value.strip()
        if len(value) > maximum or (required and not value) or "\x00" in value:
            raise ValueError(f"{key} 长度不符合要求")
        result[key] = value
    end = result.get("expected_end")
    if end in (None, ""):
        result["expected_end"] = None
    elif not isinstance(end, str) or timestamp(end) is None:
        raise ValueError("预计结束时间必须带时区，例如 2026-09-23T18:00:00+08:00")
    else:
        result["expected_end"] = iso(timestamp(end))
    kind = result.get("log_kind") or "abaqus"
    if kind not in {"abaqus", "fluent", "comsol"}:
        raise ValueError("不支持的日志类型")
    result["log_kind"] = kind
    total = result.get("total_units")
    if total is not None and (number(total) is None or total <= 0):
        raise ValueError("总进度必须是有限正数")
    result["total_units"] = total
    return result


def validate_agent_url(value: str):
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("agent_url 必须是配置的 HTTP(S) 服务地址")
    if parsed.path not in {"", "/"}:
        raise ValueError("agent_url 不应包含路径")
    return value.rstrip("/")


def validate_public_origin(value: str):
    """Accept one explicit HTTPS hostname for a loopback-only public proxy."""
    if not isinstance(value, str) or value != value.strip():
        raise ValueError("public_origin 必须是完整的 HTTPS 地址")
    try:
        parsed = urlsplit(value)
        if parsed.port is not None:
            raise ValueError
    except ValueError as exc:
        raise ValueError("public_origin 必须是无端口的 HTTPS 域名") from exc
    hostname = (parsed.hostname or "").lower()
    labels = hostname.split(".")
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        raise ValueError("public_origin 必须使用 DNS 域名")
    if (parsed.scheme != "https" or not hostname or parsed.username or parsed.password
            or parsed.netloc.lower() != hostname or parsed.path or parsed.query or parsed.fragment or len(labels) < 2
            or len(hostname) > 253 or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                                       for label in labels)):
        raise ValueError("public_origin 必须是无路径的 HTTPS 域名")
    return "https://" + hostname


def redact_public_state(state: dict):
    """Keep useful load data while omitting private network and log details."""
    for server in state["servers"]:
        snapshot = server.get("snapshot")
        if isinstance(snapshot, dict):
            snapshot.pop("hostname", None)
            if isinstance(snapshot.get("ssh"), dict):
                snapshot["ssh"]["connections"] = []
            for process in snapshot.get("processes", []):
                if isinstance(process, dict):
                    process.pop("owner", None)
        for job in server["jobs"]:
            claim = job.get("claim")
            if isinstance(claim, dict) and not claim.get("can_edit"):
                claim.pop("log_path", None)
    for claim in state["recent_tasks"]:
        if isinstance(claim, dict):
            claim.pop("log_path", None)
    return state


class AgentRuntime:
    def __init__(self, config: dict, collector=None):
        self.config = config
        platform = config.get("platform", "auto")
        if not isinstance(platform, str) or platform not in {"auto", "linux", "windows"}:
            raise ValueError("platform 必须为 auto、windows 或 linux")
        if collector is None:
            if platform == "auto":
                if sys.platform.startswith("linux"):
                    platform = "linux"
                elif sys.platform == "win32":
                    platform = "windows"
                else:
                    raise ValueError("自动平台检测仅支持 Linux 和 Windows")
            if platform == "linux":
                from .linux_collector import LinuxCollector
                collector = LinuxCollector(config["host_id"])
            elif platform == "windows":
                from .collector import Collector
                collector = Collector(config["exporter_url"], config["sidecar_path"], config["host_id"])
        elif platform == "auto":
            from .linux_collector import LinuxCollector
            from .collector import Collector
            # Custom test collectors have no implied OS. Keep the legacy bridge
            # available for those fixtures while detecting real collector types.
            platform = "linux" if isinstance(collector, LinuxCollector) else "windows" if isinstance(collector, Collector) else "custom"
        self.platform = platform
        self.collector = collector
        self.estimator = Estimator(config.get("allowed_log_roots", []))
        self.lock = threading.RLock()
        self.snapshot = None
        self.sampled = 0.0

    def sample(self):
        with self.lock:
            now = time.monotonic()
            if self.snapshot is None or now - self.sampled >= self.config.get("poll_seconds", 5):
                snapshot = self.collector.sample()
                if not isinstance(snapshot, dict):
                    raise ValueError("采集器返回格式错误")
                self.snapshot, self.sampled = snapshot, now
            return copy.deepcopy(self.snapshot)

    def metrics(self):
        """Common cross-platform metrics, with the legacy exporter bridge retained."""
        if self.platform == "linux" or self.config.get("metrics_format") == "unified" or not self.config.get("exporter_url"):
            from .metrics import render_metrics
            snapshot = self.sample()
            if snapshot.get("telemetry_status") == "error":
                raise OSError("采集数据不可用")
            return render_metrics(snapshot)
        target = self.config["exporter_url"]
        parsed = urlsplit(target)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("指标代理只允许配置的本机 HTTP exporter")
        import urllib.request
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        try:
            response = urllib.request.build_opener(NoRedirect).open(target, timeout=8)
        except HTTPError as exc:
            exc.close()
            raise
        with response:
            raw = response.read(MAX_AGENT_RESPONSE + 1)
            if len(raw) > MAX_AGENT_RESPONSE:
                raise ValueError("Exporter 指标超过 8 MiB 限制")
            return raw


class HubRuntime:
    def __init__(self, config: dict, fetcher=None, clock=time.time):
        self.config, self.clock = config, clock
        self.poll_seconds = float(config.get("poll_seconds", 5))
        self.stale_seconds = float(config.get("stale_seconds", 30))
        if self.poll_seconds < 1 or self.stale_seconds < self.poll_seconds:
            raise ValueError("轮询间隔至少 1 秒，过期阈值不能小于轮询间隔")
        self.store = Store(Path(config["data_dir"]) / "labmon.sqlite3")
        self.fetcher = fetcher or self._fetch
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread = None
        self.servers = {}
        self.server_configs = {}
        self._missing = {}
        self._recent_jobs = deque(maxlen=100)
        self.estimates = {}
        for server in config.get("servers", []):
            server_id = server["id"]
            if server_id in self.servers:
                raise ValueError("服务器 id 不能重复")
            if server.get("enabled", False):
                validate_agent_url(server["agent_url"])
                if not isinstance(server.get("token"), str) or len(server["token"]) < 16:
                    raise ValueError("Agent token 至少 16 个字符")
            self.server_configs[server_id] = server
            self.servers[server_id] = {"id": server_id, "name": server.get("name", server_id),
                "platform": server.get("platform", "auto"),
                "status": "offline" if server.get("enabled", False) else "unconfigured", "last_seen": None,
                "error": "尚未收到采集数据" if server.get("enabled", False) else None,
                "snapshot": None, "jobs": [], "history": []}

    def _fetch(self, config: dict, path: str, data: dict | None = None):
        body = json.dumps(data).encode("utf-8") if data is not None else None
        request = Request(validate_agent_url(config["agent_url"]) + path, data=body,
                          headers={"Authorization": "Bearer " + config["token"], "Content-Type": "application/json"})
        # Redirects must never forward the configured bearer token to another host.
        import urllib.request
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        try:
            response = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect).open(request, timeout=4)
        except HTTPError as exc:
            exc.close()
            raise
        with response:
            raw = response.read(MAX_AGENT_RESPONSE + 1)
            if len(raw) > MAX_AGENT_RESPONSE:
                raise ValueError("采集响应超过大小限制")
            return json.loads(raw)

    def start(self):
        self.thread = threading.Thread(target=self._loop, name="labmon-poller", daemon=True)
        self.thread.start()

    def close(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=6)

    def _loop(self):
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                self.poll_once()
            except Exception:
                LOG.exception("Telemetry polling failed")
            self.stop_event.wait(max(0.1, self.poll_seconds - (time.monotonic() - started)))

    def _stable_jobs(self, host_id, jobs, healthy, processes=None, fresh=True):
        previous = self.servers[host_id]["jobs"]
        current_by_key = {p.get("key"): p for p in (processes or [])}
        used = set()
        for job in jobs:
            keys = set(job["process_keys"])
            matches = [old for old in previous if old["id"] not in used and keys.intersection(old["process_keys"])]
            if matches:
                match = max(matches, key=lambda old: len(keys.intersection(old["process_keys"])))
                job["id"] = match["id"]
                job["started_at"] = match["started_at"]
                used.add(match["id"])
                start = timestamp(job["started_at"])
                job["elapsed_seconds"] = max(0, self.clock() - start) if start is not None else None
            self._missing.pop((host_id, job["id"]), None)
        live = {job["id"] for job in jobs}
        for old in previous:
            if old["id"] in live:
                continue
            still_alive = [current_by_key[key] for key in old["process_keys"] if key in current_by_key]
            if still_alive:
                # Generic high-resource jobs remain tracked when they become idle;
                # falling below the discovery threshold is not a process exit.
                retained = copy.deepcopy(old)
                cpu = nullable_sum(p.get("cpu_pct") for p in still_alive)
                retained.update(process_count=len(still_alive), pids=sorted(p["pid"] for p in still_alive),
                    process_keys=sorted(p["key"] for p in still_alive), cpu_pct=cpu,
                    memory_bytes=nullable_sum(p.get("memory_bytes") for p in still_alive),
                    state="unknown" if cpu is None else "computing" if cpu >= 0.05 else "idle")
                jobs.append(retained)
                self._missing.pop((host_id, old["id"]), None)
                continue
            missing_key = (host_id, old["id"])
            if fresh:
                self._missing[missing_key] = self._missing.get(missing_key, 0) + 1 if healthy else 0
            else:
                self._missing.setdefault(missing_key, 0)
            if self._missing[missing_key] >= 2:
                ended = copy.deepcopy(old)
                ended.update(state="ended", ended_at=iso(self.clock()), claim=None, estimate=None)
                self._recent_jobs.appendleft(ended)
                self.store.end_job(host_id, old["id"])
                self._missing.pop(missing_key, None)
            else:
                pending = copy.deepcopy(old)
                pending["state"] = "unknown"
                pending["cpu_pct"] = None
                pending["memory_bytes"] = None
                jobs.append(pending)
        return jobs

    def poll_once(self):
        # Bound concurrent network requests while polling configured servers.
        from concurrent.futures import ThreadPoolExecutor
        enabled = [s for s in self.server_configs.values() if s.get("enabled", False)]
        def one(config):
            try:
                return config["id"], self.fetcher(config, "/snapshot"), None
            except (OSError, ValueError, HTTPError, URLError, TimeoutError, KeyError):
                return config["id"], None, "采集服务暂时不可用；请检查服务、网络和访问令牌"
        with ThreadPoolExecutor(max_workers=max(1, min(8, len(enabled)))) as pool:
            results = list(pool.map(one, enabled))
        with self.lock:
            for host_id, snapshot, error in results:
                state = self.servers[host_id]
                now = self.clock()
                observed = timestamp(snapshot.get("observed_at")) if isinstance(snapshot, dict) else None
                if snapshot is not None and (snapshot.get("host_id") != host_id or observed is None or observed > now + 60 or not isinstance(snapshot.get("processes"), list)):
                    snapshot, error = None, "采集数据标识、时间或格式不匹配"
                if snapshot is None or snapshot.get("telemetry_status") == "error":
                    state["error"] = error or "采集器报告错误"
                    last = timestamp(state["last_seen"])
                    state["status"] = "stale" if last is not None else "offline"
                    for key in list(self._missing):
                        if key[0] == host_id:
                            self._missing[key] = 0
                    continue
                if now - observed > self.stale_seconds:
                    state["status"], state["error"] = "stale", "采集数据已超过有效时间"
                    if state["snapshot"] is None:
                        state["snapshot"], state["last_seen"] = snapshot, snapshot["observed_at"]
                    for key in list(self._missing):
                        if key[0] == host_id:
                            self._missing[key] = 0
                    continue
                previous_seen = timestamp(state["last_seen"])
                healthy = snapshot.get("process_status", snapshot.get("telemetry_status")) == "ok"
                fresh = previous_seen is None or observed > previous_seen
                state.update(snapshot=snapshot, last_seen=snapshot["observed_at"], status="online", error=None)
                state["jobs"] = self._stable_jobs(host_id, group_jobs(host_id, snapshot["processes"], now), healthy, snapshot["processes"], fresh)
                if not state["history"] or state["history"][-1]["at"] != snapshot["observed_at"]:
                    gpu_values = [number(gpu.get("utilization_pct")) for gpu in snapshot.get("gpus", [])]
                    gpu_values = [value for value in gpu_values if value is not None]
                    state["history"].append({"at": snapshot["observed_at"],
                        "cpu_pct": number(snapshot.get("cpu", {}).get("percent")),
                        "memory_pct": number(snapshot.get("memory", {}).get("percent")),
                        "gpu_pct": max(gpu_values) if gpu_values else None})
                    state["history"] = state["history"][-120:]
        # I/O stays outside the state lock so browser reads and edits remain fast.
        for claim in self.store.claims():
            if not claim.get("log_path"):
                self.estimates.pop(claim["id"], None)
                continue
            config = self.server_configs.get(claim["host_id"])
            if not config or not config.get("enabled"):
                continue
            with self.lock:
                online = self.servers[claim["host_id"]]["status"] == "online"
            if not online:
                continue
            try:
                estimate = self.fetcher(config, "/estimate", {key: claim.get(key) for key in ("log_path", "log_kind", "total_units")})
                if not isinstance(estimate, dict) or "status" not in estimate:
                    raise ValueError("estimate response invalid")
            except (OSError, ValueError, HTTPError, URLError, TimeoutError):
                estimate = {"status": "error", "remaining_seconds": None, "estimated_end": None,
                    "progress_pct": None, "source": "log", "detail": "无法读取或识别已绑定日志；请核对允许目录和日志类型",
                    "updated_at": iso(self.clock())}
            with self.lock:
                self.estimates[claim["id"]] = estimate

    def state(self, token: str, identity: str):
        claims = {(claim["host_id"], claim["job_id"]): claim for claim in self.store.claims(token)}
        with self.lock:
            servers = copy.deepcopy(list(self.servers.values()))
            recent = copy.deepcopy(list(self._recent_jobs))
            estimates = copy.deepcopy(self.estimates)
        for server in servers:
            last = timestamp(server["last_seen"])
            if server["status"] == "online" and (last is None or self.clock() - last > self.stale_seconds):
                server["status"], server["error"] = "stale", "采集数据已超过有效时间"
            for job in server["jobs"]:
                claim = claims.get((server["id"], job["id"]))
                job["claim"] = claim
                estimate = estimates.get(claim["id"]) if claim else None
                if server["status"] != "online":
                    job["state"] = "unknown"
                    if estimate:
                        estimate = {**estimate, "status": "unknown", "remaining_seconds": None,
                                    "estimated_end": None, "detail": "服务器数据已过期，暂停预计结束时间"}
                job["estimate"] = estimate
        saved = self.store.claims(token, recent=True)
        persisted_recent = [dict(claim, state="ended" if claim["ended_at"] else "released") for claim in saved if claim["ended_at"] or claim["released_at"]]
        return {"updated_at": iso(self.clock()), "poll_seconds": self.poll_seconds,
                "grafana_url": self.config.get("grafana_url"), "servers": servers,
                "board": self.store.board(),
                "recent_tasks": (persisted_recent + recent)[:50], "identity": {"name": identity}}

    def require_job(self, host_id, job_id):
        with self.lock:
            server = self.servers.get(host_id)
            if server is None:
                raise MissingError("服务器不存在")
            last = timestamp(server["last_seen"])
            if server["status"] != "online" or last is None or self.clock() - last > self.stale_seconds:
                raise ConflictError("服务器当前数据不可用，刷新后再登记")
            if not any(job["id"] == job_id and (host_id, job_id) not in self._missing for job in server["jobs"]):
                raise ConflictError("任务已消失或改变，请刷新后再登记")


class MonitorHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, config, runtime):
        self.config, self.runtime = config, runtime
        self.public_origin = None
        if config.get("mode") == "hub" and config.get("public_origin"):
            self.public_origin = validate_public_origin(config["public_origin"])
            if address[0] != "127.0.0.1":
                raise ValueError("公网代理模式要求 Hub 只监听 127.0.0.1")
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "LabMonitor/1.0"

    def log_message(self, fmt, *args):
        # Avoid query strings, request bodies, tokens, or local log paths in logs.
        LOG.info("%s %s", self.command, urlsplit(self.path).path)

    def _reply(self, status, payload, content_type="application/json; charset=utf-8", headers=None):
        raw = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8") if isinstance(payload, (dict, list)) else payload
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        if self._public_request():
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        if getattr(self, "new_cookie", None):
            secure = "; Secure" if self._public_request() else ""
            self.send_header("Set-Cookie", f"{COOKIE}={self.new_cookie}; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000{secure}")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)

    def _error(self, status, detail):
        # Some errors reject a POST before reading its body (for example the
        # read-only Grafana endpoint allowlist). Do not parse those remaining
        # bytes as another request on this HTTP/1.1 connection.
        self.close_connection = True
        self._reply(status, {"error": detail}, headers={"Connection": "close"})

    def _body(self):
        if self.headers.get("Transfer-Encoding"):
            raise ValueError("不支持分块请求正文")
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Content-Length 无效") from exc
        if size < 0 or size > MAX_BODY:
            raise ValueError("请求正文过大")
        if self.headers.get_content_type() != "application/json":
            raise ValueError("请求正文必须使用 application/json")
        raw = self.rfile.read(size)
        try:
            result = json.loads(raw or b"{}", parse_constant=lambda _: (_ for _ in ()).throw(ValueError("JSON 数字无效")))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("JSON 格式无效") from exc
        if not isinstance(result, dict):
            raise ValueError("请求正文必须是 JSON 对象")
        return result

    def _host_valid(self):
        host = self.headers.get("Host", "")
        if not host or "/" in host or "\\" in host or "@" in host:
            return False
        if self._public_request():
            return True
        try:
            parsed = urlsplit("http://" + host)
            hostname, port = parsed.hostname, parsed.port or 80
        except ValueError:
            return False
        if port != self.server.server_port:
            return False
        allowed = {"localhost", "127.0.0.1", "::1", socket.gethostname().lower(), socket.getfqdn().lower()}
        allowed.update(str(x).lower() for x in self.server.config.get("allowed_hosts", []))
        listen = self.server.config.get("listen_host", "127.0.0.1")
        if listen not in {"0.0.0.0", "::"}:
            allowed.add(listen.lower())
        try:
            allowed.update(address[4][0].lower() for address in socket.getaddrinfo(socket.gethostname(), None))
        except OSError:
            pass
        return bool(hostname and hostname.lower() in allowed)

    def _public_request(self):
        origin = self.server.public_origin
        return bool(origin and self.headers.get("Host", "").lower() == urlsplit(origin).netloc)

    def _origin_valid(self):
        if not self._host_valid():
            return False
        origin = self.headers.get("Origin", "")
        try:
            parsed = urlsplit(origin)
            scheme = "https" if self._public_request() else "http"
            return parsed.scheme == scheme and parsed.netloc.lower() == self.headers.get("Host", "").lower() and not parsed.path and not parsed.query and not parsed.fragment
        except ValueError:
            return False

    def _session(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            token = cookie[COOKIE].value if COOKIE in cookie else None
        except Exception:
            token = None
        token, identity, created = self.server.runtime.store.session(token)
        if created:
            self.new_cookie = token
        return token, identity

    def do_GET(self):
        self._handle()

    def do_HEAD(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def do_PATCH(self):
        self._handle()

    def do_PUT(self):
        self._handle()

    def do_DELETE(self):
        self._handle()

    def _handle(self):
        self.new_cookie = None
        self.connection.settimeout(15)
        try:
            if self.server.config["mode"] == "agent":
                return self._agent()
            if not self._host_valid():
                self.close_connection = True
                return self._error(403, "Host 不在允许的服务器地址中")
            if self.command not in {"GET", "HEAD"} and not self._origin_valid():
                self.close_connection = True
                return self._error(403, "请求来源校验失败")
            return self._hub()
        except OwnershipError as exc:
            self._error(403, str(exc))
        except ConflictError as exc:
            self._error(409, str(exc))
        except MissingError as exc:
            self._error(404, str(exc))
        except (ValueError, FileNotFoundError, PermissionError, IsADirectoryError) as exc:
            self.close_connection = True
            detail = str(exc) if isinstance(exc, ValueError) else "无法读取允许目录中的日志文件"
            self._error(400, detail)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
        except Exception:
            LOG.exception("Request failed")
            self.close_connection = True
            self._error(500, "服务处理失败，请稍后重试")

    def _agent(self):
        expected = "Bearer " + self.server.config["token"]
        provided = self.headers.get("Authorization", "")
        if not hmac.compare_digest(provided.encode(), expected.encode()):
            self.close_connection = True
            return self._error(401, "需要有效的采集令牌")
        path = urlsplit(self.path).path
        if self.command in {"GET", "HEAD"} and path == "/healthz":
            return self._reply(200, {"ok": True, "mode": "agent"})
        if self.command in {"GET", "HEAD"} and path == "/snapshot":
            return self._reply(200, self.server.runtime.sample())
        if self.command in {"GET", "HEAD"} and path == "/metrics":
            try:
                return self._reply(200, self.server.runtime.metrics(), "text/plain; version=0.0.4; charset=utf-8")
            except (OSError, URLError, HTTPError):
                return self._error(502, "Exporter 指标暂时不可用")
        if self.command == "POST" and path == "/estimate":
            body = self._body()
            return self._reply(200, self.server.runtime.estimator.estimate(body.get("log_path", ""), body.get("log_kind", ""), body.get("total_units")))
        return self._error(404, "接口不存在")

    def _hub(self):
        path = urlsplit(self.path).path
        if path == "/grafana" or path.startswith("/grafana/"):
            return self._proxy_grafana()
        if self.command in {"GET", "HEAD"} and path == "/healthz":
            return self._reply(200, {"ok": True, "mode": "hub"})
        if path == "/api/state" and self.command in {"GET", "HEAD"}:
            token, identity = self._session()
            state = self.server.runtime.state(token, identity)
            if self._public_request():
                state = redact_public_state(state)
            return self._reply(200, state)
        if path == "/api/board" and self.command in {"GET", "HEAD"}:
            self._session()
            return self._reply(200, self.server.runtime.store.board())
        if path == "/api/board" and self.command == "PUT":
            body = self._body()
            _, identity = self._session()
            try:
                board = self.server.runtime.store.update_board(body.get("text"), body.get("revision"), body.get("editor_name", identity))
            except ConflictError as exc:
                return self._reply(409, {"error": str(exc), "board": self.server.runtime.store.board()})
            return self._reply(200, {"ok": True, "board": board})
        if path == "/api/identity" and self.command == "POST":
            body = self._body()
            name = body.get("name", "")
            if not isinstance(name, str) or not name.strip() or len(name.strip()) > 40:
                raise ValueError("姓名需为 1–40 个字符")
            token, _ = self._session()
            self.server.runtime.store.identity(token, name.strip())
            return self._reply(200, {"ok": True, "identity": {"name": name.strip()}})
        if path == "/api/claims" and self.command == "POST":
            body = self._body()
            payload = validate_claim(body)
            token, _ = self._session()
            host, job = body.get("host_id"), body.get("job_id")
            if not isinstance(host, str) or not isinstance(job, str):
                raise ValueError("host_id 和 job_id 必须是文本")
            # Keep existence check and transaction adjacent under the state lock.
            with self.server.runtime.lock:
                self.server.runtime.require_job(host, job)
                self.server.runtime.store.create(token, host, job, payload)
            self.server.runtime.store.identity(token, payload["owner_name"])
            return self._reply(200, {"ok": True})
        match = re.fullmatch(r"/api/claims/([a-f0-9]{32})", path)
        if match and self.command in {"PATCH", "DELETE"}:
            body = self._body()
            token, _ = self._session()
            old = self.server.runtime.store.claim(match.group(1), token)
            if not old["can_edit"]:
                raise OwnershipError("仅登记时使用的浏览器会话可以修改或释放登记")
            payload = validate_claim(body, old) if self.command == "PATCH" else None
            self.server.runtime.store.update(token, match.group(1), payload)
            if payload is None or any(payload.get(key) != old.get(key) for key in ("log_path", "log_kind", "total_units")):
                with self.server.runtime.lock:
                    self.server.runtime.estimates.pop(match.group(1), None)
            return self._reply(200, {"ok": True})
        if path.startswith("/api/"):
            return self._error(404, "接口不存在")
        if self.command not in {"GET", "HEAD"}:
            return self._error(405, "此资源不支持写入")
        names = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js", "/styles.css": "styles.css", "/favicon.ico": "favicon.ico"}
        if path not in names:
            return self._error(404, "页面不存在")
        root = Path(self.server.config.get("web_dir", Path(__file__).resolve().parent.parent / "web"))
        file = root / names[path]
        if not file.is_file():
            return self._error(404, "页面文件不存在")
        kind = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        self._reply(200, file.read_bytes(), kind + ("; charset=utf-8" if kind.startswith("text/") or kind == "application/javascript" else ""),
                    {"Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-src 'self'; base-uri 'none'; frame-ancestors 'self'"})

    def _proxy_grafana(self):
        target = self.server.config.get("grafana_upstream", "http://127.0.0.1:3000")
        parsed = urlsplit(target)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/", "/grafana", "/grafana/"}:
            raise ValueError("Grafana 代理仅允许固定本机 HTTP 地址")
        split = urlsplit(self.path)
        decoded = unquote(split.path)
        if any(part in {"..", "."} for part in decoded.split("/")) or "\\" in decoded or not decoded.startswith("/grafana"):
            raise ValueError("Grafana 路径无效")
        if split.path == "/grafana":
            return self._reply(302, b"", headers={"Location": "/grafana/"})
        # The proxy always acts as an anonymous Viewer: no credentials or session
        # cookies from the browser are forwarded to the loopback-only service.
        if self.command not in {"GET", "HEAD", "POST"}:
            return self._error(405, "监测面板只提供只读访问")
        if self.command == "POST" and decoded not in {"/grafana/api/ds/query", "/grafana/api/tsdb/query", "/grafana/api/frontend-metrics"}:
            return self._error(403, "监测面板仅允许只读查询")
        if decoded.startswith(("/grafana/login", "/grafana/logout", "/grafana/admin", "/grafana/api/admin", "/grafana/api/login")):
            return self._error(403, "管理登录仅允许服务器本机访问")
        body = None
        if self.command == "POST":
            body = json.dumps(self._body()).encode("utf-8")
        path = split.path + ("?" + split.query if split.query else "")
        headers = {"Host": parsed.netloc, "X-Forwarded-Host": self.headers["Host"],
                   "X-Forwarded-Proto": "https" if self._public_request() else "http",
                   "Accept": self.headers.get("Accept", "*/*")}
        if body is not None:
            headers["Content-Type"] = self.headers.get("Content-Type", "application/json")
        for header in ("If-None-Match", "If-Modified-Since", "Range"):
            if self.headers.get(header):
                headers[header] = self.headers[header]
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=12)
        try:
            connection.request(self.command, path, body=body, headers=headers)
            response = connection.getresponse()
            data = response.read(32 * 1024 * 1024 + 1)
            if len(data) > 32 * 1024 * 1024:
                return self._error(502, "面板响应过大")
            forwarded = {}
            for header in ("ETag", "Last-Modified", "Content-Encoding", "Content-Range"):
                if response.getheader(header):
                    forwarded[header] = response.getheader(header)
            location = response.getheader("Location")
            if location:
                link = urlsplit(location)
                allowed_redirect_hosts = {parsed.netloc}
                if self._public_request():
                    allowed_redirect_hosts.add(urlsplit(self.server.public_origin).netloc)
                if link.netloc and link.netloc not in allowed_redirect_hosts:
                    return self._error(502, "面板返回了非本机跳转")
                forwarded["Location"] = (link.path or "/grafana/") + ("?" + link.query if link.query else "")
            return self._reply(response.status, data, response.getheader("Content-Type", "application/octet-stream"), forwarded)
        except (OSError, http.client.HTTPException):
            return self._error(502, "Grafana 暂时不可用，实时资源页仍可使用")
        finally:
            connection.close()


def main():
    parser = argparse.ArgumentParser(description="Laboratory server monitor")
    parser.add_argument("--config", required=True, help="Agent or hub JSON configuration")
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text(encoding="utf-8-sig"))
    mode = config.get("mode")
    if mode not in {"hub", "agent"}:
        parser.error("mode must be hub or agent")
    if mode == "agent" and (not isinstance(config.get("token"), str) or len(config["token"]) < 16):
        parser.error("agent token must have at least 16 characters")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    runtime = HubRuntime(config) if mode == "hub" else AgentRuntime(config)
    server = MonitorHTTPServer((config.get("listen_host", "127.0.0.1"), int(config.get("port", 8766 if mode == "hub" else 8767))), config, runtime)
    if mode == "hub":
        runtime.start()
    LOG.info("%s listening on %s:%d", mode, server.server_address[0], server.server_port)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if mode == "hub":
            runtime.close()


if __name__ == "__main__":
    main()
