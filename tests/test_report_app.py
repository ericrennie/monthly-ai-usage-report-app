from __future__ import annotations

import importlib.util
import os
import threading
import time
import unittest
from pathlib import Path
from urllib.request import urlopen
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


class BrowserLifecycleTests(unittest.TestCase):
    def make_server(self, *, close_delay: float = 0.03) -> tuple[object, threading.Thread]:
        server = REPORT_APP.ReportServer(
            ("127.0.0.1", 0),
            REPORT_APP.ReportHandler,
            close_grace_seconds=close_delay,
            stale_seconds=60.0,
            watch_interval_seconds=0.01,
            stream_interval_seconds=0.01,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread

    def stop_server(self, server: object, thread: threading.Thread) -> None:
        if thread.is_alive():
            server.request_quit(delay=0.0)
            thread.join(timeout=1.0)
        server.server_close()

    def test_last_browser_tab_stops_server(self) -> None:
        server, thread = self.make_server()
        try:
            self.assertTrue(server.register_client("browser-client-one"))
            server.close_client("browser-client-one")
            thread.join(timeout=1.0)
            self.assertFalse(thread.is_alive())
        finally:
            self.stop_server(server, thread)

    def test_reload_during_grace_period_cancels_shutdown(self) -> None:
        server, thread = self.make_server(close_delay=0.15)
        try:
            self.assertTrue(server.register_client("browser-client-old"))
            server.close_client("browser-client-old")
            time.sleep(0.02)
            self.assertTrue(server.register_client("browser-client-new"))
            time.sleep(0.18)
            self.assertTrue(thread.is_alive())
        finally:
            self.stop_server(server, thread)

    def test_lifecycle_messages_reject_invalid_client_identifier(self) -> None:
        with self.assertRaisesRegex(ValueError, "Invalid browser client identifier"):
            REPORT_APP.validated_client_id({"client_id": "bad id"})

    def test_disconnected_browser_stream_stops_server(self) -> None:
        server, thread = self.make_server()
        try:
            port = server.server_address[1]
            response = urlopen(
                f"http://127.0.0.1:{port}/api/client/watch?client_id=browser-stream-one",
                timeout=1.0,
            )
            self.assertIn(b"browser-lifecycle", response.readline())
            response.close()
            thread.join(timeout=1.0)
            self.assertFalse(thread.is_alive())
        finally:
            self.stop_server(server, thread)


if __name__ == "__main__":
    unittest.main()
