import ipaddress
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from labmon.linux_collector import LinuxCollector, _process_stat, _uid_name


def process_stat(pid=123, name='standard', *, start=250, ticks=100, parent=1):
    # Entries after comm begin at field 3, state.
    values = ['S', str(parent)] + ['0'] * 20
    values[11], values[12] = str(ticks), '0'
    values[19], values[20], values[21] = str(start), '1048576', '20'
    return f'{pid} ({name}) ' + ' '.join(values)


def tcp_address(address, port):
    raw = ipaddress.ip_address(address).packed
    if sys.byteorder == 'little':
        raw = b''.join(raw[i:i + 4][::-1] for i in range(0, len(raw), 4))
    return raw.hex().upper() + f':{port:04X}'


def tcp_row(local, port, remote, remote_port, state, inode):
    return f'0: {tcp_address(local, port)} {tcp_address(remote, remote_port)} {state} 00000000:00000000 00:00000000 00000000 0 0 {inode} 1\n'


class LinuxCollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.proc, self.sys = self.root / 'proc', self.root / 'sys'
        self.proc.mkdir()
        self.sys.mkdir()
        self.tick = 10.0
        self.boot = '12345678-1234-1234-1234-123456789abc'
        self.write('sys/kernel/random/boot_id', self.boot)
        self.write('meminfo', 'MemTotal: 10000 kB\nMemFree: 1 kB\nMemAvailable: 2500 kB\n')
        self.write('cpuinfo', 'processor : 0\nmodel name : Fixture CPU\n')
        self.mounts()
        self.cpu()
        self.network()
        self.diskstats()
        self.process()
        self.write('net/tcp', 'header\n')
        self.write('net/tcp6', 'header\n')
        self.fs_calls = []
        self.collector = LinuxCollector('linux-fixture', self.proc, self.sys,
            clock=lambda: 2000000000.0, monotonic=lambda: self.tick,
            statvfs=self.statvfs, uid_resolver=lambda uid: f'user-{uid}',
            run_command=self.no_gpu, clock_ticks=100, page_size=4096)

    def write(self, name, text):
        path = self.proc / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')

    def sys_write(self, name, text):
        path = self.sys / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')

    def cpu(self, seconds=0, cores=192, online=None, iowait_offset=0):
        fields = [100 + seconds * 25, 0, 0, 1000 + seconds * 50,
                  100 + seconds * 25 + iowait_offset, 0, 0, 0, 20 + seconds * 10, 0]
        aggregate = 'cpu ' + ' '.join(str(value * cores) for value in fields)
        rows = [aggregate] + [f'cpu{i} ' + ' '.join(map(str, fields)) for i in range(cores)]
        self.write('stat', '\n'.join(rows) + '\nbtime 1000\n')
        self.sys_write('devices/system/cpu/online', online if online is not None else f'0-{cores - 1}')

    def process(self, pid=123, name='standard', start=250, ticks=100, parent=1):
        self.write(f'{pid}/stat', process_stat(pid, name, start=start, ticks=ticks, parent=parent))
        self.write(f'{pid}/status', f'Name:\t{name}\nUid:\t1001\t1001\t1001\t1001\nVmRSS:\t64 kB\n')

    def mounts(self, extra='', options='rw'):
        self.write('self/mountinfo', f'20 1 0:5 / /proc rw - proc proc {options}\n'
            '21 1 8:1 / / rw - ext4 /dev/sda1 rw\n'
            '22 1 8:1 /somewhere /bind rw - ext4 /dev/sda1 rw\n' + extra)

    def network(self, seconds=0):
        self.write('net/dev', 'Inter-| Receive | Transmit\n face |bytes ...\n'
            f' lo: {999999 + seconds * 999999} 0 0 0 0 0 0 0 999999 0 0 0 0 0 0 0\n'
            f' eth0: {1000 + seconds * 200} 0 0 0 0 0 0 0 {500 + seconds * 50} 0 0 0 0 0 0 0\n')

    def diskstats(self, seconds=0):
        self.write('diskstats', f'8 1 sda1 100 0 {2000 + seconds * 2} 0 100 0 {1000 + seconds} 0 0 0 0\n')

    def statvfs(self, path):
        self.fs_calls.append(path)
        return SimpleNamespace(f_blocks=1000, f_bavail=200, f_bfree=250, f_frsize=4096, f_bsize=4096)

    @staticmethod
    def no_gpu(*args, **kwargs):
        raise FileNotFoundError('nvidia-smi')

    def sample(self, seconds=0, **cpu_args):
        self.tick = 10 + seconds
        self.cpu(seconds, **cpu_args)
        self.network(seconds)
        self.diskstats(seconds)
        return self.collector.sample()

    def test_whole_192_cpu_denominator_guest_exclusion_iowait_idle_and_mem_available(self):
        first = self.sample()
        self.assertIsNone(first['cpu']['percent'])
        self.assertIsNone(first['processes'][0]['cpu_pct'])
        self.process(ticks=19300)
        current = self.sample(10)
        self.assertEqual(current['platform'], 'linux')
        self.assertEqual(current['cpu']['logical_processors'], 192)
        self.assertEqual(current['cpu']['observed_processors'], 192)
        self.assertEqual(current['cpu']['percent'], 25)
        self.assertEqual(current['processes'][0]['cpu_pct'], 10)
        self.assertEqual(current['memory']['percent'], 75)
        self.assertEqual(current['memory']['available_bytes'], 2500 * 1024)
        self.assertEqual(current['network'], {'recv_bps': 200, 'sent_bps': 50})
        self.assertEqual(current['disks'][0]['read_bps'], 1024)
        self.assertEqual(current['disks'][0]['write_bps'], 512)
        self.assertEqual(current['process_status'], 'ok')

    def test_mount_deduplication_reserved_space_and_network_filesystem_exclusion(self):
        self.mounts('23 1 0:99 / /network rw - nfs host:/data rw\n'
                    '24 1 0:98 / /run rw - tmpfs tmpfs rw\n')
        current = self.sample()
        self.assertEqual(len(current['disks']), 1)
        self.assertEqual(self.fs_calls, ['/'])
        self.assertEqual(current['disks'][0]['free_bytes'], 200 * 4096)

    def test_unmapped_local_filesystem_io_is_unknown_and_mount_escape_decoded(self):
        self.mounts('23 1 0:99 / /data\\040set rw - zfs pool/data rw\n')
        current = self.sample()
        self.assertEqual(current['disks'][1]['name'], '/data set')
        self.assertIsNone(current['disks'][1]['read_bps'])
        self.assertTrue(any('块设备' in w for w in current['warnings']))

    def test_online_cpu_coverage_mismatch_never_reports_partial_machine_percent(self):
        self.sample()
        current = self.sample(10, cores=64, online='0-191')
        self.assertEqual(current['cpu']['logical_processors'], 192)
        self.assertEqual(current['cpu']['observed_processors'], 64)
        self.assertIsNone(current['cpu']['percent'])
        self.assertIsNone(current['processes'][0]['cpu_pct'])

    def test_cpu_hotplug_also_rewarms_process_normalization(self):
        self.sample()
        self.process(ticks=19300)
        current = self.sample(10, cores=96)
        self.assertIsNone(current['cpu']['percent'])
        self.assertIsNone(current['processes'][0]['cpu_pct'])

    def test_monitor_itself_is_a_service_not_a_computation(self):
        self.process(name='python3')
        with patch('labmon.linux_collector.os.getpid', return_value=123):
            current = self.sample()
        self.assertEqual(current['processes'][0]['software'], 'system')
        self.assertEqual(current['processes'][0]['role'], 'service')

    def test_counter_reset_and_iowait_decrease_need_new_baseline(self):
        self.sample()
        current = self.sample(10, iowait_offset=-300)
        self.assertIsNone(current['cpu']['percent'])
        self.assertTrue(any('回退' in w for w in current['warnings']))

    def test_reused_pid_and_reboot_do_not_inherit_process_rates(self):
        first = self.sample()
        self.process(start=999, ticks=200)
        second = self.sample(10)
        self.assertNotEqual(first['processes'][0]['key'], second['processes'][0]['key'])
        self.assertIsNone(second['processes'][0]['cpu_pct'])
        self.write('sys/kernel/random/boot_id', '22345678-1234-1234-1234-123456789abc')
        third = self.sample(20)
        self.assertNotEqual(second['processes'][0]['key'], third['processes'][0]['key'])
        self.assertIsNone(third['cpu']['percent'])
        self.assertIsNone(third['network']['recv_bps'])

    def test_exact_start_time_owner_parent_and_no_command_line_or_environment_read(self):
        self.process(parent=77)
        self.write('123/cmdline', 'sensitive-password')
        self.write('123/environ', 'sensitive-token')
        original = self.collector._read
        reads = []
        def read(relative, *args):
            reads.append(relative)
            return original(relative, *args)
        with patch.object(self.collector, '_read', side_effect=read):
            current = self.sample()
        record = current['processes'][0]
        self.assertEqual(record['start_time'], 1002.5)
        self.assertEqual(record['owner'], 'user-1001')
        self.assertEqual(record['parent_pid'], 77)
        self.assertEqual(record['software'], 'abaqus')
        self.assertFalse(any('/cmdline' in p or '/environ' in p for p in reads))
        self.assertNotIn('sensitive', json.dumps(current))

    def test_process_stat_name_parentheses_and_full_linux_executable_basename(self):
        self.assertEqual(_process_stat(process_stat(name='hello) (world'))['name'], 'hello) (world')
        self.process(name='comsol_long_nam')
        with patch('labmon.linux_collector.os.readlink', return_value='/opt/comsol/bin/comsolbatch'):
            current = self.sample()
        self.assertEqual(current['processes'][0]['name'], 'comsolbatch')
        self.assertEqual(current['processes'][0]['software'], 'comsol')

    def test_permission_denied_does_not_claim_process_inventory_complete_or_ssh_zero(self):
        self.process(pid=456, name='fluent')
        original = self.collector._read
        def read(relative, *args):
            if relative == '456/status':
                raise PermissionError('private path')
            return original(relative, *args)
        with patch.object(self.collector, '_read', side_effect=read):
            current = self.sample()
        self.assertEqual(current['process_status'], 'error')
        self.assertIsNone(current['ssh']['tcp_connections'])
        self.assertNotIn('private path', json.dumps(current))
        self.assertEqual(len(current['processes']), 1)

    def test_removed_uid_or_nss_error_keeps_process_and_resolves_each_uid_once(self):
        self.process(pid=456, name='fluent')
        calls = []
        def resolve(uid):
            calls.append(uid)
            raise KeyError('account removed')
        self.collector.uid_resolver = resolve
        current = self.sample()
        self.assertEqual(calls, [1001])
        self.assertEqual({p['owner'] for p in current['processes']}, {'1001'})
        self.assertEqual(current['process_status'], 'ok')
        with patch.dict(sys.modules, {'pwd': SimpleNamespace(getpwuid=resolve)}):
            self.assertEqual(_uid_name(999), '999')

    def test_filesystem_permission_keeps_unknown_capacity_without_hiding_processes(self):
        def denied(path):
            raise PermissionError('secret path')
        self.collector.statvfs = denied
        current = self.sample()
        self.assertEqual(current['process_status'], 'ok')
        self.assertIsNone(current['disks'][0]['free_bytes'])
        self.assertNotIn('secret path', json.dumps(current))

    def test_pid_disappearing_between_stat_reads_is_incomplete_not_finished(self):
        original = self.collector._read
        calls = 0
        def read(relative, *args):
            nonlocal calls
            if relative == '123/stat':
                calls += 1
                if calls == 2:
                    raise FileNotFoundError('process exited')
            return original(relative, *args)
        with patch.object(self.collector, '_read', side_effect=read):
            current = self.sample()
        self.assertEqual(current['process_status'], 'error')
        self.assertEqual(current['processes'], [])

    def test_pid_reused_during_scan_cannot_mix_old_owner_with_new_identity(self):
        original = self.collector._read
        calls = 0
        def read(relative, *args):
            nonlocal calls
            if relative == '123/stat':
                calls += 1
                if calls == 2:
                    return process_stat(start=999)
            return original(relative, *args)
        with patch.object(self.collector, '_read', side_effect=read):
            current = self.sample()
        self.assertEqual(current['process_status'], 'error')
        self.assertEqual(current['processes'], [])

    def test_zombie_is_confirmed_exited_and_does_not_keep_job_occupied(self):
        self.write('123/stat', process_stat().replace(') S ', ') Z '))
        current = self.sample()
        self.assertEqual(current['process_status'], 'ok')
        self.assertEqual(current['processes'], [])

    def test_hidepid_without_read_errors_still_cannot_prove_process_completion(self):
        self.mounts(options='rw,hidepid=2')
        current = self.sample()
        self.assertEqual(current['process_status'], 'error')
        self.assertIsNone(current['ssh']['tcp_connections'])

    def ssh_fixture(self):
        self.process(pid=10, name='sshd')
        self.write('10/fd/3', '')
        self.write('10/fd/4', '')
        self.write('net/tcp', 'header\n'
            + tcp_row('0.0.0.0', 2222, '0.0.0.0', 0, '0A', 100)
            + tcp_row('192.168.1.10', 2222, '192.168.1.20', 50001, '01', 101)
            + tcp_row('192.168.1.10', 443, '192.168.1.20', 50002, '01', 102)
            + tcp_row('192.168.1.10', 60001, '192.168.1.20', 2222, '01', 103))
        self.write('net/tcp6', 'header\n'
            + tcp_row('::', 2200, '::', 0, '0A', 200)
            + tcp_row('2001:db8::10', 2200, '2001:db8::20', 50003, '01', 201))
        def readlink(path):
            if Path(path) == self.proc / '10/fd/3':
                return 'socket:[100]'
            if Path(path) == self.proc / '10/fd/4':
                return 'socket:[200]'
            raise FileNotFoundError()
        return readlink

    def test_ssh_is_established_tcp_on_actual_sshd_v4_v6_listener_not_port_22_or_process_count(self):
        readlink = self.ssh_fixture()
        with patch('labmon.linux_collector.os.readlink', side_effect=readlink):
            current = self.sample()
        self.assertEqual(current['ssh']['tcp_connections'], 2)
        self.assertEqual({item['local_port'] for item in current['ssh']['connections']}, {2222, 2200})
        self.assertEqual({item['remote_address'] for item in current['ssh']['connections']},
                         {'192.168.1.20', '2001:db8::20'})
        self.assertIsNotNone(current['ssh']['observed_at'])

    def test_ssh_fd_permission_or_missing_tcp_table_stays_unknown(self):
        readlink = self.ssh_fixture()
        def denied(path):
            if Path(path).parent.name == 'fd':
                raise PermissionError()
            return readlink(path)
        with patch('labmon.linux_collector.os.readlink', side_effect=denied):
            current = self.sample()
        self.assertIsNone(current['ssh']['tcp_connections'])
        (self.proc / 'net/tcp').unlink()
        with patch('labmon.linux_collector.os.readlink', side_effect=readlink):
            self.assertIsNone(self.sample()['ssh']['tcp_connections'])

    def test_confirmed_no_sshd_is_zero_not_unknown(self):
        self.assertEqual(self.sample()['ssh']['tcp_connections'], 0)

    def test_missing_mem_available_is_not_replaced_with_free_memory(self):
        self.write('meminfo', 'MemTotal: 10000 kB\nMemFree: 1 kB\n')
        current = self.sample()
        self.assertIsNone(current['memory']['available_bytes'])
        self.assertIsNone(current['memory']['percent'])

    def test_nvidia_optional_timeout_unknown_values_and_no_shell(self):
        calls = []
        def run(command, **kwargs):
            calls.append((command, kwargs))
            return SimpleNamespace(returncode=0, stdout='0, "NVIDIA Example, GPU", 35, 1024, 8192, N/A, 50\n')
        self.collector.run_command = run
        current = self.sample()
        gpu = current['gpus'][0]
        self.assertEqual(gpu['memory_used_bytes'], 1024 ** 3)
        self.assertIsNone(gpu['temperature_c'])
        self.assertEqual(gpu['name'], 'NVIDIA Example, GPU')
        self.assertEqual(calls[0][1]['timeout'], 3)
        self.assertNotIn('shell', calls[0][1])
        self.collector.run_command = lambda *a, **kw: (_ for _ in ()).throw(subprocess.TimeoutExpired('nvidia-smi', 3))
        current = self.sample()
        self.assertEqual(current['gpus'], [])
        self.assertEqual(current['process_status'], 'ok')


if __name__ == '__main__':
    unittest.main()
