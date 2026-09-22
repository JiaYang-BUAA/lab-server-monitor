"""Renderer tests use a fake WinSW file; no Windows service or installer is run."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET


PROJECT = Path(__file__).resolve().parents[1]
RENDERER = PROJECT / "scripts/render-config.py"


class WindowsInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "monitor"
        (self.root / "downloads").mkdir(parents=True)
        (self.root / "downloads/WinSW-x64.exe").write_bytes(b"test-only wrapper; never executable")

    def tearDown(self):
        self.temp.cleanup()

    def render(self, role="hub", address="192.168.1.10", host_id="node-01", more=()):
        args = [sys.executable, str(RENDERER), "--root", str(self.root), "--role", role,
                "--address", address, "--host-id", host_id]
        if role == "agent":
            args += ["--hub-address", "192.168.1.9"]
        return subprocess.run(args + list(more), capture_output=True, text=True, encoding="utf-8", timeout=30)

    def read(self, name):
        return json.loads((self.root / "config" / name).read_text(encoding="utf-8"))

    def snapshot(self):
        return {str(path.relative_to(self.root)): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}

    def assert_rendered(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_new_hub_has_one_local_server_and_unified_authenticated_scrape(self):
        self.assert_rendered(self.render(more=("--host-name", "计算节点")))
        hub, agent = self.read("hub.json"), self.read("agent.json")
        self.assertEqual(len(hub["servers"]), 1)
        self.assertEqual(hub["servers"][0]["id"], "node-01")
        self.assertEqual(hub["servers"][0]["name"], "计算节点")
        self.assertEqual(hub["servers"][0]["platform"], "windows")
        self.assertEqual(agent["listen_host"], "127.0.0.1")
        self.assertEqual(agent["platform"], "windows")
        self.assertEqual(agent["metrics_format"], "unified")
        self.assertEqual(agent["allowed_log_roots"], [])
        prometheus = (self.root / "config/prometheus.yml").read_text()
        self.assertIn('"127.0.0.1:8767"', prometheus)
        self.assertIn("bearer_token_file:", prometheus)
        self.assertNotIn(agent["token"], prometheus)
        self.assertEqual((self.root / "config/secrets/node-01.token").read_text().strip(), agent["token"])
        dashboard = self.read("grafana-dashboards/resources.json")
        for panel in dashboard["panels"]:
            for target in panel["targets"]:
                self.assertNotIn("windows_", target["expr"])

    def test_agent_only_has_three_services(self):
        self.assert_rendered(self.render("agent"))
        self.assertFalse((self.root / "config/hub.json").exists())
        self.assertFalse((self.root / "config/prometheus.yml").exists())
        self.assertEqual(self.read("service-list.json"), ["LabMonExporter", "LabMonSidecar", "LabMonAgent"])
        self.assertEqual(self.read("agent.json")["listen_host"], "192.168.1.10")

    def test_identical_rerun_preserves_every_file(self):
        self.assert_rendered(self.render())
        before = self.snapshot()
        self.assert_rendered(self.render())
        self.assertEqual(self.snapshot(), before)

    def test_existing_configuration_and_data_are_not_reset(self):
        self.assert_rendered(self.render())
        hub = self.read("hub.json")
        hub["servers"].append({"id": "another-node", "enabled": False})
        hub["servers"][0]["name"] = "已自定义名称"
        (self.root / "config/hub.json").write_text(json.dumps(hub), encoding="utf-8")
        agent = self.read("agent.json")
        agent.pop("metrics_format")
        agent.pop("platform")
        agent["allowed_log_roots"] = ["D:\\calculations"]
        (self.root / "config/agent.json").write_text(json.dumps(agent), encoding="utf-8")
        (self.root / "config/grafana.ini").write_text("custom Grafana settings", encoding="utf-8")
        (self.root / "config/grafana-dashboards/resources.json").write_text('{"custom":true}', encoding="utf-8")
        (self.root / "config/grafana-provisioning/datasources/prometheus.yml").write_text("custom datasource", encoding="utf-8")
        (self.root / "data/hub/labmon.sqlite3").write_bytes(b"registration fixture")
        before = self.snapshot()
        self.assert_rendered(self.render())
        self.assertEqual(self.snapshot(), before)

    def test_hub_address_and_id_changes_fail_before_any_file_write(self):
        self.assert_rendered(self.render())
        before = self.snapshot()
        for values in ({"address": "192.168.1.20"}, {"host_id": "other"}):
            with self.subTest(values=values):
                result = self.render(**values)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.snapshot(), before)

    def test_agent_address_id_and_role_changes_fail_before_any_file_write(self):
        self.assert_rendered(self.render("agent"))
        before = self.snapshot()
        for values in ({"role": "agent", "address": "192.168.1.20"},
                       {"role": "agent", "host_id": "other"}, {"role": "hub"}):
            with self.subTest(values=values):
                result = self.render(**values)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.snapshot(), before)

    def test_missing_private_config_does_not_regenerate_token(self):
        self.assert_rendered(self.render())
        (self.root / "config/private.json").unlink()
        before = self.snapshot()
        result = self.render()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.snapshot(), before)

    def test_invalid_credential_error_does_not_include_secret(self):
        self.assert_rendered(self.render())
        private = self.read("private.json")
        private["agent_token"] = "secret bad value"
        (self.root / "config/private.json").write_text(json.dumps(private), encoding="utf-8")
        before = self.snapshot()
        result = self.render()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(private["agent_token"], result.stdout + result.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_invalid_address_and_identifier_do_not_create_configuration(self):
        before = self.snapshot()
        for values in ({"address": "0.0.0.0"}, {"address": "not-an-ip"}, {"host_id": "bad/id"}):
            with self.subTest(values=values):
                self.assertNotEqual(self.render(**values).returncode, 0)
                self.assertEqual(self.snapshot(), before)

    def test_changed_service_file_is_backed_up(self):
        self.assert_rendered(self.render())
        unit = self.root / "services/LabMonAgent.xml"
        unit.write_text("custom service fixture", encoding="utf-8")
        self.assert_rendered(self.render())
        backups = list(unit.parent.glob("LabMonAgent.xml.before-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), "custom service fixture")
        self.assertEqual(ET.parse(unit).findtext("depend"), "LabMonExporter")

    def test_dependency_manifest_is_https_pinned_and_has_complete_roles(self):
        items = json.loads((PROJECT / "assets/windows-dependencies.json").read_text())
        self.assertEqual(len({item["file"] for item in items}), len(items))
        for item in items:
            self.assertTrue(item["url"].startswith("https://"))
            self.assertRegex(item["sha256"], r"^[a-fA-F0-9]{64}$")
            self.assertEqual(Path(item["file"]).name, item["file"])
            self.assertTrue(set(item["roles"]).issubset({"agent", "hub"}))

    def test_powershell_scripts_parse_without_execution(self):
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        if not powershell:
            self.skipTest("PowerShell unavailable")
        # Script paths are passed as arguments, not interpolated into shell code.
        check = '$failed=$false; foreach($path in $args) {$tokens=$null; $errors=$null; [void][System.Management.Automation.Language.Parser]::ParseFile($path,[ref]$tokens,[ref]$errors); if($errors.Count){$errors | ForEach-Object {$_.Message}; $failed=$true}}; if($failed){exit 1}'
        runner = Path(self.temp.name) / "parse.ps1"
        runner.write_text(check, encoding="utf-8")
        files = [str(PROJECT / "scripts" / name) for name in ("install.ps1", "download-dependencies.ps1")]
        result = subprocess.run([powershell, "-NoProfile", "-File", str(runner), *files], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
