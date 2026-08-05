# Monthly AI Usage Report App

A local, dependency-free browser app for generating a daily, weekly, monthly,
or custom-range usage report from:

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

This URL is the HTTPS endpoint of the deployed usage-collector Lambda—not a
Bedrock endpoint, function Amazon Resource Name (ARN), access key, or AWS sign-in
URL. If your team provided the collector, ask its owner for the approved URL. Administrators can
find it in AWS Console > Lambda > Functions > select the collector function >
Function overview or Configuration > Function URL, or run:

`aws lambda get-function-url-config --function-name <collector-function-name> --query FunctionUrl --output text`

It looks like `https://abcde12345.lambda-url.us-east-1.on.aws/`. Verify that
the authentication type is AWS Identity and Access Management (`AWS_IAM`). If
the collector Lambda has not been deployed, creating a Function URL alone is
insufficient; the collector owner must deploy the correct function and grant
your AWS identity access.

## Data boundaries

- Credentials stay in process memory for one collector run and are not saved.
- Local Codex telemetry is not authoritative ChatGPT subscription billing.
- Circuit figures can be extracted from dashboard text the user explicitly
  copies or pastes. Browser same-origin controls prevent silent cross-tab access.
- Provider token totals are not directly comparable units.
- Word export is a Word-compatible `.doc`; use Print / Save PDF for a PDF copy.
