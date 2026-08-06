#!/usr/bin/env python3
"""Run the Monthly AI Usage Report guided setup app on localhost.

The server uses only the Python standard library, binds to loopback, never logs
request bodies, and does not persist AWS credentials or generated reports.
"""

from __future__ import annotations

import argparse
import ast
import configparser
import errno
import json
import os
import re
import runpy
import subprocess
import sys
import threading
import webbrowser
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones, reset_tzpath


if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    SKILL_DIR = Path(sys._MEIPASS).resolve()
else:
    SKILL_DIR = Path(__file__).resolve().parent.parent
HTML_PATH = SKILL_DIR / "assets" / "report_app.html"
CODEX_COLLECTOR = SKILL_DIR / "scripts" / "collect_codex_usage.py"
BUNDLED_TZDATA = SKILL_DIR / "tzdata"
if BUNDLED_TZDATA.is_dir():
    os.environ["PYTHONTZPATH"] = str(BUNDLED_TZDATA)
    reset_tzpath((str(BUNDLED_TZDATA),))

BUNDLED_BEDROCK_SCRIPT = SKILL_DIR / "scripts" / "bedrock_usage_check.py"
MAX_REQUEST_BYTES = 256_000
LAMBDA_HOST = re.compile(r"^[a-z0-9]+\.lambda-url\.[a-z0-9-]+\.on\.aws$")
DEFAULT_CIRCUIT_URL = "https://circuit.cisco.com/app/usage-dashboard"
APP_VERSION = "1.3.2"
RELEASE_DATE = "2026-08-05"


