import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from labmon.collector import Collector, classify_process, parse_metrics


def exporter_fixture(tick=0, *, cores=192, start=1000, process_cpu=0, secret=False):
    secret_label = ',cmdline="SECRET_TOKEN"' if secret else ''
    lines = ['windows_cpu_logical_processor 192', 'windows_memory_physical_total_bytes 10000',
             'windows_memory_available_bytes 2500', 'windows_exporter_collector_success{collector="cpu"} 1']
    for i in range(cores):
        # Three Windows processor groups; all core instances must be included.
        core = f'{i // 64},{i % 64}'
        lines.append(f'windows_cpu_time_total{{core="{core}",mode="idle"}} {100 + tick * .75}')
    lines += [
        f'windows_process_info{{process="standard",process_id="123",creating_process_id="100",owner="DOMAIN\\\\李明"{secret_label}}} 1',
        f'windows_process_start_time_seconds_timestamp{{process="standard",process_id="123"}} {start}',
        f'windows_process_cpu_time_total{{process="standard",process_id="123",mode="user"}} {process_cpu * .8}',
        f'windows_process_cpu_time_total{{process="standard",process_id="123",mode="privileged"}} {process_cpu * .2}',
        'windows_process_working_set_private_bytes{process="standard",process_id="123"} 4096',
        'windows_logical_disk_size_bytes{volume="E:"} 20000',
        'windows_logical_disk_free_bytes{volume="E:"} 8000',
        f'windows_logical_disk_read_bytes_total{{volume="E:"}} {1000 + tick * 100}',
        f'windows_logical_disk_write_bytes_total{{volume="E:"}} {2000 + tick * 50}',
        f'windows_net_bytes_received_total{{nic="Ethernet"}} {1000 + tick * 200}',
        f'windows_net_bytes_sent_total{{nic="Ethernet"}} {1000 + tick * 50}',
    ]
    return parse_metrics('\n'.join(lines))


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.sidecar = Path(self.temp.name) / 'sidecar.json'
        self.now = 2_000_000_000
        self.write_sidecar()
        self.collector = Collector('http://127.0.0.1:9182/metrics', str(self.sidecar), 'lab-new')

    def write_sidecar(self, age=0, **overrides):
        data = {'observed_at': datetime.fromtimestamp(self.now - age, timezone.utc).isoformat(),
                'cpu': {'logical_processors': 192, 'model': 'Test CPU'},
                'process_identities': [{'pid': 123, 'name': 'standard.exe', 'start_time': 1000}],
                'ssh': {'tcp_connections': 1, 'connections': [{'remote_address': '10.0.0.2', 'remote_port': 49999}],
                        'observed_at': datetime.fromtimestamp(self.now - age, timezone.utc).isoformat()},
                'gpus': [{'id': '0', 'name': 'Test GPU', 'utilization_pct': 35, 'memory_used_bytes': 1024,
                          'memory_total_bytes': 4096, 'driver_model': 'WDDM'}]}
        data.update(overrides)
        self.sidecar.write_text(json.dumps(data), encoding='utf-8')

    def sample(self, metrics, monotonic):
        with patch.object(self.collector, '_fetch', return_value=metrics), patch('labmon.collector.time.time', return_value=self.now), patch('labmon.collector.time.monotonic', return_value=monotonic):
            return self.collector.sample()

    def test_192_core_rates_and_process_whole_machine_denominator(self):
        first = self.sample(exporter_fixture(process_cpu=100), 10)
        self.assertIsNone(first['cpu']['percent'])
        self.assertIsNone(first['processes'][0]['cpu_pct'])
        current = self.sample(exporter_fixture(10, process_cpu=292), 20)
        self.assertEqual(current['cpu']['observed_processors'], 192)
        self.assertEqual(current['cpu']['percent'], 25)
        # 192 CPU-seconds in 10 seconds on 192 logical CPUs = 10% of machine.
        self.assertEqual(current['processes'][0]['cpu_pct'], 10)
        self.assertEqual(current['memory']['percent'], 75)
        self.assertEqual(current['disks'][0]['read_bps'], 100)
        self.assertEqual(current['network']['recv_bps'], 200)
        self.assertEqual(current['processes'][0]['gpu_memory_bytes'], None)

    def test_incomplete_processor_group_not_reported_as_whole_machine(self):
        self.sample(exporter_fixture(cores=64), 10)
        sample = self.sample(exporter_fixture(10, cores=64), 20)
        self.assertIsNone(sample['cpu']['percent'])
        self.assertIsNone(sample['processes'][0]['cpu_pct'])
        self.assertEqual(sample['cpu']['observed_processors'], 64)
        self.assertTrue(any('覆盖' in warning for warning in sample['warnings']))

    def test_reused_pid_has_new_identity_and_warms_rate(self):
        original = self.sample(exporter_fixture(process_cpu=100), 10)
        new = self.sample(exporter_fixture(10, start=2000, process_cpu=150), 20)
        self.assertNotEqual(original['processes'][0]['key'], new['processes'][0]['key'])
        self.assertIsNone(new['processes'][0]['cpu_pct'])

    def test_counter_reset_and_failed_scrape_clear_rate_baseline(self):
        self.sample(exporter_fixture(100, process_cpu=100), 10)
        reset = self.sample(exporter_fixture(0, process_cpu=0), 20)
        self.assertIsNone(reset['cpu']['percent'])
        self.assertIsNone(reset['network']['recv_bps'])
        with patch.object(self.collector, '_fetch', side_effect=OSError('secret URL/password')):
            failed = self.collector.sample()
        self.assertEqual(failed['telemetry_status'], 'error')
        self.assertNotIn('secret URL/password', json.dumps(failed))
        warmed = self.sample(exporter_fixture(10, process_cpu=100), 40)
        self.assertIsNone(warmed['cpu']['percent'])

    def test_sidecar_stale_unknown_not_zero(self):
        self.write_sidecar(age=31)
        sample = self.sample(exporter_fixture(), 10)
        self.assertIsNone(sample['ssh']['tcp_connections'])
        self.assertEqual(sample['ssh']['connections'], [])
        self.assertEqual(sample['gpus'], [])
        self.assertIsNone(sample['cpu']['model'])
        # Expired identities must not turn temporary missing keys into completion.
        self.assertEqual(sample['process_status'], 'error')

    def test_exporter_creation_timestamp_jitter_keeps_precise_identity_and_rate(self):
        first = self.sample(exporter_fixture(start=1000.799096, process_cpu=100), 10)
        second = self.sample(exporter_fixture(10, start=1001.524403, process_cpu=292), 20)
        self.assertEqual(first['processes'][0]['key'], '123:1000.000000')
        self.assertEqual(second['processes'][0]['key'], first['processes'][0]['key'])
        self.assertEqual(second['processes'][0]['cpu_pct'], 10)
        self.assertEqual(second['process_status'], 'ok')

    def test_stale_sidecar_identity_cannot_hide_counter_reset_after_pid_reuse(self):
        self.sample(exporter_fixture(start=1000.1, process_cpu=100), 10)
        sample = self.sample(exporter_fixture(10, start=1000.9, process_cpu=1), 20)
        self.assertIsNone(sample['processes'][0]['start_time'])
        self.assertIsNone(sample['processes'][0]['cpu_pct'])
        self.assertEqual(sample['process_status'], 'error')
        # A third sample before the sidecar refresh cannot resurrect the old key.
        third = self.sample(exporter_fixture(20, start=1000.9, process_cpu=2), 30)
        self.assertIsNone(third['processes'][0]['start_time'])
        self.now += 10
        self.write_sidecar(process_identities=[{'pid': 123, 'name': 'standard.exe', 'start_time': 1000.8}])
        refreshed = self.sample(exporter_fixture(30, start=1000.9, process_cpu=3), 40)
        self.assertEqual(refreshed['processes'][0]['key'], '123:1000.800000')
        self.assertIsNone(refreshed['processes'][0]['cpu_pct'])

    def test_gpu_limitation_does_not_invalidate_process_telemetry(self):
        self.write_sidecar(warnings=['WDDM per-process GPU unavailable'])
        sample = self.sample(exporter_fixture(), 10)
        self.assertEqual(sample['process_status'], 'ok')

    def test_failed_process_collector_must_not_signal_process_completion(self):
        metrics = exporter_fixture()
        metrics['windows_exporter_collector_success'].append(({'collector': 'process'}, 0))
        sample = self.sample(metrics, 10)
        self.assertEqual(sample['process_status'], 'error')

    def test_unknown_start_does_not_create_stable_pid_identity(self):
        metrics = exporter_fixture()
        del metrics['windows_process_start_time_seconds_timestamp']
        first = self.sample(metrics, 10)
        self.now += 1
        second = self.sample(metrics, 11)
        self.assertNotEqual(first['processes'][0]['key'], second['processes'][0]['key'])
        self.assertIsNone(second['processes'][0]['cpu_pct'])

    def test_command_line_never_exported_and_unicode_owner_preserved(self):
        sample = self.sample(exporter_fixture(secret=True), 10)
        self.assertNotIn('SECRET_TOKEN', json.dumps(sample))
        self.assertEqual(sample['processes'][0]['owner'], 'DOMAIN\\李明')

    def test_service_override_requires_same_creation_time(self):
        self.write_sidecar(process_overrides=[{'pid': 123, 'start_time': 1000, 'role': 'service'}])
        sample = self.sample(exporter_fixture(), 10)
        self.assertEqual(sample['processes'][0]['software'], 'system')
        self.assertEqual(sample['processes'][0]['role'], 'service')
        sample = self.sample(exporter_fixture(start=3000), 20)
        self.assertEqual(sample['processes'][0]['role'], 'solver')

    def test_process_classification_excludes_middleware_and_license(self):
        for name in ('ansys-fluent-mcp', 'ansyslmd.exe', 'lmgrd', 'comsolmphserver', 'ABAQUSLM'):
            self.assertEqual(classify_process(name), ('system', 'service'))
        for name, software in [('fluent.exe#12', 'fluent'), ('explicit_dp.exe', 'abaqus'), ('comsolbatch.exe', 'comsol')]:
            self.assertEqual(classify_process(name), (software, 'solver'))
        self.assertEqual(classify_process('python.exe'), ('general', 'general'))
        self.assertEqual(classify_process('ComsolUI'), ('comsol', 'application'))
        self.assertEqual(classify_process('not_a_fluent_solver'), ('system', 'system'))

    def test_prometheus_escaped_label_and_nan(self):
        parsed = parse_metrics('metric{label="中文\\n\\\"quoted\\\"\\\\path"} 2\ninvalid NaN\ninvalid_inf +Inf\n')
        self.assertEqual(parsed['metric'][0][0]['label'], '中文\n"quoted"\\path')
        self.assertNotIn('invalid', parsed)
        self.assertNotIn('invalid_inf', parsed)


if __name__ == '__main__':
    unittest.main()
