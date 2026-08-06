#!/usr/bin/env python3
"""Smoke-test the local browser lifecycle in Google Chrome and Mozilla Firefox."""

from __future__ import annotations

import argparse
import json
import os
import signal
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable
from urllib.error import URLError
from urllib.request import Request, urlopen


SKILL_DIR = Path(__file__).resolve().parents[1]
REPORT_APP = SKILL_DIR / "scripts" / "report_app.py"


def executable(candidate: str | None, defaults: list[str]) -> str:
    choices = ([candidate] if candidate else []) + defaults
    for choice in choices:
        if not choice:
            continue
        resolved = shutil.which(choice)
        if resolved:
            return resolved
        path = Path(choice).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    raise FileNotFoundError(f"Browser executable not found. Checked: {', '.join(choices)}")


def available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_app(url: str, timeout: float = 10.0) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urlopen(f"{url}api/defaults", timeout=1.0) as response:
                return json.load(response)
        except (OSError, URLError, json.JSONDecodeError):
            time.sleep(0.1)
    raise TimeoutError(f"Local app did not become ready at {url}")


def stop_app(process: subprocess.Popen[str], url: str) -> None:
    if process.poll() is not None:
        return
    try:
        request = Request(
            f"{url}api/quit",
            data=b'{"client_id":"browser-smoke-cleanup"}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=2.0):
            pass
        process.wait(timeout=3.0)
    except (OSError, subprocess.TimeoutExpired):
        process.terminate()
        try:
            process.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3.0)


def stop_browser(process: subprocess.Popen[str], *, force: bool) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        command = ["taskkill", "/PID", str(process.pid), "/T"]
        if force:
            command.append("/F")
        subprocess.run(command, check=False, capture_output=True, text=True)
        return
    os.killpg(os.getpgid(process.pid), signal.SIGKILL if force else signal.SIGTERM)


def run_browser(name: str, command_builder: Callable[[Path, str], list[str]]) -> None:
    port = available_port()
    url = f"http://127.0.0.1:{port}/"
    server = subprocess.Popen(
        [sys.executable, str(REPORT_APP), "--port", str(port), "--no-browser"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        defaults = wait_for_app(url)
        with tempfile.TemporaryDirectory(prefix=f"monthly-ai-{name.lower()}-") as temporary_name:
            temporary = Path(temporary_name)
            (temporary / "profile").mkdir()
            stdout_path = temporary / "browser.stdout"
            stderr_path = temporary / "browser.stderr"
            stopped_after_render = False
            with stdout_path.open("w", encoding="utf-8") as stdout_file, stderr_path.open(
                "w", encoding="utf-8"
            ) as stderr_file:
                browser = subprocess.Popen(
                    command_builder(temporary, url),
                    stdout=stdout_file,
                    stderr=stderr_file,
                    text=True,
                    start_new_session=os.name != "nt",
                )
                try:
                    browser.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    stopped_after_render = True
                    stop_browser(browser, force=False)
                    try:
                        browser.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        stop_browser(browser, force=True)
                        browser.wait(timeout=5)
            stdout = stdout_path.read_text(encoding="utf-8", errors="replace")
            stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
            if browser.returncode and not stopped_after_render:
                detail = stderr.strip() or stdout.strip()
                raise RuntimeError(f"{name} failed to render the app: {detail}")
            if name == "Chrome":
                expected_version = str(defaults.get("version") or "")
                if 'id="quit-app"' not in stdout or expected_version not in stdout:
                    raise RuntimeError("Chrome did not render the lifecycle control and app version.")
            else:
                screenshot = Path(temporary_name) / "firefox.png"
                if not screenshot.is_file() or screenshot.stat().st_size < 10_000:
                    raise RuntimeError("Firefox did not produce a valid rendered-page screenshot.")
        try:
            server.wait(timeout=12.0)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"{name} closed, but the local app did not stop.") from exc
        if server.returncode:
            raise RuntimeError(f"The local app exited with {server.returncode} after {name} closed.")
        print(f"{name}: render and final-tab shutdown PASS")
    finally:
        stop_app(server, url)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chrome", help="Google Chrome executable path or command name.")
    parser.add_argument("--firefox", help="Mozilla Firefox executable path or command name.")
    args = parser.parse_args()

    chrome = executable(
        args.chrome,
        [
            "google-chrome",
            "google-chrome-stable",
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            str(Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "Google/Chrome/Application/chrome.exe"),
        ],
    )
    firefox = executable(
        args.firefox,
        [
            "firefox",
            "/Applications/Firefox.app/Contents/MacOS/firefox",
            str(Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "Mozilla Firefox/firefox.exe"),
        ],
    )

    run_browser(
        "Chrome",
        lambda temporary, url: [
            chrome,
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--virtual-time-budget=3000",
            f"--user-data-dir={temporary / 'profile'}",
            "--dump-dom",
            url,
        ],
    )
    run_browser(
        "Firefox",
        lambda temporary, url: [
            firefox,
            "--headless",
            "--no-remote",
            "--profile",
            str(temporary / "profile"),
            "--window-size",
            "1440,1200",
            "--screenshot",
            str(temporary / "firefox.png"),
            url,
        ],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
