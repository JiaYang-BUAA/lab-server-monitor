"""Read-only Linux host telemetry, using the standard library and procfs.

Install on the host, not in an isolated PID/network namespace. See
docs/linux-collector.md for measurement definitions and permission requirements.
"""
from __future__ import annotations

import csv
import ipaddress
import math
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from .collector import classify_process


def _sysconf(name):
    try:
        value = os.sysconf(name)
        return int(value) if value > 0 else None
    except (AttributeError, OSError, ValueError):
        return None


def _uid_name(uid):
    # Importing this module for fixture tests must also work on Windows.
    try:
        import pwd
        return pwd.getpwuid(uid).pw_name
    except (ImportError, KeyError, OSError, OverflowError):
        return str(uid)


def _cpu_ids(value):
    result = set()
    for group in value.strip().split(','):
        ends = group.split('-')
        first, last = int(ends[0]), int(ends[-1])
        if len(ends) > 2 or first < 0 or last < first or last > 1_000_000:
            raise ValueError('invalid CPU list')
        result.update(range(first, last + 1))
    return result


def _process_stat(text):
    """comm can contain spaces and parentheses; split after its final ')'."""
    left, right = text.find('('), text.rfind(')')
    if left < 1 or right <= left:
        raise ValueError('invalid process stat')
    rest = text[right + 1:].split()
    if len(rest) < 22:
        raise ValueError('short process stat')
    result = {'pid': int(text[:left].strip()), 'name': text[left + 1:right],
              'state': rest[0], 'parent_pid': int(rest[1]),
              'cpu_ticks': int(rest[11]) + int(rest[12]),
              'start_ticks': int(rest[19]), 'rss_pages': int(rest[21])}
    if result['pid'] <= 0 or result['cpu_ticks'] < 0 or result['start_ticks'] < 0:
        raise ValueError('invalid process counters')
    return result


def _unescape_mount(value):
    return re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), value)


def _tcp_address(value, family):
    address, port = value.rsplit(':', 1)
    raw = bytes.fromhex(address)
    if len(raw) != (4 if family == 4 else 16):
        raise ValueError('invalid TCP address')
    # procfs prints each native-endian 32-bit word in hexadecimal.
    if sys.byteorder == 'little':
        raw = b''.join(raw[i:i + 4][::-1] for i in range(0, len(raw), 4))
    decoded = ipaddress.ip_address(raw)
    if isinstance(decoded, ipaddress.IPv6Address) and decoded.ipv4_mapped:
        decoded = decoded.ipv4_mapped
    return str(decoded), int(port, 16)


