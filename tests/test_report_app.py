from __future__ import annotations

import importlib.util
import os
import unittest
from pathlib import Path
from unittest.mock import patch


REPORT_APP_PATH = Path(__file__).resolve().parents[1] / "scripts" / "report_app.py"
SPEC = importlib.util.spec_from_file_location("monthly_ai_report_app", REPORT_APP_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Could not load {REPORT_APP_PATH}")
REPORT_APP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REPORT_APP)


class TlsTrustTests(unittest.TestCase):
    def test_explicit_ssl_bundle_takes_precedence(self) -> None:
        with patch.dict(os.environ, {"SSL_CERT_FILE": "/approved/company-ca.pem"}, clear=False):
            source, error = REPORT_APP.configure_tls_trust()
        self.assertEqual(source, "environment")
        self.assertIsNone(error)

    def test_aws_ca_bundle_is_supported_by_stdlib_collector(self) -> None:
        environment = {
            key: value
            for key, value in os.environ.items()
            if key not in {"SSL_CERT_FILE", "SSL_CERT_DIR", "AWS_CA_BUNDLE"}
        }
        environment["AWS_CA_BUNDLE"] = "/approved/company-ca.pem"
        with patch.dict(os.environ, environment, clear=True):
            source, error = REPORT_APP.configure_tls_trust()
            configured = os.environ.get("SSL_CERT_FILE")
        self.assertEqual(source, "aws_ca_bundle")
        self.assertIsNone(error)
        self.assertEqual(configured, "/approved/company-ca.pem")


class BedrockFailureTests(unittest.TestCase):
    def test_certificate_failure_is_not_reported_as_bad_credentials(self) -> None:
        result = REPORT_APP.bedrock_failure(
            "Error: Could not verify AWS identity -- <urlopen error "
            "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
            "unable to get local issuer certificate>",
            "2026-08-05T12:00:00-05:00",
        )
        self.assertEqual(result["failure"]["category"], "certificate_verification_failed")
        self.assertNotIn("refresh", result["failure"]["remediation"].lower())
        self.assertIn("Do not disable TLS", result["failure"]["remediation"])


if __name__ == "__main__":
    unittest.main()
