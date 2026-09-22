"""Safe tests: no package installation, service changes or real SSH key writes.

PowerShell tests extract only pure validation functions. Linux key installation
tests use a temporary directory and run only on POSIX systems with openat support.
"""
import base64
import builtins
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
PS_SCRIPT = ROOT / "scripts/setup-ssh.ps1"
SH_SCRIPT = ROOT / "scripts/setup-ssh.sh"
PACKET = b"\0\0\0\x0bssh-ed25519\0\0\0\x20" + bytes(range(32))
KEY = "ssh-ed25519 " + base64.b64encode(PACKET).decode("ascii") + " test-only"


def linux_namespace():
    text = SH_SCRIPT.read_text(encoding="utf-8")
    code = text.split('python3 - "$@" <<\'PY\'\n', 1)[1].split("\nPY\n}", 1)[0]
    namespace = {"__name__": "ssh_setup_test"}
    # pwd does not exist on Windows; pure validation can still be tested there.
    with mock.patch.dict(sys.modules, {"pwd": types.SimpleNamespace()}):
        exec(compile(code, str(SH_SCRIPT) + " embedded Python", "exec"), namespace)
    return namespace


class LinuxPublicKeyValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.key = self.root / "test.pub"
        self.key.write_text(KEY + "\n", encoding="utf-8")
        self.ns = linux_namespace()
        account = types.SimpleNamespace(pw_uid=12345, pw_gid=12345, pw_dir=str(self.root), pw_shell="/bin/bash")
        self.ns["pwd"].getpwnam = lambda _: account
        self.ns["check_path"] = lambda _: None
        actual_os = self.ns["os"]

        class OSProxy:
            def __getattr__(self, name):
                return getattr(actual_os, name)

            def stat(self, path):
                return types.SimpleNamespace(st_uid=12345)

        self.ns["os"] = OSProxy()
        self.ns["open"] = lambda path, *args, **kwargs: (
            io.StringIO("researcher:x:12345:12345::/home/researcher:/bin/bash\n")
            if str(path) == "/etc/passwd" else builtins.open(path, *args, **kwargs)
        )

    def tearDown(self):
        self.temp.cleanup()

    def validate(self, sources=None):
        return self.ns["validate"]("researcher", str(self.key), sources or ["192.0.2.10"])

    def test_valid_packet_and_source_normalization(self):
        _, key, sources = self.validate(["192.0.2.41/24", "2001:db8::1", "192.0.2.41/24"])
        self.assertEqual(key, KEY)
        self.assertEqual(sources, ["192.0.2.0/24", "2001:db8::1/128"])

    def test_bad_keys_rejected_before_any_write(self):
        for text in ["", KEY + "\n" + KEY, "-----BEGIN OPENSSH PRIVATE KEY-----", "from=\"x\" " + KEY,
                     KEY.replace("ssh-ed25519", "ssh-rsa", 1), "ssh-ed25519 AAAA",
                     "ssh-ed25519 " + base64.b64encode(PACKET + b"x").decode(), KEY + "\x00"]:
            with self.subTest(text=text[:28]):
                self.key.write_text(text, encoding="utf-8")
                with self.assertRaises(SystemExit):
                    self.validate()
                self.assertFalse((self.root / ".ssh").exists())

    def test_unbounded_or_invalid_sources_rejected(self):
        for source in ["Any", "0.0.0.0/0", "::/0", "0.0.0.0", "::", "224.0.0.1", "ff02::1",
                       "::ffff:192.0.2.1", "192.0.2.1/33", "192.0.2.1;id", "fe80::1%eth0", "1"]:
            with self.subTest(source=source), self.assertRaises(SystemExit):
                self.validate([source])

    def test_private_key_is_never_exposed_in_error(self):
        secret = "secret-test-fixture-never-print"
        self.key.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\n" + secret, encoding="utf-8")
        with self.assertRaises(SystemExit) as caught:
            self.validate()
        self.assertNotIn(secret, str(caught.exception))

    def test_embedded_helpers_all_compile(self):
        text = SH_SCRIPT.read_text(encoding="utf-8")
        parts = text.split("<<'PY'\n")[1:]
        self.assertGreaterEqual(len(parts), 4)
        for index, part in enumerate(parts):
            compile(part.split("\nPY\n", 1)[0], "shell helper %s" % index, "exec")


