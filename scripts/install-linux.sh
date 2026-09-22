#!/usr/bin/env bash
# Install from an unpacked source release. This script never changes SSH or reboots.
set -euo pipefail
umask 077

usage() {
  cat <<'EOF'
Usage:
  sudo bash scripts/install-linux.sh --role hub --address 192.168.1.10 \
    --host-id compute-01 --allow-from 192.168.1.0/24
  sudo bash scripts/install-linux.sh --role agent --address 192.168.1.11 \
    --host-id compute-02 --hub-address 192.168.1.10
Options:
  --root PATH           Dedicated directory (default /opt/lab-server-monitor; no spaces)
  --source PATH         Unpacked release directory (default parent of this script)
  --name NAME           Display name of the local server (hub role)
  --allow-log-root PATH  Explicit readable log directory; repeat as needed (default none)
  --python PATH         Python >=3.10 (default python3 from PATH)
  --dry-run             Validate parameters/configuration without writing or starting
  --prepare-only        Copy files and prepare configuration, without users/firewall/services
Existing configuration, tokens and task registrations are preserved. Changed source and
service files are backed up before replacement. Basic installation excludes Prometheus/Grafana.
EOF
}
fail() { printf 'Installation stopped: %s\n' "$*" >&2; exit 2; }
root=/opt/lab-server-monitor
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
python_bin=$(command -v python3 || true)
role= address= host_id= hub_address= allow_from= name=
dry_run=0 prepare_only=0
log_roots=()
while (($#)); do
  case "$1" in
    --help|-h) usage; exit 0 ;;
    --dry-run) dry_run=1; shift ;;
    --prepare-only) prepare_only=1; shift ;;
    --root|--source|--python|--role|--address|--host-id|--hub-address|--allow-from|--name|--allow-log-root)
      (($# >= 2)) || fail "Missing value for $1"
      case "$1" in
        --root) root=$2 ;; --source) source_dir=$2 ;; --python) python_bin=$2 ;;
        --role) role=$2 ;; --address) address=$2 ;; --host-id) host_id=$2 ;;
        --hub-address) hub_address=$2 ;; --allow-from) allow_from=$2 ;; --name) name=$2 ;;
        --allow-log-root) log_roots+=("$2") ;;
      esac
      shift 2 ;;
    *) fail "Unknown option: $1" ;;
  esac
done
[[ -n "$python_bin" && -x "$python_bin" ]] || fail 'Python 3.10+ is required; install it first.'
[[ -n "$role" && -n "$address" && -n "$host_id" ]] || fail '--role, --address and --host-id are required.'
[[ -d "$source_dir" ]] || fail 'Source directory does not exist.'
source_dir=$(cd -- "$source_dir" && pwd -P)
[[ -f "$source_dir/scripts/configure-linux.py" && ! -L "$source_dir/scripts/configure-linux.py" ]] || fail 'Unpacked source release is incomplete.'
# BEGIN hub temporary-path preflight: PrivateTmp hides these paths at runtime.
# Preparing inspectable artifacts in a temporary directory is still allowed.
if [[ "$role" == hub ]] && ((!prepare_only)); then
  "$python_bin" - "$root" "$python_bin" <<'PY'
from pathlib import Path, PurePosixPath
import sys
for label, value in zip(('--root', '--python'), sys.argv[1:]):
    for candidate in (PurePosixPath(value), PurePosixPath(Path(value).resolve().as_posix())):
        for hidden in (PurePosixPath('/tmp'), PurePosixPath('/var/tmp')):
            if candidate == hidden or hidden in candidate.parents:
                sys.exit(label + ' is hidden by Hub PrivateTmp; use a persistent /opt or /srv installation and system Python. --prepare-only may generate temporary artifacts, but cannot install a service there.')
PY
fi
# END hub temporary-path preflight
config_args=(--root "$root" --role "$role" --address "$address" --host-id "$host_id" --python "$python_bin")
[[ -z "$hub_address" ]] || config_args+=(--hub-address "$hub_address")
[[ -z "$allow_from" ]] || config_args+=(--allow-from "$allow_from")
[[ -z "$name" ]] || config_args+=(--name "$name")
for path in "${log_roots[@]}"; do config_args+=(--allow-log-root "$path"); done
# Includes existing-config conflict checks; all happen before directories are created.
"$python_bin" "$source_dir/scripts/configure-linux.py" "${config_args[@]}" --dry-run
if ((dry_run)); then
  printf 'Dry run complete. No files, users, firewall rules or services were changed.\n'
  exit 0
