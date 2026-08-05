# Monthly AI Usage Report App

A local, dependency-free browser app for generating a daily, weekly, monthly,
or custom-range usage report from:

- Claude Code usage collected through an AWS Bedrock collector
- local Codex session telemetry
- verified figures entered from a Circuit usage dashboard

## Run

The platform-specific desktop ZIPs include a private Python runtime. Users do
not install Python, need administrator access, or alter system Python.

## Run a desktop build

1. Download the ZIP matching Windows x64, macOS Apple Silicon, or macOS Intel
   from the latest release.
2. Extract it.
3. Double-click `START-WINDOWS.bat` or `START-MAC.command`.

The executable is currently unsigned. Follow your organization's software
approval policy; do not bypass endpoint protections.

### First launch on macOS

If macOS says it cannot verify `START-MAC.command`:

1. Click **Done**.
2. Open **System Settings > Privacy & Security** and scroll to **Security**.
3. Click **Open Anyway** beside `START-MAC.command` within about one hour of the
   blocked launch, authenticate, and confirm **Open**.
4. Keep the Terminal window open while using the browser app.

Only continue after verifying the release ZIP's SHA-256 checksum. If **Open
Anyway** is unavailable on a managed Mac, contact IT. Do not disable Gatekeeper
or remove quarantine attributes.

## Run from source

Python 3.11 or newer is required; no runtime packages need to be installed.

- macOS: double-click `START-MAC.command`
- Windows: double-click `START-WINDOWS.bat`
- terminal: `python3 scripts/report_app.py`

The launcher opens the correct local address in the default browser. The
preferred address is `http://127.0.0.1:8765/`, but the port can differ when that
address is occupied. Use the address opened by the launcher instead of typing a
saved localhost URL. The app remains available only on the local computer. See
`START-HERE.txt` for collection and security details.

## Build desktop packages

PyInstaller is needed only on the build machine. Build each operating-system
artifact on that operating system:

```text
python -m pip install -r scripts/requirements-build.txt
python scripts/build_desktop_app.py
```

The included GitHub Actions workflow builds Windows x64, macOS Apple Silicon,
and macOS Intel packages and runs the frozen-app self-test.

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
