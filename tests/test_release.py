import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/build-release.py"
SPEC = importlib.util.spec_from_file_location("release_builder", SCRIPT)
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for name in (*release.SOURCE_REQUIRED, "labmon/__init__.py", "tests/test_example.py"):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("public fixture\n", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_exact_allowlist_excludes_runtime_and_private_legacy_files(self):
        excluded = ["config/private.json", "data/hub/labmon.sqlite3", "evidence/screenshot.png",
                    "docs/local-control.md", "docs/old-server.md", "scripts/enable-old-ssh.ps1",
                    "scripts/bootstrap-telemetry.ps1", "labmon/__pycache__/x.pyc", "downloads/tool.exe"]
        for name in excluded:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("private excluded fixture")
        results = release.build_release(self.root)
        self.assertEqual([Path(item["archive"]).name for item in results], [item[0] for item in release.ARCHIVES])
        for result, (_, prefix) in zip(results, release.ARCHIVES):
            archive_path = Path(result["archive"])
            with zipfile.ZipFile(archive_path) as archive:
                members = {name.removeprefix(prefix + "/") for name in archive.namelist()}
                self.assertFalse(members.intersection(excluded))
                manifest = json.loads(archive.read(prefix + "/package-manifest.json"))
                for record in manifest:
                    content = archive.read(prefix + "/" + record["file"])
                    self.assertEqual(record["sha256"], hashlib.sha256(content).hexdigest())
                    self.assertEqual(record["bytes"], len(content))
                if prefix == "lab-monitor-ssh-setup":
                    self.assertEqual(members, set(release.SSH_REQUIRED) | {"package-manifest.json"})
            self.assertEqual(result["sha256"], hashlib.sha256(archive_path.read_bytes()).hexdigest())

    def test_missing_document_prevents_any_archive_write(self):
        (self.root / "docs/release-validation.md").unlink()
        with self.assertRaisesRegex(release.ReleaseError, "Missing required"):
            release.build_release(self.root)
        self.assertFalse((self.root / "dist").exists())

    def test_real_keys_and_hostnames_are_blocked_without_echoing_values(self):
        values = [b"ssh-ed25519 " + b"A" * 68, b"DESKTOP-" + b"A1B2C3D",
                  b"-----BEGIN " + b"OPENSSH PRIVATE KEY-----\n" + b"A" * 100 + b"\n-----END OPENSSH PRIVATE KEY-----"]
        for value in values:
            with self.subTest(kind=value[:10]):
                (self.root / "README.md").write_bytes(value)
                with self.assertRaises(release.ReleaseError) as caught:
                    release.build_release(self.root)
                self.assertNotIn(value.decode(), str(caught.exception))
                self.assertFalse((self.root / "dist").exists())

    def test_external_private_markers_are_scanned_but_never_packaged(self):
        marker = "private-site-" + "marker"
        deny = self.root / "private-deny.txt"
        deny.write_text(marker + "\n")
        (self.root / "web/app.js").write_text(marker)
        with self.assertRaisesRegex(release.ReleaseError, "private deployment marker"):
            release.build_release(self.root, deny_file=deny)
        (self.root / "web/app.js").write_text("public content")
        results = release.build_release(self.root, deny_file=deny)
        for result in results:
            with zipfile.ZipFile(result["archive"]) as archive:
                self.assertFalse(any("private-deny" in name for name in archive.namelist()))

    def test_check_mode_has_no_output_files_and_builds_are_reproducible(self):
        self.assertEqual(len(release.build_release(self.root, check=True)), 2)
        self.assertFalse((self.root / "dist").exists())
        first = release.build_release(self.root)
        second = release.build_release(self.root)
        self.assertEqual([item["sha256"] for item in first], [item["sha256"] for item in second])

    def test_optional_ci_is_included_only_if_present(self):
        source, _ = release.collect_payloads(self.root)
        self.assertNotIn(".github/workflows/test.yml", source)
        ci = self.root / ".github/workflows/test.yml"
        ci.parent.mkdir(parents=True)
        ci.write_text("name: tests\n")
        source, _ = release.collect_payloads(self.root)
        self.assertIn(".github/workflows/test.yml", source)

    def test_symlink_input_is_rejected(self):
        linked = self.root / "web/app.js"
        linked.unlink()
        try:
            linked.symlink_to(self.root / "README.md")
        except OSError:
            self.skipTest("symlink creation is unavailable")
        with self.assertRaisesRegex(release.ReleaseError, "symlink"):
            release.collect_payloads(self.root)


if __name__ == "__main__":
    unittest.main()