fi
root=$("$python_bin" -c 'import pathlib, sys; print(pathlib.Path(sys.argv[1]).resolve())' "$root")
config_args[1]=$root
[[ $(uname -s) == Linux ]] || fail 'Use configure-linux.py --prepare-only to inspect artifacts on non-Linux systems.'
[[ $EUID == 0 ]] || fail 'Run with sudo; the agent needs system-wide process and SSH visibility.'

files=(web/index.html web/app.js web/styles.css scripts/configure-linux.py scripts/install-linux.sh scripts/connect-server.py)
while IFS= read -r -d '' path; do files+=("${path#"$source_dir/"}"); done < <(find "$source_dir/labmon" -maxdepth 1 -type f -name '*.py' -print0)
for path in README.md LICENSE docs/linux-install.md scripts/setup-ssh.sh; do
  [[ ! -f "$source_dir/$path" ]] || files+=("$path")
done
[[ -f "$source_dir/labmon/__main__.py" && -f "$source_dir/labmon/server.py" ]] || fail 'Source package has no labmon application.'
for relative in "${files[@]}"; do
  [[ -f "$source_dir/$relative" && ! -L "$source_dir/$relative" ]] || fail "Missing or linked source: $relative"
done
"$python_bin" - "$root" "$source_dir" "${files[@]}" <<'PY'
from pathlib import Path
import sys
root, source = Path(sys.argv[1]), Path(sys.argv[2])
for relative in sys.argv[3:]:
    origin = source / relative
    if any(part.is_symlink() for part in (origin, *origin.parents)):
        sys.exit('Source files must not traverse symlinks')
    target = root / relative
    if any(part.is_symlink() for part in (target, *target.parents)):
        sys.exit('Destination source paths must not traverse symlinks')
    if target.exists() and not target.is_file():
        sys.exit('Destination source file conflicts with an existing directory')
PY

