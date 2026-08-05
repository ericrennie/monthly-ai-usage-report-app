#!/usr/bin/env python3
"""Standalone Bedrock usage checker — query your personal AWS Bedrock costs and tokens.

Zero external dependencies. Uses only Python 3.11+ stdlib for SigV4 signing.

Usage:
  python bedrock_usage_check.py                    # Last 24 hours
  python bedrock_usage_check.py -d 7               # Last 7 days
  python bedrock_usage_check.py --profile duo-sso  # Use a specific AWS profile
  python bedrock_usage_check.py --json              # JSON output (for piping)

Credential resolution (first match wins):
  1. --profile NAME         Read ~/.aws/credentials [NAME] section
  2. AWS_PROFILE env var    Read that profile from ~/.aws/credentials
  3. Environment vars       AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY
  4. Windows registry       User-level env vars (for bash-on-Windows)
  5. ~/.aws/credentials     [default] profile

Environment:
  BEDROCK_USAGE_URL     Verified Lambda Function URL (required)
  AWS_PROFILE           AWS CLI profile name (alternative to --profile flag)
  AWS_ACCESS_KEY_ID     AWS access key (from provisioning)
  AWS_SECRET_ACCESS_KEY AWS secret key (from provisioning)
  AWS_SESSION_TOKEN     (optional) session token for temporary credentials
"""

import argparse
import configparser
import hashlib
import hmac
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

# --- Configuration ---
DEFAULT_LAMBDA_URL = ""
DEFAULT_REGION = "us-east-1"

# Pricing per 1M tokens (last known good: 2026-02-25)
PRICING = {
    "claude-sonnet-4-6": {"input": 3.30, "output": 7.50, "cache_read": 0.30, "cache_write": 6.00},
    "claude-sonnet-4-5": {"input": 3.30, "output": 8.25, "cache_read": 0.33, "cache_write": 6.00},
    "claude-opus-4-6":   {"input": 5.50, "output": 12.50, "cache_read": 0.55, "cache_write": 11.00},
    "claude-opus-4-5":   {"input": 2.75, "output": 25.00, "cache_read": 0.55, "cache_write": 6.25},
    "claude-haiku-4-5":  {"input": 0.50, "output": 2.50, "cache_read": 0.11, "cache_write": 2.20},
}
DEFAULT_PRICING = {"input": 3.30, "output": 8.25, "cache_read": 0.33, "cache_write": 6.00}


# ---------------------------------------------------------------------------
# SigV4 signing (pure stdlib)
# ---------------------------------------------------------------------------

def _sign(key, msg):
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _get_signature_key(secret, date_stamp, region, service):
    k_date = _sign(("AWS4" + secret).encode("utf-8"), date_stamp)
    k_region = _sign(k_date, region)
    k_service = _sign(k_region, service)
    return _sign(k_service, "aws4_request")


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def sigv4_request(method, url, body, region, service, access_key, secret_key, session_token=None):
    """Make a SigV4-signed HTTP request using only stdlib."""
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname
    path = parsed.path or "/"

    now = datetime.now(UTC)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")

    body_bytes = body.encode("utf-8") if isinstance(body, str) else body
    payload_hash = _sha256(body_bytes)

    # Canonical headers — must be sorted
    headers_to_sign = {
        "host": host,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
    }
    if session_token:
        headers_to_sign["x-amz-security-token"] = session_token

    signed_header_names = ";".join(sorted(headers_to_sign))
    canonical_headers = "".join(f"{k}:{v}\n" for k, v in sorted(headers_to_sign.items()))

    canonical_request = "\n".join([
        method, path, "",  # empty query string
        canonical_headers, signed_header_names, payload_hash,
    ])

    credential_scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256", amz_date, credential_scope,
        _sha256(canonical_request.encode("utf-8")),
    ])

    signing_key = _get_signature_key(secret_key, date_stamp, region, service)
    signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    auth_header = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{credential_scope}, "
        f"SignedHeaders={signed_header_names}, Signature={signature}"
    )

    req_headers = {
        "Content-Type": "application/json",
        "Authorization": auth_header,
        "x-amz-date": amz_date,
        "x-amz-content-sha256": payload_hash,
    }
    if session_token:
        req_headers["x-amz-security-token"] = session_token

    req = urllib.request.Request(url, data=body_bytes, method=method, headers=req_headers)
    return urllib.request.urlopen(req, timeout=65)  # noqa: S310


# ---------------------------------------------------------------------------
# AWS credential and identity helpers
# ---------------------------------------------------------------------------

