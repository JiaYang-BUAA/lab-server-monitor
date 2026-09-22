import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/connect-server.py"
SPEC = importlib.util.spec_from_file_location("connect_server_script", SCRIPT)
connect = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(connect)


class ConnectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "config").mkdir()
        self.hub = self.root / "config/hub.json"
        self.prom = self.root / "config/prometheus.yml"
        self.hub.write_text(json.dumps({"mode": "hub", "listen_host": "192.168.50.10", "custom": "preserved",
            "servers": [{"id": "lab-new", "enabled": True, "token": "new-server-token-123456789"},
                        {"id": "lab-old", "enabled": False, "name": "旧服务器"}]}), encoding="utf-8")
        self.prom.write_text("global:\n  scrape_interval: 10s\nscrape_configs:\n  - job_name: windows\n    static_configs:\n      - targets: [\"127.0.0.1:9182\"]\n", encoding="utf-8")
        self.token = "old-server-token-123456789"
        self.token_file = self.root / "incoming-private.json"
        self.token_file.write_text(json.dumps({"agent_token": self.token}), encoding="utf-8")
        self.calls = []
        self.checked = []

    def tearDown(self):
        self.temp.cleanup()

    def fetch(self, address, path, token):
        self.calls.append((address, path, token))
        return {"ok": True, "mode": "agent"} if path == "/healthz" else {"host_id": "lab-old", "telemetry_status": "partial"}

    def check(self, root, candidate):
        self.checked.append(candidate.read_text(encoding="utf-8"))
        # The actual token target must exist before promtool validates the final YAML.
        line = next(line for line in self.checked[-1].splitlines() if "bearer_token_file:" in line)
        target = json.loads(line.split(":", 1)[1].strip())
        self.assertEqual(Path(target).read_text(encoding="utf-8").strip(), self.token)

    def run_connect(self, **kwargs):
        params = {"fetcher": self.fetch, "validator": self.check, "protector": lambda _: None, **kwargs}
        return connect.connect_server(self.root, "192.168.50.11", self.token_file, **params)

    def test_unreachable_does_not_change_any_configuration(self):
        before = {str(path.relative_to(self.root)): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        def failure(*args):
            raise connect.ConnectError("unreachable")
        with self.assertRaises(connect.ConnectError):
            self.run_connect(fetcher=failure)
        after = {str(path.relative_to(self.root)): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(self.checked, [])

    def test_wrong_host_identity_does_not_change_configuration(self):
        original_hub, original_prom = self.hub.read_bytes(), self.prom.read_bytes()
        def wrong(address, path, token):
            return {"ok": True, "mode": "agent"} if path == "/healthz" else {"host_id": "lab-new", "telemetry_status": "ok"}
        with self.assertRaises(connect.ConnectError):
            self.run_connect(fetcher=wrong)
        self.assertEqual(self.hub.read_bytes(), original_hub)
        self.assertEqual(self.prom.read_bytes(), original_prom)
        self.assertFalse((self.root / "config/secrets").exists())

    def test_success_backs_up_preserves_other_entries_and_is_idempotent(self):
        original_hub, original_prom = self.hub.read_bytes(), self.prom.read_bytes()
        result = self.run_connect()
        self.assertTrue(result["changed"])
        backup = Path(result["backup_dir"])
        self.assertEqual((backup / "hub.json").read_bytes(), original_hub)
        self.assertEqual((backup / "prometheus.yml").read_bytes(), original_prom)
        hub = json.loads(self.hub.read_text(encoding="utf-8"))
        self.assertEqual(hub["custom"], "preserved")
        self.assertEqual(hub["servers"][0]["token"], "new-server-token-123456789")
        self.assertTrue(hub["servers"][1]["enabled"])
        self.assertEqual(hub["servers"][1]["agent_url"], "http://192.168.50.11:8767")
        self.assertNotIn(self.token, self.prom.read_text(encoding="utf-8"))
        before_hub, before_prom = self.hub.read_bytes(), self.prom.read_bytes()
        repeat = self.run_connect()
        self.assertFalse(repeat["changed"])
        self.assertEqual(self.hub.read_bytes(), before_hub)
        self.assertEqual(self.prom.read_bytes(), before_prom)
        self.assertEqual(self.prom.read_text(encoding="utf-8").count("job_name: lab-old"), 1)
        self.assertEqual(len(list((self.root / "config/backups").iterdir())), 1)
        self.assertEqual(len(self.checked), 2)

    def test_validation_failure_leaves_configs_and_tokens_unchanged(self):
        before = {str(path.relative_to(self.root)): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        def bad_validator(*args):
            raise connect.ConnectError("rejected")
        with self.assertRaises(connect.ConnectError):
            self.run_connect(validator=bad_validator)
        after = {str(path.relative_to(self.root)): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        self.assertFalse(list((self.root / "config").glob(".connect-*")))

    def test_private_ipv4_restrictions(self):
        for address in ("127.0.0.1", "8.8.8.8", "169.254.1.1", "100.64.0.1", "::1", "http://192.168.50.11", "localhost"):
            with self.subTest(address=address), self.assertRaises(connect.ConnectError):
                connect.private_ipv4(address)
        self.assertEqual(connect.private_ipv4("192.168.50.11"), "192.168.50.11")

    def test_block_stays_in_scrape_section_with_later_top_level_config(self):
        original = self.prom.read_text(encoding="utf-8") + "rule_files: []\n"
        first = connect.render_prometheus(original, "192.168.50.11", Path("E:/private/token"))
        second = connect.render_prometheus(first, "192.168.50.11", Path("E:/private/token"))
        self.assertEqual(first, second)
        self.assertLess(first.index(connect.END), first.index("rule_files:"))

    def test_manual_duplicate_and_broken_markers_are_rejected(self):
        original = self.prom.read_text(encoding="utf-8")
        for text in (original + "  - job_name: lab-old\n", original + connect.BEGIN + "\n"):
            with self.assertRaises(connect.ConnectError):
                connect.render_prometheus(text, "192.168.50.11", Path("E:/private/token"))

    def test_half_swap_failure_rolls_back_prometheus(self):
        original_hub, original_prom = self.hub.read_bytes(), self.prom.read_bytes()
        real_replace = connect.os.replace
        def fail_hub(source, target):
            # Windows runner TEMP can use an 8.3 alias while the helper resolves it.
            if Path(target).resolve() == self.hub.resolve():
                raise PermissionError("test blocked replacement")
            return real_replace(source, target)
        with mock.patch.object(connect.os, "replace", side_effect=fail_hub), self.assertRaises(PermissionError):
            self.run_connect()
        self.assertEqual(self.hub.read_bytes(), original_hub)
        self.assertEqual(self.prom.read_bytes(), original_prom)

    def test_third_linux_server_custom_port_preserves_other_servers(self):
        before = json.loads(self.hub.read_text(encoding="utf-8"))["servers"]
        def third(address, path, token):
            return {"ok": True, "mode": "agent"} if path == "/healthz" else {"host_id": "compute-03", "platform": "linux", "telemetry_status": "ok"}
        self.run_connect(host_id="compute-03", name="第三台 Linux", platform="linux", port=9876, fetcher=third)
        hub = json.loads(self.hub.read_text(encoding="utf-8"))
        self.assertEqual(hub["servers"][:2], before)
        self.assertEqual(hub["servers"][2]["agent_url"], "http://192.168.50.11:9876")
        self.assertEqual(hub["servers"][2]["platform"], "linux")
        self.assertIn('server: "compute-03"', self.prom.read_text(encoding="utf-8"))
        self.assertFalse(self.run_connect(host_id="compute-03", name="第三台 Linux", platform="linux", port=9876, fetcher=third)["changed"])

    def test_without_history_does_not_require_prometheus(self):
        self.prom.unlink()
        result = self.run_connect(with_prometheus=False)
        self.assertTrue(result["changed"])
        self.assertEqual(self.checked, [])
        self.assertFalse(self.prom.exists())
        self.assertFalse(self.run_connect(with_prometheus=False)["changed"])

    def test_platform_mismatch_or_unsafe_id_leaves_files_unchanged(self):
        before = self.hub.read_bytes()
        for kwargs in ({"platform": "linux"}, {"host_id": "../escape"}, {"port": 0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(connect.ConnectError):
                self.run_connect(**kwargs)
        self.assertEqual(before, self.hub.read_bytes())


if __name__ == "__main__":
    unittest.main()