units=(labmon-agent.service)
[[ "$role" != hub ]] || units+=(labmon-web.service)
if ((!prepare_only)); then
  command -v systemctl >/dev/null || fail 'systemd is required.'
  [[ -d /run/systemd/system ]] || fail 'systemd is not running as the service manager.'
  systemd_version=$(systemctl --version | head -n 1)
  systemd_version=${systemd_version#systemd }
  systemd_version=${systemd_version%% *}
  [[ "$systemd_version" =~ ^[0-9]+$ ]] && ((systemd_version >= 247)) || fail 'systemd >=247 is required for private Hub credentials.'
  for unit in "${units[@]}"; do
    path="/etc/systemd/system/$unit"
    [[ ! -L "$path" ]] || fail "Existing $unit is a symlink; review it first."
    if [[ -e "$path" ]] && ! grep -Fxq "WorkingDirectory=$root" "$path"; then
      fail "Existing $unit belongs to another installation; it was not changed."
    fi
  done
  "$python_bin" - "$address" <<'PY'
import socket, sys
try:
    with socket.socket() as probe:
        probe.bind((sys.argv[1], 0))
except OSError:
    sys.exit('--address is not available on this machine; no installation changes were made')
PY
  if [[ "$role" == hub ]]; then
    command -v getent >/dev/null && command -v useradd >/dev/null || fail 'getent and useradd are required.'
    if getent passwd labmon >/dev/null; then
      IFS=: read -r _ _ service_uid _ _ service_home service_shell < <(getent passwd labmon)
      [[ "$service_uid" != 0 && "$service_home" == "$root/data/hub" && "$service_shell" == /usr/sbin/nologin && $(id -gn labmon) == labmon ]] || fail 'An unrelated labmon account exists; choose another machine or resolve the account manually.'
    elif getent group labmon >/dev/null; then
      fail 'A labmon group already exists without its service account; review it first.'
    fi
  fi
fi

install -d -m 0755 -- "$root"
install -d -m 0700 -- "$root/backups"
stamp=$(date -u +%Y%m%dT%H%M%S)-$$
backup="$root/backups/$stamp-install"
for relative in "${files[@]}"; do
  target="$root/$relative"
  if [[ -f "$target" ]] && cmp -s -- "$source_dir/$relative" "$target"; then continue; fi
  if [[ -f "$target" ]]; then
    install -d -m 0700 -- "$backup/$(dirname -- "$relative")"
    cp -p -- "$target" "$backup/$relative"
  fi
  install -D -m 0644 -- "$source_dir/$relative" "$target"
done
"$python_bin" "$root/scripts/configure-linux.py" "${config_args[@]}" --prepare-only
if ((prepare_only)); then
  printf 'Prepared %s. No service account, firewall or systemd actions were taken.\n' "$root"
  exit 0
fi

if [[ "$role" == hub ]]; then
  if ! getent passwd labmon >/dev/null; then
    useradd --system --user-group --home-dir "$root/data/hub" --no-create-home --shell /usr/sbin/nologin labmon
  fi
  chown labmon:labmon -- "$root/data/hub"
  chmod 0700 -- "$root/data/hub"
fi
for unit in "${units[@]}"; do
  target="/etc/systemd/system/$unit"
  if [[ -f "$target" ]] && ! cmp -s -- "$root/services/$unit" "$target"; then
    install -d -m 0700 -- "$backup/systemd"
    cp -p -- "$target" "$backup/systemd/$unit"
  fi
  install -m 0644 -- "$root/services/$unit" "$target"
done

firewall_status=unconfigured
if command -v ufw >/dev/null && LC_ALL=C ufw status | grep -Fxq 'Status: active'; then
  if [[ "$role" == agent ]]; then
    ufw allow proto tcp from "$hub_address" to "$address" port 8767 comment 'Lab Monitor Agent'
  else
    ufw allow proto tcp from "$allow_from" to "$address" port 8766 comment 'Lab Monitor Web'
  fi
  firewall_status='requested UFW rule added; existing rules preserved'
else
  printf 'FIREWALL NOT CONFIGURED: UFW is missing or inactive. No firewall was enabled, disabled or flushed.\n' >&2
  printf 'Before sharing, configure your existing firewall for only the source supplied to this installer.\n' >&2
fi

systemctl daemon-reload
systemctl enable "${units[@]}"
# Stop the dependent Web unit before restarting its required Agent.
if [[ "$role" == hub ]]; then systemctl stop labmon-web.service; fi
systemctl restart labmon-agent.service
if [[ "$role" == hub ]]; then systemctl start labmon-web.service; fi
"$python_bin" - "$root" "$role" "$address" <<'PY'
import json, sys, time
from pathlib import Path
from urllib.request import Request, build_opener, ProxyHandler
root, role, address = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
token = json.loads((root/'config/private.json').read_text())['agent_token']
opener = build_opener(ProxyHandler({}))
agent_address = '127.0.0.1' if role == 'hub' else address
checks = [(f'http://{agent_address}:8767/healthz', {'Authorization': 'Bearer '+token}, 'agent')]
if role == 'hub':
    checks.append((f'http://{address}:8766/healthz', {}, 'hub'))
for url, headers, expected_mode in checks:
    for attempt in range(12):
        try:
            with opener.open(Request(url, headers=headers), timeout=2) as response:
                data = json.load(response)
            if data.get('ok') is not True or data.get('mode') != expected_mode:
                raise ValueError('health check')
            break
        except Exception:
            if attempt == 11:
                sys.exit('Health check failed. Inspect systemctl status / journalctl for labmon-agent and labmon-web; configuration and backups were retained.')
            time.sleep(1)
print('Local service health checks passed. Remote reachability still depends on firewall/network policy.')
PY
printf 'Installation ready: %s\nFirewall: %s\n' "$root" "$firewall_status"
[[ ! -d "$backup" ]] || printf 'Backup of replaced files: %s\n' "$backup"
if [[ "$role" == hub ]]; then
  printf 'Dashboard: http://%s:8766/\n' "$address"
else
  printf 'Connect this agent from the hub using host ID %s and its protected config/private.json file.\n' "$host_id"
fi
