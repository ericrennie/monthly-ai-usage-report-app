from __future__ import annotations

import importlib.util
import plistlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


BUILD_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_desktop_app.py"
SPEC = importlib.util.spec_from_file_location("monthly_ai_desktop_builder", BUILD_SCRIPT)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Could not load {BUILD_SCRIPT}")
BUILDER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILDER)


class MacPackagingTests(unittest.TestCase):
    def test_app_metadata_identifies_one_executable(self) -> None:
        metadata = BUILDER.macos_info_plist()
        self.assertEqual(metadata["CFBundleExecutable"], BUILDER.APP_EXECUTABLE)
        self.assertEqual(metadata["CFBundleIdentifier"], BUILDER.MAC_BUNDLE_IDENTIFIER)
        self.assertEqual(metadata["CFBundleDisplayName"], "AI Usage Report")
        self.assertEqual(metadata["CFBundleShortVersionString"], BUILDER.APP_VERSION)

    def test_app_bundle_replaces_two_stage_shell_launcher(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            temporary = Path(temporary_name)
            executable = temporary / "frozen-runtime"
            executable.write_bytes(b"frozen runtime")
            release = temporary / "release"
            release.mkdir()
            with patch.object(BUILDER, "run_checked") as run_checked:
                app, target = BUILDER.create_macos_app(executable, release)

            self.assertEqual(app.name, "AI Usage Report.app")
            self.assertEqual(target, app / "Contents" / "MacOS" / BUILDER.APP_EXECUTABLE)
            self.assertTrue(target.is_file())
            self.assertFalse((release / "START-MAC.command").exists())
            with (app / "Contents" / "Info.plist").open("rb") as handle:
                metadata = plistlib.load(handle)
            self.assertEqual(metadata["CFBundlePackageType"], "APPL")
            self.assertEqual(run_checked.call_count, 2)

    def test_desktop_instructions_name_only_the_app(self) -> None:
        self.assertIn("single AI Usage Report app", BUILDER.DESKTOP_START_HERE)
        self.assertIn("Quit app", BUILDER.DESKTOP_START_HERE)
        self.assertNotIn("START-MAC.command", BUILDER.DESKTOP_START_HERE)


if __name__ == "__main__":
    unittest.main()