def _read_windows_env(name):
    """Read a user-level environment variable from the Windows registry.

    Bash-on-Windows (Git Bash, MSYS2) doesn't always inherit user env vars
    set via System Properties or setx. This reads them directly from the registry.
    """
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as key:
            value, _ = winreg.QueryValueEx(key, name)
            return value
    except Exception:
        return ""


def _get_env(name):
    """Get env var from process environment, falling back to Windows registry."""
    value = os.environ.get(name, "")
    if not value and sys.platform == "win32":
        value = _read_windows_env(name)
    return value


def _read_aws_profile(profile_name):
    """Read credentials from ~/.aws/credentials for the given profile.

    Supports profiles written by duo-sso, aws sso login, aws configure, etc.
    Returns (access_key, secret_key, session_token) or (None, None, None).
    """
    creds_file = Path.home() / ".aws" / "credentials"
    if not creds_file.exists():
        return None, None, None

    config = configparser.ConfigParser()
    config.read(creds_file)

    if profile_name not in config:
        return None, None, None

    section = config[profile_name]
    access_key = section.get("aws_access_key_id", "")
    secret_key = section.get("aws_secret_access_key", "")
    token = section.get("aws_session_token") or section.get("aws_security_token") or None

    if access_key and secret_key:
        return access_key, secret_key, token
    return None, None, None


def get_credentials(profile=None):
    """Resolve AWS credentials from multiple sources.

    Resolution order:
      1. --profile flag -> read that profile from ~/.aws/credentials
      2. AWS_PROFILE env var -> read that profile from ~/.aws/credentials
      3. Process environment variables (AWS_ACCESS_KEY_ID, etc.)
      4. Windows registry (user-level env vars, for bash-on-Windows)
      5. ~/.aws/credentials [default] profile
    """
    source = None

    # 1. Explicit --profile flag
    if profile:
        ak, sk, tok = _read_aws_profile(profile)
        if ak and sk:
            return ak, sk, tok, f"profile:{profile}"
        print(f"Error: Profile '{profile}' not found in ~/.aws/credentials.", file=sys.stderr)
        creds_file = Path.home() / ".aws" / "credentials"
        if creds_file.exists():
            config = configparser.ConfigParser()
            config.read(creds_file)
            profiles = [s for s in config.sections() if s != "DEFAULT"]
            if profiles:
                print(f"  Available profiles: {', '.join(profiles)}", file=sys.stderr)
        sys.exit(2)

    # 2. AWS_PROFILE env var
    env_profile = _get_env("AWS_PROFILE")
    if env_profile:
        ak, sk, tok = _read_aws_profile(env_profile)
        if ak and sk:
            return ak, sk, tok, f"profile:{env_profile}"

    # 3. Process environment variables
    access_key = os.environ.get("AWS_ACCESS_KEY_ID", "")
    secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
    token = os.environ.get("AWS_SESSION_TOKEN") or os.environ.get("AWS_SECURITY_TOKEN") or None
    if access_key and secret_key:
        return access_key, secret_key, token, "env"

    # 4. Windows registry (user-level env vars)
    if sys.platform == "win32":
        access_key = _read_windows_env("AWS_ACCESS_KEY_ID")
        secret_key = _read_windows_env("AWS_SECRET_ACCESS_KEY")
        token = _read_windows_env("AWS_SESSION_TOKEN") or _read_windows_env("AWS_SECURITY_TOKEN") or None
        if access_key and secret_key:
            return access_key, secret_key, token, "windows-env"

    # 5. ~/.aws/credentials [default] profile
    ak, sk, tok = _read_aws_profile("default")
    if ak and sk:
        return ak, sk, tok, "profile:default"

    print("Error: AWS credentials not found.", file=sys.stderr)
    print("  Checked: env vars, ~/.aws/credentials, --profile flag", file=sys.stderr)
    if sys.platform == "win32":
        print("  Checked: Windows user environment (registry)", file=sys.stderr)
    print("  Fix: run 'duo-sso', set env vars, or use --profile <name>", file=sys.stderr)
    sys.exit(2)


