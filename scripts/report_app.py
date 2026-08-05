#!/usr/bin/env python3
"""Run the Monthly AI Usage Report guided setup app on localhost.

The server uses only the Python standard library, binds to loopback, never logs
request bodies, and does not persist AWS credentials or generated reports.
"""

from __future__ import annotations

import argparse
import ast
import configparser
import json
import os
import re
import subprocess
import sys
import threading
import webbrowser
from datetime import datetime
from decimal import Decimal, InvalidOperation
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, reset_tzpath


SKILL_DIR = Path(__file__).resolve().parent.parent
HTML_PATH = SKILL_DIR / "assets" / "report_app.html"
CODEX_COLLECTOR = SKILL_DIR / "scripts" / "collect_codex_usage.py"
BUNDLED_TZDATA = SKILL_DIR / "tzdata"
if BUNDLED_TZDATA.is_dir():
    os.environ["PYTHONTZPATH"] = str(BUNDLED_TZDATA)
    reset_tzpath((str(BUNDLED_TZDATA),))

BUNDLED_BEDROCK_SCRIPT = SKILL_DIR / "scripts" / "bedrock_usage_check.py"
DEFAULT_BEDROCK_SCRIPT = Path(
    os.environ.get(
        "BEDROCK_USAGE_SCRIPT",
        BUNDLED_BEDROCK_SCRIPT
        if BUNDLED_BEDROCK_SCRIPT.is_file()
        else Path.home() / ".bedrock" / "bedrock_usage_check.py",
    )
)
MAX_REQUEST_BYTES = 256_000
LAMBDA_HOST = re.compile(r"^[a-z0-9]+\.lambda-url\.[a-z0-9-]+\.on\.aws$")


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


def validate_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown IANA timezone: {value}") from exc
    return value


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


def collect_bedrock(config: dict[str, Any], timezone: ZoneInfo) -> dict[str, Any]:
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

    script = Path(str(config.get("script") or DEFAULT_BEDROCK_SCRIPT)).expanduser()
    if not script.is_file():
        return failure(
            "collector_missing",
            f"Collector not found: {script}",
            "Set BEDROCK_USAGE_SCRIPT or select the correct collector path.",
            retrieved,
        )

    args = [sys.executable, str(script), "-d", "30", "--json"]
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
        "window": "rolling 30 days",
        "window_end": retrieved,
        "retrieved_at": retrieved,
        "data": data,
    }


def collect_codex(config: dict[str, Any], month: str, timezone_name: str) -> dict[str, Any]:
    timezone = ZoneInfo(timezone_name)
    retrieved = datetime.now(timezone).isoformat()
    sessions_dir = Path(str(config.get("sessions_dir") or Path.home() / ".codex" / "sessions")).expanduser()
    args = [
        sys.executable,
        str(CODEX_COLLECTOR),
        "--month",
        month,
        "--timezone",
        timezone_name,
        "--sessions-dir",
        str(sessions_dir),
    ]
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
        "window": data.get("reporting_window_local"),
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


def collect_circuit(config: dict[str, Any], report_month: str, timezone: ZoneInfo) -> dict[str, Any]:
    retrieved = datetime.now(timezone).isoformat()
    mode = str(config.get("mode") or "unavailable")
    if mode == "unavailable":
        return failure(
            "source_not_provided",
            "No Circuit Monthly-view totals or export were provided.",
            "Enter the completed month totals or attach a fresh dashboard screenshot/export to the Codex task.",
            retrieved,
        )

    try:
        if mode == "direct":
            if not config.get("confirmed"):
                raise ValueError("Confirm that the Circuit figures were transcribed from the completed Monthly view.")
            tokens = parse_integer(config.get("tokens"), "Circuit tokens")
            cost = parse_money(config.get("cost"), "Circuit approximate cost")
            return {
                "status": "verified",
                "source": "user-transcribed Circuit Monthly view",
                "window": report_month,
                "retrieved_at": retrieved,
                "data": {
                    "tokens": tokens,
                    "approximate_cost": str(cost) if cost is not None else None,
                    "comparison": str(config.get("comparison") or "").strip(),
                    "cross_charge": str(config.get("cross_charge") or "").strip(),
                    "dashboard_url": str(config.get("dashboard_url") or "").strip(),
                    "method": "direct monthly view",
                },
            }

        if mode != "derived":
            raise ValueError("Unknown Circuit collection mode.")

        if not config.get("confirmed"):
            raise ValueError("Confirm that the Circuit QTD and MTD figures were transcribed from the dashboard.")

        if (datetime.now(timezone).month - 1) % 3 != 1:
            raise ValueError("QTD minus MTD is only allowed during the second month of a calendar quarter.")
        expected_previous = previous_month(datetime.now(timezone))
        if report_month != expected_previous:
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
            "window": report_month,
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
            "Use the completed month Monthly-view tooltip/export and do not reconcile contradictions by assumption.",
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


