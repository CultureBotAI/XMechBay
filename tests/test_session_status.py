"""Fixture tests for read-only status inventory; no SSH or live sessions.

Inputs are synthetic process/pane snapshots; output is unittest results.
Public infrastructure extraction, 2026-10-25.
"""

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".agents/skills/session-status/scripts/session_status.py"
SPEC = importlib.util.spec_from_file_location("session_status", SCRIPT)
status = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(status)


class SessionStatusTests(unittest.TestCase):
    def fixture(self):
        return json.loads((ROOT / "examples/session-snapshot.json").read_text())

    def test_host_rejects_options_users_and_shell_syntax(self):
        for bad in ("-oProxyCommand=bad", "name@example.invalid", "host;touch file", "host/other", "host:22", "a..b", "a\n"):
            with self.subTest(host=bad), self.assertRaises(argparse.ArgumentTypeError):
                status.host_name(bad)
        self.assertEqual(status.host_name("worker.example.invalid"), "worker.example.invalid")
        self.assertEqual(status.host_name("192.0.2.10"), "192.0.2.10")

    def test_process_parser_uses_executable_not_arbitrary_substring(self):
        rows = status.parse_processes("101 100 pts/0 codex\n102 100 pts/0 codex-helper\ninvalid row\n")
        self.assertEqual([row["executable"] for row in rows], ["codex", "codex-helper"])
        snapshot = {"processes": rows, "panes": []}
        self.assertEqual([row["pid"] for row in status.agent_rows(snapshot)], [101])

    def test_pane_parser_paths_are_opt_in(self):
        text = "%1\t100\tpts/0\tdemo\tcodex\texamples/demo-repo\n"
        self.assertNotIn("cwd", status.parse_panes(text)[0])
        self.assertEqual(status.parse_panes(text, True)[0]["cwd"], "examples/demo-repo")

    def test_ancestry_maps_agent_to_shell_pane(self):
        rows = status.agent_rows(self.fixture())
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["pane_info"]["pane"], "%1")
        self.assertNotIn("pane_info", rows[1])

    def test_cyclic_ancestry_terminates(self):
        processes = {10: {"ppid": 11}, 11: {"ppid": 10}}
        self.assertFalse(status.belongs_to_pane(10, 99, processes))

    def test_tmux_command_fallback_has_no_invented_agent_pid(self):
        snapshot = self.fixture()
        snapshot["processes"] = []
        row = status.agent_rows(snapshot)[0]
        self.assertEqual(row["agent"], "codex")
        self.assertIsNone(row["pid"])

    def test_report_omits_screen_and_paths_by_default(self):
        snapshot = self.fixture()
        snapshot["panes"][0]["screen"] = "synthetic-private-screen-marker"
        result = status.report(snapshot)
        serialized = json.dumps(result)
        self.assertNotIn("synthetic-private-screen-marker", serialized)
        self.assertNotIn("examples/demo-repo", serialized)
        self.assertEqual([row["status"] for row in result["sessions"]], ["UNKNOWN", "UNKNOWN"])

    def test_classifier_gets_screen_without_exposing_it(self):
        received = []
        def classify(screen):
            received.append(screen)
            return "Working"
        result = status.report(self.fixture(), True, classify)
        self.assertEqual(result["sessions"][0]["status"], "Working")
        self.assertEqual(result["sessions"][0]["cwd"], "examples/demo-repo")
        self.assertEqual(len(received), 1)
        self.assertNotIn("screen", result["sessions"][0])

    def test_collector_handles_missing_tmux_without_losing_processes(self):
        process = subprocess.CompletedProcess([], 0, "101 1 pts/0 codex\n", "")
        with patch.object(status.subprocess, "run", side_effect=[process, FileNotFoundError()]):
            snapshot = status.collect()
        self.assertEqual(len(snapshot["processes"]), 1)
        self.assertEqual(snapshot["errors"], ["tmux_executable_unavailable"])

    def test_tmux_failures_report_coverage_without_exposing_stderr(self):
        process = subprocess.CompletedProcess([], 0, "101 1 pts/0 codex\n", "")
        failures = (
            (subprocess.TimeoutExpired(["tmux"], 15), "tmux_probe_timed_out"),
            (subprocess.CalledProcessError(1, ["tmux"], stderr="synthetic-private-error"),
             "tmux_server_unavailable_or_inaccessible"),
            (PermissionError("synthetic-private-error"), "tmux_probe_failed"),
        )
        for failure, expected in failures:
            with self.subTest(expected=expected):
                with patch.object(status.subprocess, "run", side_effect=[process, failure]):
                    snapshot = status.collect()
                self.assertEqual(snapshot["errors"], [expected])
                self.assertEqual(len(status.report(snapshot)["sessions"]), 1)
                self.assertNotIn("synthetic-private-error", json.dumps(snapshot))

    def test_malformed_snapshots_fail_with_generic_json_and_no_traceback(self):
        invalid = [[], None, "synthetic-private-marker",
                   {"processes": {}, "panes": []},
                   {"processes": [None], "panes": []},
                   {"processes": [], "panes": [], "errors": "synthetic-private-marker"},
                   {"processes": [], "panes": [], "errors": ["synthetic-private-marker"]}]
        for snapshot in invalid:
            with self.subTest(snapshot=snapshot):
                result = subprocess.run([sys.executable, "-B", str(SCRIPT),
                                         "--snapshot", "/dev/stdin"], input=json.dumps(snapshot),
                                        text=True, capture_output=True)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                error = json.loads(result.stderr)
                self.assertEqual(error["error"], "status probe could not complete")
                self.assertNotIn("synthetic-private-marker", result.stderr)
                self.assertNotIn(str(SCRIPT), result.stderr)
                self.assertNotIn("Traceback", result.stderr)

    def test_snapshot_identity_field_types_are_validated(self):
        invalid_fields = (("processes", "pid", True), ("processes", "ppid", -1),
                          ("processes", "tty", []), ("processes", "executable", None),
                          ("panes", "pid", 0), ("panes", "pane", "%1;command"),
                          ("panes", "session", {}), ("panes", "screen", []),
                          ("panes", "cwd", False))
        for collection, field, value in invalid_fields:
            with self.subTest(collection=collection, field=field):
                snapshot = self.fixture()
                snapshot[collection][0][field] = value
                with self.assertRaises(ValueError):
                    status.report(snapshot)

    def test_known_probe_error_codes_are_retained(self):
        snapshot = self.fixture()
        snapshot["errors"] = ["process_probe_failed", "screen_probe_failed:%1"]
        self.assertEqual(status.report(snapshot)["errors"], snapshot["errors"])

    def test_offline_cli_needs_no_host_and_hides_screen(self):
        result = subprocess.run([sys.executable, "-B", str(SCRIPT), "--snapshot", str(ROOT / "examples/session-snapshot.json")],
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(json.loads(result.stdout)["sessions"]), 2)
        self.assertNotIn("synthetic demonstration", result.stdout)

    def test_cli_requires_explicit_source(self):
        result = subprocess.run([sys.executable, "-B", str(SCRIPT)], text=True, capture_output=True)
        self.assertEqual(result.returncode, 2)

    def test_offline_screen_classification_uses_shared_contract(self):
        result = subprocess.run([sys.executable, "-B", str(SCRIPT), "--snapshot", str(ROOT / "examples/session-snapshot.json"), "--screens"],
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["sessions"][0]["status"], "Working")
        self.assertNotIn("esc to interrupt", result.stdout)

    def test_screen_inspection_without_bytecode_flag_creates_no_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            inspector = root / ".agents/skills/session-status/scripts/session_status.py"
            classifier = root / ".agents/skills/mac-orchestration/scripts/mac_term_control.py"
            inspector.parent.mkdir(parents=True)
            classifier.parent.mkdir(parents=True)
            shutil.copyfile(SCRIPT, inspector)
            shutil.copyfile(ROOT / classifier.relative_to(root), classifier)
            before = {path.relative_to(root) for path in root.rglob("*")}
            environment = {key: value for key, value in os.environ.items()
                           if key != "PYTHONDONTWRITEBYTECODE"}
            result = subprocess.run([sys.executable, str(inspector), "--snapshot",
                                     str(ROOT / "examples/session-snapshot.json"), "--screens"],
                                    text=True, capture_output=True, env=environment)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["sessions"][0]["status"], "Working")
            self.assertEqual({path.relative_to(root) for path in root.rglob("*")}, before)


if __name__ == "__main__":
    unittest.main()
