"""Prepare Linux configuration and systemd units without installing or starting services.

Only the standard library is used. Validation runs before the first filesystem write.
Existing configuration and credentials are preserved; conflicting install parameters fail.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import sys


class ConfigurationError(ValueError):
    """An installation error safe to show without exposing credentials."""


PRIVATE_NETWORKS = tuple(ipaddress.ip_network(x) for x in
                         ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


def ipv4(value, label):
    try:
        address = ipaddress.IPv4Address(value)
    except (ValueError, TypeError):
        raise ConfigurationError(f"{label} must be a private IPv4 address") from None
    if not any(address in net for net in PRIVATE_NETWORKS) and not address.is_loopback:
        raise ConfigurationError(f"{label} must be RFC1918 or loopback IPv4")
    return str(address)


def cidr(value):
    try:
        network = ipaddress.IPv4Network(value, strict=True)
    except (ValueError, TypeError):
        raise ConfigurationError("--allow-from must be an IPv4 network CIDR, e.g. 192.168.1.0/24") from None
    if not any(network.subnet_of(net) for net in PRIVATE_NETWORKS) and not network.is_loopback:
        raise ConfigurationError("--allow-from must be a private or loopback subnet")
    return str(network)


def guard_path(path):
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ConfigurationError("Installation paths must not traverse symlinks")
    if path.exists() and not path.is_file() and not path.is_dir():
        raise ConfigurationError("Installation paths must be regular files or directories")


def guard_hub_home_path(value, label):
    """ProtectHome=true hides these paths even when POSIX modes allow access."""
    path = PurePosixPath(str(value))
    for hidden in (PurePosixPath("/home"), PurePosixPath("/root"), PurePosixPath("/run/user")):
        if path == hidden or hidden in path.parents:
            raise ConfigurationError(f"{label} is hidden by the Hub service ProtectHome policy; use /opt, /srv or a system Python executable")


def read_json(path):
    guard_path(path)
    if not path.exists():
        return None
    try:
        if not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
            raise ValueError("invalid file")
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("not an object")
        return data
    except (OSError, UnicodeError, ValueError):
        raise ConfigurationError(f"Cannot safely read existing {path.name}; no configuration was changed") from None


def unit_text(root, executable, role):
    web = role == "hub"
    name = "Web" if web else "Agent"
    unit = ["[Unit]", f"Description=Lab Monitor {name}", "Wants=network-online.target",
            "After=network-online.target" + (" labmon-agent.service" if web else "")]
    if web:
        unit.append("Requires=labmon-agent.service")
    unit += ["", "[Service]", "Type=simple", "User=labmon" if web else "User=root",
             "Group=labmon" if web else "Group=root", f"WorkingDirectory={root}",
             "Environment=PYTHONIOENCODING=utf-8", "Environment=PYTHONDONTWRITEBYTECODE=1",
             "UMask=0077"]
    if web:
        unit += [f"LoadCredential=hub-config:{root}/config/hub.json",
                 f"ExecStart={executable} -u -m labmon --config %d/hub-config"]
    else:
        unit += [f"ExecStart={executable} -u -m labmon --config {root}/config/agent.json"]
    unit += ["Restart=on-failure", "RestartSec=5", "TimeoutStopSec=20",
             "NoNewPrivileges=true", "ProtectSystem=strict",
             f"ReadWritePaths={root}/data/{'hub' if web else 'agent'}"]
    if web:
        unit += ["ProtectHome=true", "PrivateTmp=true"]
    unit += ["", "[Install]", "WantedBy=multi-user.target", ""]
    return "\n".join(unit)


def plan(args):
    if sys.version_info < (3, 10):
        raise ConfigurationError("Python 3.10 or newer is required")
    raw = str(args.root)
    if args.role == "hub":
        guard_hub_home_path(raw, "--root")
        guard_hub_home_path(args.python, "--python")
        if os.name != "nt":
            guard_hub_home_path(Path(args.python).resolve(), "--python target")
    root = Path(raw)
    if not root.is_absolute() or len(root.parts) < 3:
        raise ConfigurationError("--root must be an absolute dedicated installation directory")
    if re.search(r"\s|[%$\"'`;]", raw) or ".." in root.parts or (os.name != "nt" and "\\" in raw):
        raise ConfigurationError("--root must not contain spaces, traversal, or systemd special characters")
    guard_path(root)
    if root.exists() and not root.is_dir():
        raise ConfigurationError("--root must be a directory")
    root = root.resolve()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", args.host_id):
        raise ConfigurationError("--host-id must be 1-64 letters, digits, underscores or hyphens")
    if args.name is not None and (not args.name.strip() or len(args.name) > 80):
        raise ConfigurationError("--name must contain 1-80 characters")
    address = ipv4(args.address, "--address")
    hub_address = ipv4(args.hub_address, "--hub-address") if args.hub_address else None
    if args.role == "agent" and not hub_address:
        raise ConfigurationError("--hub-address is required for the agent role")
    if args.role == "agent" and ipaddress.ip_address(address).is_loopback:
        raise ConfigurationError("An agent installed for another hub needs a private non-loopback --address")
    allowed = cidr(args.allow_from) if args.allow_from else None
    if args.role == "hub" and not allowed:
        raise ConfigurationError("--allow-from is required for the hub role")
    if args.role == "agent" and args.allow_from:
        raise ConfigurationError("Use --hub-address, not --allow-from, for an agent")
    if args.role == "hub" and args.hub_address:
        raise ConfigurationError("--hub-address is only used for the agent role")
    if not re.fullmatch(r"/[A-Za-z0-9_./+-]+", args.python) or ".." in PurePosixPath(args.python).parts:
        raise ConfigurationError("--python must be a safe absolute Linux executable path")
    log_roots = args.allow_log_root or []
    for value in log_roots:
        path = PurePosixPath(value)
        if not path.is_absolute() or str(path) == "/" or ".." in path.parts or "\x00" in value or "\n" in value:
            raise ConfigurationError("Each --allow-log-root must be an explicit absolute Linux directory other than /")
    for directory in ("config", "services", "data", "data/agent", "data/hub", "backups"):
        target = root / directory
        guard_path(target)
        if target.exists() and not target.is_dir():
            raise ConfigurationError(f"{directory} must be a directory")

    private = read_json(root / "config/private.json")
    agent = read_json(root / "config/agent.json")
    hub = read_json(root / "config/hub.json")
    previous = read_json(root / "config/linux-install.json")
    manifest = {"role": args.role, "host_id": args.host_id, "address": address,
                "hub_address": hub_address, "allow_from": allowed}
    if previous is not None and previous != manifest:
        raise ConfigurationError("Existing role, host ID, address or firewall source differs; migrate explicitly instead of reinstalling")
    if args.role == "agent" and hub is not None:
        raise ConfigurationError("Existing hub configuration cannot be changed into an agent installation")
    token = private.get("agent_token") if private is not None else agent.get("token") if agent else secrets.token_urlsafe(40)
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,256}", token):
        raise ConfigurationError("Existing agent credentials are invalid; no credentials were replaced")
    agent_defaults = {"mode": "agent", "listen_host": "127.0.0.1" if args.role == "hub" else address,
                      "port": 8767, "platform": "linux", "metrics_format": "unified",
                      "host_id": args.host_id, "token": token, "data_dir": str(root / "data/agent"),
                      "allowed_log_roots": log_roots, "poll_seconds": 5, "stale_seconds": 30}
    if agent is not None:
        for key in ("mode", "listen_host", "port", "platform", "metrics_format", "host_id", "token", "data_dir"):
            if agent.get(key) != agent_defaults[key]:
                raise ConfigurationError(f"Existing agent {key} conflicts with the requested installation")
        if args.allow_log_root is not None and agent.get("allowed_log_roots", []) != log_roots:
            raise ConfigurationError("Existing allowed log directories differ; edit the private agent configuration explicitly")
    hub_defaults = {"mode": "hub", "listen_host": address, "port": 8766,
                    "data_dir": str(root / "data/hub"), "web_dir": str(root / "web"),
                    "grafana_url": None, "poll_seconds": 5, "stale_seconds": 30,
                    "servers": [{"id": args.host_id, "name": args.name or args.host_id,
                                 "platform": "linux", "agent_url": "http://127.0.0.1:8767",
                                 "token": token, "enabled": True}]}
    if hub is not None:
        for key in ("mode", "listen_host", "port", "data_dir", "web_dir"):
            if hub.get(key) != hub_defaults[key]:
                raise ConfigurationError(f"Existing hub {key} conflicts with the requested installation")
        servers = hub.get("servers")
        if not isinstance(servers, list) or not all(isinstance(s, dict) for s in servers):
            raise ConfigurationError("Existing hub servers must be a list")
        local = [s for s in servers if s.get("id") == args.host_id]
        if len(local) != 1 or any(local[0].get(k) != hub_defaults["servers"][0][k] for k in ("agent_url", "token")):
            raise ConfigurationError("Existing hub local server identity or credentials conflict")

    documents = {"config/private.json": private or {"agent_token": token},
                 "config/agent.json": agent or agent_defaults, "config/linux-install.json": manifest}
    if args.role == "hub":
        documents["config/hub.json"] = hub or hub_defaults
    files = {name: json.dumps(value, indent=2, ensure_ascii=False) + "\n" for name, value in documents.items()}
    posix_root = root.as_posix()
    files["services/labmon-agent.service"] = unit_text(posix_root, args.python, "agent")
    if args.role == "hub":
        files["services/labmon-web.service"] = unit_text(posix_root, args.python, "hub")
    for name in files:
        target = root / name
        guard_path(target)
        if target.exists() and not target.is_file():
            raise ConfigurationError(f"{name} must be a regular file")
    return root, files


def secure_directory(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def generate(args):
    root, files = plan(args)
    services = [Path(name).name for name in files if name.startswith("services/")]
    summary = {"role": args.role, "host_id": args.host_id, "root": str(root),
               "services": services, "dry_run": args.dry_run, "service_actions": False}
    if args.dry_run:
        return summary
    root.mkdir(mode=0o755, parents=True, exist_ok=True)
    for relative in ("config", "services", "data/agent", "data/hub", "backups"):
        secure_directory(root / relative)
    # The web service must traverse data/ to reach its own private directory.
    (root / "data").chmod(0o755)
    backup = None
    for name, text in files.items():
        target = root / name
        if target.exists():
            target.chmod(0o600)
            if name.startswith("config/") or target.read_text(encoding="utf-8") == text:
                continue
            if backup is None:
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(3)
                backup = root / "backups" / (stamp + "-units")
                secure_directory(backup)
            saved = backup / target.name
            shutil.copy2(target, saved)
            saved.chmod(0o600)
        # Private parent permissions are in place before creating even temporary files.
        temp = target.with_name(target.name + ".tmp-" + secrets.token_hex(5))
        try:
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(text)
            os.replace(temp, target)
            target.chmod(0o600)
        finally:
            if temp.exists():
                temp.unlink()
    summary["prepared"] = True
    return summary


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--root", required=True)
    result.add_argument("--role", choices=("hub", "agent"), required=True)
    result.add_argument("--address", required=True)
    result.add_argument("--host-id", required=True)
    result.add_argument("--name")
    result.add_argument("--hub-address")
    result.add_argument("--allow-from")
    result.add_argument("--allow-log-root", action="append", default=None)
    result.add_argument("--python", default="/usr/bin/python3")
    result.add_argument("--dry-run", action="store_true", help="Validate only; do not write files")
    result.add_argument("--prepare-only", action="store_true", help="Generate artifacts only (this script never starts services)")
    return result


def main():
    cli = parser()
    args = cli.parse_args()
    try:
        print(json.dumps(generate(args), ensure_ascii=False))
    except (ConfigurationError, OSError) as error:
        # OS error filenames are useful; configuration contents and tokens are never printed.
        cli.exit(2, f"Linux configuration not completed: {error}\n")


if __name__ == "__main__":
    main()
