#!/usr/bin/env python3
"""Aggregate local Codex session telemetry for an exact local date range."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


USAGE_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)


def parse_month(value: str) -> tuple[int, int]:
    try:
        parsed = datetime.strptime(value, "%Y-%m")
    except ValueError as exc:
        raise argparse.ArgumentTypeError("month must use YYYY-MM") from exc
    return parsed.year, parsed.month


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from exc


def previous_month(now: datetime) -> tuple[int, int]:
    if now.month == 1:
        return now.year - 1, 12
    return now.year, now.month - 1


def month_after(year: int, month: int) -> tuple[int, int]:
    if month == 12:
        return year + 1, 1
    return year, month + 1


def local_timezone(name: str | None):
    if name:
        try:
            return ZoneInfo(name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown IANA timezone: {name}") from exc

    environment_name = os.environ.get("TZ")
    if environment_name:
        try:
            return ZoneInfo(environment_name)
        except ZoneInfoNotFoundError:
            pass

    localtime = Path("/etc/localtime")
    if localtime.is_symlink():
        target = str(localtime.resolve())
        marker = "/zoneinfo/"
        if marker in target:
            try:
                return ZoneInfo(target.split(marker, 1)[1])
            except ZoneInfoNotFoundError:
                pass
    return datetime.now().astimezone().tzinfo


def session_files(root: Path, _year: int, _month: int) -> Iterable[Path]:
    # A task can stay active after the month in which its rollout file was
    # created, so exact calendar-month accounting must inspect all rollouts.
    return sorted(root.rglob("*.jsonl")) if root.is_dir() else []


def parse_timestamp(value: str, timezone) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone)


def read_session(path: Path) -> dict[str, Any] | None:
    metadata: dict[str, Any] | None = None
    usage_events: list[dict[str, Any]] = []
    model_events: list[dict[str, str]] = []
    previous_total = {field: 0 for field in USAGE_FIELDS}
    parse_errors = 0

    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                parse_errors += 1
                continue

            payload = event.get("payload") or {}
            if event.get("type") == "session_meta" and metadata is None:
                metadata = payload
            elif event.get("type") == "event_msg" and payload.get("type") == "token_count":
                info = payload.get("info") or {}
                total_usage = info.get("total_token_usage") or {}
                last_usage = info.get("last_token_usage")
                if total_usage:
                    current_total = {
                        field: int(total_usage.get(field) or 0) for field in USAGE_FIELDS
                    }
                    reset = current_total["total_tokens"] < previous_total["total_tokens"]
                    if reset:
                        usage = current_total
                    else:
                        usage = {
                            field: max(0, current_total[field] - previous_total[field])
                            for field in USAGE_FIELDS
                        }
                    previous_total = current_total
                elif last_usage:
                    usage = {field: int(last_usage.get(field) or 0) for field in USAGE_FIELDS}
                else:
                    continue
                usage_events.append({"timestamp": event.get("timestamp"), "usage": usage})
            elif event.get("type") == "turn_context":
                model = payload.get("model")
                if model:
                    model_events.append(
                        {"timestamp": str(event.get("timestamp") or ""), "model": str(model)}
                    )

    if metadata is None:
        return None
    return {
        "metadata": metadata,
        "usage_events": usage_events,
        "model_events": model_events,
        "parse_errors": parse_errors,
        "path": str(path),
    }


def collect_range(root: Path, start_date: date, end_date: date, timezone) -> dict[str, Any]:
    if end_date < start_date:
        raise ValueError("end date must be on or after start date")
    start = datetime.combine(start_date, time.min, tzinfo=timezone)
    end = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=timezone)

    by_session_id: dict[str, dict[str, Any]] = {}
    file_errors: list[str] = []
    parse_errors = 0

    for path in session_files(root, start_date.year, start_date.month):
        try:
            record = read_session(path)
        except OSError as exc:
            file_errors.append(f"{path}: {exc}")
            continue
        if record is None:
            continue

        # `session_id` can identify a parent task shared by multiple rollouts or
        # subagents. `id` is the unique local telemetry session identifier.
        metadata = record["metadata"]
        session_id = str(metadata.get("id") or metadata.get("session_id") or record["path"])
        existing = by_session_id.get(session_id)
        if existing is None:
            by_session_id[session_id] = record
        else:
            existing["usage_events"].extend(record["usage_events"])
            existing["model_events"].extend(record["model_events"])
        parse_errors += int(record["parse_errors"])

    totals = {field: 0 for field in USAGE_FIELDS}
    origins: Counter[str] = Counter()
    models: Counter[str] = Counter()
    active_sessions = 0
    sessions_started = 0
    token_count_events = 0

    for record in by_session_id.values():
        metadata = record["metadata"]
        metadata_timestamp = metadata.get("timestamp")
        if metadata_timestamp:
            try:
                metadata_time = parse_timestamp(str(metadata_timestamp), timezone)
                if start <= metadata_time < end:
                    sessions_started += 1
            except ValueError:
                parse_errors += 1

        session_has_usage = False
        for usage_event in record["usage_events"]:
            timestamp = usage_event.get("timestamp")
            if not timestamp:
                parse_errors += 1
                continue
            try:
                event_time = parse_timestamp(str(timestamp), timezone)
            except ValueError:
                parse_errors += 1
                continue
            if not (start <= event_time < end):
                continue
            session_has_usage = True
            token_count_events += 1
            usage = usage_event["usage"]
            for field in USAGE_FIELDS:
                totals[field] += int(usage.get(field) or 0)

        for model_event in record["model_events"]:
            timestamp = model_event.get("timestamp")
            if not timestamp:
                continue
            try:
                event_time = parse_timestamp(timestamp, timezone)
            except ValueError:
                continue
            if start <= event_time < end:
                models[model_event["model"]] += 1

        if session_has_usage:
            active_sessions += 1
            origins[str(metadata.get("originator") or "unknown")] += 1

    input_tokens = totals["input_tokens"]
    cached_tokens = totals["cached_input_tokens"]
    cache_hit_rate = cached_tokens / input_tokens if input_tokens else None
    non_cached_input = max(
        0,
        input_tokens - cached_tokens - totals["cache_write_input_tokens"],
    )

    warnings: list[str] = []
    if not active_sessions:
        warnings.append("No Codex token-count events matched the requested date range.")
    if file_errors or parse_errors:
        warnings.append("Some session data could not be parsed; review data_quality details.")

    return {
        "source": "local Codex session telemetry",
        "sessions_dir": str(root),
        "reporting_period": {
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "days": (end_date - start_date).days + 1,
        },
        "reporting_window_local": {
            "start": start.isoformat(),
            "end_exclusive": end.isoformat(),
        },
        "retrieved_at": datetime.now().astimezone(timezone).isoformat(),
        "sessions": active_sessions,
        "sessions_started": sessions_started,
        "token_count_events": token_count_events,
        "usage": {
            **totals,
            "non_cached_input_tokens": non_cached_input,
            "cache_hit_rate": cache_hit_rate,
        },
        "origins": dict(sorted(origins.items())),
        "turn_context_model_counts": dict(sorted(models.items())),
        "billing": {
            "status": "unavailable",
            "note": "Local Codex telemetry does not contain authoritative ChatGPT billing.",
        },
        "data_quality": {
            "json_parse_errors": parse_errors,
            "file_errors": file_errors,
            "warnings": warnings,
        },
    }


def collect(root: Path, year: int, month: int, timezone) -> dict[str, Any]:
    """Backward-compatible calendar-month wrapper."""
    next_year, next_month = month_after(year, month)
    end_date = date(next_year, next_month, 1) - timedelta(days=1)
    report = collect_range(root, date(year, month, 1), end_date, timezone)
    report["reporting_month"] = f"{year:04d}-{month:02d}"
    return report


def build_parser() -> argparse.ArgumentParser:
    default_root = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "sessions"
    parser = argparse.ArgumentParser(
        description="Aggregate local Codex session telemetry for a calendar month or exact date range."
    )
    parser.add_argument(
        "--month",
        help="Reporting month in YYYY-MM; defaults to the previous local calendar month.",
    )
    parser.add_argument("--start-date", help="Inclusive start date in YYYY-MM-DD.")
    parser.add_argument("--end-date", help="Inclusive end date in YYYY-MM-DD.")
    parser.add_argument(
        "--sessions-dir",
        type=Path,
        default=default_root,
        help=f"Codex sessions directory (default: {default_root}).",
    )
    parser.add_argument(
        "--timezone",
        help="IANA timezone such as America/Chicago; defaults to the computer timezone.",
    )
    parser.add_argument("--compact", action="store_true", help="Emit compact JSON.")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        timezone = local_timezone(args.timezone)
        if args.month and (args.start_date or args.end_date):
            raise ValueError("choose --month or --start-date/--end-date, not both")
        if bool(args.start_date) != bool(args.end_date):
            raise ValueError("--start-date and --end-date must be provided together")
        if args.start_date and args.end_date:
            report = collect_range(
                args.sessions_dir.expanduser(),
                parse_date(args.start_date),
                parse_date(args.end_date),
                timezone,
            )
        else:
            year, month = parse_month(args.month) if args.month else previous_month(datetime.now(timezone))
            report = collect(args.sessions_dir.expanduser(), year, month, timezone)
    except (argparse.ArgumentTypeError, ValueError, OSError) as exc:
        parser.error(str(exc))
        return 2

    json.dump(report, sys.stdout, indent=None if args.compact else 2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
