#!/usr/bin/env python3
"""Build a self-contained Monthly AI Usage Report desktop package."""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import plistlib
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


APP_VERSION = "1.3.6"
APP_EXECUTABLE = "Monthly-AI-Usage-Report"
MAC_APP_NAME = "Monthly AI Usage Report.app"
MAC_BUNDLE_IDENTIFIER = "com.ericrennie.monthly-ai-usage-report"
PACKAGE_FOLDER = "Monthly-AI-Usage-Report-App"
SOURCE_ROOT = Path(__file__).resolve().parent.parent
REQUIRED_PATHS = (
    Path("assets/report_app.html"),
    Path("scripts/report_app.py"),
    Path("scripts/collect_codex_usage.py"),
    Path("scripts/bedrock_usage_check.py"),
    Path("tzdata"),
)

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
1. From the DMG, drag Monthly AI Usage Report to Applications, then open it.
   From the ZIP, open the single Monthly AI Usage Report app directly.
2. A Developer ID-signed and Apple-notarized release opens normally. An
   unsigned development release still requires one macOS approval for the app,
   but no shell launcher or second executable approval.
3. Only approve an unsigned build after verifying its release file's SHA-256 checksum and
   following your organization's software policy. If macOS blocks it, try once,
   then use System Settings > Privacy & Security > Open Anyway. Managed-device
   users should contact IT; never disable Gatekeeper or remove quarantine.
4. Closing the final report tab stops the local app after a short grace period.
   Use the page's Quit app button for an immediate shutdown. If the browser
   crashes, the heartbeat watchdog closes the app automatically.

Windows
-------
1. Double-click START-WINDOWS.bat.
2. Keep the command window open while using the browser app. Close it to stop.
3. If Microsoft Defender SmartScreen blocks the unsigned download, use an
   organization-approved signed build rather than bypassing security policy.
4. The local browser interface supports current Google Chrome and Mozilla
   Firefox on Windows, as well as the operating system's default browser.

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
    if sys.platform == "darwin":
        identity = os.environ.get("MACOS_CODESIGN_IDENTITY", "").strip()
        if identity:
            arguments.extend(["--codesign-identity", identity])
    if sys.platform == "win32":
        arguments.extend(["--hidden-import", "winreg"])
    return arguments


def macos_info_plist() -> dict[str, object]:
    """Return metadata for the single macOS application bundle."""
    return {
        "CFBundleDevelopmentRegion": "en",
        "CFBundleDisplayName": "Monthly AI Usage Report",
        "CFBundleExecutable": APP_EXECUTABLE,
        "CFBundleIdentifier": MAC_BUNDLE_IDENTIFIER,
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleName": "Monthly AI Usage Report",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": APP_VERSION,
        "CFBundleVersion": APP_VERSION,
        "LSApplicationCategoryType": "public.app-category.productivity",
        "LSMinimumSystemVersion": "12.0",
        "NSHighResolutionCapable": True,
        "NSHumanReadableCopyright": "Copyright 2026 Eric Rennie",
    }


