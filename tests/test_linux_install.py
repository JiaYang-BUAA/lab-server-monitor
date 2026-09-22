"""Cross-platform validation of Linux installation artifacts (no service mutations)."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest


PROJECT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("configure_linux", PROJECT / "scripts/configure-linux.py")
configure = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(configure)


class LinuxInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "lab-server-monitor"

    def tearDown(self):
        self.temp.cleanup()

    def args(self, role="agent", **changes):
        values = ["--root", str(self.root), "--role", role, "--address", "192.168.10.20",
                  "--host-id", "compute-02", "--prepare-only"]
        values += ["--allow-from", "192.168.10.0/24"] if role == "hub" else ["--hub-address", "192.168.10.10"]
        args = configure.parser().parse_args(values)
        for key, value in changes.items():
            setattr(args, key, value)
        return args

    def read(self, name):
        return json.loads((self.root / "config" / name).read_text(encoding="utf-8"))

    def snapshot(self):
        if not self.root.exists():
            return {}
        return {str(path.relative_to(self.root)): path.read_bytes()
                for path in self.root.rglob("*") if path.is_file()}

    def test_agent_is_linux_unified_and_private_by_default(self):
        result = configure.generate(self.args())
        agent = self.read("agent.json")
        self.assertEqual(agent["platform"], "linux")
        self.assertEqual(agent["metrics_format"], "unified")
        self.assertEqual(agent["listen_host"], "192.168.10.20")
        self.assertEqual(agent["host_id"], "compute-02")
        self.assertEqual(agent["port"], 8767)
        self.assertEqual(agent["allowed_log_roots"], [])
        self.assertEqual(agent["token"], self.read("private.json")["agent_token"])
        self.assertFalse((self.root / "config/hub.json").exists())
        self.assertFalse(result["service_actions"])
        self.assertNotIn(agent["token"], json.dumps(result))

    def test_hub_starts_with_only_local_server_and_no_history_stack(self):
        configure.generate(self.args("hub", name="计算节点"))
        hub, agent = self.read("hub.json"), self.read("agent.json")
        self.assertEqual(agent["listen_host"], "127.0.0.1")
        self.assertEqual(hub["listen_host"], "192.168.10.20")
        self.assertEqual(hub["port"], 8766)
        self.assertIsNone(hub["grafana_url"])
        self.assertEqual(len(hub["servers"]), 1)
        self.assertEqual(hub["servers"][0]["name"], "计算节点")
        self.assertEqual(hub["servers"][0]["token"], agent["token"])
        web_unit = (self.root / "services/labmon-web.service").read_text()
        self.assertIn("User=labmon", web_unit)
        self.assertIn("LoadCredential=hub-config:", web_unit)
        self.assertIn("--config %d/hub-config", web_unit)
        self.assertIn("Requires=labmon-agent.service", web_unit)
        self.assertNotIn(agent["token"], web_unit)

    def test_rerun_preserves_token_registrations_and_added_servers(self):
        args = self.args("hub")
        configure.generate(args)
        hub = self.read("hub.json")
        hub["servers"].append({"id": "remote", "enabled": False})
        (self.root / "config/hub.json").write_text(json.dumps(hub), encoding="utf-8")
        data = self.root / "data/hub/labmon.sqlite3"
        data.write_bytes(b"retained registration fixture")
        before = self.snapshot()
        configure.generate(args)
        self.assertEqual(self.snapshot(), before)

    def test_default_rerun_preserves_explicit_allowed_log_roots(self):
        configure.generate(self.args(allow_log_root=["/srv/calculations"]))
        configure.generate(self.args())
        self.assertEqual(self.read("agent.json")["allowed_log_roots"], ["/srv/calculations"])

    def test_dry_run_does_not_create_root(self):
        result = configure.generate(self.args(dry_run=True))
        self.assertFalse(self.root.exists())
        self.assertTrue(result["dry_run"])
        self.assertNotIn("token", json.dumps(result))

    def test_conflicting_host_address_role_and_firewall_source_leave_all_files_unchanged(self):
        configure.generate(self.args())
        before = self.snapshot()
        for changes in ({"host_id": "other"}, {"address": "192.168.10.21"},
                        {"hub_address": "192.168.10.12"}, {"allow_log_root": ["/srv/other"]}):
            with self.subTest(changes=changes), self.assertRaises(configure.ConfigurationError):
                configure.generate(self.args(**changes))
            self.assertEqual(self.snapshot(), before)
        with self.assertRaises(configure.ConfigurationError):
            configure.generate(self.args("hub"))
        self.assertEqual(self.snapshot(), before)

    def test_invalid_parameters_never_create_partial_installation(self):
        bad = [{"host_id": "bad/id"}, {"host_id": "x" * 65}, {"address": "0.0.0.0"},
               {"address": "8.8.8.8"}, {"address": "example.com"}, {"hub_address": None},
               {"allow_from": "192.168.0.0/16"}, {"python": "/usr/bin/python3;bad"},
               {"allow_log_root": ["relative/logs"]}, {"allow_log_root": ["/"]},
               {"allow_log_root": ["/srv/../etc"]}, {"name": " "}]
        for changes in bad:
            with self.subTest(changes=changes), self.assertRaises(configure.ConfigurationError):
                configure.generate(self.args(**changes))
            self.assertFalse(self.root.exists())

    def test_invalid_hub_subnets_are_rejected(self):
        for value in (None, "0.0.0.0/0", "192.168.1.22/24", "example.com", "::/0"):
            with self.subTest(value=value), self.assertRaises(configure.ConfigurationError):
                configure.generate(self.args("hub", allow_from=value))
            self.assertFalse(self.root.exists())

    def test_root_with_spaces_or_traversal_is_rejected(self):
        for path in (self.root / "with space", self.root / ".." / "elsewhere"):
            with self.subTest(path=path), self.assertRaises(configure.ConfigurationError):
                configure.generate(self.args(root=str(path)))
            self.assertFalse(self.root.exists())

    def test_hub_paths_hidden_by_protect_home_fail_before_writes(self):
        for parent in ("/home/researcher", "/root", "/run/user/1000"):
            for changes in ({"root": parent + "/lab-server-monitor"}, {"python": parent + "/venv/bin/python3"}):
                with self.subTest(changes=changes), self.assertRaisesRegex(configure.ConfigurationError, "ProtectHome"):
                    configure.generate(self.args("hub", **changes))
                self.assertFalse(self.root.exists())

    def test_protect_home_guard_does_not_reject_similar_unhidden_paths(self):
        for value in ("/opt/lab-server-monitor", "/srv/lab-monitor", "/usr/bin/python3", "/home-services/lab-monitor"):
            configure.guard_hub_home_path(value, "fixture")

    def test_real_install_temporary_path_preflight_rejects_masked_targets(self):
        script = (PROJECT / "scripts/install-linux.sh").read_text(encoding="utf-8")
        section = script.split("# BEGIN hub temporary-path preflight", 1)[1].split("# END hub temporary-path preflight", 1)[0]
        self.assertIn("(!prepare_only)", section)
        code = section.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
        for root, executable in (("/tmp/lab-monitor", "/usr/bin/python3"),
                                 ("/var/tmp/lab-monitor", "/usr/bin/python3"),
                                 ("/opt/lab-monitor", "/tmp/venv/bin/python3"),
                                 ("/opt/lab-monitor", "/var/tmp/venv/bin/python3"),
                                 ("/opt/lab-monitor", "/usr/bin/python3")):
            with self.subTest(root=root, executable=executable):
                result = subprocess.run([sys.executable, "-", root, executable], input=code,
                                        capture_output=True, text=True, timeout=10)
                expected_ok = root == "/opt/lab-monitor" and executable == "/usr/bin/python3"
                self.assertEqual(result.returncode == 0, expected_ok, result.stderr)
                if not expected_ok:
                    self.assertIn("PrivateTmp", result.stderr)
                self.assertFalse(self.root.exists())

    def test_invalid_existing_secret_fails_without_leaking_or_rewriting_it(self):
        (self.root / "config").mkdir(parents=True)
        secret = self.root / "config/private.json"
        secret.write_text('{"agent_token":"sensitive but malformed value"}', encoding="utf-8")
        before = self.snapshot()
        with self.assertRaises(configure.ConfigurationError) as error:
            configure.generate(self.args())
        self.assertNotIn("sensitive", str(error.exception))
        self.assertEqual(self.snapshot(), before)

    def test_parameter_error_on_existing_hub_is_atomic(self):
        configure.generate(self.args("hub"))
        before = self.snapshot()
        with self.assertRaises(configure.ConfigurationError):
            configure.generate(self.args("hub", allow_from="192.168.0.0/16"))
        self.assertEqual(self.snapshot(), before)

    def test_changed_unit_is_backed_up_and_config_is_preserved(self):
        configure.generate(self.args())
        unit = self.root / "services/labmon-agent.service"
        old = unit.read_text()
        token = self.read("private.json")["agent_token"]
        configure.generate(self.args(python="/usr/bin/python3.12"))
        backups = list((self.root / "backups").glob("*-units/labmon-agent.service"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), old)
        self.assertIn("/usr/bin/python3.12", unit.read_text())
        self.assertEqual(self.read("private.json")["agent_token"], token)

    @unittest.skipIf(os.name == "nt", "Windows chmod does not implement POSIX ownership/modes")
    def test_private_directories_and_files_have_restrictive_modes(self):
        configure.generate(self.args("hub"))
        for name in ("config", "services", "data/agent", "data/hub", "backups"):
            self.assertEqual(stat.S_IMODE((self.root / name).stat().st_mode), 0o700)
        for path in (self.root / "config").iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.root / "data").stat().st_mode), 0o755)

    def test_symlink_destination_is_rejected_before_config_write(self):
        elsewhere = Path(self.temp.name) / "elsewhere"
        elsewhere.mkdir()
        self.root.mkdir()
        try:
            (self.root / "config").symlink_to(elsewhere, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("Symlink privilege unavailable")
        with self.assertRaises(configure.ConfigurationError):
            configure.generate(self.args())
        self.assertEqual(list(elsewhere.iterdir()), [])
        self.assertFalse((self.root / "services").exists())

    def test_shell_syntax_when_bash_is_available(self):
        bash = shutil.which("bash")
        if bash is None:
            bash = os.environ.get("LABMON_BASH")
        if bash is None:
            self.skipTest("Bash is not installed")
        result = subprocess.run([bash, "-n", (PROJECT / "scripts/install-linux.sh").as_posix()],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