class LinuxCollector:
    def __init__(self, host_id: str, proc_root='/proc', sys_root='/sys', *,
                 clock=time.time, monotonic=time.monotonic, statvfs=None,
                 uid_resolver=None, run_command=None, clock_ticks=None, page_size=None):
        self.host_id = host_id
        self.proc_root, self.sys_root = Path(proc_root), Path(sys_root)
        self.clock, self.monotonic = clock, monotonic
        self.statvfs = statvfs if statvfs is not None else getattr(os, 'statvfs', None)
        self.uid_resolver = uid_resolver or _uid_name
        self.run_command = run_command or subprocess.run
        self.clock_ticks = clock_ticks or _sysconf('SC_CLK_TCK')
        self.page_size = page_size or _sysconf('SC_PAGE_SIZE')
        self._previous = {}
        self._previous_at = None
        self._previous_cpu = None
        self._previous_boot = None
        self._lock = threading.Lock()

    def _read(self, relative, limit=8 * 1024 * 1024):
        with (self.proc_root / relative).open('rb') as stream:
            value = stream.read(limit + 1)
        if len(value) > limit:
            raise ValueError('proc entry too large')
        return value.decode('utf-8', errors='replace')

    def sample(self):
        with self._lock:
            return self._sample()

    def _sample(self):
        now, tick = self.clock(), self.monotonic()
        observed = datetime.fromtimestamp(now, timezone.utc).isoformat()
        warnings = []
        cpu, cpu_counters, boot_time = self._cpu(warnings)
        boot = self._boot_identity(boot_time, warnings)
        if boot != self._previous_boot:
            self._previous, self._previous_at, self._previous_cpu = {}, None, None
        elapsed = tick - self._previous_at if self._previous_at is not None else None
        current = {}

        def rate(key, value):
            current[key] = value
            previous = self._previous.get(key)
            if previous is None or not elapsed or elapsed <= 0 or value < previous:
                return None
            return (value - previous) / elapsed

        if cpu_counters is not None:
            prior = self._previous_cpu
            if prior and prior[0] == cpu_counters[0]:
                changes = [v - p for v, p in zip(cpu_counters[1], prior[1])]
                total = sum(changes)
                if all(change >= 0 for change in changes) and total > 0:
                    cpu['percent'] = round((1 - (changes[3] + changes[4]) / total) * 100, 2)
                else:
                    warnings.append('CPU计数器回退或未变化，等待下一次有效采样。')
            else:
                warnings.append('CPU速率正在预热或在线处理器发生变化，需两个连续有效样本。')
        memory = self._memory(warnings)
        mounts, restricted = self._mounts(warnings)
        disks = self._disks(mounts, rate, warnings)
        network = self._network(rate, warnings)
        processes, process_ok, ssh_pids = self._processes(
            boot, boot_time, cpu['logical_processors'] if cpu_counters else None,
            rate, now, warnings)
        if restricted:
            process_ok = False
            warnings.append('procfs启用了hidepid，无法确认已看见整机所有进程；不会自动判定任务结束。')
        ssh = self._ssh(ssh_pids, process_ok, observed, warnings)
        gpus = self._gpus(warnings)
        self._previous, self._previous_at = current, tick
        self._previous_cpu, self._previous_boot = cpu_counters, boot
        return {
            'host_id': self.host_id, 'platform': 'linux', 'observed_at': observed,
            'telemetry_status': ('error' if cpu_counters is None and memory['total_bytes'] is None
                                 else 'partial' if warnings else 'ok'),
            'process_status': 'ok' if process_ok else 'error',
            'cpu': cpu, 'memory': memory, 'disks': disks, 'network': network,
            'gpus': gpus, 'processes': processes, 'ssh': ssh, 'warnings': warnings,
        }

    def _cpu(self, warnings):
        result = {'percent': None, 'logical_processors': None, 'observed_processors': 0, 'model': None}
        counters, boot_time = None, None
        try:
            rows = {parts[0]: parts[1:] for line in self._read('stat').splitlines() if (parts := line.split())}
            ids = {int(key[3:]) for key in rows if re.fullmatch(r'cpu\d+', key)}
            result['observed_processors'] = len(ids)
            online_path = self.sys_root / 'devices/system/cpu/online'
            try:
                online = _cpu_ids(online_path.read_text(encoding='ascii'))
            except FileNotFoundError:
                online = ids
            result['logical_processors'] = len(online) or None
            if online != ids or not ids:
                warnings.append('CPU在线列表与procfs采样覆盖不一致，占用率未知。')
            else:
                # user/nice already include guest/guest_nice; do not add them again.
                values = tuple(int(v) for v in rows['cpu'][:8])
                if len(values) != 8 or any(v < 0 for v in values):
                    raise ValueError('invalid CPU counters')
                counters = (frozenset(ids), values)
            if 'btime' in rows:
                boot_time = int(rows['btime'][0])
                if boot_time <= 0:
                    boot_time = None
        except (OSError, ValueError, KeyError, IndexError):
            warnings.append('Linux CPU计数器或在线处理器列表不可读，占用率未知。')
        try:
            for line in self._read('cpuinfo').splitlines():
                field, _, value = line.partition(':')
                if field.strip() in ('model name', 'Hardware', 'Processor') and value.strip():
                    result['model'] = value.strip()[:240]
                    break
        except (OSError, ValueError):
            pass
        return result, counters, boot_time

    def _boot_identity(self, boot_time, warnings):
        try:
            value = self._read('sys/kernel/random/boot_id', 128).strip()
            if re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', value):
                return value.lower()
        except (OSError, ValueError):
            pass
        if boot_time is not None:
            return f'btime-{boot_time}'
        warnings.append('系统启动标识不可读，进程身份未知。')
        return None

    def _memory(self, warnings):
        result = {'total_bytes': None, 'available_bytes': None, 'percent': None}
        try:
            values = {}
            for line in self._read('meminfo').splitlines():
                key, _, tail = line.partition(':')
                if key in ('MemTotal', 'MemAvailable'):
                    parts = tail.split()
                    if len(parts) != 2 or parts[1] != 'kB':
                        raise ValueError('invalid memory unit')
                    values[key] = int(parts[0]) * 1024
            total, available = values.get('MemTotal'), values.get('MemAvailable')
            if total is not None and total > 0:
                result['total_bytes'] = total
            if available is not None and available >= 0:
                result['available_bytes'] = available
            if total and available is not None and 0 <= available <= total:
                result['percent'] = round((1 - available / total) * 100, 2)
        except (OSError, ValueError, IndexError):
            pass
        if result['percent'] is None:
            warnings.append('MemTotal/MemAvailable不可用或无效，内存占用未知。')
        return result

    def _mounts(self, warnings):
        mounts, restricted = {}, False
        local_types = {'ext2', 'ext3', 'ext4', 'xfs', 'btrfs', 'zfs', 'f2fs', 'bcachefs',
                       'jfs', 'reiserfs', 'vfat', 'exfat', 'ntfs', 'ntfs3', 'fuseblk', 'udf', 'iso9660'}
        try:
            for line in self._read('self/mountinfo').splitlines():
                before, after = line.split(' - ', 1)
                left, right = before.split(), after.split()
                device, mountpoint, fs_type = left[2], _unescape_mount(left[4]), right[0]
                if fs_type == 'proc':
                    options = (left[5] + ',' + right[2]).split(',')
                    restricted |= any(opt.startswith('hidepid=') and opt != 'hidepid=0' for opt in options)
                if fs_type not in local_types:
                    continue
                # Bind mounts and subvolume views must not repeat one device's capacity.
                prior = mounts.get(device)
                if prior is None or (len(mountpoint), mountpoint) < (len(prior), prior):
                    mounts[device] = mountpoint
        except (OSError, ValueError, IndexError):
            warnings.append('本地挂载列表不完整，磁盘或进程可见范围无法确认。')
            restricted = True
        return mounts, restricted

    def _disks(self, mounts, rate, warnings):
        counters = {}
        try:
            for line in self._read('diskstats').splitlines():
                parts = line.split()
                if len(parts) >= 14:
                    counters[f'{int(parts[0])}:{int(parts[1])}'] = (int(parts[5]) * 512, int(parts[9]) * 512)
        except (OSError, ValueError):
            warnings.append('磁盘I/O计数器不可用，读写速率未知。')
        result, unknown_io = [], False
        for device, mountpoint in sorted(mounts.items(), key=lambda item: item[1]):
            disk = dict(name=mountpoint, total_bytes=None, free_bytes=None, read_bps=None, write_bps=None)
            try:
                if self.statvfs is None:
                    raise OSError('statvfs unavailable')
                stat = self.statvfs(mountpoint)
                block = stat.f_frsize or stat.f_bsize
                total, free = stat.f_blocks * block, stat.f_bavail * block
                if total < 0 or free < 0 or free > total:
                    raise ValueError('invalid disk capacity')
                disk.update(total_bytes=total, free_bytes=free)
            except (OSError, ValueError):
                warnings.append(f'本地文件系统 {mountpoint[:120]} 容量不可读。')
            if device in counters:
                read, write = counters[device]
                if read >= 0 and write >= 0:
                    disk['read_bps'] = rate(('disk_read', device), read)
                    disk['write_bps'] = rate(('disk_write', device), write)
                else:
                    unknown_io = True
            else:
                unknown_io = True
            result.append(disk)
        if unknown_io:
            warnings.append('部分文件系统无法唯一映射到块设备，读写速率未知。')
        return result

    def _network(self, rate, warnings):
        receives, sends = [], []
        try:
            for line in self._read('net/dev').splitlines():
                if ':' not in line:
                    continue
                interface, tail = line.rsplit(':', 1)
                interface = interface.strip()
                if interface == 'lo':
                    continue
                values = tail.split()
                received, sent = int(values[0]), int(values[8])
                if min(received, sent) < 0:
                    raise ValueError('negative network counter')
                receives.append(rate(('net_rx', interface), received))
                sends.append(rate(('net_tx', interface), sent))
            if not receives:
                warnings.append('没有可采样的非回环网络接口，网络速率未知。')
        except (OSError, ValueError, IndexError):
            warnings.append('网络计数器不完整，网络速率未知。')
            receives, sends = [], []
        return {'recv_bps': sum(receives) if receives and all(v is not None for v in receives) else None,
                'sent_bps': sum(sends) if sends and all(v is not None for v in sends) else None}

    def _processes(self, boot, boot_time, logical_count, rate, now, warnings):
        result, ssh_pids = [], []
        owners = {}
        complete = boot is not None and boot_time is not None and self.clock_ticks is not None
        failures = 0
        try:
            pids = sorted(int(path.name) for path in self.proc_root.iterdir() if path.name.isdecimal())
        except OSError:
            pids = []
        if not pids:
            complete = False
            warnings.append('进程目录不可读或为空，进程列表未知。')
        for pid in pids:
            try:
                initial = _process_stat(self._read(f'{pid}/stat', 64 * 1024))
                status = self._read(f'{pid}/status', 128 * 1024)
                uid, memory = None, None
                for line in status.splitlines():
                    if line.startswith('Uid:'):
                        uid = int(line.split()[1])
                    elif line.startswith('VmRSS:'):
                        fields = line.split()
                        if len(fields) == 3 and fields[2] == 'kB':
                            memory = int(fields[1]) * 1024
                name = initial['name']
                try:
                    target = os.readlink(self.proc_root / str(pid) / 'exe')
                    name = PurePosixPath(target.removesuffix(' (deleted)')).name
                except FileNotFoundError:
                    # Kernel threads/zombies legitimately have no executable link.
                    pass
                except OSError:
                    complete = False
                    failures += 1
                final = _process_stat(self._read(f'{pid}/stat', 64 * 1024))
                if initial['pid'] != pid or final['pid'] != pid or initial['start_ticks'] != final['start_ticks']:
                    raise ValueError('PID changed during scan')
                if final['state'] == 'Z':
                    # A zombie has exited and only awaits its parent's wait().
                    # Keeping its key would make the Hub retain a finished job.
                    continue
                start = boot_time + final['start_ticks'] / self.clock_ticks if boot_time and self.clock_ticks else None
                key = f'{pid}:{boot}:{final["start_ticks"]}' if boot else f'{pid}:unknown:{now:.6f}'
                cpu = None
                if self.clock_ticks and boot:
                    cpu_rate = rate(('process_cpu', key, logical_count), final['cpu_ticks'] / self.clock_ticks)
                    if cpu_rate is not None and logical_count:
                        cpu = round(min(100, cpu_rate / logical_count * 100), 3)
                if memory is None and self.page_size and final['rss_pages'] >= 0:
                    memory = final['rss_pages'] * self.page_size
                if uid is not None and uid not in owners:
                    try:
                        owners[uid] = self.uid_resolver(uid) or str(uid)
                    except (KeyError, OSError, OverflowError):
                        # An unmapped/deleted account or NSS outage must not hide
                        # an otherwise readable process from the inventory.
                        owners[uid] = str(uid)
                owner = owners.get(uid)
                software, role = classify_process(name)
                if pid == os.getpid():
                    software, role = 'system', 'service'
                result.append({'key': key, 'pid': pid, 'parent_pid': final['parent_pid'], 'name': name,
                               'owner': owner, 'start_time': start, 'cpu_pct': cpu, 'memory_bytes': memory,
                               'gpu_memory_bytes': None, 'software': software, 'role': role})
                if name in ('sshd', 'sshd-session', 'sshd-auth'):
                    ssh_pids.append((pid, final['start_ticks']))
            except (OSError, ValueError, IndexError):
                # Includes normal exit/PID-reuse races. Never turn an incomplete
                # inventory into proof that somebody's calculation completed.
                complete = False
                failures += 1
        if failures:
            warnings.append(f'{failures}个进程在采样时退出、身份变化或访问受限；本次不会自动判定任务结束。')
        if boot_time is None or self.clock_ticks is None:
            warnings.append('进程启动时间的系统时钟参数不可用，进程身份状态未确认。')
        return sorted(result, key=lambda item: (-(item['cpu_pct'] or 0), item['pid'])), complete, ssh_pids

    def _ssh(self, ssh_pids, process_ok, observed, warnings):
        result = {'tcp_connections': None, 'connections': [], 'count_basis': 'established_tcp', 'observed_at': None}
        if not process_ok:
            warnings.append('进程可见范围不完整，SSH连接数未知。')
            return result
        try:
            rows = []
            for family, filename in ((4, 'net/tcp'), (6, 'net/tcp6')):
                try:
                    content = self._read(filename)
                except FileNotFoundError:
                    if family == 6:  # IPv6 may not be loaded/enabled in this kernel.
                        continue
                    raise
                for line in content.splitlines()[1:]:
                    parts = line.split()
                    if not parts:
                        continue
                    local, local_port = _tcp_address(parts[1], family)
                    remote, remote_port = _tcp_address(parts[2], family)
                    rows.append({'family': family, 'local_address': local, 'local_port': local_port,
                                 'remote_address': remote, 'remote_port': remote_port,
                                 'state': parts[3], 'inode': int(parts[9])})
            inodes = set()
            for pid, start_ticks in ssh_pids:
                for fd in (self.proc_root / str(pid) / 'fd').iterdir():
                    try:
                        target = os.readlink(fd)
                    except FileNotFoundError:
                        continue  # Individual file descriptors can close normally.
                    match = re.fullmatch(r'socket:\[(\d+)\]', target)
                    if match:
                        inodes.add(int(match[1]))
                after = _process_stat(self._read(f'{pid}/stat', 64 * 1024))
                if after['start_ticks'] != start_ticks:
                    raise ValueError('sshd PID changed')
            listeners = [row for row in rows if row['state'] == '0A' and row['inode'] in inodes]
            if ssh_pids and not listeners:
                raise ValueError('no attributable listener')
            connections = {}
            for row in rows:
                if row['state'] != '01':
                    continue
                if any(row['family'] == listener['family'] and row['local_port'] == listener['local_port']
                       and listener['local_address'] in ('0.0.0.0', '::', row['local_address'])
                       for listener in listeners):
                    identity = (row['family'], row['local_address'], row['local_port'], row['remote_address'], row['remote_port'])
                    connections[identity] = {key: row[key] for key in ('remote_address', 'remote_port', 'local_port')}
            result.update(tcp_connections=len(connections), connections=list(connections.values()), observed_at=observed)
        except (OSError, ValueError, IndexError):
            warnings.append('无法完整核验sshd监听socket及TCP连接，SSH连接数未知。')
        return result

    def _gpus(self, warnings):
        query = ['nvidia-smi', '--query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw',
                 '--format=csv,noheader,nounits']
        try:
            response = self.run_command(query, capture_output=True, text=True, timeout=3, check=False)
            if response.returncode != 0 or len(response.stdout) > 1024 * 1024:
                raise ValueError('GPU query failed')
            gpus = []
            for row in csv.reader(response.stdout.splitlines(), skipinitialspace=True):
                if len(row) != 7:
                    raise ValueError('invalid GPU data')
                gpu = {'id': row[0].strip(), 'name': row[1].strip()[:240]}
                for index, field in enumerate(('utilization_pct', 'memory_used_bytes', 'memory_total_bytes', 'temperature_c', 'power_w'), 2):
                    try:
                        value = float(row[index].strip())
                        if not math.isfinite(value) or value < 0:
                            raise ValueError('invalid GPU counter')
                        gpu[field] = value * 1024 * 1024 if 'memory' in field else value
                    except ValueError:
                        gpu[field] = None
                gpus.append(gpu)
            if not gpus:
                warnings.append('nvidia-smi未返回GPU；其他厂商GPU目前不采集。')
            else:
                warnings.append('Linux逐进程GPU显存尚未采集，保持未知。')
            return gpus
        except (OSError, ValueError, subprocess.SubprocessError):
            warnings.append('nvidia-smi不可用、超时或驱动未就绪；GPU信息未知，其他厂商GPU目前不采集。')
            return []
