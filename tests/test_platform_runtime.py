"""Cross-platform runtime contracts: real Linux fixture -> Agent -> Hub/metrics."""
import copy
import http.client
import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from labmon.collector import parse_metrics
from labmon.linux_collector import LinuxCollector
from labmon.server import AgentRuntime, HubRuntime, MonitorHTTPServer, group_jobs
from tests.test_linux_collector import process_stat


def full_snapshot():
    return {
        'host_id': 'linux-node', 'platform': 'linux',
        'observed_at': '2033-05-18T03:33:20+00:00',
        'telemetry_status': 'partial', 'process_status': 'ok',
        'cpu': {'percent': 25, 'logical_processors': 8, 'observed_processors': 8, 'model': 'Example'},
        'memory': {'total_bytes': 8192, 'available_bytes': 2048, 'percent': 75},
        'disks': [{'name': '/data/"quoted"\nline', 'total_bytes': 1000, 'free_bytes': 500,
                   'read_bps': None, 'write_bps': 0}],
        'network': {'recv_bps': 200, 'sent_bps': 50},
        'gpus': [], 'processes': [],
        'ssh': {'tcp_connections': None, 'connections': [], 'count_basis': 'established_tcp', 'observed_at': None},
        'warnings': ['Optional GPU unavailable'],
    }


class PlatformSelectionTests(unittest.TestCase):
    def test_auto_selects_linux_without_windows_configuration(self):
        for platform in ('linux', 'linux2'):
            with self.subTest(platform=platform), patch('labmon.server.sys.platform', platform), \
                 patch('labmon.linux_collector.LinuxCollector') as factory:
                runtime = AgentRuntime({'host_id': 'node'})
                factory.assert_called_once_with('node')
                self.assertIs(runtime.collector, factory.return_value)

    def test_auto_selects_windows_with_legacy_exporter_configuration(self):
        config = {'host_id': 'node', 'exporter_url': 'http://127.0.0.1:9182/metrics', 'sidecar_path': 'sidecar.json'}
        with patch('labmon.server.sys.platform', 'win32'), patch('labmon.collector.Collector') as factory:
            runtime = AgentRuntime(config)
        factory.assert_called_once_with(config['exporter_url'], config['sidecar_path'], 'node')
        self.assertIs(runtime.collector, factory.return_value)

    def test_explicit_platform_overrides_runtime_operating_system(self):
        with patch('labmon.server.sys.platform', 'win32'), patch('labmon.linux_collector.LinuxCollector') as factory:
            runtime = AgentRuntime({'platform': 'linux', 'host_id': 'node'})
            self.assertIs(runtime.collector, factory.return_value)
        config = {'platform': 'windows', 'host_id': 'node', 'exporter_url': 'http://localhost:9182/metrics',
                  'sidecar_path': 'sidecar.json'}
        with patch('labmon.server.sys.platform', 'linux'), patch('labmon.collector.Collector') as factory:
            runtime = AgentRuntime(config)
            self.assertIs(runtime.collector, factory.return_value)

    def test_unsupported_explicit_platform_is_rejected(self):
        for value in ('unsupported', None, ['linux'], {'platform': 'linux'}):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'platform'):
                AgentRuntime({'platform': value, 'host_id': 'node'})

    def test_auto_rejects_unhandled_operating_system_instead_of_guessing_windows(self):
        for platform in ('darwin', 'freebsd14', 'cygwin'):
            with self.subTest(platform=platform), patch('labmon.server.sys.platform', platform):
                with self.assertRaisesRegex(ValueError, 'Linux.*Windows'):
                    AgentRuntime({'host_id': 'node'})