def build_report(month: str, timezone_name: str, sources: dict[str, dict[str, Any]]) -> str:
    timezone = ZoneInfo(timezone_name)
    retrieved = datetime.now(timezone)
    month_date = datetime.strptime(month, "%Y-%m")
    month_label = month_date.strftime("%B %Y")
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
            f"**{format_integer(usage.get('total_tokens'))} total tokens**, with a **{hit_text} cache-hit rate**."
        )
        finding_lines.append(
            f"- **Inference — repeated-context efficiency:** The **{hit_text}** Codex cache-hit rate suggests strong reuse; it does not establish subscription savings."
        )

    if bedrock.get("status") == "verified":
        summary = (bedrock.get("data") or {}).get("summary") or {}
        finding_lines.append(
            f"- **Bedrock rolling window:** **{format_integer(summary.get('api_calls'))} requests** generated an **estimated {format_money(summary.get('total_cost'))}** over 30 days ending {retrieved:%B %d, %Y}."
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

    bed_window = f"Rolling 30 days ending {retrieved:%Y-%m-%d}; retrieved {retrieval_date(bedrock)}"
    cod_window = f"{month_label} calendar month; retrieved {retrieval_date(codex)}"
    cir_window = f"{month_label} Monthly view; retrieved {retrieval_date(circuit)}"
    action = "No management action is required on verified figures; complete missing sources before external sharing."

    report = f"""# AI Usage Report — {month_label}

**Reporting period:** {month_label} | **Timezone:** {timezone_name} | **Retrieved:** {retrieved:%Y-%m-%d}
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

**Subject: {month_label} AI usage — {verified_count} of 3 sources verified**

{codex_sentence} Bedrock costs remain estimates, Circuit costs remain approximate/informational, and provider token units are not directly comparable.

{action}
"""
    return report.strip()


def process_report(payload: dict[str, Any]) -> dict[str, Any]:
    timezone_name = validate_timezone(str(payload.get("timezone") or local_timezone_name()))
    timezone = ZoneInfo(timezone_name)
    month = validate_month(str(payload.get("month") or previous_month(datetime.now(timezone))))
    sources = {
        "bedrock": collect_bedrock(payload.get("bedrock") or {}, timezone),
        "codex": collect_codex(payload.get("codex") or {}, month, timezone_name),
        "circuit": collect_circuit(payload.get("circuit") or {}, month, timezone),
    }
    report = build_report(month, timezone_name, sources)
    return {
        "reporting_month": month,
        "timezone": timezone_name,
        "sources": sources,
        "report": report,
        "word_count": len(report.split()),
    }


class ReportHandler(BaseHTTPRequestHandler):
    server_version = "MonthlyAIUsageReport/1.0"

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
            self.send_json(
                HTTPStatus.OK,
                {
                    "month": previous_month(datetime.now(timezone)),
                    "timezone": timezone_name,
                    "bedrock_script": str(DEFAULT_BEDROCK_SCRIPT),
                    "bedrock_lambda_url": configured_lambda_url(DEFAULT_BEDROCK_SCRIPT),
                    "aws_profiles": available_profiles(),
                    "codex_sessions_dir": str(Path.home() / ".codex" / "sessions"),
                },
            )
            return
        self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/api/run":
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
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not HTML_PATH.is_file():
        print(f"Error: app asset not found: {HTML_PATH}", file=sys.stderr)
        return 2
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), ReportHandler)
    except OSError as exc:
        print(f"Error: could not start local app: {exc}", file=sys.stderr)
        return 2

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
    raise SystemExit(main())
