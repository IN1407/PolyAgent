import os
import plistlib
import tempfile
import unittest
from pathlib import Path

from polyagent.discovery import HARNESSES, codex_app_cli, discover


class HarnessDiscovery(unittest.TestCase):
    def make_codex_app(self, root):
        bundle = Path(root) / "ChatGPT.app"
        (bundle / "Contents/MacOS").mkdir(parents=True)
        (bundle / "Contents/Info.plist").write_bytes(plistlib.dumps({
            "CFBundleIdentifier": "com.openai.codex", "CFBundleName": "ChatGPT",
            "CFBundleExecutable": "ChatGPT", "CFBundleShortVersionString": "1.0"}))
        executable = bundle / "Contents/MacOS/ChatGPT"
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o755)
        bundled_cli = bundle / HARNESSES[0]["app_cli"][0]
        bundled_cli.parent.mkdir(parents=True)
        bundled_cli.write_text("#!/bin/sh\nexit 0\n")
        bundled_cli.chmod(0o755)
        return bundle, bundled_cli

    def test_working_cli_is_preferred_to_installed_app(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle, _ = self.make_codex_app(directory)
            entry = discover(app_roots=(directory,), which=lambda command: "/tmp/codex" if command == "codex" else None,
                             probe=lambda path: (True, "codex 1.0"))[0]
            self.assertEqual(entry["selected"], "cli")
            self.assertEqual(entry["app"]["path"], str(bundle))
            self.assertTrue(entry["connectable"])

    def test_failed_or_missing_cli_falls_back_to_app(self):
        with tempfile.TemporaryDirectory() as directory:
            _, bundled_cli = self.make_codex_app(directory)
            for path in ("/tmp/broken-codex", None):
                with self.subTest(path=path):
                    entry = discover(app_roots=(directory,), which=lambda command: path if command == "codex" else None,
                                     probe=lambda command: (False, "exited"))[0]
                    self.assertEqual(entry["selected"], "app")
                    self.assertEqual(codex_app_cli(entry["app"]), str(bundled_cli))
                    self.assertTrue(entry["connectable"])

    def test_antigravity_app_offers_verified_connection_path(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory) / "Antigravity.app"
            (bundle / "Contents/MacOS").mkdir(parents=True)
            (bundle / "Contents/Info.plist").write_bytes(plistlib.dumps({
                "CFBundleIdentifier": "com.google.antigravity", "CFBundleName": "Antigravity",
                "CFBundleExecutable": "Antigravity"}))
            executable = bundle / "Contents/MacOS/Antigravity"
            executable.write_text("#!/bin/sh\nexit 0\n")
            executable.chmod(0o755)
            entry = discover(app_roots=(directory,), which=lambda command: None)[1]
            self.assertEqual(entry["selected"], "app")
            self.assertTrue(entry["connectable"])
            self.assertEqual(entry["integration"], "antigravity_cli_or_app")

    def test_claude_desktop_is_not_detected_as_claude_code(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory) / "Claude.app"
            (bundle / "Contents/MacOS").mkdir(parents=True)
            (bundle / "Contents/Info.plist").write_bytes(plistlib.dumps({
                "CFBundleIdentifier": "com.anthropic.claudefordesktop",
                "CFBundleName": "Claude", "CFBundleExecutable": "Claude"}))
            executable = bundle / "Contents/MacOS/Claude"
            executable.write_text("#!/bin/sh\nexit 0\n")
            executable.chmod(0o755)
            entry = next(item for item in discover(app_roots=(directory,), which=lambda command: None)
                         if item["id"] == "claude_code")
            self.assertIsNone(entry["selected"])
            self.assertFalse(entry["connectable"])


if __name__ == "__main__":
    unittest.main()