def python_script_command(script: Path, *arguments: str) -> list[str]:
    """Run a collector with source Python or the private frozen runtime."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--run-python-script", str(script), *arguments]
    return [sys.executable, str(script), *arguments]


def run_python_script_mode() -> None:
    """Execute an approved collector inside the bundled Python runtime."""
    if len(sys.argv) < 3:
        raise SystemExit("A Python script path is required.")
    script = Path(sys.argv[2]).expanduser().resolve()
    if not script.is_file():
        raise SystemExit(f"Python script not found: {script}")
    sys.argv = [str(script), *sys.argv[3:]]
    runpy.run_path(str(script), run_name="__main__")


def existing_app_version(port: int) -> str | None:
    """Return the version of an app already listening on the requested port."""
    if port <= 0:
        return None
    request = Request(f"http://127.0.0.1:{port}/api/defaults", headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=1) as response:  # noqa: S310 -- fixed loopback URL
            server = response.headers.get("Server", "")
            if response.status != HTTPStatus.OK or not server.startswith("MonthlyAIUsageReport/"):
                return None
    except OSError:
        return None
    product = server.split(None, 1)[0]
    return product.partition("/")[2] or None


def local_timezone_name() -> str:
    configured = os.environ.get("TZ")
    if configured:
        try:
            ZoneInfo(configured)
            return configured
        except ZoneInfoNotFoundError:
            pass

    localtime = Path("/etc/localtime")
    if localtime.is_symlink() and "/zoneinfo/" in str(localtime.resolve()):
        candidate = str(localtime.resolve()).split("/zoneinfo/", 1)[1]
        try:
            ZoneInfo(candidate)
            return candidate
        except ZoneInfoNotFoundError:
            pass
    return "UTC"


def previous_month(now: datetime) -> str:
    year = now.year if now.month > 1 else now.year - 1
    month = now.month - 1 if now.month > 1 else 12
    return f"{year:04d}-{month:02d}"


def validate_month(value: str) -> str:
    try:
        datetime.strptime(value, "%Y-%m")
    except ValueError as exc:
        raise ValueError("Reporting month must use YYYY-MM.") from exc
    return value


def validate_date(value: str, label: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} must use YYYY-MM-DD.") from exc


def previous_month_dates(now: datetime) -> tuple[date, date]:
    year = now.year if now.month > 1 else now.year - 1
    month = now.month - 1 if now.month > 1 else 12
    start = date(year, month, 1)
    end = date(now.year, now.month, 1) - timedelta(days=1)
    return start, end


def format_period_label(start: date, end: date) -> str:
    if start == end:
        return f"{start.strftime('%B')} {start.day}, {start.year}"
    next_day = end + timedelta(days=1)
    if start.day == 1 and next_day.day == 1 and start.month == end.month:
        return start.strftime("%B %Y")
    if start.year == end.year and start.month == end.month:
        return f"{start.strftime('%B')} {start.day}-{end.day}, {start.year}"
    return f"{start.isoformat()} to {end.isoformat()}"


def resolve_period(payload: dict[str, Any], timezone: ZoneInfo) -> dict[str, Any]:
    now = datetime.now(timezone)
    if payload.get("start_date") or payload.get("end_date"):
        start = validate_date(str(payload.get("start_date") or ""), "Start date")
        end = validate_date(str(payload.get("end_date") or ""), "End date")
    else:
        month = validate_month(str(payload.get("month") or previous_month(now)))
        parsed = datetime.strptime(month, "%Y-%m")
        start = date(parsed.year, parsed.month, 1)
        next_month = date(parsed.year + (1 if parsed.month == 12 else 0), parsed.month % 12 + 1, 1)
        end = next_month - timedelta(days=1)
    if end < start:
        raise ValueError("End date must be on or after start date.")
    days = (end - start).days + 1
    if days > 366:
        raise ValueError("Reporting ranges cannot exceed 366 days.")
    previous_start, previous_end = previous_month_dates(now)
    return {
        "start_date": start,
        "end_date": end,
        "days": days,
        "label": format_period_label(start, end),
        "slug": start.isoformat() if start == end else f"{start.isoformat()}-to-{end.isoformat()}",
        "is_previous_calendar_month": start == previous_start and end == previous_end,
        "ends_today": end == now.date(),
    }


def validate_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown IANA timezone: {value}") from exc
    return value


def resolve_local_path(value: str, default: Path) -> Path:
    path = Path(value or str(default)).expanduser()
    return path if path.is_absolute() else (SKILL_DIR / path).resolve()


def bedrock_script_details() -> tuple[Path, bool, str]:
    """Return the first valid Bedrock collector from predictable local locations."""
    configured = os.environ.get("BEDROCK_USAGE_SCRIPT", "").strip()
    configured_path = Path(configured).expanduser() if configured else None
    if configured_path and configured_path.is_file():
        return configured_path.resolve(), True, "environment"

    conventional = Path.home() / ".bedrock" / "bedrock_usage_check.py"
    if conventional.is_file():
        return conventional.resolve(), True, "conventional"

    if BUNDLED_BEDROCK_SCRIPT.is_file():
        return BUNDLED_BEDROCK_SCRIPT.resolve(), True, "bundled"

    if configured_path:
        return configured_path, False, "environment_missing"
    return conventional, False, "missing"


def codex_sessions_details() -> tuple[str, bool, int]:
    """Return the conventional local Codex sessions path and discovery status."""
    sessions = Path.home() / ".codex" / "sessions"
    if not sessions.is_dir():
        return str(sessions), False, 0
    try:
        count = sum(1 for path in sessions.rglob("*.jsonl") if path.is_file())
    except OSError:
        count = 0
    return str(sessions), True, count


def select_local_path(kind: str) -> str | None:
    if kind not in {"file", "directory"}:
        raise ValueError("Path selection kind must be file or directory.")
    if sys.platform == "darwin":
        expression = (
            'POSIX path of (choose file with prompt "Select the Bedrock collector script")'
            if kind == "file"
            else 'POSIX path of (choose folder with prompt "Select the Codex sessions folder")'
        )
        completed = subprocess.run(
            ["osascript", "-e", expression], check=False, capture_output=True, text=True
        )
        if completed.returncode:
            return None
        return completed.stdout.strip() or None
    if sys.platform == "win32":
        if kind == "file":
            script = (
                "Add-Type -AssemblyName System.Windows.Forms; "
                "$d=New-Object System.Windows.Forms.OpenFileDialog; "
                "$d.Filter='Python scripts (*.py)|*.py|All files (*.*)|*.*'; "
                "if($d.ShowDialog() -eq 'OK'){[Console]::Write($d.FileName)}"
            )
        else:
            script = (
                "Add-Type -AssemblyName System.Windows.Forms; "
                "$d=New-Object System.Windows.Forms.FolderBrowserDialog; "
                "if($d.ShowDialog() -eq 'OK'){[Console]::Write($d.SelectedPath)}"
            )
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-STA", "-Command", script],
            check=False,
            capture_output=True,
            text=True,
        )
        return completed.stdout.strip() or None

    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        selected = (
            filedialog.askopenfilename(filetypes=[("Python scripts", "*.py"), ("All files", "*")])
            if kind == "file"
            else filedialog.askdirectory()
        )
        root.destroy()
        return selected or None
    except (ImportError, OSError, RuntimeError) as exc:
        raise ValueError("Native path selection is unavailable; enter the path manually.") from exc


def available_profiles() -> list[str]:
    credentials = Path.home() / ".aws" / "credentials"
    parser = configparser.RawConfigParser()
    try:
        parser.read(credentials)
    except (OSError, configparser.Error):
        return []
    return sorted(section for section in parser.sections() if section != "DEFAULT")


def configured_lambda_url(script: Path) -> str | None:
    """Read a literal DEFAULT_LAMBDA_URL without importing the collector."""
    try:
        tree = ast.parse(script.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeError):
        return None
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == "DEFAULT_LAMBDA_URL" for target in node.targets):
            continue
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            return node.value.value
    return None


def validate_lambda_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname or not LAMBDA_HOST.fullmatch(parsed.hostname):
        raise ValueError("Lambda URL must be an HTTPS AWS Lambda Function URL (*.lambda-url.<region>.on.aws).")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Lambda URL must not include credentials, query parameters, or fragments.")
    return value


def run_json_command(
    args: list[str], *, env: dict[str, str] | None = None, timeout: int = 120
) -> tuple[int, dict[str, Any] | None, str]:
    try:
        completed = subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return 124, None, "Collector timed out."
    except OSError as exc:
        return 127, None, str(exc)

    message = completed.stderr.strip()
    if completed.returncode:
        return completed.returncode, None, message or completed.stdout.strip()
    try:
        return 0, json.loads(completed.stdout), message
    except json.JSONDecodeError:
        return 65, None, "Collector returned invalid JSON."


def failure(category: str, message: str, remediation: str, retrieved: str) -> dict[str, Any]:
    return {
        "status": "unavailable",
        "retrieved_at": retrieved,
        "failure": {
            "category": category,
            "message": message,
            "remediation": remediation,
        },
    }


def bedrock_failure(message: str, retrieved: str) -> dict[str, Any]:
    lower = message.lower()
    if "403" in lower or "access denied" in lower:
        return failure(
            "access_denied_403",
            message,
            "Confirm the AWS identity belongs to the bedrock-access IAM group.",
            retrieved,
        )
    if "expiredtoken" in lower or "expired token" in lower:
        return failure(
            "expired_credentials",
            message,
            "Refresh AWS single sign-on and retry with the intended profile.",
            retrieved,
        )
    if "credentials not found" in lower or "profile" in lower and "not found" in lower:
        return failure(
            "missing_credentials",
            message,
            "Refresh AWS single sign-on, select a valid profile, or enter temporary credentials.",
            retrieved,
        )
    if "could not verify aws identity" in lower:
        return failure(
            "identity_verification_failed",
            message,
            "Check network access and refresh the selected AWS profile or temporary credentials.",
            retrieved,
        )
    if "timed out" in lower:
        return failure("timeout", message, "Check network access and retry.", retrieved)
    return failure("collector_error", message, "Review the collector output and AWS configuration.", retrieved)


def collect_bedrock(
    config: dict[str, Any], timezone: ZoneInfo, period: dict[str, Any]
) -> dict[str, Any]:
    retrieved = datetime.now(timezone).isoformat()
    if not config.get("enabled", True):
        return failure("disabled", "Bedrock collection was disabled.", "Enable it to include Bedrock usage.", retrieved)
    if not config.get("approved_egress"):
        return failure(
            "egress_not_approved",
            "Signed egress to the configured Lambda Function URL was not approved.",
            "Verify the Lambda owner, then check the explicit approval box in the wizard.",
            retrieved,
        )

    default_script, _, _ = bedrock_script_details()
    script = resolve_local_path(str(config.get("script") or ""), default_script)
    if not script.is_file():
        return failure(
            "collector_missing",
            f"Collector not found: {script}",
            "Set BEDROCK_USAGE_SCRIPT or select the correct collector path.",
            retrieved,
        )

    days = int(period["days"])
    args = python_script_command(script, "-d", str(days), "--json")
    profile = str(config.get("profile") or "").strip()
    access_key = str(config.get("access_key_id") or "").strip()
    secret_key = str(config.get("secret_access_key") or "")
    session_token = str(config.get("session_token") or "")
    override_url = str(config.get("lambda_url") or "").strip()

    if profile and (access_key or secret_key or session_token):
        return failure(
            "credential_conflict",
            "Choose either an AWS profile or temporary credentials, not both.",
            "Clear one credential method and retry.",
            retrieved,
        )
    if bool(access_key) != bool(secret_key):
        return failure(
            "incomplete_credentials",
            "Temporary access-key ID and secret access key must both be provided.",
            "Complete both fields or use an AWS profile.",
            retrieved,
        )
    if profile:
        args.extend(["--profile", profile])
    if override_url:
        try:
            args.extend(["--url", validate_lambda_url(override_url)])
        except ValueError as exc:
            return failure("invalid_lambda_url", str(exc), "Enter a verified AWS Lambda Function URL.", retrieved)

    child_env = os.environ.copy()
    if access_key:
        for key in (
            "AWS_PROFILE",
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
            "AWS_SECURITY_TOKEN",
        ):
            child_env.pop(key, None)
        child_env["AWS_ACCESS_KEY_ID"] = access_key
        child_env["AWS_SECRET_ACCESS_KEY"] = secret_key
        if session_token:
            child_env["AWS_SESSION_TOKEN"] = session_token

    code, data, message = run_json_command(args, env=child_env)
    if code or data is None:
        return bedrock_failure(message, retrieved)
    return {
        "status": "verified",
        "source": "personal Bedrock usage collector",
        "window": f"rolling {days} day{'s' if days != 1 else ''}",
        "window_end": retrieved,
        "matches_selected_period": bool(period["ends_today"]),
        "retrieved_at": retrieved,
        "data": data,
    }


def collect_codex(
    config: dict[str, Any], period: dict[str, Any], timezone_name: str
) -> dict[str, Any]:
    timezone = ZoneInfo(timezone_name)
    retrieved = datetime.now(timezone).isoformat()
    sessions_dir = resolve_local_path(
        str(config.get("sessions_dir") or "~/.codex/sessions"),
        Path.home() / ".codex" / "sessions",
    )
    args = python_script_command(
        CODEX_COLLECTOR,
        "--start-date",
        period["start_date"].isoformat(),
        "--end-date",
        period["end_date"].isoformat(),
        "--timezone",
        timezone_name,
        "--sessions-dir",
        str(sessions_dir),
    )
    code, data, message = run_json_command(args, timeout=180)
    if code or data is None:
        return failure(
            "collector_error",
            message,
            "Confirm the Codex sessions directory and reporting timezone.",
            retrieved,
        )
    return {
        "status": "verified",
        "source": "local Codex session telemetry",
        "retrieved_at": data.get("retrieved_at", retrieved),
        "window": f"{period['label']} exact local range",
        "data": data,
    }


def parse_integer(value: Any, label: str) -> int:
    text = str(value if value is not None else "").replace(",", "").strip()
    if not text:
        raise ValueError(f"{label} is required.")
    try:
        parsed = int(text)
    except ValueError as exc:
        raise ValueError(f"{label} must be a whole number.") from exc
    if parsed < 0:
        raise ValueError(f"{label} cannot be negative.")
    return parsed


def parse_money(value: Any, label: str) -> Decimal | None:
    text = str(value if value is not None else "").replace("$", "").replace(",", "").strip()
    if not text:
        return None
    try:
        parsed = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"{label} must be a decimal amount.") from exc
    if parsed < 0:
        raise ValueError(f"{label} cannot be negative.")
    return parsed


def collect_circuit(
    config: dict[str, Any], period: dict[str, Any], timezone: ZoneInfo
) -> dict[str, Any]:
    retrieved = datetime.now(timezone).isoformat()
    mode = str(config.get("mode") or "unavailable")
    if mode == "unavailable":
        return failure(
            "source_not_provided",
            "No Circuit dashboard totals or export were provided.",
            "Open the dashboard and provide verified totals, copied dashboard text, or a fresh screenshot/export.",
            retrieved,
        )

    try:
        if mode == "direct":
            if not config.get("confirmed"):
                raise ValueError("Confirm that the Circuit figures came from the visible dashboard.")
            tokens = parse_integer(config.get("tokens"), "Circuit tokens")
            cost = parse_money(config.get("cost"), "Circuit approximate cost")
            window_kind = str(config.get("window_kind") or "month_to_date")
            if window_kind == "month_to_date":
                window_label = f"Month-to-date through {retrieved[:10]}"
            elif window_kind == "selected_period":
                window_label = str(period["label"])
            elif window_kind == "completed_month":
                window_label = f"Completed Monthly view for {period['label']}"
            else:
                raise ValueError("Unknown Circuit source window.")
            return {
                "status": "verified",
                "source": "user-verified Circuit dashboard",
                "window": window_label,
                "retrieved_at": retrieved,
                "data": {
                    "tokens": tokens,
                    "approximate_cost": str(cost) if cost is not None else None,
                    "comparison": str(config.get("comparison") or "").strip(),
                    "cross_charge": str(config.get("cross_charge") or "").strip(),
                    "dashboard_url": str(config.get("dashboard_url") or "").strip(),
                    "method": str(config.get("capture_method") or "direct dashboard entry"),
                },
            }

        if mode != "derived":
            raise ValueError("Unknown Circuit collection mode.")

        if not config.get("confirmed"):
            raise ValueError("Confirm that the Circuit QTD and MTD figures were transcribed from the dashboard.")

        if (datetime.now(timezone).month - 1) % 3 != 1:
            raise ValueError("QTD minus MTD is only allowed during the second month of a calendar quarter.")
        if not period["is_previous_calendar_month"]:
            raise ValueError("QTD minus MTD can only isolate the immediately preceding month.")

        qtd_tokens = parse_integer(config.get("qtd_tokens"), "Quarter-to-date tokens")
        mtd_tokens = parse_integer(config.get("mtd_tokens"), "Month-to-date tokens")
        if qtd_tokens < mtd_tokens:
            raise ValueError("Circuit contradiction: quarter-to-date tokens are below month-to-date tokens.")

        qtd_cost = parse_money(config.get("qtd_cost"), "Quarter-to-date approximate cost")
        mtd_cost = parse_money(config.get("mtd_cost"), "Month-to-date approximate cost")
        if qtd_cost is not None and mtd_cost is not None and qtd_cost < mtd_cost:
            raise ValueError("Circuit contradiction: quarter-to-date cost is below month-to-date cost.")

        derived_cost = qtd_cost - mtd_cost if qtd_cost is not None and mtd_cost is not None else None
        return {
            "status": "inference",
            "source": "user-transcribed Circuit QTD and current MTD views",
            "window": str(period["label"]),
            "retrieved_at": retrieved,
            "data": {
                "tokens": qtd_tokens - mtd_tokens,
                "approximate_cost": str(derived_cost) if derived_cost is not None else None,
                "comparison": str(config.get("comparison") or "").strip(),
                "cross_charge": str(config.get("cross_charge") or "").strip(),
                "dashboard_url": str(config.get("dashboard_url") or "").strip(),
                "method": "Inference — QTD minus current MTD",
                "arithmetic": f"{qtd_tokens:,} QTD - {mtd_tokens:,} MTD = {qtd_tokens - mtd_tokens:,}",
            },
        }
    except ValueError as exc:
        return failure(
            "invalid_or_inconsistent_data",
            str(exc),
            "Use a visible dashboard window or export and do not reconcile contradictions by assumption.",
            retrieved,
        )


def format_integer(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "unavailable"


def format_money(value: Any) -> str:
    if value in (None, ""):
        return "unavailable"
    try:
        return f"${Decimal(str(value)):,.2f}"
    except InvalidOperation:
        return "unavailable"


def clean_cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


def source_problem(source: dict[str, Any]) -> str:
    details = source.get("failure") or {}
    return clean_cell(details.get("message") or "Unavailable")


def retrieval_date(source: dict[str, Any]) -> str:
    value = str(source.get("retrieved_at") or "")
    return value[:10] if len(value) >= 10 else "unavailable"


def build_report(
    period: dict[str, Any], timezone_name: str, sources: dict[str, dict[str, Any]]
) -> str:
    timezone = ZoneInfo(timezone_name)
    retrieved = datetime.now(timezone)
    period_label = str(period["label"])
    verified_count = sum(source.get("status") in {"verified", "inference"} for source in sources.values())
    status = "Complete" if verified_count == 3 else f"Partial — {3 - verified_count} source(s) unavailable"

    bedrock = sources["bedrock"]
    codex = sources["codex"]
    circuit = sources["circuit"]

    if bedrock.get("status") == "verified":
        bed_data = bedrock.get("data") or {}
        summary = bed_data.get("summary") or {}
        models = bed_data.get("models") or []
        cache_read = sum(int(item.get("cache_read_tokens") or 0) for item in models)
        cache_write = sum(int(item.get("cache_write_tokens") or 0) for item in models)
        direct_input = max(0, int(summary.get("input_tokens") or 0) - cache_read - cache_write)
        mix = ", ".join(
            f"{clean_cell(item.get('model', 'unknown'))} ({format_integer(item.get('api_calls'))} requests)"
            for item in models[:4]
        ) or "model mix unavailable"
        bed_usage = (
            f"{format_integer(summary.get('api_calls'))} requests; "
            f"{format_integer(direct_input)} direct input; "
            f"{format_integer(cache_read)} cache read; "
            f"{format_integer(cache_write)} cache write; "
            f"{format_integer(summary.get('output_tokens'))} output; {mix}"
        )
        bed_cost = f"{format_money(summary.get('total_cost'))} estimated"
    else:
        bed_usage = source_problem(bedrock)
        bed_cost = "estimated cost unavailable"

    if codex.get("status") == "verified":
        cod_data = codex.get("data") or {}
        usage = cod_data.get("usage") or {}
        hit_rate = usage.get("cache_hit_rate")
        hit_text = f"{float(hit_rate) * 100:.1f}%" if hit_rate is not None else "unavailable"
        cod_usage = (
            f"{format_integer(cod_data.get('sessions'))} sessions; "
            f"{format_integer(usage.get('input_tokens'))} input; "
            f"{format_integer(usage.get('cached_input_tokens'))} cached input; "
            f"{format_integer(usage.get('cache_write_input_tokens'))} cache write; "
            f"{format_integer(usage.get('output_tokens'))} output; "
            f"{format_integer(usage.get('reasoning_output_tokens'))} reasoning output; "
            f"{format_integer(usage.get('total_tokens'))} total; {hit_text} cache hit"
        )
    else:
        cod_usage = source_problem(codex)

    if circuit.get("status") in {"verified", "inference"}:
        cir_data = circuit.get("data") or {}
        cir_usage = f"{format_integer(cir_data.get('tokens'))} tokens"
        if cir_data.get("comparison"):
            cir_usage += f"; {clean_cell(cir_data['comparison'])}"
        cir_cost = f"{format_money(cir_data.get('approximate_cost'))} approximate/informational"
    else:
        cir_usage = source_problem(circuit)
        cir_cost = "approximate/informational cost unavailable"

    codex_sentence = "Codex telemetry was unavailable."
    finding_lines: list[str] = []
    if codex.get("status") == "verified":
        cod_data = codex["data"]
        usage = cod_data.get("usage") or {}
        hit_rate = usage.get("cache_hit_rate")
        hit_text = f"{float(hit_rate) * 100:.1f}%" if hit_rate is not None else "unavailable"
        codex_sentence = (
            f"Local Codex telemetry recorded **{format_integer(cod_data.get('sessions'))} sessions** and "
            f"**{format_integer(usage.get('total_tokens'))} total tokens**; the cache-hit rate was **{hit_text}**."
        )
        if hit_rate is not None:
            finding_lines.append(
                f"- **Inference — repeated-context efficiency:** The **{hit_text}** Codex cache-hit rate suggests strong reuse; it does not establish subscription savings."
            )

    if bedrock.get("status") == "verified":
        summary = (bedrock.get("data") or {}).get("summary") or {}
        finding_lines.append(
            f"- **Bedrock rolling window:** **{format_integer(summary.get('api_calls'))} requests** generated an **estimated {format_money(summary.get('total_cost'))}** over {clean_cell(bedrock.get('window', 'the collector window'))} ending {retrieved:%B %d, %Y}."
        )
        if not bedrock.get("matches_selected_period"):
            finding_lines.append(
                "- **Window caveat:** The Bedrock collector ends on the retrieval date, so it does not exactly match the selected historical period."
            )
    if circuit.get("status") == "inference":
        finding_lines.append(
            f"- **Inference — Circuit monthly value:** {clean_cell((circuit.get('data') or {}).get('arithmetic', 'QTD - MTD'))}; verify the direct Monthly tooltip before sharing."
        )
    if not finding_lines:
        finding_lines.append("- **Verified trend unavailable:** Missing sources prevent a defensible cross-platform trend.")
    finding_lines.append("- **Unit caveat:** Providers count context, caching, and reasoning differently; token totals are not equivalent units.")

    risk_lines: list[str] = []
    for label, source in (("Bedrock", bedrock), ("Codex", codex), ("Circuit", circuit)):
        if source.get("status") == "unavailable":
            details = source.get("failure") or {}
            risk_lines.append(
                f"- **{label} — {clean_cell(details.get('category', 'unavailable'))}:** "
                f"{clean_cell(details.get('message', 'Unavailable'))} "
                f"**Follow-up:** {clean_cell(details.get('remediation', 'Provide a verified source.'))}"
            )
    if circuit.get("status") in {"verified", "inference"}:
        cross_charge = clean_cell((circuit.get("data") or {}).get("cross_charge") or "")
        if cross_charge:
            risk_lines.append(f"- **Circuit cross-charge statement:** {cross_charge}")
    risk_lines.append("- **Billing boundary:** Local Codex telemetry does not represent complete ChatGPT subscription or billing usage.")

    bed_window = f"{clean_cell(bedrock.get('window', 'rolling collector window')).capitalize()} ending {retrieved:%Y-%m-%d}; retrieved {retrieval_date(bedrock)}"
    cod_window = f"{period_label} exact local range; retrieved {retrieval_date(codex)}"
    cir_window = f"{clean_cell(circuit.get('window', period_label))}; retrieved {retrieval_date(circuit)}"
    action = "No management action is required on verified figures; complete missing sources before external sharing."

    report = f"""# AI Usage Report — {period_label}

