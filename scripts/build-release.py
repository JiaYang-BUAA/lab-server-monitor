"""Build public source/SSH ZIPs from explicit allowlists, never runtime folders.

Usage: python scripts/build-release.py --check
       python scripts/build-release.py --deny-file /private/release-deny.txt

The optional deny file contains one private deployment marker per line. It stays
outside both packages. Use it to reject actual site addresses/hostnames without
hard-coding private deployment data into public source code.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE_REQUIRED = (
    "README.md", "CONTRACT.md", "LICENSE", ".gitignore", ".gitattributes",
    "web/index.html", "web/app.js", "web/styles.css", "assets/windows-dependencies.json",
    "scripts/install.ps1", "scripts/render-config.py", "scripts/download-dependencies.ps1",
    "scripts/collect-windows.ps1", "scripts/connect-server.py", "scripts/render-dashboard.py",
    "scripts/install-linux.sh", "scripts/configure-linux.py", "scripts/setup-ssh.ps1",
    "scripts/setup-ssh.sh", "scripts/build-release.py", "scripts/check-capacity.py", "docs/deployment.md",
    "docs/security.md", "docs/release-validation.md", "docs/linux-install.md",
    "docs/linux-collector.md", "docs/ssh-setup.md", "docs/public-access.md", "docs/codex-preflight.md",
)
SOURCE_GLOBS = ("labmon/*.py", "tests/*.py")
SOURCE_OPTIONAL = (".github/workflows/test.yml",)
SSH_REQUIRED = ("LICENSE", "scripts/setup-ssh.ps1", "scripts/setup-ssh.sh", "docs/ssh-setup.md")
ARCHIVES = (
    ("lab-server-monitor-source.zip", "lab-server-monitor"),
    ("lab-monitor-ssh-setup.zip", "lab-monitor-ssh-setup"),
)
SENSITIVE_PATTERNS = (
    ("private-key block", re.compile(rb"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----\s+[A-Za-z0-9+/=\r\n]{64,}\s*-----END (?:[A-Z0-9 ]+ )?PRIVATE KEY-----")),
    ("embedded SSH public key", re.compile(rb"(?:ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp\d+) [A-Za-z0-9+/]{60,}={0,2}")),
    ("Windows deployment hostname", re.compile(rb"\bDESKTOP-(?!(?:EXAMPLE|TEST)(?:\b|_))[A-Z0-9]{5,}\b", re.I)),
)


class ReleaseError(ValueError):
    """A release preflight failed before any archive was replaced."""


def read_deny_file(path: Path | None) -> tuple[bytes, ...]:
    if path is None:
        return ()
    markers = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        value = line.strip()
        if value and not value.startswith("#"):
            if len(value) < 4:
                raise ReleaseError("Deny markers must contain at least four characters.")
            markers.append(value.encode("utf-8"))
    if not markers:
        raise ReleaseError("The supplied deny file is empty.")
    return tuple(markers)


def read_public_file(root: Path, relative: str) -> bytes:
    path = root / relative
    if not path.is_file():
        raise ReleaseError("Missing required public file: " + relative)
    for part in (path, *path.parents):
        if part == root:
            break
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ReleaseError("Public file path contains a symlink/junction: " + relative)
    if not path.resolve().is_relative_to(root):
        raise ReleaseError("Public file escapes source root: " + relative)
    if path.stat().st_nlink > 1:
        raise ReleaseError("Public file is hard-linked; copy it to a regular file first: " + relative)
    return path.read_bytes()


def scan_payload(files: dict[str, bytes], deny_markers: tuple[bytes, ...] = ()) -> None:
    failures = []
    for name, content in files.items():
        for label, pattern in SENSITIVE_PATTERNS:
            if pattern.search(content):
                failures.append(name + ": " + label)
        if any(marker.lower() in content.lower() for marker in deny_markers):
            failures.append(name + ": private deployment marker")
    if failures:
        raise ReleaseError("Public package privacy scan failed:\n" + "\n".join(failures))


def collect_payloads(root: Path, deny_markers: tuple[bytes, ...] = ()) -> tuple[dict[str, bytes], dict[str, bytes]]:
    root = root.resolve(strict=True)
    names = set(SOURCE_REQUIRED)
    for pattern in SOURCE_GLOBS:
        matched = sorted(root.glob(pattern))
        if not matched:
            raise ReleaseError("Required source group is empty: " + pattern)
        names.update(path.relative_to(root).as_posix() for path in matched)
    names.update(name for name in SOURCE_OPTIONAL if (root / name).exists())
    source = {name: read_public_file(root, name) for name in sorted(names)}
    scan_payload(source, deny_markers)
    ssh = {name: source[name] for name in SSH_REQUIRED}
    return source, ssh


def manifest_for(files: dict[str, bytes]) -> bytes:
    manifest = [{"file": name, "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
                for name, content in sorted(files.items())]
    return (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def write_archive(path: Path, prefix: str, files: dict[str, bytes]) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=path.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        members = {**files, "package-manifest.json": manifest_for(files)}
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for name, content in sorted(members.items()):
                entry = zipfile.ZipInfo(prefix + "/" + name, date_time=(1980, 1, 1, 0, 0, 0))
                entry.create_system = 3
                entry.external_attr = (stat.S_IFREG | (0o755 if name.endswith(".sh") else 0o644)) << 16
                entry.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(entry, content, compress_type=zipfile.ZIP_DEFLATED, compresslevel=6)
        with zipfile.ZipFile(temporary) as archive:
            if archive.testzip() is not None:
                raise ReleaseError("ZIP verification failed: " + path.name)
        digest = hashlib.sha256(temporary.read_bytes()).hexdigest()
        os.replace(temporary, path)
        path.with_suffix(path.suffix + ".sha256").write_text(digest + "  " + path.name + "\n", encoding="ascii")
        return {"archive": str(path), "files": len(files), "bytes": path.stat().st_size, "sha256": digest}
    finally:
        temporary.unlink(missing_ok=True)


def build_release(root: Path = ROOT, out: Path | None = None, deny_file: Path | None = None, check: bool = False) -> list[dict]:
    root = root.resolve(strict=True)
    payloads = collect_payloads(root, read_deny_file(deny_file))
    # Snapshot and scan all source files before creating either archive.
    if check:
        return [{"archive": name, "files": len(files), "status": "checked; no archive written"}
                for (name, _), files in zip(ARCHIVES, payloads)]
    destination = out if out is not None else root / "dist"
    return [write_archive(destination / name, prefix, files)
            for (name, prefix), files in zip(ARCHIVES, payloads)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help="Source checkout root")
    parser.add_argument("--out", type=Path, help="Output folder (default: ROOT/dist)")
    parser.add_argument("--deny-file", type=Path, help="Private file with forbidden deployment markers, one per line; never packaged")
    parser.add_argument("--check", action="store_true", help="Validate allowlist/privacy without writing archives")
    args = parser.parse_args()
    try:
        result = build_release(args.root, args.out, args.deny_file, args.check)
    except (ReleaseError, OSError) as exc:
        parser.exit(1, str(exc) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