@unittest.skipUnless(os.name == "posix" and os.open in os.supports_dir_fd, "requires POSIX openat/chown support")
class LinuxAuthorizedKeysTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.ns = linux_namespace()
        self.account = types.SimpleNamespace(pw_dir=str(self.root), pw_uid=os.getuid(), pw_gid=os.getgid())
        (self.root / ".ssh").mkdir(mode=0o700)
        self.path = self.root / ".ssh/authorized_keys"

    def tearDown(self):
        self.temp.cleanup()

    def install(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.ns["install"](self.account, KEY)

    def test_preserves_existing_contents_backs_up_and_sets_modes(self):
        original = b"# existing users\nssh-rsa unrelated-test-key no-final-newline"
        self.path.write_bytes(original)
        self.install()
        self.assertEqual(self.path.read_bytes(), original + b"\n" + KEY.encode() + b"\n")
        backups = list(self.path.parent.glob("authorized_keys.labmon-before-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)

    def test_existing_restricted_key_not_duplicated_unrestricted(self):
        original = 'from="192.0.2.1",command="true" ' + KEY + "\n"
        self.path.write_text(original)
        self.install()
        self.install()
        self.assertEqual(self.path.read_text(), original)

    def test_symlink_and_hardlink_are_rejected(self):
        outside = self.root / "unrelated"
        outside.write_text("do not modify")
        self.path.symlink_to(outside)
        with self.assertRaises(OSError):
            self.install()
        self.path.unlink()
        os.link(outside, self.path)
        with self.assertRaises(SystemExit):
            self.install()
        self.assertEqual(outside.read_text(), "do not modify")


POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")


@unittest.skipUnless(POWERSHELL, "PowerShell is not installed")
class PowerShellInputValidationTests(unittest.TestCase):
    def run_cases(self, cases):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = root / "request.json"
            request.write_text(json.dumps({"script": str(PS_SCRIPT), "cases": cases}), encoding="utf-8")
            runner = root / "validate.ps1"
            runner.write_text(r'''$ErrorActionPreference = 'Stop'
$request = Get-Content -LiteralPath $args[0] -Raw | ConvertFrom-Json
$tokens = $null; $parseErrors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($request.script, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw ($parseErrors | Out-String) }
$functions = $ast.FindAll({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] }, $true)
foreach ($function in $functions) {
    if ($function.Name -in @('Read-Ed25519PublicKey', 'ConvertTo-SourceNetwork', 'Assert-KeyLocation')) { Invoke-Expression $function.Extent.Text }
}
$results = @()
foreach ($case in $request.cases) {
    try {
        if ($case.kind -eq 'source') { $value = ConvertTo-SourceNetwork $case.value }
        elseif ($case.kind -eq 'key') { $value = Read-Ed25519PublicKey $case.value; $value = 'valid' }
        else { Assert-KeyLocation @($case.value) ([bool]$case.admin); $value = 'valid' }
        $results += @{ok=$true; value=$value}
    } catch { $results += @{ok=$false; error=$_.Exception.Message} }
}
ConvertTo-Json -InputObject $results -Compress
''', encoding="utf-8")
            completed = subprocess.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(runner), str(request)], text=True, capture_output=True, timeout=30)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            return json.loads(completed.stdout)

    def test_sources_normalize_and_reject_unsafe_values(self):
        accepted = {"192.0.2.10": "192.0.2.10/32", "192.0.2.41/24": "192.0.2.0/24", "2001:db8::1": "2001:db8::1/128"}
        rejected = ["Any", "0.0.0.0/0", "::/0", "0.0.0.0", "::", "224.0.0.1", "ff02::1", "::ffff:192.0.2.1",
                    "192.0.2.1/33", "192.0.2.1;id", "1", "fe80::1%eth0"]
        values = list(accepted) + rejected
        results = self.run_cases([{"kind": "source", "value": value} for value in values])
        for value, result in zip(values, results):
            with self.subTest(value=value):
                self.assertEqual(result["ok"], value in accepted, result)
                if value in accepted:
                    self.assertEqual(result["value"], accepted[value])

    def test_key_structure_and_single_key_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            values = [KEY + "\n", "", KEY + "\n" + KEY, "ssh-ed25519 AAAA", "from=x " + KEY, KEY + "\x00"]
            paths = []
            for index, value in enumerate(values):
                path = Path(directory) / (str(index) + ".pub")
                path.write_text(value, encoding="utf-8")
                paths.append(path)
            results = self.run_cases([{"kind": "key", "value": str(path)} for path in paths])
            self.assertEqual([result["ok"] for result in results], [True, False, False, False, False, False])

    def test_custom_authorization_policy_is_not_silently_ignored(self):
        results = self.run_cases([
            {"kind": "location", "value": ["authorizedkeysfile __PROGRAMDATA__/ssh/administrators_authorized_keys"], "admin": True},
            {"kind": "location", "value": ["authorizedkeysfile .ssh/authorized_keys"], "admin": False},
            {"kind": "location", "value": ["authorizedkeysfile D:/managed/keys"], "admin": True},
            {"kind": "location", "value": ["authorizedkeysfile .ssh/authorized_keys", "pubkeyauthentication no"], "admin": False},
        ])
        self.assertEqual([result["ok"] for result in results], [True, True, False, False])


if __name__ == "__main__":
    unittest.main()
