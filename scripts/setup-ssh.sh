#!/usr/bin/env bash
# Existing local users only. Requires Bash 4+, Python 3, systemd and sudo/root.
# Read docs/ssh-setup.md and run --dry-run before applying.
set -euo pipefail
export LC_ALL=C
usage() {
    printf '%s\n' 'Usage: sudo bash setup-ssh.sh --user USER --public-key FILE --allow-from IP[/PREFIX] [--allow-from IP[/PREFIX] ...] [--dry-run]'
}
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
target_user= key_file= dry_run=false
sources=()
while (($#)); do
    case "$1" in
        --user|--public-key|--allow-from)
            (($# >= 2)) || die "Missing value for $1"
            case "$1" in
                --user) target_user=$2 ;;
                --public-key) key_file=$2 ;;
                --allow-from) sources+=("$2") ;;
            esac
            shift 2 ;;
        --dry-run) dry_run=true; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage; die "Unknown argument: $1" ;;
    esac
done
[[ -n $target_user && -n $key_file && ${#sources[@]} -gt 0 ]] || { usage; exit 2; }
command -v python3 >/dev/null || die 'Python 3 is required for safe input and filesystem validation; install it with your distribution package manager first.'

# Validation and safe openat-based key installation are shared here so paths and
# key contents are rechecked immediately before writing. No key is printed.
key_tool() {
python3 - "$@" <<'PY'
import base64
import ipaddress
import os
import pathlib
import pwd
import re
import stat
import sys
import time

def fail(message):
    raise SystemExit('ERROR: ' + message)

def check_path(path):
    path = pathlib.Path(path)
    if not path.is_absolute():
        fail('User home must be absolute.')
    for part in [*reversed(path.parents), path]:
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            fail('Refusing a symlink or non-directory home component: ' + str(part))
        if info.st_mode & 0o022:
            fail('Home and its parents must not be group/world writable: ' + str(part))

def validate(user, key_file, source_values):
    if not re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_.-]{0,63}', user):
        fail('Use an existing local account name, without domain syntax.')
    try:
        account = pwd.getpwnam(user)
    except KeyError:
        fail('The target user does not exist.')
    # A local account is required; do not change network-directory account homes.
    with open('/etc/passwd', encoding='utf-8') as stream:
        if not any(line.split(':', 1)[0] == user for line in stream):
            fail('Only accounts present in /etc/passwd are supported.')
    if account.pw_uid == 0:
        fail('Choose an existing non-root user; this script does not enable root SSH login.')
    if account.pw_shell.endswith(('/nologin', '/false')) or not account.pw_shell:
        fail('The target account has no interactive login shell.')
    check_path(account.pw_dir)
    if os.stat(account.pw_dir).st_uid != account.pw_uid:
        fail('The home directory must belong to the target user.')
    path = pathlib.Path(key_file)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > 8192:
        fail('The public key must be a regular file, not a symlink, of at most 8192 bytes.')
    with open(path, 'r', encoding='utf-8', errors='strict') as stream:
        lines = [line.rstrip('\r\n') for line in stream if line.strip()]
    if len(lines) != 1:
        fail('Provide exactly one public key.')
    match = re.fullmatch(r'ssh-ed25519 ([A-Za-z0-9+/]+={0,2})(?: [^\x00-\x1f\x7f]*)?', lines[0])
    if not match:
        fail('Only one ssh-ed25519 public key is accepted, without authorized_keys options.')
    try:
        blob = base64.b64decode(match[1], validate=True)
    except ValueError:
        fail('Invalid public-key base64.')
    if (len(blob) != 51 or not blob.startswith(b'\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20')
            or base64.b64encode(blob).decode('ascii') != match[1]):
        fail('Invalid Ed25519 public-key packet.')
    normalized = []
    for source in source_values:
        if '%' in source:
            fail('Scoped IPv6 addresses are not supported; use a routable source address.')
        try:
            interface = ipaddress.ip_interface(source)
        except ValueError:
            fail('Invalid source IP/CIDR: ' + source)
        if (interface.network.prefixlen == 0 or interface.ip.is_unspecified
                or interface.ip.is_multicast or getattr(interface.ip, 'ipv4_mapped', None)):
            fail('Use a specific unicast IP/CIDR; unspecified, multicast, mapped IPv6 and /0 are not accepted.')
        normalized.append(str(interface.network))
    # Existing key paths must be regular and single-link; never follow a symlink.
    for suffix, is_directory in [('.ssh', True), ('.ssh/authorized_keys', False)]:
        path = pathlib.Path(account.pw_dir, suffix)
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        expected = stat.S_ISDIR if is_directory else stat.S_ISREG
        if not expected(info.st_mode) or (not is_directory and info.st_nlink != 1):
            fail('Refusing symlink, hardlink or unexpected key path: ' + str(path))
        if info.st_uid not in (0, account.pw_uid):
            fail('Key path is owned by an unrelated user: ' + str(path))
    return account, lines[0], sorted(set(normalized))

def install(account, key):
    flags = os.O_DIRECTORY | os.O_NOFOLLOW | os.O_RDONLY
    home_fd = os.open(account.pw_dir, flags)
    try:
        try:
            os.mkdir('.ssh', 0o700, dir_fd=home_fd)
        except FileExistsError:
            pass
        directory_fd = os.open('.ssh', flags, dir_fd=home_fd)
        try:
            before_dir = os.fstat(directory_fd)
            if before_dir.st_uid not in (0, account.pw_uid):
                fail('SSH directory ownership changed during setup.')
            print('Previous .ssh owner/mode: %d:%d %04o' % (before_dir.st_uid, before_dir.st_gid, stat.S_IMODE(before_dir.st_mode)))
            os.fchown(directory_fd, account.pw_uid, account.pw_gid)
            os.fchmod(directory_fd, 0o700)
            fd = os.open('authorized_keys', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_APPEND, 0o600, dir_fd=directory_fd)
            try:
                before = os.fstat(fd)
                if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_uid not in (0, account.pw_uid):
                    fail('Unsafe authorized_keys file; no key appended.')
                if before.st_size > 16 * 1024 * 1024:
                    fail('authorized_keys exceeds the 16 MiB safety limit; inspect manually.')
                existing = b''
                while True:
                    chunk = os.read(fd, 65536)
                    if not chunk:
                        break
                    existing += chunk
                if existing:
                    backup_name = 'authorized_keys.labmon-before-' + str(time.time_ns())
                    backup_fd = os.open(backup_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
                    with os.fdopen(backup_fd, 'wb') as backup:
                        backup.write(existing)
                        os.fchown(backup.fileno(), account.pw_uid, account.pw_gid)
                        backup.flush()
                        os.fsync(backup.fileno())
                    print('Previous authorized keys: ' + os.path.join(account.pw_dir, '.ssh', backup_name))
                print('Previous authorized_keys owner/mode: %d:%d %04o' % (before.st_uid, before.st_gid, stat.S_IMODE(before.st_mode)))
                blob = key.split(' ')[1].encode('ascii')
                pattern = rb'(?:^|[ \t])ssh-ed25519[ \t]+' + re.escape(blob) + rb'(?:[ \t\r\n]|$)'
                if re.search(pattern, existing, re.MULTILINE):
                    print('The same public key already exists; existing key options were preserved.')
                else:
                    addition = (b'\n' if existing and not existing.endswith(b'\n') else b'') + key.encode('utf-8') + b'\n'
                    remaining = memoryview(addition)
                    while remaining:
                        remaining = remaining[os.write(fd, remaining):]
                    print('Added one public key; all existing keys were preserved.')
                os.fchown(fd, account.pw_uid, account.pw_gid)
                os.fchmod(fd, 0o600)
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            os.close(directory_fd)
    finally:
        os.close(home_fd)

if __name__ == '__main__':
    try:
        mode, user, key_file, *sources = sys.argv[1:]
        account, key, normalized = validate(user, key_file, sources)
        if mode == 'validate':
            print(account.pw_dir)
            print('\n'.join(normalized))
        elif mode == 'install':
            if os.geteuid() != 0:
                fail('Key installation requires root.')
            install(account, key)
        else:
            fail('Invalid internal mode.')
    except (OSError, UnicodeError) as exc:
        fail(str(exc))
PY
}

validated=$(key_tool validate "$target_user" "$key_file" "${sources[@]}")
mapfile -t values <<< "$validated"
user_home=${values[0]}
sources=("${values[@]:1}")
[[ -d /run/systemd/system ]] || die 'Only systemd-managed Debian/Ubuntu or dnf-based Linux hosts are supported; containers/non-systemd need manual SSH setup.'
command -v systemctl >/dev/null || die 'systemctl is required.'
package_manager=
if command -v apt-get >/dev/null; then package_manager=apt
elif command -v dnf >/dev/null; then package_manager=dnf
else die 'Only apt (Debian/Ubuntu) or dnf distributions are supported.'
fi
sshd_bin=/usr/sbin/sshd
service_name=ssh.service
[[ $package_manager == dnf ]] && service_name=sshd.service
ports=(22)
configured_host_keys=()
inspect_sshd() {
    [[ -x $sshd_bin && -f /etc/ssh/sshd_config ]] || return 0
    # Do not silently use the wrong config when an administrator customized the service.
    local unit source effective authorized arg_file
    unit=$(systemctl show "$service_name" --property=ExecStart --value)
    [[ ! $unit =~ [[:space:]]-[fpo] ]] || die 'Custom sshd service options are configured; preserve them and perform SSH setup manually.'
    for arg_file in /etc/default/ssh /etc/sysconfig/sshd; do
        if [[ -f $arg_file ]] && python3 - "$arg_file" <<'PY'
import re, sys
for line in open(sys.argv[1], encoding='utf-8'):
    match = re.match(r'\s*(?:export\s+)?(?:SSHD_OPTS|OPTIONS)\s*=\s*(.*?)\s*$', line)
    if match and match[1] not in ('', '""', "''"):
        raise SystemExit(0)
raise SystemExit(1)
PY
        then die "Nonempty SSH daemon options in $arg_file require manual setup."; fi
    done
    for source in "${sources[@]}"; do
        effective=$("$sshd_bin" -T -f /etc/ssh/sshd_config -C "user=$target_user,host=$(hostname),addr=${source%/*}")
        authorized=$(awk '$1 == "authorizedkeysfile" {$1=""; print}' <<< "$effective")
        [[ " $authorized " == *' .ssh/authorized_keys '* ]] || die 'Custom AuthorizedKeysFile is in use; this script only supports .ssh/authorized_keys.'
        ! grep -qx 'pubkeyauthentication no' <<< "$effective" || die 'Public-key login is disabled by existing policy; review it manually.'
    done
    mapfile -t ports < <(awk '$1 == "port" {print $2}' <<< "$effective" | sort -nu)
    mapfile -t configured_host_keys < <(sed -n 's/^hostkey //p' <<< "$effective")
    ((${#ports[@]})) || die 'sshd reported no ports.'
    if systemctl is-active --quiet ssh.socket; then
        local socket_listeners socket_ports
        socket_listeners=$(systemctl show ssh.socket --property=Listen --value)
        socket_ports=$(python3 - "$socket_listeners" <<'PY'
import re, sys
ports = sorted({int(value) for value in re.findall(r':(\d+)\s+\(Stream\)', sys.argv[1])})
if not ports or any(not 1 <= value <= 65535 for value in ports):
    raise SystemExit('Cannot inspect active ssh.socket ports; configure the firewall manually.')
print('\n'.join(map(str, ports)))
PY
        )
        printf 'Existing ssh.socket owns the actual listener: %s ; sshd configured ports: %s\n' "$socket_listeners" "${ports[*]}" >&2
        mapfile -t ports <<< "$socket_ports"
    fi
}
inspect_sshd
printf 'Target: %s ; existing local user: %s ; home: %s\n' "$(hostname)" "$target_user" "$user_home"
printf 'Source networks: %s\n' "${sources[*]}"
printf 'Configured ports: %s (a missing server will be checked after installation)\n' "${ports[*]}"
printf '%s\n' 'Plan: official system package if missing, append key if absent, set .ssh 0700 / authorized_keys 0600, add scoped rules to an already-active supported firewall, validate sshd, start only if stopped.'
printf '%s\n' 'Existing authentication policy/ports are preserved. Other firewall allow rules may still allow other sources.'
if $dry_run; then printf '%s\n' 'DRY RUN: no changes made.'; exit 0; fi
(( EUID == 0 )) || die 'Run with sudo/root; no changes were made.'

if [[ ! -x $sshd_bin ]]; then
    if [[ $package_manager == apt ]]; then
        # Debian packages can auto-start daemons. Temporarily deny package service
        # starts, without replacing any administrator-owned policy-rc.d.
        policy=/usr/sbin/policy-rc.d
        [[ ! -e $policy && ! -L $policy ]] || die 'A policy-rc.d already exists. Install openssh-server under your existing package policy, then rerun; it will not be overwritten.'
        python3 - "$policy" <<'PY'
import os, sys
fd = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o755)
with os.fdopen(fd, 'w') as stream:
    stream.write('#!/bin/sh\n# temporary Lab Monitor SSH package-start guard\nexit 101\n')
PY
        cleanup_policy() {
            python3 - "$policy" <<'PY'
import os, stat, sys
path = sys.argv[1]
try:
    info = os.lstat(path)
    if stat.S_ISREG(info.st_mode) and open(path).read() == '#!/bin/sh\n# temporary Lab Monitor SSH package-start guard\nexit 101\n':
        os.unlink(path)
except FileNotFoundError:
    pass
PY
        }
        trap cleanup_policy EXIT
        DEBIAN_FRONTEND=noninteractive apt-get update
        DEBIAN_FRONTEND=noninteractive apt-get install -y openssh-server
        cleanup_policy
        trap - EXIT
    else
        dnf install -y openssh-server
    fi
fi
[[ -x $sshd_bin ]] || die 'openssh-server installation did not provide /usr/sbin/sshd.'
command -v ssh-keygen >/dev/null || die 'ssh-keygen is missing after package installation.'
ssh-keygen -A
# sshd -t requires its runtime privilege-separation directory on Debian.
if [[ ! -d /run/sshd ]]; then install -d -o root -g root -m 0755 /run/sshd; fi
"$sshd_bin" -t -f /etc/ssh/sshd_config
inspect_sshd
key_tool install "$target_user" "$key_file" "${sources[@]}"
if command -v restorecon >/dev/null; then restorecon -R "$user_home/.ssh"; fi

firewall_configured=false
if command -v ufw >/dev/null && ufw status | grep -q '^Status: active'; then
    for source in "${sources[@]}"; do
        for port in "${ports[@]}"; do
            ufw allow from "$source" to any port "$port" proto tcp comment 'lab-monitor-ssh'
        done
    done
    firewall_configured=true
    printf '%s\n' 'Added scoped UFW rules tagged lab-monitor-ssh; previous rules remain unchanged.'
elif command -v firewall-cmd >/dev/null && firewall-cmd --state >/dev/null 2>&1; then
    zone=$(firewall-cmd --get-default-zone)
    for source in "${sources[@]}"; do
        family=ipv4; [[ $source == *:* ]] && family=ipv6
        for port in "${ports[@]}"; do
            rule="rule family=\"$family\" source address=\"$source\" port port=\"$port\" protocol=\"tcp\" accept"
            # Do not reload the whole firewall or discard unrelated runtime rules.
            if ! firewall-cmd --zone="$zone" --query-rich-rule="$rule" >/dev/null; then
                firewall-cmd --zone="$zone" --add-rich-rule="$rule"
                printf 'Added runtime firewalld rule in zone %s: %s\n' "$zone" "$rule"
            fi
            if ! firewall-cmd --permanent --zone="$zone" --query-rich-rule="$rule" >/dev/null; then
                firewall-cmd --permanent --zone="$zone" --add-rich-rule="$rule"
                printf 'Added permanent firewalld rule in zone %s: %s\n' "$zone" "$rule"
            fi
        done
    done
    firewall_configured=true
    printf 'firewalld rules added to default zone %s only; verify the receiving interface belongs to this zone.\n' "$zone"
else
    printf '%s\n' 'WARNING: No active supported firewall (UFW/firewalld). NO FIREWALL WAS CONFIGURED. Existing nftables/iptables/cloud rules must be handled separately. No firewall was enabled or flushed.' >&2
fi
printf 'Previous service enabled state: %s\n' "$(systemctl is-enabled "$service_name" 2>/dev/null || true)"
if systemctl is-active --quiet ssh.socket; then
    printf '%s\n' 'Existing ssh.socket activation is preserved. No service/socket restart is necessary for authorized_keys.'
    printf 'Socket listeners: %s\n' "$(systemctl show ssh.socket --property=Listen --value)"
elif systemctl is-active --quiet "$service_name"; then
    printf '%s\n' 'Existing SSH service is running. Only authorized_keys changed; no reload/restart is necessary.'
else
    systemctl enable "$service_name"
    systemctl start "$service_name"
fi
printf 'Host: %s ; configured SSH ports: %s\n' "$(hostname)" "${ports[*]}"
printf '%s\n' 'Actual listening sockets (check the SSH port; a listener is not a verified client login):'
if command -v ss >/dev/null; then
    ss -ltnp | awk 'NR == 1 || /sshd|systemd/'
else
    printf '%s\n' 'ss is unavailable; actual listener inspection was not performed.'
fi
printf '%s\n' 'Host Ed25519 fingerprint (compare on your client before trusting this host):'
fingerprint_found=false
for host_key in "${configured_host_keys[@]}"; do
    if [[ -f $host_key.pub ]]; then
        fingerprint=$(ssh-keygen -l -E sha256 -f "$host_key.pub")
        if [[ $fingerprint == *'(ED25519)' ]]; then
            printf '%s\n' "$fingerprint"
            fingerprint_found=true
        fi
    fi
done
$fingerprint_found || printf '%s\n' 'WARNING: No public Ed25519 host key from the active configuration could be inspected. Verify the configured host-key fingerprint manually.' >&2
printf 'Connection example: ssh -p %s %s@<server-address>\n' "${ports[0]}" "$target_user"
printf '%s\n' 'Test a NEW client login before closing existing sessions. NAT, routing, security groups and other allow rules are not changed.'
$firewall_configured || printf '%s\n' 'Setup finished with FIREWALL NOT CONFIGURED; review network access before relying on source restrictions.'