def get_caller_identity(access_key, secret_key, token, region):
    """Call sts:GetCallerIdentity to get the current username."""
    url = "https://sts.amazonaws.com/"
    body = "Action=GetCallerIdentity&Version=2011-06-15"

    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname
    path = parsed.path or "/"
    now = datetime.now(UTC)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")

    body_bytes = body.encode("utf-8")
    payload_hash = _sha256(body_bytes)

    headers_to_sign = {
        "content-type": "application/x-www-form-urlencoded; charset=utf-8",
        "host": host,
        "x-amz-date": amz_date,
    }
    if token:
        headers_to_sign["x-amz-security-token"] = token

    signed_header_names = ";".join(sorted(headers_to_sign))
    canonical_headers = "".join(f"{k}:{v}\n" for k, v in sorted(headers_to_sign.items()))

    canonical_request = "\n".join([
        "POST", path, "",
        canonical_headers, signed_header_names, payload_hash,
    ])

    credential_scope = f"{date_stamp}/us-east-1/sts/aws4_request"
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256", amz_date, credential_scope,
        _sha256(canonical_request.encode("utf-8")),
    ])

    signing_key = _get_signature_key(secret_key, date_stamp, "us-east-1", "sts")
    signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    auth_header = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{credential_scope}, "
        f"SignedHeaders={signed_header_names}, Signature={signature}"
    )

    req_headers = {
        "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
        "Authorization": auth_header,
        "x-amz-date": amz_date,
    }
    if token:
        req_headers["x-amz-security-token"] = token

    req = urllib.request.Request(url, data=body_bytes, method="POST", headers=req_headers)

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            # Parse XML response (avoid importing xml.etree for simplicity)
            text = resp.read().decode()
            arn = text.split("<Arn>")[1].split("</Arn>")[0] if "<Arn>" in text else "unknown"
            return arn.split("/")[-1], arn
    except Exception as e:
        print(f"Error: Could not verify AWS identity -- {e}", file=sys.stderr)
        sys.exit(2)


# ---------------------------------------------------------------------------
# Business logic
# ---------------------------------------------------------------------------

def get_lambda_url(cli_url):
    """Resolve Lambda Function URL."""
    url = cli_url or _get_env("BEDROCK_USAGE_URL") or DEFAULT_LAMBDA_URL
    if not url:
        print("Error: No Lambda URL configured.", file=sys.stderr)
        print("Set BEDROCK_USAGE_URL environment variable.", file=sys.stderr)
        sys.exit(2)
    return url


def query_lambda(url, hours, region, access_key, secret_key, token):
    """Invoke the Lambda Function URL."""
    body = json.dumps({"hours": hours, "region": region})
    try:
        resp = sigv4_request("POST", url, body, region, "lambda", access_key, secret_key, token)
        data = json.loads(resp.read())
        return data.get("results", [])
    except urllib.error.HTTPError as e:
        if e.code == 403:
            print("Error: Access denied (403).", file=sys.stderr)
            print("Verify your AWS user is in the bedrock-access IAM group.", file=sys.stderr)
            sys.exit(2)
        body_text = e.read().decode() if e.fp else ""
        print(f"Error: HTTP {e.code} -- {body_text}", file=sys.stderr)
        sys.exit(2)


def resolve_pricing(model_id):
    """Match a Bedrock model ID to pricing rates."""
    m = model_id.lower()
    for prefix in ("us.", "global.", "eu.", "ap."):
        if m.startswith(prefix):
            m = m[len(prefix):]
            break
    if "/" in m:
        m = m.split("/")[-1]
    for pattern, rates in PRICING.items():
        if pattern in m:
            return rates
    return DEFAULT_PRICING


def parse_and_cost(raw_results):
    """Parse Lambda response into costed records."""
    records = []
    for row in raw_results:
        fields = {f["field"]: f["value"] for f in row}
        model = fields.get("modelId", "unknown")
        input_tok = int(fields.get("total_input", 0) or 0)
        output_tok = int(fields.get("total_output", 0) or 0)
        cache_read = int(fields.get("total_cache_read", 0) or 0)
        cache_write = int(fields.get("total_cache_write", 0) or 0)
        calls = int(fields.get("api_calls", 0) or 0)

        p = resolve_pricing(model)
        non_cached = max(0, input_tok - cache_read - cache_write)
        input_cost = (non_cached / 1_000_000) * p["input"]
        output_cost = (output_tok / 1_000_000) * p["output"]
        cr_cost = (cache_read / 1_000_000) * p["cache_read"]
        cw_cost = (cache_write / 1_000_000) * p["cache_write"]

        records.append({
            "model": model,
            "api_calls": calls,
            "input_tokens": input_tok,
            "output_tokens": output_tok,
            "cache_read_tokens": cache_read,
            "cache_write_tokens": cache_write,
            "input_cost": input_cost,
            "output_cost": output_cost,
            "cache_read_cost": cr_cost,
            "cache_write_cost": cw_cost,
            "total_cost": input_cost + output_cost + cr_cost + cw_cost,
        })
    return records


