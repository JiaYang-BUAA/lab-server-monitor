"""Connect any prepared Windows or Linux agent without restarting running services.

Run on the hub. No YAML dependency: only a clearly delimited scrape_configs
block is maintained; promtool validates the complete prospective configuration.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import uuid


HOST_ID = "lab-old"  # Backwards compatibility for the original Python helper API.
BEGIN = "# BEGIN LABMON MANAGED lab-old"
END = "# END LABMON MANAGED lab-old"
PRIVATE_NETWORKS = tuple(ipaddress.IPv4Network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


class ConnectError(Exception):
    """Safe-to-display error text; never include tokens or response bodies."""


def private_ipv4(value: str) -> str:
    try:
        address = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError:
        raise ConnectError("目标服务器地址必须是 IPv4 地址，不能使用主机名或 URL") from None
    if not any(address in network for network in PRIVATE_NETWORKS):
        raise ConnectError("仅允许 10/8、172.16/12 或 192.168/16 内网 IPv4，不能使用公网或回环地址")
    return str(address)


def read_token(path: Path) -> str:
    try:
        if path.stat().st_size > 64 * 1024:
            raise ConnectError("令牌文件过大")
        raw = path.read_text(encoding="utf-8-sig").strip()
        if raw.startswith("{"):
            credentials = json.loads(raw)
            token = credentials.get("agent_token")
        else:
            token = raw
    except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
        raise ConnectError("无法读取令牌文件；应为目标 Agent private.json 或仅包含 agent_token 的文本文件") from None
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,256}", token):
        raise ConnectError("令牌格式不符合本项目生成的 agent_token；请重新安全转移目标 Agent令牌文件")
    return token


def fetch_json(address: str, path: str, token: str, port: int = 8767) -> dict:
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    request = urllib.request.Request(f"http://{address}:{port}{path}",
        headers={"Authorization": "Bearer " + token, "Accept": "application/json"})
    # Ignore machine HTTP proxy settings for this explicitly private peer.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)
    try:
        with opener.open(request, timeout=8) as response:
            data = response.read(8 * 1024 * 1024 + 1)
        if len(data) > 8 * 1024 * 1024:
            raise ConnectError("目标 Agent采集响应超过 8 MiB，未修改配置")
        result = json.loads(data)
        if not isinstance(result, dict):
            raise ValueError("not an object")
        return result
    except urllib.error.HTTPError as exc:
        code = exc.code
        exc.close()
        if code in {401, 403}:
            raise ConnectError("目标 Agent拒绝访问；请核对目标 Agent agent_token 和防火墙来源限制，未修改配置") from None
        raise ConnectError(f"目标 Agent采集接口返回 HTTP {code}，未修改配置") from None
    except (OSError, urllib.error.URLError, ValueError, UnicodeError):
        raise ConnectError("无法读取目标 Agent采集接口；请检查指定 Agent 端口连通性及 Agent 服务，未修改配置") from None


def protect_private_directory(path: Path):
    if os.name == "nt":
        command = ["icacls.exe", str(path), "/inheritance:r", "/grant:r",
                   "*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F"]
        try:
            result = subprocess.run(command, capture_output=True, timeout=20, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise ConnectError("无法设置私密目录权限，请从管理员 PowerShell 运行") from None
        if result.returncode:
            raise ConnectError("无法设置私密目录权限，请从管理员 PowerShell 运行")
    else:
        path.chmod(0o700)


def check_prometheus(root: Path, candidate: Path):
    binary = root / ("runtime/prometheus/promtool.exe" if os.name == "nt" else "runtime/prometheus/promtool")
    if not binary.is_file() and os.name != "nt" and shutil.which("promtool"):
        binary = Path(shutil.which("promtool"))
    if not binary.is_file():
        raise ConnectError("缺少 runtime/prometheus/promtool.exe；请先完成Hub 服务器的 hub 安装")
    try:
        result = subprocess.run([str(binary), "check", "config", str(candidate)],
                                cwd=root, capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise ConnectError("promtool 配置检查无法执行或超时，未替换活动配置") from None
    if result.returncode:
        # promtool can echo user-configured sensitive values. Do not relay output.
        raise ConnectError("promtool 拒绝候选配置，未替换活动配置；请检查现有 YAML 结构和受控接入区块")


def valid_host_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", value):
        raise ConnectError("服务器 id 需为 1–64 个字母、数字、下划线或连字符，首字符须为字母或数字")
    return value


def render_prometheus(original: str, address: str, token_path: Path, host_id=HOST_ID, port=8767) -> str:
    host_id = valid_host_id(host_id)
    begin, end = f"# BEGIN LABMON MANAGED {host_id}", f"# END LABMON MANAGED {host_id}"
    text = original.replace("\r\n", "\n")
    begins = list(re.finditer(r"(?m)^" + re.escape(begin) + r"\s*$", text))
    ends = list(re.finditer(r"(?m)^" + re.escape(end) + r"\s*$", text))
    if len(begins) != len(ends) or len(begins) > 1:
        raise ConnectError("Prometheus 目标 Agent受控标记不完整或重复，未修改配置")
    if begins:
        if begins[0].start() > ends[0].start():
            raise ConnectError("Prometheus 目标 Agent受控标记顺序错误，未修改配置")
        stop = ends[0].end()
        if stop < len(text) and text[stop] == "\n":
            stop += 1
        text = text[:begins[0].start()] + text[stop:]
    manual_job = re.search(r"(?m)^\s*-\s*job_name\s*:\s*['\"]?" + re.escape(host_id) + r"['\"]?\s*(?:#.*)?$", text)
    if manual_job:
        raise ConnectError("现有 YAML 已包含同名的非脚本管理 job；请先由管理员整理该项")
    sections = list(re.finditer(r"(?m)^scrape_configs\s*:\s*(?:#[^\n]*)?$", text))
    if len(sections) != 1:
        raise ConnectError("只支持具有一个顶层 scrape_configs 块的 Prometheus YAML，未修改配置")
    section = sections[0]
    next_top_level = re.search(r"(?m)^[A-Za-z_][A-Za-z0-9_-]*\s*:", text[section.end():])
    insertion = section.end() + next_top_level.start() if next_top_level else len(text)
    quote = lambda value: json.dumps(value, ensure_ascii=False)
    block = (
        f"{begin}\n"
        f"  - job_name: {host_id}\n"
        "    scheme: http\n"
        "    metrics_path: /metrics\n"
        f"    bearer_token_file: {quote(token_path.as_posix())}\n"
        "    static_configs:\n"
        f"      - targets: [{quote(address + ':' + str(port))}]\n"
        "        labels:\n"
        f"          server: {quote(host_id)}\n"
        f"{end}\n"
    )
    return text[:insertion].rstrip("\n") + "\n" + block + text[insertion:].lstrip("\n")


def prospective_hub(original: dict, address: str, token: str, host_id=HOST_ID, name=None, port=8767, platform="auto") -> dict:
    if original.get("mode") != "hub" or not isinstance(original.get("servers"), list):
        raise ConnectError("指定 root 下的 config/hub.json 不是有效的 hub 配置")
    clone = json.loads(json.dumps(original))
    existing = [server for server in clone["servers"] if server.get("id") == host_id]
    if len(existing) > 1:
        raise ConnectError("hub.json 中目标 id 重复，未修改配置")
    if existing:
        entry = existing[0]
    else:
        entry = {"id": host_id, "name": name or host_id}
        clone["servers"].append(entry)
    entry.update(agent_url=f"http://{address}:{port}", token=token, enabled=True)
    if name:
        entry["name"] = name
    if platform != "auto":
        entry["platform"] = platform
    return clone


def connect_server(root, address, token_file, *, host_id=HOST_ID, name=None, port=8767, platform="auto",
                   with_prometheus=True, fetcher=None, validator=None, protector=None):
    root = Path(root).resolve(strict=True)
    host_id = valid_host_id(host_id)
    if type(port) is not int or not 1 <= port <= 65535:
        raise ConnectError("Agent 端口无效")
    if platform not in {"auto", "windows", "linux"}:
        raise ConnectError("服务器平台无效")
    if name is not None and (not isinstance(name, str) or not name.strip() or len(name) > 100):
        raise ConnectError("服务器名称需为 1–100 个字符")
    address = private_ipv4(address)
    token = read_token(Path(token_file))
    fetcher, validator = fetcher or (lambda a, p, t: fetch_json(a, p, t, port)), validator or check_prometheus
    protector = protector or protect_private_directory
    hub_path, prom_path = root / "config/hub.json", root / "config/prometheus.yml"
    try:
        hub_bytes, prom_bytes = hub_path.read_bytes(), prom_path.read_bytes() if with_prometheus else b""
        original_hub = json.loads(hub_bytes.decode("utf-8-sig"))
        original_prom = prom_bytes.decode("utf-8-sig")
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ConnectError("无法读取Hub 服务器的 hub.json 和 prometheus.yml，未修改配置") from None
    hub = prospective_hub(original_hub, address, token, host_id, name, port, platform)
    if address == original_hub.get("listen_host"):
        raise ConnectError("目标服务器地址不能与当前 hub 的监听地址相同")
    health = fetcher(address, "/healthz", token)
    if health.get("ok") is not True or health.get("mode") != "agent":
        raise ConnectError("目标没有返回有效 Agent 健康状态，未修改配置")
    snapshot = fetcher(address, "/snapshot", token)
    if snapshot.get("host_id") != host_id:
        raise ConnectError("目标 host_id 与 --id 不一致，未修改配置")
    if platform != "auto" and snapshot.get("platform") != platform:
        raise ConnectError("目标采集平台与指定平台不一致，未修改配置")
    if snapshot.get("telemetry_status") not in {"ok", "partial"}:
        raise ConnectError("目标 Agent采集器尚未产生有效数据，请稍后重试，未修改配置")
    if not with_prometheus:
        if hub == original_hub:
            return {"changed": False, "address": address, "backup_dir": None}
        backup = root / "config/backups" / (f"connect-{host_id}-" + uuid.uuid4().hex)
        backup.mkdir(parents=True)
        protector(backup)
        (backup / "hub.json").write_bytes(hub_bytes)
        candidate = backup / "hub.next.json"
        candidate.write_text(json.dumps(hub, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if hub_path.read_bytes() != hub_bytes:
            raise ConnectError("配置在验证时已改变，请重新运行")
        os.replace(candidate, hub_path)
        return {"changed": True, "address": address, "backup_dir": str(backup)}
    secret_dir = root / "config/secrets"
    # Versioned token paths allow validation before touching the currently used
    # token and preserve historical configuration backups during token rotation.
    token_path = secret_dir / (host_id + "-" + hashlib.sha256(token.encode()).hexdigest()[:16] + ".token")
    prometheus = render_prometheus(original_prom, address, token_path, host_id, port)
    new_secret = False
    new_secret_directory = not secret_dir.exists()
    stage = None
    backup = None
    replaced_hub = False
    replaced_prom = False
    try:
        secret_dir.mkdir(parents=True, exist_ok=True)
        protector(secret_dir)
        if token_path.exists():
            if token_path.read_text(encoding="utf-8").strip() != token:
                raise ConnectError("私密令牌文件与预期不一致，未修改配置")
        else:
            # Directory permissions are restricted before writing any credential.
            with token_path.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(token + "\n")
            if os.name != "nt":
                token_path.chmod(0o600)
            new_secret = True
        stage = Path(tempfile.mkdtemp(prefix=f".connect-{host_id}-", dir=root / "config"))
        protector(stage)
        candidate_hub, candidate_prom = stage / "hub.json", stage / "prometheus.yml"
        candidate_hub.write_text(json.dumps(hub, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        candidate_prom.write_text(prometheus, encoding="utf-8", newline="\n")
        validator(root, candidate_prom)
        # Avoid overwriting a configuration changed by an administrator while the
        # remote health checks or promtool validation were running.
        if hub_path.read_bytes() != hub_bytes or prom_path.read_bytes() != prom_bytes:
            raise ConnectError("配置在检查期间被其他操作修改，请重新运行；未替换活动配置")
        changed = hub != original_hub or prometheus != original_prom.replace("\r\n", "\n")
        if not changed:
            return {"changed": False, "address": address, "backup_dir": None}
        backup_name = datetime.now(timezone.utc).strftime(f"connect-{host_id}-%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
        backup = root / "config/backups" / backup_name
        backup.mkdir(parents=True)
        protector(backup)
        (backup / "hub.json").write_bytes(hub_bytes)
        (backup / "prometheus.yml").write_bytes(prom_bytes)
        os.replace(candidate_prom, prom_path)
        replaced_prom = True
        os.replace(candidate_hub, hub_path)
        replaced_hub = True
        return {"changed": True, "address": address, "backup_dir": str(backup)}
    except Exception:
        if replaced_prom and not replaced_hub:
            # Services have not been reloaded, so the previous files are enough
            # to restore a consistent active configuration after a partial swap.
            restore = stage / "prometheus.restore.yml"
            restore.write_bytes(prom_bytes)
            os.replace(restore, prom_path)
        if new_secret and not replaced_hub:
            token_path.unlink(missing_ok=True)
        raise
    finally:
        if stage is not None and stage.exists():
            shutil.rmtree(stage)
        if new_secret_directory and secret_dir.exists() and not any(secret_dir.iterdir()):
            secret_dir.rmdir()


def main():
    parser = argparse.ArgumentParser(description="验证并接入目标服务器 Agent；不自动重启服务")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]), help="Hub 安装目录")
    parser.add_argument("--id", required=True, help="与 Agent host_id 一致的唯一编号")
    parser.add_argument("--name", help="网页显示名称")
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--platform", choices=["auto", "windows", "linux"], default="auto")
    parser.add_argument("--without-history", action="store_true", help="仅接入网页，未安装 Prometheus 时使用")
    parser.add_argument("--address", required=True, help="目标服务器的私网 IPv4")
    parser.add_argument("--token-file", required=True, help="安全转移的目标 Agent private.json 或 agent_token 文本文件")
    args = parser.parse_args()
    try:
        result = connect_server(args.root, args.address, args.token_file, host_id=args.id, name=args.name,
            port=args.port, platform=args.platform, with_prometheus=not args.without_history)
    except ConnectError as exc:
        print("接入未完成：" + str(exc), file=sys.stderr)
        return 1
    except (OSError, ValueError):
        print("接入未完成：文件操作失败，请检查安装目录、管理员权限和备份；令牌不会输出。", file=sys.stderr)
        return 1
    if not result["changed"]:
        print("目标服务器验证通过，配置已经一致，无需重复写入。")
    else:
        print("目标服务器验证通过，所选接入配置已更新；未重启任何服务。")
        print("配置备份：" + result["backup_dir"])
    print("配置已保存；按运维说明重启 Hub。启用历史采集时还需重新加载 Prometheus 配置。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