**Reporting period:** {period_label} ({period['start_date'].isoformat()} to {period['end_date'].isoformat()}) | **Timezone:** {timezone_name} | **Retrieved:** {retrieved:%Y-%m-%d}
**Status:** {status}

## Verified usage; gaps remain visible

{codex_sentence} **{verified_count} of 3 sources** supplied verified or explicitly inferred data. {action}

| Platform | Source window | Usage | Cost |
|---|---|---|---|
| Claude Code / AWS Bedrock | {bed_window} | {bed_usage} | {bed_cost} |
| Codex / ChatGPT | {cod_window} | {cod_usage} | authoritative billing unavailable |
| Circuit | {cir_window} | {cir_usage} | {cir_cost} |

## Key trends and business impact

{chr(10).join(finding_lines)}

## Risks and concrete follow-ups

{chr(10).join(risk_lines)}

## Draft email

**Subject: {period_label} AI usage — {verified_count} of 3 sources verified**

{codex_sentence} Bedrock costs remain estimates, Circuit costs remain approximate/informational, and provider token units are not directly comparable.

{action}
"""
    return report.strip()


def process_report(payload: dict[str, Any]) -> dict[str, Any]:
    timezone_name = validate_timezone(str(payload.get("timezone") or local_timezone_name()))
    timezone = ZoneInfo(timezone_name)
    period = resolve_period(payload, timezone)
    sources = {
        "bedrock": collect_bedrock(payload.get("bedrock") or {}, timezone, period),
        "codex": collect_codex(payload.get("codex") or {}, period, timezone_name),
        "circuit": collect_circuit(payload.get("circuit") or {}, period, timezone),
    }
    report = build_report(period, timezone_name, sources)
    return {
        "reporting_period": {
            "start_date": period["start_date"].isoformat(),
            "end_date": period["end_date"].isoformat(),
            "label": period["label"],
        },
        "download_slug": period["slug"],
        "timezone": timezone_name,
        "sources": sources,
        "report": report,
        "word_count": len(report.split()),
    }


class ReportHandler(BaseHTTPRequestHandler):
    server_version = f"MonthlyAIUsageReport/{APP_VERSION}"

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def security_headers(self, content_type: str) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
        )

    def send_json(self, status: int, data: dict[str, Any]) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.security_headers("application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/":
            try:
                body = HTML_PATH.read_bytes()
            except OSError as exc:
                self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
                return
            self.send_response(HTTPStatus.OK)
            self.security_headers("text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/api/defaults":
            timezone_name = local_timezone_name()
            timezone = ZoneInfo(timezone_name)
            period_start, period_end = previous_month_dates(datetime.now(timezone))
            default_script, bedrock_found, bedrock_source = bedrock_script_details()
            sessions_dir, sessions_found, sessions_count = codex_sessions_details()
            self.send_json(
                HTTPStatus.OK,
                {
                    "start_date": period_start.isoformat(),
                    "end_date": period_end.isoformat(),
                    "timezone": timezone_name,
                    "timezones": sorted(available_timezones()),
                    "bedrock_script": str(default_script),
                    "bedrock_script_example": "~/tools/bedrock_usage_check.py",
                    "bedrock_script_found": bedrock_found,
                    "bedrock_script_source": bedrock_source,
                    "bedrock_lambda_url": configured_lambda_url(default_script),
                    "aws_profiles": available_profiles(),
                    "codex_sessions_dir": sessions_dir,
                    "codex_sessions_example": "~/.codex/sessions",
                    "codex_sessions_found": sessions_found,
                    "codex_session_files": sessions_count,
                    "circuit_dashboard_url": DEFAULT_CIRCUIT_URL,
                    "version": APP_VERSION,
                    "release_date": RELEASE_DATE,
                },
            )
            return
        self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path not in {"/api/run", "/api/select-path"}:
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return
        origin = self.headers.get("Origin", "")
        if origin and not (origin.startswith("http://127.0.0.1:") or origin.startswith("http://localhost:")):
            self.send_json(HTTPStatus.FORBIDDEN, {"error": "Untrusted request origin."})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid content length."})
            return
        if length <= 0 or length > MAX_REQUEST_BYTES:
            self.send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "Request body is empty or too large."})
            return
        try:
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("Request must be a JSON object.")
            if self.path == "/api/select-path":
                kind = str(payload.get("kind") or "")
                selected = select_local_path(kind)
                response: dict[str, Any] = {"path": selected, "cancelled": selected is None}
                if kind == "file" and selected:
                    response["lambda_url"] = configured_lambda_url(Path(selected).expanduser())
                self.send_json(HTTPStatus.OK, response)
                return
            result = process_report(payload)
        except (json.JSONDecodeError, ValueError) as exc:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        except Exception as exc:  # Keep credentials and tracebacks out of the browser.
            self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"Report generation failed: {type(exc).__name__}"})
            return
        self.send_json(HTTPStatus.OK, result)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the Monthly AI Usage Report setup wizard.")
    parser.add_argument("--port", type=int, default=8765, help="Loopback port (default: 8765; use 0 for automatic).")
    parser.add_argument("--no-browser", action="store_true", help="Do not open the browser automatically.")
    parser.add_argument("--self-test", action="store_true", help=argparse.SUPPRESS)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.self_test:
        command = python_script_command(
            CODEX_COLLECTOR,
            "--start-date",
            "2000-01-01",
            "--end-date",
            "2000-01-01",
            "--timezone",
            "UTC",
            "--sessions-dir",
            str(SKILL_DIR / "__empty_self_test_sessions__"),
        )
        code, data, message = run_json_command(command, timeout=90)
        checks = {
            "html": HTML_PATH.is_file(),
            "codex_collector": CODEX_COLLECTOR.is_file(),
            "bedrock_collector": BUNDLED_BEDROCK_SCRIPT.is_file(),
            "timezone_data": BUNDLED_TZDATA.is_dir(),
            "collector_subprocess": code == 0 and data is not None,
        }
        print(
            json.dumps(
                {
                    "version": APP_VERSION,
                    "frozen": bool(getattr(sys, "frozen", False)),
                    "checks": checks,
                    "message": "ok" if all(checks.values()) else message,
                }
            )
        )
        return 0 if all(checks.values()) else 2
    if not HTML_PATH.is_file():
        print(f"Error: app asset not found: {HTML_PATH}", file=sys.stderr)
        return 2
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), ReportHandler)
    except OSError as exc:
        if exc.errno != errno.EADDRINUSE or args.port == 0:
            print(f"Error: could not start local app: {exc}", file=sys.stderr)
            return 2
        running_version = existing_app_version(args.port)
        existing_url = f"http://127.0.0.1:{args.port}/"
        if running_version == APP_VERSION:
            print(f"Monthly AI Usage Report {APP_VERSION} is already running: {existing_url}")
            if not args.no_browser:
                webbrowser.open(existing_url)
            return 0
        try:
            server = ThreadingHTTPServer(("127.0.0.1", 0), ReportHandler)
        except OSError as fallback_exc:
            print(f"Error: could not start local app: {fallback_exc}", file=sys.stderr)
            return 2
        if running_version:
            print(
                f"Port {args.port} is used by Monthly AI Usage Report {running_version}; "
                f"starting {APP_VERSION} on an available port."
            )
        else:
            print(f"Port {args.port} is used by another process; starting on an available port.")

    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"Monthly AI Usage Report app: {url}")
    print("Credentials stay in memory for the current request. Press Ctrl+C to stop.")
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping local app.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--run-python-script":
        run_python_script_mode()
        raise SystemExit(0)
    raise SystemExit(main())
