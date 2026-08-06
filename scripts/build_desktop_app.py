#!/usr/bin/env python3
"""Build a self-contained Monthly AI Usage Report desktop package."""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


APP_VERSION = "1.3.3"
APP_EXECUTABLE = "Monthly-AI-Usage-Report"
PACKAGE_FOLDER = "Monthly-AI-Usage-Report-App"
SOURCE_ROOT = Path(__file__).resolve().parent.parent
REQUIRED_PATHS = (
    Path("assets/report_app.html"),
    Path("scripts/report_app.py"),
    Path("scripts/collect_codex_usage.py"),
    Path("scripts/bedrock_usage_check.py"),
    Path("tzdata"),
)

MAC_LAUNCHER = f"""#!/bin/zsh
set -eu

app_dir="${{0:A:h}}"
exec "$app_dir/{APP_EXECUTABLE}"
"""

WINDOWS_LAUNCHER = rf"""@echo off
setlocal
cd /d "%~dp0"
"{APP_EXECUTABLE}.exe"
"""

DESKTOP_START_HERE = """MONTHLY AI USAGE REPORT — DESKTOP APP

No Python installation is required. This package includes a private Python
runtime used only by the app; it does not install or change system Python.

macOS
-----
1. Double-click START-MAC.command.
2. If macOS says it cannot verify the launcher, click Done. Do not click Move
   to Trash if you intend to verify and run this download.
3. Open Apple menu > System Settings > Privacy & Security, scroll to Security,
   and click Open Anyway beside START-MAC.command. This option is normally
   available for about one hour after the blocked launch.
4. Authenticate, confirm Open, and keep the Terminal window open while using
   the browser app. Close the Terminal window to stop the app.
5. Only use Open Anyway after verifying the release ZIP's SHA-256 checksum. If
   it is unavailable on a managed Mac, contact IT; do not disable Gatekeeper or
   remove quarantine attributes.

Windows
-------
1. Double-click START-WINDOWS.bat.
2. Keep the command window open while using the browser app. Close it to stop.
3. If Microsoft Defender SmartScreen blocks the unsigned download, use an
   organization-approved signed build rather than bypassing security policy.

Double-click the launcher and use the browser address it opens automatically.
Do not type a saved localhost address: 127.0.0.1:8765 is only the preferred
default. If it is occupied, the app reopens the same version or selects another
free local port and opens that exact address. The app is reachable only from
this device.
AWS credentials remain in memory for a single collector run and are not saved.
HTTPS verification uses the certificate authorities trusted by macOS Keychain
or Windows. Organization-managed root certificates therefore work without
editing the app. Administrators may instead set SSL_CERT_FILE or AWS_CA_BUNDLE
to an approved Privacy-Enhanced Mail (PEM) certificate bundle before launch.
Never disable certificate verification.
See README.md for full collection instructions and data boundaries.
"""