def run_checked(args: list[str], *, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(args, check=False, capture_output=True, text=True, timeout=timeout)
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit {completed.returncode}"
        raise RuntimeError(f"{' '.join(args[:2])} failed: {detail}")
    return completed


def create_macos_app(executable: Path, release_dir: Path) -> tuple[Path, Path]:
    """Wrap the console-capable frozen runtime in one Finder-launchable app."""
    app = release_dir / MAC_APP_NAME
    contents = app / "Contents"
    macos = contents / "MacOS"
    resources = contents / "Resources"
    macos.mkdir(parents=True)
    resources.mkdir()
    target_executable = macos / APP_EXECUTABLE
    shutil.copy2(executable, target_executable)
    target_executable.chmod(
        target_executable.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    )
    with (contents / "Info.plist").open("wb") as handle:
        plistlib.dump(macos_info_plist(), handle, fmt=plistlib.FMT_XML, sort_keys=True)

    identity = os.environ.get("MACOS_CODESIGN_IDENTITY", "").strip() or "-"
    codesign = ["codesign", "--force", "--deep", "--sign", identity]
    if identity != "-":
        codesign[1:1] = ["--options", "runtime", "--timestamp"]
    codesign.append(str(app))
    run_checked(codesign)
    run_checked(["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(app)])
    return app, target_executable


def create_macos_dmg(app: Path, output_dir: Path, tag: str) -> Path:
    """Create a drag-to-Applications disk image and optionally notarize it."""
    dmg = output_dir / f"monthly-ai-usage-report-app-v{APP_VERSION}-{tag}.dmg"
    if dmg.exists():
        dmg.unlink()
    with tempfile.TemporaryDirectory(prefix="monthly-ai-dmg-") as staging_name:
        staging = Path(staging_name)
        shutil.copytree(app, staging / app.name, symlinks=True)
        os.symlink("/Applications", staging / "Applications")
        run_checked(
            [
                "hdiutil",
                "create",
                "-volname",
                "Monthly AI Usage Report",
                "-srcfolder",
                str(staging),
                "-ov",
                "-format",
                "UDZO",
                str(dmg),
            ]
        )

    identity = os.environ.get("MACOS_CODESIGN_IDENTITY", "").strip()
    if identity:
        run_checked(["codesign", "--force", "--sign", identity, "--timestamp", str(dmg)])

    notary_profile = os.environ.get("MACOS_NOTARY_KEYCHAIN_PROFILE", "").strip()
    if notary_profile:
        if not identity:
            raise RuntimeError("MACOS_NOTARY_KEYCHAIN_PROFILE requires MACOS_CODESIGN_IDENTITY.")
        run_checked(
            ["xcrun", "notarytool", "submit", str(dmg), "--keychain-profile", notary_profile, "--wait"],
            timeout=1800,
        )
        run_checked(["xcrun", "stapler", "staple", str(dmg)])
        run_checked(["xcrun", "stapler", "validate", str(dmg)])
    return dmg


def notarize_macos_app(app: Path) -> None:
    """Notarize and staple the app before placing it in ZIP and DMG containers."""
    notary_profile = os.environ.get("MACOS_NOTARY_KEYCHAIN_PROFILE", "").strip()
    if not notary_profile:
        return
    if not os.environ.get("MACOS_CODESIGN_IDENTITY", "").strip():
        raise RuntimeError("MACOS_NOTARY_KEYCHAIN_PROFILE requires MACOS_CODESIGN_IDENTITY.")
    with tempfile.TemporaryDirectory(prefix="monthly-ai-notary-") as temporary_name:
        submission = Path(temporary_name) / "Monthly-AI-Usage-Report.zip"
        run_checked(["ditto", "-c", "-k", "--keepParent", str(app), str(submission)])
        run_checked(
            [
                "xcrun",
                "notarytool",
                "submit",
                str(submission),
                "--keychain-profile",
                notary_profile,
                "--wait",
            ],
            timeout=1800,
        )
    run_checked(["xcrun", "stapler", "staple", str(app)])
    run_checked(["xcrun", "stapler", "validate", str(app)])


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


def build(output_dir: Path) -> tuple[Path, Path, Path | None]:
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
        if sys.platform == "darwin":
            app, target_executable = create_macos_app(built_executable, release_dir)
        else:
            app = None
            target_executable = release_dir / executable_name
            shutil.copy2(built_executable, target_executable)

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
    write_text(release_dir / "START-HERE.txt", DESKTOP_START_HERE)
    if (root / "README.md").is_file():
        shutil.copy2(root / "README.md", release_dir / "README.md")
    write_text(release_dir / "VERSION.txt", f"Version: {APP_VERSION}\nPlatform: {tag}\n")

    if app is not None:
        notarize_macos_app(app)
        dmg = create_macos_dmg(app, output_dir, tag)
    else:
        dmg = None
    archive_tree(release_dir, archive)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    write_text(archive.with_suffix(archive.suffix + ".sha256"), f"{digest}  {archive.name}\n")
    if dmg is not None:
        dmg_digest = hashlib.sha256(dmg.read_bytes()).hexdigest()
        write_text(dmg.with_suffix(dmg.suffix + ".sha256"), f"{dmg_digest}  {dmg.name}\n")
    return release_dir, archive, dmg


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=SOURCE_ROOT / "dist")
    args = parser.parse_args()
    try:
        release_dir, archive, dmg = build(args.output_dir)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    print(f"Desktop folder: {release_dir}")
    print(f"Desktop archive: {archive}")
    if dmg is not None:
        print(f"macOS disk image: {dmg}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
