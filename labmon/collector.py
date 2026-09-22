"""Read-only windows_exporter collector; rates always require two observations."""
from __future__ import annotations

import json
import math
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

_SAMPLE = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{((?:[^"}]|"(?:\\.|[^"\\])*")*)\})?\s+(\S+)')
_LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:\\.|[^"\\])*)"')


def parse_metrics(text: str) -> dict[str, list[tuple[dict[str, str], float]]]:
    """Parse Prometheus text, preserving Unicode and ignoring non-finite samples."""
    result: dict[str, list[tuple[dict[str, str], float]]] = {}
    for line in text.splitlines():
        if not line or line.startswith('#'):
            continue
        match = _SAMPLE.match(line)
        if not match:
            continue
        try:
            value = float(match[3])
        except ValueError:
            continue
        if not math.isfinite(value):
            continue
        labels = {key: re.sub(r'\\([\\"n])', lambda m: '\n' if m[1] == 'n' else m[1], val)
                  for key, val in _LABEL.findall(match[2] or '')}
        result.setdefault(match[1], []).append((labels, value))
    return result


def classify_process(name: str) -> tuple[str, str]:
    """Classify executable names conservatively, never command lines."""
    name = re.sub(r'#\d+$', '', name.lower().rsplit('\\', 1)[-1])
    name = re.sub(r'\.exe$', '', name)
    if re.search(r'(?:license|licens|lmgrd|lmutil|abaquslm|ansyslmd|ansysli_|dsls|flexnet|fluent[-_]mcp|mcp[-_]server)', name):
        return 'system', 'service'
    if re.fullmatch(r'(?:fluent(?:[._-]\d+(?:\.\d+)*)?|fluent_mpi|fl_mpi\d*)', name):
        return 'fluent', 'solver'
    if name in ('cortex', 'fluentlauncher'):
        return 'fluent', 'launcher'
    if name in ('standard', 'explicit', 'explicit_dp', 'abqstandard', 'abqexplicit'):
        return 'abaqus', 'solver'
    if re.fullmatch(r'(?:abaqus|abq\d{4}(?:se)?|abqcae[kg]?|smapython)', name):
        return 'abaqus', 'application'
    if name in ('comsolbatch', 'comsol'):
        return 'comsol', 'solver'
    if name in ('comsolmphserver', 'comsolserver'):
        return 'system', 'service'
    if name in ('comsolgui', 'comsolui', 'comsolmphclient'):
        return 'comsol', 'application'
    if re.fullmatch(r'(?:python(?:w|\d+(?:\.\d+)*)?|matlab|julia|rscript|rterm|octave|lmp|lammps)', name):
        return 'general', 'general'
    return 'system', 'system'


def _number(value, *, nonnegative=True):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and (not nonnegative or result >= 0) else None


def _utc_timestamp(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed.timestamp() if parsed.tzinfo else None
    except (TypeError, ValueError, OverflowError):
        return None


class Collector:
    def __init__(self, exporter_url: str, sidecar_path: str, host_id: str):
        self.exporter_url = exporter_url
        self.sidecar_path = Path(sidecar_path)
        self.host_id = host_id
        self._previous: dict[tuple, float] = {}
        self._previous_at: float | None = None
        self._rejected_identities: dict[str, str] = {}
        self._lock = threading.Lock()

    def _fetch(self):
        request = Request(self.exporter_url, headers={'Accept': 'text/plain', 'User-Agent': 'LabMonitor/1'})
        with urlopen(request, timeout=5) as response:
            data = response.read(32 * 1024 * 1024 + 1)
        if len(data) > 32 * 1024 * 1024:
            raise ValueError('metrics payload too large')
        return parse_metrics(data.decode('utf-8'))

    def _sidecar(self, now, warnings):
        try:
            with self.sidecar_path.open('r', encoding='utf-8-sig') as stream:
                data = json.load(stream)
            stamp = _utc_timestamp(data.get('observed_at'))
            if stamp is None or not -5 <= now - stamp <= 30:
                warnings.append('Windows辅助采样已过期或时间无效，SSH/GPU等数据未知。')
                return {}
            for warning in data.get('warnings', []):
                if isinstance(warning, str):
                    warnings.append(warning[:300])
            return data
        except (OSError, ValueError, TypeError, AttributeError):
            warnings.append('Windows辅助采样不可用，SSH/GPU等数据未知。')
            return {}

    def sample(self) -> dict:
        with self._lock:
            return self._sample()

    def _sample(self):
        now = time.time()
        observed = datetime.fromtimestamp(now, timezone.utc).isoformat()
        warnings: list[str] = []
        sidecar = self._sidecar(now, warnings)
        exporter_ok = True
        try:
            metrics = self._fetch()
            if not any(name.startswith('windows_') for name in metrics):
                raise ValueError('no Windows metrics')
        except Exception:
            # Do not expose URLs, credentials, command labels, or raw exceptions.
            metrics, exporter_ok = {}, False
            warnings.append('windows_exporter采样失败，硬件与进程数据未知。')
        sampled_at = time.monotonic()
        delta = sampled_at - self._previous_at if self._previous_at is not None else None
        current: dict[tuple, float] = {}

        def scalar(*names):
            for name in names:
                rows = metrics.get(name, [])
                if rows:
                    return _number(rows[0][1])
            return None

        def rate(key, value):
            value = _number(value)
            if value is None:
                return None
            current[key] = value
            prior = self._previous.get(key)
            if prior is None or not delta or delta <= 0 or value < prior:
                return None
            return (value - prior) / delta

        for labels, value in metrics.get('windows_exporter_collector_success', []):
            if value != 1:
                warnings.append('exporter子采集器失败：' + labels.get('collector', 'unknown')[:80])

        cpu_meta = sidecar.get('cpu') or {}
        actual_count = _number(cpu_meta.get('logical_processors'))
        exporter_count = scalar('windows_cpu_logical_processor', 'windows_cs_logical_processors')
        count = actual_count or exporter_count
        logical_count = int(count) if count and count.is_integer() else None
        idle = {labels.get('core'): value for labels, value in metrics.get('windows_cpu_time_total', [])
                if labels.get('mode') == 'idle' and labels.get('core') and '_total' not in labels.get('core', '').lower()}
        idle_rates = [rate(('cpu_idle', core), value) for core, value in idle.items()]
        observed_count = len(idle)
        coverage_ok = logical_count is not None and observed_count == logical_count
        if actual_count and exporter_count and actual_count != exporter_count:
            warnings.append(f'CPU核数核验不一致：CIM={int(actual_count)}，exporter={int(exporter_count)}。')
        if not coverage_ok:
            warnings.append(f'CPU采集覆盖不足或未知：已采集{observed_count}，整机逻辑处理器{logical_count or "未知"}。')
        cpu_percent = None
        if coverage_ok and all(value is not None for value in idle_rates):
            average_idle = sum(idle_rates) / logical_count
            if 0 <= average_idle <= 1.05:
                cpu_percent = round(max(0, min(100, (1 - average_idle) * 100)), 2)
            else:
                warnings.append('CPU计数器变化异常，等待下一次有效采样。')
        elif coverage_ok:
            warnings.append('CPU速率正在预热或计数器已重置，需两个连续有效样本。')

        total = scalar('windows_memory_physical_total_bytes', 'windows_cs_physical_memory_bytes')
        available = scalar('windows_memory_available_bytes', 'windows_os_physical_memory_free_bytes')
        memory_percent = round((1 - available / total) * 100, 2) if total and available is not None and available <= total else None
        if memory_percent is None:
            warnings.append('内存指标不完整，内存占用未知。')

        disks = {}
        for metric, field in [('windows_logical_disk_size_bytes', 'total_bytes'),
                              ('windows_logical_disk_free_bytes', 'free_bytes'),
                              ('windows_logical_disk_read_bytes_total', 'read_bps'),
                              ('windows_logical_disk_write_bytes_total', 'write_bps')]:
            for labels, value in metrics.get(metric, []):
                volume = labels.get('volume')
                if not volume or '_total' in volume.lower():
                    continue
                disk = disks.setdefault(volume, dict(name=volume, total_bytes=None, free_bytes=None, read_bps=None, write_bps=None))
                disk[field] = rate((metric, volume), value) if field.endswith('_bps') else _number(value)
        network = {}
        for metric, field in [('windows_net_bytes_received_total', 'recv_bps'), ('windows_net_bytes_sent_total', 'sent_bps')]:
            rates = [rate((metric, labels.get('nic', '')), value) for labels, value in metrics.get(metric, [])]
            network[field] = sum(rates) if rates and all(value is not None for value in rates) else None

        processes = self._processes(metrics, sidecar, logical_count if coverage_ok else None, now, rate, warnings)
        process_ok = bool(metrics.get('windows_process_info')) and bool(metrics.get('windows_process_start_time_seconds_timestamp'))
        # A stale/missing identity inventory must not turn existing claimed jobs
        # into falsely completed jobs merely because they temporarily lack keys.
        process_ok = process_ok and bool(sidecar.get('process_identities'))
        if any(item['start_time'] is None and item['software'] != 'system' for item in processes):
            process_ok = False
        if any(labels.get('collector') == 'process' and value != 1
               for labels, value in metrics.get('windows_exporter_collector_success', [])):
            process_ok = False
        ssh_source = sidecar.get('ssh') or {}
        ssh_count = _number(ssh_source.get('tcp_connections'))
        connections = []
        if ssh_count is not None:
            for item in ssh_source.get('connections', []):
                if isinstance(item, dict):
                    connection = {key: item.get(key) for key in ('remote_address', 'remote_port', 'local_port', 'created_at')}
                    connections.append(connection)
        if ssh_count is None and sidecar:
            warnings.append('SSH连接采样不可用，连接数未知。')
        gpus = []
        for gpu in sidecar.get('gpus') or []:
            if not isinstance(gpu, dict):
                continue
            item = {'id': str(gpu.get('id', 'unknown')), 'name': str(gpu.get('name') or '未知GPU')}
            for field in ('utilization_pct', 'memory_used_bytes', 'memory_total_bytes', 'temperature_c', 'power_w'):
                item[field] = _number(gpu.get(field))
            item['driver_model'] = gpu.get('driver_model')
            gpus.append(item)
        if exporter_ok:
            self._previous, self._previous_at = current, sampled_at
        else:
            self._previous, self._previous_at = {}, None
        return {
            'host_id': self.host_id, 'observed_at': observed,
            'telemetry_status': ('partial' if warnings else 'ok') if exporter_ok else 'error',
            'platform': 'windows',
            'process_status': 'ok' if exporter_ok and process_ok else 'error',
            'cpu': {'percent': cpu_percent, 'logical_processors': logical_count, 'observed_processors': observed_count,
                    'model': str(cpu_meta['model']).strip() if cpu_meta.get('model') else None},
            'memory': {'total_bytes': total, 'available_bytes': available, 'percent': memory_percent},
            'disks': sorted(disks.values(), key=lambda item: item['name']), 'network': network,
            'gpus': gpus, 'processes': processes,
            'ssh': {'tcp_connections': int(ssh_count) if ssh_count is not None else None, 'connections': connections,
                    'count_basis': 'established_tcp', 'observed_at': ssh_source.get('observed_at') if sidecar else None},
            'warnings': warnings,
        }

    def _processes(self, metrics, sidecar, logical_count, now, rate, warnings):
        records = {}
        wanted = {'windows_process_info', 'windows_process_start_time_seconds_timestamp',
                  'windows_process_cpu_time_total', 'windows_process_working_set_private_bytes',
                  'windows_process_working_set_bytes', 'windows_process_private_bytes'}
        for metric in wanted:
            for labels, value in metrics.get(metric, []):
                try:
                    pid = int(labels.get('process_id', '-1'))
                except ValueError:
                    continue
                name = labels.get('process', '')
                if pid <= 0 or name.lower() in ('idle', '_total'):
                    continue
                record = records.setdefault(pid, {'pid': pid, 'name': name, 'cpu_modes': {}})
                if metric == 'windows_process_info':
                    record['owner'] = labels.get('owner') or None
                    try:
                        record['parent_pid'] = int(labels['creating_process_id'])
                    except (KeyError, ValueError):
                        record['parent_pid'] = None
                elif metric == 'windows_process_start_time_seconds_timestamp':
                    record['start_time'] = _number(value) if value > 0 else None
                elif metric == 'windows_process_cpu_time_total':
                    if labels.get('mode') in ('user', 'privileged'):
                        record['cpu_modes'][labels['mode']] = value
                else:
                    record[metric] = _number(value)
        overrides = {int(item['pid']): item for item in sidecar.get('process_overrides', [])
                     if isinstance(item, dict) and str(item.get('pid', '')).isdigit()}
        identities = {int(item['pid']): item for item in sidecar.get('process_identities', [])
                      if isinstance(item, dict) and str(item.get('pid', '')).isdigit()}
        identity_observed_at = str(sidecar.get('observed_at', ''))
        self._rejected_identities = {key: stamp for key, stamp in self._rejected_identities.items()
                                     if stamp == identity_observed_at}
        results = []
        unknown_starts = 0
        for pid, record in records.items():
            reported_start = record.get('start_time')
            identity = identities.get(pid, {})
            exact_start = _number(identity.get('start_time'))
            expected_name = re.sub(r'\.exe$', '', str(identity.get('name', '')).lower())
            reported_name = re.sub(r'\.exe$', '', re.sub(r'#\d+$', '', record['name'].lower()))
            # Exporter reconstructs creation time from coarse elapsed counters;
            # it jitters by approximately one second every scrape. CIM provides
            # the stable creation timestamp, never a PID-only identity.
            start = exact_start if (exact_start and reported_start and expected_name == reported_name
                                    and abs(exact_start - reported_start) < 2) else None
            key = f'{pid}:{start:.6f}' if start is not None else f'{pid}:unknown:{now:.6f}'
            previous_cpu = self._previous.get(('process_cpu', key))
            process_cpu = sum(record['cpu_modes'].values()) if len(record['cpu_modes']) == 2 else None
            if previous_cpu is not None and process_cpu is not None and process_cpu < previous_cpu:
                # A reused PID can precede the next sidecar refresh; do not
                # attribute its new CPU counter to the prior creation identity.
                self._rejected_identities[key] = identity_observed_at
            if key in self._rejected_identities:
                start, key = None, f'{pid}:unknown:{now:.6f}'
            if start is None:
                unknown_starts += 1
            software, role = classify_process(record['name'])
            override = overrides.get(pid)
            override_start = _number(override.get('start_time')) if override else None
            if override_start and reported_start and abs(override_start - reported_start) < 2 and override.get('role') == 'service':
                software, role = 'system', 'service'
            cpu = None
            if len(record['cpu_modes']) == 2 and start is not None:
                cpu_rate = rate(('process_cpu', key), process_cpu)
                if cpu_rate is not None and logical_count:
                    cpu = round(min(100, cpu_rate / logical_count * 100), 3)
            memory = next((record[field] for field in ('windows_process_working_set_private_bytes',
                          'windows_process_working_set_bytes', 'windows_process_private_bytes')
                          if record.get(field) is not None), None)
            results.append({'key': key, 'pid': pid, 'parent_pid': record.get('parent_pid'), 'name': record['name'],
                            'owner': record.get('owner'), 'start_time': start, 'cpu_pct': cpu,
                            'memory_bytes': memory, 'gpu_memory_bytes': None, 'software': software, 'role': role})
        if unknown_starts:
            warnings.append(f'{unknown_starts}个进程的创建时间尚未确认，身份及 CPU 速率保持未知；部分受保护系统进程可能无法读取。')
        if not records:
            warnings.append('进程指标不可用，进程列表未知。')
        return sorted(results, key=lambda item: (-(item['cpu_pct'] or 0), item['pid']))
