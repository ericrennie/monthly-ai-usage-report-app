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

1. Download the DMG matching your Mac, or the ZIP matching Windows x64. A
   macOS ZIP containing the same app is also available as a fallback.
2. On macOS, open the DMG, drag **Monthly AI Usage Report** to Applications,
   and open the single app. The ZIP is a fallback containing the same app.
3. On Windows, extract the ZIP and double-click `START-WINDOWS.bat`.

Unsigned development assets are labeled by their release owner. Follow your
organization's software approval policy; do not bypass endpoint protections.

### First launch on macOS

Developer ID-signed and Apple-notarized releases open normally. If a development
release is unsigned and macOS says it cannot verify **Monthly AI Usage Report**:

1. Click **Done**.
2. Open **System Settings > Privacy & Security** and scroll to **Security**.
3. Click **Open Anyway** beside **Monthly AI Usage Report** within about one hour of the
   blocked launch, authenticate, and confirm **Open**.
4. Open the app again. There is no shell launcher and no second executable approval.

Only continue after verifying the release file's SHA-256 checksum. If **Open
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
`START-HERE.txt` for collection and security details. Closing the final report
tab stops the local app after a short grace period; choose **Quit app** for an
immediate shutdown. A heartbeat watchdog stops the app after a browser crash.

## Build desktop packages

PyInstaller is needed only on the build machine. Build each operating-system
artifact on that operating system:

```text
python -m pip install -r scripts/requirements-build.txt
python scripts/build_desktop_app.py
```

The included GitHub Actions workflow builds Windows x64, macOS Apple Silicon,
and macOS Intel packages, runs the frozen-app self-test on every desktop target,
and verifies browser lifecycle behavior in current Chrome and Firefox. macOS
builds include a single `.app`, a drag-to-Applications DMG, a ZIP fallback, and
checksums.

For frictionless macOS distribution, configure the workflow's optional
Developer ID and Apple notarization secrets. Without those credentials, macOS
correctly requires one manual Gatekeeper approval for the unsigned app.

Configure these repository secrets to sign and notarize macOS releases:

- `MACOS_CERTIFICATE_BASE64`: base64-encoded Developer ID Application `.p12`
- `MACOS_CERTIFICATE_PASSWORD`: password for that `.p12`
- `MACOS_NOTARY_PRIVATE_KEY_BASE64`: base64-encoded App Store Connect API `.p8`
- `MACOS_NOTARY_KEY_ID`: App Store Connect API key ID
- `MACOS_NOTARY_ISSUER_ID`: App Store Connect issuer ID

## Bedrock setup

The public build intentionally contains no default AWS Lambda Function URL.
Enter a verified HTTPS AWS Lambda Function URL and review the preselected egress
acknowledgement before collection. The included collector signs the request with
temporary AWS credentials or a selected AWS profile.

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
- Finder-launched native macOS builds can securely import only the approved AWS
  and certificate variables initialized by the user's login shell for that run.
- Desktop builds use the native macOS or Windows certificate store for HTTPS.
  An administrator-provided PEM bundle may be selected with `SSL_CERT_FILE` or
  `AWS_CA_BUNDLE`; TLS verification is never disabled.
- Local Codex telemetry is not authoritative ChatGPT subscription billing.
- The conventional Codex sessions folder is detected automatically at runtime.
- Circuit figures can be extracted from dashboard text the user explicitly
  pastes. Browser same-origin controls prevent silent cross-tab access, and
  direct clipboard reading may be denied by browser privacy settings.
- Provider token totals are not directly comparable units.
- Word export is a Word-compatible `.doc`; use Print / Save PDF for a PDF copy.
