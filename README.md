# Monthly AI Usage Report App

A local, dependency-free browser app for generating a monthly usage report from:

- Claude Code usage collected through an AWS Bedrock collector
- local Codex session telemetry
- verified figures entered from a Circuit usage dashboard

## Run

Python 3.11 or newer is required; no Python packages need to be installed.

- macOS: double-click `START-MAC.command`
- Windows: double-click `START-WINDOWS.bat`
- terminal: `python3 scripts/report_app.py`

The app opens at `http://127.0.0.1:8765/` and is available only on the local
computer. See `START-HERE.txt` for collection and security details.

## Bedrock setup

The public build intentionally contains no default AWS Lambda Function URL.
Enter a verified HTTPS AWS Lambda Function URL in the app and explicitly approve
egress before collection. The included collector signs the request with temporary
AWS credentials or a selected AWS profile.

## Data boundaries

- Credentials stay in process memory for one collector run and are not saved.
- Local Codex telemetry is not authoritative ChatGPT subscription billing.
- Circuit figures are manually transcribed because browser single sign-on cannot
  be shared with this localhost app.
- Provider token totals are not directly comparable units.