def write_text(path: Path, content: str, *, newline: str = "\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline=newline) as handle:
        handle.write(content)


def platform_tag() -> str:
    machine = platform.machine().lower().replace("amd64", "x86_64").replace("aarch64", "arm64")
    if sys.platform == "darwin":
        return f"macos-{machine}"
    if sys.platform == "win32":
        return f"windows-{machine}"
    if sys.platform.startswith("linux"):
        return f"linux-{machine}"
    raise RuntimeError(f"Unsupported build platform: {sys.platform}")


def verify_sources(root: Path) -> None:
    missing = [str(path) for path in REQUIRED_PATHS if not (root / path).exists()]
    if missing:
        raise FileNotFoundError(f"Missing required app content: {', '.join(missing)}")


def pyinstaller_arguments(root: Path, temporary: Path) -> list[str]:
    separator = os.pathsep
    arguments = [
        str(root / "scripts" / "report_app.py"),
        "--name",
        APP_EXECUTABLE,
        "--onefile",
        "--console",
        "--clean",
        "--noconfirm",
        "--noupx",
        "--distpath",
        str(temporary / "dist"),
        "--workpath",
        str(temporary / "build"),
        "--specpath",
        str(temporary / "spec"),
        "--add-data",
        f"{root / 'assets' / 'report_app.html'}{separator}assets",
        "--add-data",
        f"{root / 'scripts' / 'collect_codex_usage.py'}{separator}scripts",
        "--add-data",
        f"{root / 'scripts' / 'bedrock_usage_check.py'}{separator}scripts",
        "--add-data",
        f"{root / 'tzdata'}{separator}tzdata",
        "--hidden-import",
        "configparser",
        "--hidden-import",
        "hashlib",
        "--hidden-import",
        "hmac",
        "--hidden-import",
        "urllib.error",
        "--hidden-import",
        "urllib.request",
        "--hidden-import",
        "truststore",
    ]
    if sys.platform == "win32":
        arguments.extend(["--hidden-import", "winreg"])
    return arguments


def archive_tree(source: Path, destination: Path) -> None:
    if destination.exists():
        destination.unlink()
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(source.rglob("*")):
            if not path.is_file():
                continue
            archive_name = path.relative_to(source.parent)
            info = zipfile.ZipInfo.from_file(path, archive_name)
            info.compress_type = zipfile.ZIP_DEFLATED
            with path.open("rb") as handle:
                archive.writestr(info, handle.read(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)


def build(output_dir: Path) -> tuple[Path, Path]:
    root = SOURCE_ROOT
    verify_sources(root)
    try:
        import PyInstaller.__main__ as pyinstaller
        import truststore  # noqa: F401 -- required inside the frozen runtime
    except ImportError as exc:
        raise RuntimeError(
            "PyInstaller and truststore are required only on the build machine. "
            "Install requirements-build.txt first."
        ) from exc

    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    tag = platform_tag()
    release_dir = output_dir / f"{PACKAGE_FOLDER}-{tag}"
    archive = output_dir / f"monthly-ai-usage-report-app-v{APP_VERSION}-{tag}.zip"
    if release_dir.exists():
        shutil.rmtree(release_dir)
    release_dir.mkdir()

    with tempfile.TemporaryDirectory(prefix="monthly-ai-desktop-build-") as temporary_name:
        temporary = Path(temporary_name)
        pyinstaller.run(pyinstaller_arguments(root, temporary))
        executable_name = APP_EXECUTABLE + (".exe" if sys.platform == "win32" else "")
        built_executable = temporary / "dist" / executable_name
        if not built_executable.is_file():
            raise RuntimeError(f"PyInstaller did not create {built_executable}")
        target_executable = release_dir / executable_name
        shutil.copy2(built_executable, target_executable)
        if sys.platform != "win32":
            target_executable.chmod(target_executable.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    completed = subprocess.run(
        [str(target_executable), "--self-test"],
        check=False,
        capture_output=True,
        text=True,
        timeout=90,
    )
    if completed.returncode:
        raise RuntimeError(f"Frozen app self-test failed: {completed.stderr or completed.stdout}")

    if sys.platform == "win32":
        write_text(release_dir / "START-WINDOWS.bat", WINDOWS_LAUNCHER, newline="\r\n")
    else:
        launcher = release_dir / "START-MAC.command"
        write_text(launcher, MAC_LAUNCHER)
        launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    write_text(release_dir / "START-HERE.txt", DESKTOP_START_HERE)
    if (root / "README.md").is_file():
        shutil.copy2(root / "README.md", release_dir / "README.md")
    write_text(release_dir / "VERSION.txt", f"Version: {APP_VERSION}\nPlatform: {tag}\n")

    archive_tree(release_dir, archive)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    write_text(archive.with_suffix(archive.suffix + ".sha256"), f"{digest}  {archive.name}\n")
    return release_dir, archive


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=SOURCE_ROOT / "dist")
    args = parser.parse_args()
    try:
        release_dir, archive = build(args.output_dir)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    print(f"Desktop folder: {release_dir}")
    print(f"Desktop archive: {archive}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
