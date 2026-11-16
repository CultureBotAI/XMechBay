"""Synthetic permission-audit tests; no live settings, credentials, or commands.

Inputs are fabricated JSON rules; output is unittest results.
Public infrastructure extraction, 2026-10-25.
"""

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".agents/skills/agy-permissions/scripts/check_agy_permissions.py"
SPEC = importlib.util.spec_from_file_location("check_agy_permissions", SCRIPT)
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)


class PermissionAuditTests(unittest.TestCase):
    def fixture(self):
        return json.loads((ROOT / "examples/permissions.json").read_text())

    def test_validates_sections_without_coercing_strings(self):
        for data in ({}, [], {"permissions": []}, {"permissions": {"allow": "command(x)"}}, {"permissions": {"deny": [4]}}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                audit.audit_settings(data)

    def test_counts_fixture_and_never_outputs_rule_payloads(self):
        result = audit.audit_settings(self.fixture())
        self.assertEqual(result["counts"], {"allow": 3, "ask": 2, "deny": 2})
        self.assertEqual(result["path_rule_count"], 3)
        self.assertEqual(result["regex_rule_count"], 3)
        self.assertNotIn("git status", json.dumps(result))
        self.assertNotIn("credentials", json.dumps(result))

    def test_duplicate_and_invalid_rule_locations(self):
        data = {"permissions": {"allow": ["command(example)", "command(example)", "command(regex:[)", "unknown(x)"], "ask": ["command(example)"]}}
        result = audit.audit_settings(data)
        findings = result["findings"]
        self.assertEqual(sum(item["kind"] == "duplicate_rule" for item in findings), 2)
        self.assertIn({"kind": "invalid_python_regex", "location": "permissions.allow[2]"}, findings)
        self.assertIn({"kind": "unrecognized_rule_syntax", "location": "permissions.allow[3]"}, findings)

    def test_exact_literal_match_and_regex_are_distinct(self):
        data = {"permissions": {"allow": ["command(git status)", "command(regex:git status.*)"]}}
        result = audit.command_diagnostics(data, "git status --short")
        self.assertEqual([item["matched"] for item in result["evidence"]], [False, True])
        self.assertNotIn("verdict", result)

    def test_multiline_diagnostic_does_not_claim_actual_policy(self):
        data = {"permissions": {"allow": ["command(regex:python3 .+)", r"command(regex:python3 [\s\S]+)"]}}
        result = audit.command_diagnostics(data, "python3 -c 'line1\nline2'")
        self.assertEqual(result["evidence"][0]["note"], "matches_only_when_dot_matches_newlines_in_python")
        self.assertFalse(result["evidence"][0]["matched"])
        self.assertTrue(result["evidence"][1]["matched"])

    def test_rule_families_and_policy_sections_are_separate(self):
        data = {"permissions": {"allow": ["command(example)", "unsandboxed(example)"], "deny": ["command(example)"]}}
        result = audit.command_diagnostics(data, "example", "command")
        self.assertEqual([item["location"] for item in result["evidence"]], ["permissions.allow[0]", "permissions.deny[0]"])
        self.assertEqual(result["skipped_rule_count"], 1)

    def test_regex_timeout_is_incomplete_evidence(self):
        with patch.object(audit.subprocess, "run", side_effect=subprocess.TimeoutExpired("worker", 3)):
            result = audit.bounded_diagnostics(self.fixture(), "example", "command")
        self.assertFalse(result["complete"])
        self.assertEqual(result["error"], "regex_worker_failed_or_timed_out")

    def test_overflow_and_deep_regexes_are_reported_without_tracebacks(self):
        depth = sys.getrecursionlimit() + 10
        for payload in ("a{999999999999999999999}", "(" * depth + "a" + ")" * depth):
            with self.subTest(kind="nested" if payload.startswith("(") else "repeat"):
                data = {"permissions": {"allow": ["command(regex:" + payload + ")"]}}
                report = audit.audit_settings(data)
                self.assertIn({"kind": "invalid_python_regex", "location": "permissions.allow[0]"},
                              report["findings"])
                diagnostic = audit.command_diagnostics(data, "a")["evidence"][0]
                self.assertIsNone(diagnostic["matched"])
                self.assertEqual(diagnostic["note"], "invalid_python_regex")
                result = subprocess.run([sys.executable, "-B", str(SCRIPT),
                                         "--settings", "/dev/stdin", "--check-command", "a"],
                                        input=json.dumps(data), text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, "")
                self.assertNotIn(str(SCRIPT), result.stdout)
                self.assertNotIn(payload, result.stdout)
                self.assertEqual(json.loads(result.stdout)["findings"][0]["kind"], "invalid_python_regex")

    def test_cli_is_read_only_and_does_not_execute_checked_command(self):
        with tempfile.TemporaryDirectory() as folder:
            settings = Path(folder) / "settings.json"
            sentinel = Path(folder) / "must-not-exist"
            command = "touch " + str(sentinel)
            content = json.dumps({"permissions": {"allow": ["command(" + command + ")"]}})
            settings.write_text(content)
            result = subprocess.run([sys.executable, "-B", str(SCRIPT), "--settings", str(settings), "--check-command", command],
                                    text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(settings.read_text(), content)
            self.assertFalse(sentinel.exists())
            self.assertEqual(sorted(path.name for path in Path(folder).iterdir()), ["settings.json"])
            self.assertNotIn(str(settings), result.stdout)
            self.assertNotIn(command, result.stdout)
            self.assertTrue(json.loads(result.stdout)["command_check"]["evidence"][0]["matched"])

    def test_cli_requires_settings(self):
        result = subprocess.run([sys.executable, "-B", str(SCRIPT)], text=True, capture_output=True)
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
