"""Render isolated, repeatable Windows service configuration (standard library)."""
import argparse
from datetime import datetime, timezone
import ipaddress
import json
from pathlib import Path
import secrets
import shutil
import re
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from labmon.dashboard import resource_dashboard


def write(path, text, preserve=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if preserve or path.read_text(encoding="utf-8") == text:
            return
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(3)
        shutil.copy2(path, path.with_name(path.name + ".before-" + stamp))
    path.write_text(text, encoding="utf-8")


def read_existing(path, parser):
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(value, dict):
            raise ValueError("not an object")
        return value
    except (OSError, ValueError):
        parser.error(f"Invalid existing {path.name}; configuration was not changed")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--root", required=True)
    p.add_argument("--role", choices=["hub", "agent"], default="hub")
    p.add_argument("--address", required=True)
    p.add_argument("--host-id", required=True)
    p.add_argument("--host-name")
    p.add_argument("--hub-address")
    args = p.parse_args()
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", args.host_id):
        p.error("Invalid host-id")
    if not Path(args.root).is_absolute():
        p.error("root must be an absolute project directory")
    for value, name in ((args.address, "address"), (args.hub_address, "hub-address")):
        if value is None:
            continue
        try:
            address = ipaddress.IPv4Address(value)
        except ValueError:
            p.error(f"{name} must be an IPv4 address")
        if address.is_unspecified or address.is_multicast or value == "255.255.255.255":
            p.error(f"{name} must be a specific unicast IPv4 address")
    if args.role == "agent" and not args.hub_address:
        p.error("agent role requires hub-address")
    if args.host_name is not None and (not args.host_name.strip() or len(args.host_name) > 80):
        p.error("host-name must contain 1-80 characters")
    root = Path(args.root).resolve()
    private = root / "config/private.json"
    agent_path = root / "config/agent.json"
    hub_path = root / "config/hub.json"
    credentials = read_existing(private, p)
    old_agent = read_existing(agent_path, p)
    old_hub = read_existing(hub_path, p)
    if (old_agent is not None or old_hub is not None) and credentials is None:
        p.error("Existing configuration has no private.json; credentials were not regenerated")
    if credentials is not None:
        for key in ("agent_token", "grafana_admin_password"):
            if not isinstance(credentials.get(key), str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,256}", credentials[key]):
                p.error("Existing private credentials are invalid; they were not replaced")
    else:
        credentials = {"agent_token": secrets.token_urlsafe(40), "grafana_admin_password": secrets.token_urlsafe(32)}
    listen_host = "127.0.0.1" if args.role == "hub" else args.address
    if old_agent is not None:
        expected = {"mode": "agent", "host_id": args.host_id, "listen_host": listen_host,
                    "token": credentials["agent_token"], "port": 8767}
        if any(old_agent.get(key) != value for key, value in expected.items()) or old_agent.get("platform", "windows") != "windows":
            p.error("Existing Agent identity/address/credentials differs; configuration was not changed")
    if old_hub is not None:
        if args.role != "hub" or old_hub.get("mode") != "hub" or old_hub.get("listen_host") != args.address or old_hub.get("port") != 8766:
            p.error("Existing Hub role/address differs; configuration was not changed")
        old_servers = old_hub.get("servers")
        if not isinstance(old_servers, list):
            p.error("Existing Hub servers must be a list; configuration was not changed")
        local = [item for item in old_servers if isinstance(item, dict) and item.get("id") == args.host_id]
        if len(local) != 1 or local[0].get("token") != credentials["agent_token"] or local[0].get("agent_url") != "http://127.0.0.1:8767":
            p.error("Existing Hub local server identity/credentials differs; configuration was not changed")
    if not (root / "downloads/WinSW-x64.exe").is_file():
        p.error("Missing downloaded WinSW wrapper; configuration was not changed")
    # All identity and address checks happen before the first directory/file mutation.
    for d in ["config", "services", "logs", "data/textfile", "data/agent", "data/hub", "data/prometheus", "data/grafana", "data/plugins", "data/temp"]:
        (root / d).mkdir(parents=True, exist_ok=True)
    write(private, json.dumps(credentials, indent=2), preserve=True)
    python_exe = root / "runtime/python/python.exe"
    write(root / "runtime/python/python313._pth", f"python313.zip\n.\n{root}\nimport site\n")
    agent = {"mode": "agent", "platform": "windows", "metrics_format": "unified", "listen_host": "127.0.0.1" if args.role == "hub" else args.address,
             "port": 8767, "data_dir": str(root / "data/agent"), "host_id": args.host_id,
             "token": credentials["agent_token"], "exporter_url": "http://127.0.0.1:9182/metrics",
             "sidecar_path": str(root / "data/sidecar.json"), "allowed_log_roots": [],
             "poll_seconds": 5, "stale_seconds": 30}
    if old_agent is None:
        write(agent_path, json.dumps(agent, indent=2, ensure_ascii=False))
    exporter_args = ('--web.listen-address=127.0.0.1:9182 '
                     '--collectors.enabled=cpu,cpu_info,os,memory,logical_disk,net,process,gpu,textfile '
                     '--no-collector.process.cmdline '
                     f'--collector.textfile.directories="{root / "data/textfile"}"')
    services = [
        ("LabMonExporter", root / "runtime/exporter/windows_exporter.exe", exporter_args, []),
        ("LabMonSidecar", Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"),
         f'-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{root / "scripts/collect-windows.ps1"}" '
         f'-OutputPath "{root / "data/sidecar.json"}" -TextfilePath "{root / "data/textfile/labmon.prom"}" -IntervalSeconds 10', []),
        ("LabMonAgent", python_exe, f'-u -m labmon --config "{root / "config/agent.json"}"', ["LabMonExporter"]),
    ]
    if args.role == "hub":
        if not hub_path.exists():
            hub = {"mode": "hub", "listen_host": args.address, "port": 8766,
                   "data_dir": str(root / "data/hub"), "web_dir": str(root / "web"),
                   "grafana_url": "/grafana/", "grafana_upstream": "http://127.0.0.1:3000",
                   "poll_seconds": 5, "stale_seconds": 30,
                   "servers": [{"id": args.host_id, "name": args.host_name or args.host_id, "platform": "windows", "agent_url": "http://127.0.0.1:8767", "token": credentials["agent_token"], "enabled": True}]}
            write(hub_path, json.dumps(hub, indent=2, ensure_ascii=False))
        prom_config = root / "config/prometheus.yml"
        if not prom_config.exists():
            token_file = root / "config/secrets" / (args.host_id + ".token")
            write(token_file, credentials["agent_token"] + "\n")
            write(prom_config, 'global:\n  scrape_interval: 10s\n  scrape_timeout: 8s\nscrape_configs:\n'
                  f'  - job_name: "{args.host_id}"\n    bearer_token_file: {json.dumps(token_file.as_posix())}\n'
                  '    static_configs:\n      - targets: ["127.0.0.1:8767"]\n'
                  f'        labels:\n          server: "{args.host_id}"\n')
        provision = root / "config/grafana-provisioning"
        for folder in ["alerting", "plugins", "access-control"]:
            (provision / folder).mkdir(parents=True, exist_ok=True)
        write(provision / "datasources/prometheus.yml", 'apiVersion: 1\ndatasources:\n  - name: Lab Prometheus\n    uid: lab-prometheus\n'
              '    type: prometheus\n    access: proxy\n    url: http://127.0.0.1:9090\n    isDefault: true\n    editable: false\n'
              '    jsonData:\n      timeInterval: 10s\n      httpMethod: POST\n', preserve=True)
        dashboard_dir = root / "config/grafana-dashboards"
        write(provision / "dashboards/lab.yml", 'apiVersion: 1\nproviders:\n  - name: Lab\n    orgId: 1\n    folder: Laboratory\n'
              '    type: file\n    disableDeletion: true\n    editable: false\n    updateIntervalSeconds: 30\n'
              f'    options:\n      path: "{dashboard_dir.as_posix()}"\n', preserve=True)
        write(dashboard_dir / "resources.json", json.dumps(resource_dashboard(f"http://{args.address}:8766/"), indent=2, ensure_ascii=False), preserve=True)
        grafana_ini = f'''[paths]
data = {root / 'data/grafana'}
logs = {root / 'logs/grafana'}
plugins = {root / 'data/plugins'}
provisioning = {provision}
[server]
http_addr = 127.0.0.1
http_port = 3000
domain = {args.address}
root_url = http://{args.address}:8766/grafana/
serve_from_sub_path = true
[analytics]
reporting_enabled = false
check_for_updates = false
check_for_plugin_updates = false
[auth]
disable_login_form = true
[auth.anonymous]
enabled = true
org_role = Viewer
[users]
allow_sign_up = false
viewers_can_edit = false
[security]
admin_user = labadmin
admin_password = {credentials['grafana_admin_password']}
cookie_samesite = lax
[dashboards]
default_home_dashboard_path = {dashboard_dir / 'resources.json'}
[live]
max_connections = 0
[plugins]
preinstall_disabled = true
preinstall_auto_update = false
plugin_admin_enabled = false
[log]
mode = console
level = warn
'''
        write(root / "config/grafana.ini", grafana_ini, preserve=True)
        services.extend([
            ("LabMonPrometheus", root / "runtime/prometheus/prometheus.exe",
             f'--config.file="{prom_config}" --storage.tsdb.path="{root / "data/prometheus"}" '
             '--storage.tsdb.retention.time=14d --storage.tsdb.retention.size=2GB --web.listen-address=127.0.0.1:9090', ["LabMonExporter"]),
            ("LabMonGrafana", root / "runtime/grafana/bin/grafana.exe",
             f'server --homepath "{root / "runtime/grafana"}" --config "{root / "config/grafana.ini"}"', ["LabMonPrometheus"]),
            ("LabMonWeb", python_exe, f'-u -m labmon --config "{hub_path}"', ["LabMonAgent"]),
        ])
    for name, executable, arguments, dependencies in services:
        xml = ET.Element("service")
        for key, value in [("id", name), ("name", name), ("description", "Laboratory read-only telemetry and task registration"),
                           ("executable", str(executable)), ("arguments", arguments), ("workingdirectory", str(root)),
                           ("startmode", "Automatic"), ("delayedAutoStart", "true"), ("stoptimeout", "20sec"),
                           ("logpath", str(root / "logs"))]:
            ET.SubElement(xml, key).text = value
        for dep in dependencies:
            ET.SubElement(xml, "depend").text = dep
        ET.SubElement(xml, "env", name="PYTHONIOENCODING", value="utf-8")
        ET.SubElement(xml, "env", name="TEMP", value=str(root / "data/temp"))
        ET.SubElement(xml, "env", name="TMP", value=str(root / "data/temp"))
        ET.SubElement(xml, "onfailure", action="restart", delay="15sec")
        log = ET.SubElement(xml, "log", mode="roll-by-size")
        ET.SubElement(log, "sizeThreshold").text = "10240"
        ET.SubElement(log, "keepFiles").text = "3"
        ET.indent(xml)
        write(root / f"services/{name}.xml", ET.tostring(xml, encoding="unicode"))
        wrapper = root / f"services/{name}.exe"
        if not wrapper.exists():
            shutil.copy2(root / "downloads/WinSW-x64.exe", wrapper)
    write(root / "config/service-list.json", json.dumps([x[0] for x in services]))
    print(json.dumps({"configured": True, "role": args.role, "services": [x[0] for x in services]}))


if __name__ == "__main__":
    main()