def display(records, username, hours):
    """Print human-readable usage summary."""
    if not records:
        print(f"\n  No Bedrock usage found for {username} in the last {hours} hours.")
        return

    total_input = sum(r["input_tokens"] for r in records)
    total_output = sum(r["output_tokens"] for r in records)
    total_calls = sum(r["api_calls"] for r in records)
    total_cost = sum(r["total_cost"] for r in records)
    total_cr = sum(r["cache_read_tokens"] for r in records)
    total_cw = sum(r["cache_write_tokens"] for r in records)

    period = f"Last {hours} Hours" if hours <= 48 else f"Last {hours // 24} Days"

    print(f"\n{'=' * 60}")
    print(f"  Bedrock Usage: {username}")
    print(f"  Period: {period}")
    print(f"{'=' * 60}")
    print(f"\n  Summary:")
    print(f"    API Calls:     {total_calls:,}")
    print(f"    Input tokens:  {total_input:,}")
    print(f"    Output tokens: {total_output:,}")
    print(f"    Total tokens:  {total_input + total_output:,}")
    if total_cr or total_cw:
        print(f"    Cache read:    {total_cr:,}")
        print(f"    Cache write:   {total_cw:,}")

    print(f"\n  Estimated Cost:")
    print(f"    Input:       ${sum(r['input_cost'] for r in records):.4f}")
    print(f"    Output:      ${sum(r['output_cost'] for r in records):.4f}")
    if total_cr or total_cw:
        print(f"    Cache read:  ${sum(r['cache_read_cost'] for r in records):.4f}")
        print(f"    Cache write: ${sum(r['cache_write_cost'] for r in records):.4f}")
    print(f"    Total:       ${total_cost:.4f}")

    if len(records) > 1:
        print(f"\n  Breakdown by Model:")
        print(f"    {'Model':<45s} {'Calls':>6s} {'Input':>10s} {'Output':>10s} {'Cost':>10s}")
        print(f"    {'-' * 45} {'-' * 6} {'-' * 10} {'-' * 10} {'-' * 10}")
        for r in sorted(records, key=lambda x: x["total_cost"], reverse=True):
            model_short = r["model"].split("/")[-1] if "/" in r["model"] else r["model"]
            if len(model_short) > 45:
                model_short = model_short[:42] + "..."
            print(
                f"    {model_short:<45s} {r['api_calls']:>6,} "
                f"{r['input_tokens']:>10,} {r['output_tokens']:>10,} ${r['total_cost']:>9.4f}"
            )
    print()


def main():
    parser = argparse.ArgumentParser(
        prog="bedrock-usage-check",
        description="Check your personal AWS Bedrock usage and costs.",
    )
    parser.add_argument("-d", "--days", type=int, help="Number of days to query (default: 1)")
    parser.add_argument("--hours", type=int, default=24, help="Number of hours (default: 24)")
    parser.add_argument("--profile", type=str, default=None, help="AWS profile from ~/.aws/credentials (e.g., duo-sso, default)")
    parser.add_argument("--region", default=DEFAULT_REGION, help="AWS region (default: us-east-1)")
    parser.add_argument("--json", dest="json_output", action="store_true", help="Output JSON")
    parser.add_argument("--url", default=None, help="Lambda Function URL override")
    args = parser.parse_args()

    hours = args.days * 24 if args.days else args.hours
    url = get_lambda_url(args.url)
    access_key, secret_key, token, cred_source = get_credentials(profile=args.profile)
    username, arn = get_caller_identity(access_key, secret_key, token, args.region)

    if not args.json_output:
        print("Bedrock Usage Check")
        print("-" * 40)
        print(f"  User:  {username}")
        print(f"  Auth:  {cred_source}")
        print(f"  Querying last {hours} hours...")

    results = query_lambda(url, hours, args.region, access_key, secret_key, token)
    records = parse_and_cost(results)

    if args.json_output:
        out = {
            "user": username,
            "hours_queried": hours,
            "summary": {
                "api_calls": sum(r["api_calls"] for r in records),
                "input_tokens": sum(r["input_tokens"] for r in records),
                "output_tokens": sum(r["output_tokens"] for r in records),
                "total_cost": round(sum(r["total_cost"] for r in records), 6),
            } if records else None,
            "models": [
                {"model": r["model"], "api_calls": r["api_calls"],
                 "input_tokens": r["input_tokens"], "output_tokens": r["output_tokens"],
                 "cache_read_tokens": r["cache_read_tokens"], "cache_write_tokens": r["cache_write_tokens"],
                 "total_cost": round(r["total_cost"], 6)}
                for r in sorted(records, key=lambda x: x["total_cost"], reverse=True)
            ],
        }
        print(json.dumps(out, indent=2))
    else:
        display(records, username, hours)

    return 0 if records else 1


if __name__ == "__main__":
    sys.exit(main())