class UnifiedMetricsTests(unittest.TestCase):
    def test_linux_legacy_exporter_url_does_not_redirect_metrics_to_windows_bridge(self):
        collector = Mock(sample=Mock(return_value=full_snapshot()))
        config = {'platform': 'linux', 'exporter_url': 'http://127.0.0.1:9182/metrics'}
        runtime = AgentRuntime(config, collector)
        with patch('urllib.request.build_opener', side_effect=AssertionError('Linux must use its own snapshot')):
            values = parse_metrics(runtime.metrics().decode())
        self.assertEqual(values['labmon_cpu_percent'][0][1], 25)

    def test_explicit_unified_avoids_exporter_even_when_legacy_url_exists(self):
        collector = Mock()
        collector.sample.return_value = full_snapshot()
        config = {'metrics_format': 'unified', 'exporter_url': 'http://not-the-source.invalid/metrics'}
        runtime = AgentRuntime(config, collector=collector)
        with patch('urllib.request.build_opener', side_effect=AssertionError('should not fetch exporter')):
            values = parse_metrics(runtime.metrics().decode())
        self.assertEqual(values['labmon_cpu_percent'][0][1], 25)
        self.assertNotIn('labmon_ssh_tcp_connections', values)
        self.assertNotIn('labmon_disk_read_bytes_per_second', values)
        self.assertEqual(values['labmon_disk_write_bytes_per_second'][0][1], 0)
        self.assertEqual(values['labmon_disk_size_bytes'][0][0]['volume'], '/data/"quoted"\nline')

    def test_absent_exporter_url_uses_unified_and_caches_same_snapshot(self):
        collector = Mock()
        collector.sample.return_value = full_snapshot()
        runtime = AgentRuntime({'platform': 'linux', 'poll_seconds': 5}, collector=collector)
        with patch('labmon.server.time.monotonic', return_value=100):
            first = runtime.sample()
            payload = runtime.metrics()
            first['cpu']['percent'] = 999
            second = runtime.sample()
        self.assertEqual(second['cpu']['percent'], 25)
        self.assertIsInstance(payload, bytes)
        collector.sample.assert_called_once()

    def test_error_snapshot_cannot_be_published_as_fresh_good_metrics(self):
        collector = Mock()
        collector.sample.return_value = {**full_snapshot(), 'telemetry_status': 'error'}
        runtime = AgentRuntime({'platform': 'linux'}, collector=collector)
        with self.assertRaises(OSError):
            runtime.metrics()

    def test_unknown_nonfinite_and_boolean_measurements_are_omitted(self):
        current = full_snapshot()
        current['cpu']['percent'] = float('nan')
        current['memory']['percent'] = True
        current['network']['recv_bps'] = float('inf')
        collector = Mock(sample=Mock(return_value=current))
        values = parse_metrics(AgentRuntime({}, collector).metrics().decode())
        for key in ('labmon_cpu_percent', 'labmon_memory_percent', 'labmon_network_receive_bytes_per_second'):
            self.assertNotIn(key, values)

    def test_unified_metrics_http_is_still_bearer_protected(self):
        collector = Mock(sample=Mock(return_value=full_snapshot()))
        config = {'mode': 'agent', 'platform': 'linux', 'token': 'test-only-token-123456789', 'host_id': 'linux-node'}
        runtime = AgentRuntime(config, collector)
        server = MonitorHTTPServer(('127.0.0.1', 0), config, runtime)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for authorized, status in ((False, 401), (True, 200)):
                connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
                try:
                    headers = {'Authorization': 'Bearer ' + config['token']} if authorized else {}
                    connection.request('GET', '/metrics', headers=headers)
                    response = connection.getresponse()
                    raw = response.read()
                    self.assertEqual(response.status, status)
                    if authorized:
                        self.assertIn('text/plain', response.getheader('Content-Type'))
                        self.assertIn('labmon_cpu_percent', parse_metrics(raw.decode()))
                        self.assertNotIn(config['token'], raw.decode())
                finally:
                    connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


class LinuxSnapshotHubTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.proc, self.sys = self.root / 'proc', self.root / 'sys'
        self.now = 2000000000.0
        files = {
            'stat': 'cpu 100 0 0 1000 0 0 0 0 0 0\ncpu0 50 0 0 500 0 0 0 0 0 0\n'
                    'cpu1 50 0 0 500 0 0 0 0 0 0\nbtime 1000\n',
            'sys/kernel/random/boot_id': '12345678-1234-1234-1234-123456789abc',
            'cpuinfo': 'model name : Example Linux CPU\n',
            'meminfo': 'MemTotal: 10000 kB\nMemAvailable: 2500 kB\n',
            'self/mountinfo': '10 1 0:5 / /proc rw - proc proc rw\n11 1 8:1 / / rw - ext4 /dev/sda1 rw\n',
            'diskstats': '8 1 sda1 1 0 100 0 1 0 100 0 0 0 0\n',
            'net/dev': 'header\n eth0: 1000 0 0 0 0 0 0 0 500 0 0 0 0 0 0 0\n',
            'net/tcp': 'header\n', 'net/tcp6': 'header\n',
            '123/stat': process_stat(123, start=250),
            '124/stat': process_stat(124, start=300, parent=123),
            '123/status': 'Uid: 1000 1000 1000 1000\nVmRSS: 100 kB\n',
            '124/status': 'Uid: 1000 1000 1000 1000\nVmRSS: 200 kB\n',
        }
        for name, content in files.items():
            path = self.proc / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding='utf-8')
        online = self.sys / 'devices/system/cpu/online'
        online.parent.mkdir(parents=True)
        online.write_text('0-1', encoding='ascii')
        def no_gpu(*args, **kwargs):
            raise FileNotFoundError()
        self.collector = LinuxCollector('linux-node', self.proc, self.sys, clock=lambda: self.now,
            monotonic=lambda: self.now, clock_ticks=100, page_size=4096,
            statvfs=lambda path: SimpleNamespace(f_frsize=4096, f_bsize=4096, f_blocks=1000, f_bavail=500),
            uid_resolver=lambda uid: 'researcher', run_command=no_gpu)

    def test_actual_linux_snapshot_groups_in_hub_and_exports_warmup_as_unknown(self):
        runtime = AgentRuntime({'platform': 'linux'}, collector=self.collector)
        snapshot = runtime.sample()
        required = {'host_id', 'observed_at', 'telemetry_status', 'cpu', 'memory', 'disks', 'network',
                    'gpus', 'processes', 'ssh', 'warnings'}
        self.assertTrue(required.issubset(snapshot))
        self.assertEqual(snapshot['platform'], 'linux')
        self.assertEqual(snapshot['process_status'], 'ok')
        self.assertEqual(snapshot['telemetry_status'], 'partial')
        jobs = group_jobs('linux-node', snapshot['processes'], now=self.now)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]['pids'], [123, 124])
        self.assertEqual(jobs[0]['memory_bytes'], 300 * 1024)
        self.assertEqual(jobs[0]['state'], 'unknown')
        self.assertIsNone(jobs[0]['cpu_pct'])
        metrics = parse_metrics(runtime.metrics().decode())
        self.assertNotIn('labmon_cpu_percent', metrics)
        self.assertNotIn('labmon_process_cpu_percent', metrics)
        self.assertEqual(metrics['labmon_ssh_tcp_connections'][0][1], 0)
        self.assertEqual(len(metrics['labmon_process_memory_bytes']), 2)

    def test_injected_real_linux_collector_auto_detects_unified_despite_exporter_url(self):
        runtime = AgentRuntime({'exporter_url': 'http://127.0.0.1:9182/metrics'}, self.collector)
        self.assertEqual(runtime.platform, 'linux')
        with patch('urllib.request.build_opener', side_effect=AssertionError('Linux must use its own snapshot')):
            values = parse_metrics(runtime.metrics().decode())
        self.assertIn('labmon_memory_total_bytes', values)

    def test_linux_partial_gpu_warning_is_online_and_incomplete_process_scan_cannot_end_job(self):
        self.current = self.collector.sample()
        config = {'data_dir': str(self.root / 'hub'), 'servers': [
            {'id': 'linux-node', 'name': 'Linux node', 'platform': 'linux', 'enabled': True,
             'agent_url': 'http://127.0.0.1:8767', 'token': 'test-only-token-123456789'}]}
        hub = HubRuntime(config, fetcher=lambda *args: copy.deepcopy(self.current), clock=lambda: self.now)
        self.addCleanup(hub.close)
        session, name, _ = hub.store.session(None)
        hub.poll_once()
        server = hub.state(session, name)['servers'][0]
        self.assertEqual(server['status'], 'online')
        self.assertEqual(server['platform'], 'linux')
        original_id = server['jobs'][0]['id']
        for _ in range(2):
            self.now += 5
            self.current = {**self.current, 'observed_at': datetime.fromtimestamp(self.now, timezone.utc).isoformat(),
                            'process_status': 'error', 'processes': []}
            hub.poll_once()
        jobs = hub.state(session, name)['servers'][0]['jobs']
        self.assertEqual(jobs[0]['id'], original_id)
        self.assertEqual(jobs[0]['state'], 'unknown')
        for _ in range(2):
            self.now += 5
            self.current = {**self.current, 'observed_at': datetime.fromtimestamp(self.now, timezone.utc).isoformat(),
                            'process_status': 'ok'}
            hub.poll_once()
        self.assertEqual(hub.state(session, name)['servers'][0]['jobs'], [])


if __name__ == '__main__':
    unittest.main()
