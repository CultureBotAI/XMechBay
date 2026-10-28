"""Synthetic port-pressure regression tests; no live host or notification access.

Purpose: validate monitor failure handling for the port-pressure PR, 2026-10-27.
Inputs: fabricated netstat rows, temporary state and mocked local subprocesses.
Outputs: unittest results. No credentials, network requests or session input.
"""

import ast
from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/check_port_pressure.py"
SPEC = importlib.util.spec_from_file_location("port_pressure", SCRIPT)
pressure = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pressure)


def row(port, owner="agy:101", state="LISTEN", proto="tcp4", address="127.0.0.1"):
    return "%s 0 0 %s.%s *.* %s 0 0 131072 131072 %s 0 0 0" % (proto, address, port, state, owner)


class MeasurementTests(unittest.TestCase):
    def test_fixture_deduplicates_host_ports_but_counts_socket_listeners(self):
        report = pressure.analyze((ROOT / "examples/port-pressure-snapshot.txt").read_text(), 49152, 65535)
        self.assertEqual(report["host"]["distinct_local_ports"], 3)
        self.assertEqual(report["largest_processes"][0]["listen_sockets"], 3)
        self.assertEqual(report["status"], "ok")
        serialized = json.dumps(report)
        self.assertNotIn("127.0.0.1", serialized)
        self.assertNotIn("198.51.100", serialized)

    def test_unknown_owner_still_contributes_host_occupancy(self):
        report = pressure.analyze(row(50000) + "\n" + row(50001, "unknown-owner"), 50000, 50003)
        self.assertEqual(report["host"]["distinct_local_ports"], 2)
        self.assertEqual(report["host"]["status"], "warning")
        self.assertEqual(report["attribution"], {"status": "partial", "unattributed_rows": 1})
        self.assertEqual(report["status"], "warning")

    def test_port_measurement_does_not_require_owner_columns(self):
        report = pressure.analyze("tcp4 0 0 *.50000 *.* LISTEN", 50000, 50001)
        self.assertEqual(report["host"]["status"], "warning")
        self.assertEqual(report["attribution"]["status"], "unknown")

    def test_host_threshold_boundaries(self):
        for count, expected in ((1, "ok"), (2, "warning"), (3, "critical"), (4, "critical")):
            with self.subTest(count=count):
                report = pressure.analyze("\n".join(row(50000 + i) for i in range(count)), 50000, 50003)
                self.assertEqual(report["host"]["status"], expected)
                self.assertEqual(report["exit_code"], pressure.EXIT_CODES[expected])

    def test_mixed_dual_stack_rows_reach_critical_pressure(self):
        # Issue #2: ignoring tcp46 hid two ports and incorrectly reported ok.
        snapshot = "\n".join((row(50000), row(50001, proto="tcp46", address="*"),
                              row(50002, proto="tcp46", address="*")))
        report = pressure.analyze(snapshot, 50000, 50003)
        self.assertEqual(report["host"]["distinct_local_ports"], 3)
        self.assertEqual(report["host"]["tcp_rows"], 3)
        self.assertEqual(report["host"]["status"], "critical")
        self.assertEqual(report["largest_processes"][0]["listen_sockets"], 3)
        self.assertEqual(report["exit_code"], 20)

    def test_all_dual_stack_snapshot_is_measured(self):
        snapshot = "\n".join(row(50000 + i, proto="tcp46", address="*") for i in range(3))
        report = pressure.analyze(snapshot, 50000, 50003)
        self.assertEqual(report["status"], "critical")
        self.assertEqual(report["host"]["distinct_local_ports"], 3)
        self.assertEqual(report["attribution"]["status"], "complete")
        self.assertEqual(report["errors"], [])

    def test_process_maintenance_is_separate_from_host_pressure(self):
        report = pressure.analyze("\n".join(row(50000 + i) for i in range(3)), 49152, 65535,
                                  process_warning=2, process_critical=3)
        self.assertEqual(report["host"]["status"], "ok")
        self.assertEqual(report["process_status"], "critical")
        self.assertEqual(report["status"], "critical")
        self.assertEqual(len(report["offenders"]), 1)

    def test_offenders_are_not_limited_to_top_eight(self):
        report = pressure.analyze("\n".join(row(50000 + i, "agy:%s" % (101 + i)) for i in range(12)),
                                  49152, 65535, process_warning=1, process_critical=2)
        self.assertEqual(len(report["offenders"]), 12)
        self.assertEqual(len(report["largest_processes"]), 8)

    def test_established_rows_do_not_count_as_listeners(self):
        report = pressure.analyze(row(50000, state="ESTABLISHED"), 49152, 65535,
                                  process_warning=1, process_critical=2)
        self.assertEqual(report["largest_processes"][0]["listen_sockets"], 0)
        self.assertEqual(report["status"], "ok")

    def test_out_of_range_and_wildcard_ports(self):
        report = pressure.analyze("\n".join([row(443), row(50000), row("*", state="CLOSED", address="*")]),
                                  49152, 65535)
        self.assertEqual(report["host"]["distinct_local_ports"], 1)
        self.assertEqual(report["host"]["unparsed_rows"], 0)

    def test_empty_and_unrecognized_snapshots_never_healthy(self):
        for text in ("", "Proto header only", "not a network measurement", "tcp4 malformed"):
            with self.subTest(text=text):
                report = pressure.analyze(text, 49152, 65535)
                self.assertEqual(report["status"], "unknown")
                self.assertEqual(report["exit_code"], 30)

    def test_partial_measurement_retains_observed_risk_but_reports_unknown(self):
        report = pressure.analyze(row(50000) + "\ntcp4 broken", 50000, 50000)
        self.assertEqual(report["host"]["observed_status"], "critical")
        self.assertEqual(report["host"]["status"], "unknown")
        self.assertEqual(report["status"], "unknown")

    def test_invalid_endpoint_or_state_is_measurement_failure(self):
        for text in (row(70000), row("bad"), row("9" * 5000), row(50000, state="INVALID")):
            with self.subTest(text=text):
                self.assertEqual(pressure.analyze(text, 49152, 65535)["status"], "unknown")

    def test_oversized_owner_pid_does_not_prevent_host_measurement(self):
        report = pressure.analyze(row(50000, "agy:" + "9" * 5000), 50000, 50001)
        self.assertEqual(report["host"]["status"], "warning")
        self.assertEqual(report["attribution"]["status"], "unknown")

    def test_size_limits_fail_visibly(self):
        with patch.object(pressure, "MAX_INPUT_BYTES", 10):
            self.assertEqual(pressure.analyze(row(50000), 49152, 65535)["errors"], ["input_size_limit_exceeded"])
        with patch.object(pressure, "MAX_REPORT_BYTES", 100):
            report = pressure.analyze(row(50000), 49152, 65535, process_warning=1, process_critical=2)
        self.assertEqual(report["status"], "unknown")
        self.assertIn("process_detail_limit_exceeded", report["errors"])
        self.assertEqual(report["offender_count"], 1)

    def test_local_probe_commands_are_read_only_and_fixed(self):
        with patch.object(pressure, "run_probe", side_effect=["49152\n65535\n", row(50000)]) as run:
            text, first, last = pressure.collect()
        self.assertEqual((first, last), (49152, 65535))
        self.assertEqual(run.call_args_list[0].args[0],
                         ["/usr/sbin/sysctl", "-n", "net.inet.ip.portrange.first", "net.inet.ip.portrange.last"])
        self.assertEqual(run.call_args_list[1].args[0], ["/usr/sbin/netstat", "-anv", "-p", "tcp"])
        self.assertEqual(text, row(50000))

    def test_probe_errors_hide_stderr_and_paths(self):
        errors = ((subprocess.TimeoutExpired(["tool"], 10), "probe_timed_out"),
                  (subprocess.CalledProcessError(1, ["tool"], stderr="private-marker"), "probe_failed"),
                  (FileNotFoundError("private-marker"), "probe_failed"))
        for error, expected in errors:
            with self.subTest(expected=expected), patch.object(pressure.subprocess, "run", side_effect=error):
                with self.assertRaises(pressure.ProbeError) as caught:
                    pressure.run_probe(["tool"])
                self.assertEqual(str(caught.exception), expected)


class StateTests(unittest.TestCase):
    def report(self, status="warning"):
        if status == "unknown":
            return pressure.empty_report(now=1000)
        return pressure.analyze(row(50000), 50000, {"ok": 50007, "warning": 50001, "critical": 50000}[status], now=1000)

    def test_private_atomic_files_and_bounded_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "state"
            with pressure.locked_state(directory):
                for _ in range(5):
                    pressure.persist_report(directory, self.report(), 3)
            self.assertEqual(len(json.loads((directory / "history.json").read_text())), 3)
            self.assertTrue(json.loads((directory / "alert.json").read_text())["active"])
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
            for path in directory.iterdir():
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertFalse(path.name.startswith(".port-pressure-"))

    def test_healthy_measurement_clears_alert(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            pressure.persist_report(directory, self.report(), 2)
            pressure.persist_report(directory, self.report("ok"), 2)
            self.assertFalse(json.loads((directory / "alert.json").read_text())["active"])

    def test_nonprivate_directory_and_symlink_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            directory = parent / "state"
            directory.mkdir(mode=0o755)
            with self.assertRaises(pressure.ProbeError):
                with pressure.locked_state(directory):
                    self.fail("unsafe directory accepted")
            link = parent / "linked-state"
            link.symlink_to(directory)
            with self.assertRaises(pressure.ProbeError):
                with pressure.locked_state(link):
                    self.fail("symlink accepted")

    def test_overlapping_run_refuses_without_overwriting(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with pressure.locked_state(directory):
                with self.assertRaisesRegex(pressure.ProbeError, "probe_already_running"):
                    with pressure.locked_state(directory):
                        self.fail("overlap accepted")

    def test_notification_cooldown_escalation_and_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with patch.object(pressure.subprocess, "run") as run:
                self.assertEqual(pressure.notify(directory, self.report(), 900, now=1000)["status"], "requested")
                self.assertEqual(pressure.notify(directory, self.report(), 900, now=1001)["status"], "cooldown")
                self.assertEqual(pressure.notify(directory, self.report("critical"), 900, now=1002)["status"], "requested")
                self.assertEqual(run.call_count, 2)
                self.assertEqual(run.call_args.args[0][0], "/usr/bin/osascript")
                self.assertIn("host pressure:", run.call_args.args[0][-1])
                self.assertEqual(run.call_args.kwargs["timeout"], 10)
            with patch.object(pressure.subprocess, "run", side_effect=OSError()):
                self.assertEqual(pressure.notify(directory, self.report("unknown"), 900, now=1003)["status"], "failed")
            with patch.object(pressure.subprocess, "run") as run:
                self.assertEqual(pressure.notify(directory, self.report("unknown"), 900, now=1004)["status"], "cooldown")
                run.assert_not_called()

    def test_no_notification_when_healthy(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(pressure.subprocess, "run") as run:
            self.assertEqual(pressure.notify(Path(temporary), self.report("ok"), 900)["status"], "not_needed")
            run.assert_not_called()

    def test_new_host_pressure_bypasses_existing_process_critical_cooldown(self):
        # Issue #3: overall critical can hide a newly critical host.
        snapshots = ["\n".join(row(50000 + i) for i in range(count)) for count in (2, 4, 6)]
        reports = [pressure.analyze(snapshot, 50000, 50007, process_warning=1, process_critical=2)
                   for snapshot in snapshots]
        self.assertEqual([report["status"] for report in reports], ["critical"] * 3)
        self.assertEqual([report["host"]["status"] for report in reports], ["ok", "warning", "critical"])
        with tempfile.TemporaryDirectory() as temporary, patch.object(pressure.subprocess, "run") as run:
            directory = Path(temporary)
            for index, report in enumerate(reports):
                self.assertEqual(pressure.notify(directory, report, 900, now=1000 + index)["status"], "requested")
            self.assertEqual(pressure.notify(directory, reports[-1], 900, now=1004)["status"], "cooldown")
            self.assertEqual(run.call_count, 3)

    def test_unknown_to_confirmed_critical_bypasses_cooldown(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(pressure.subprocess, "run") as run:
            directory = Path(temporary)
            self.assertEqual(pressure.notify(directory, self.report("unknown"), 900, now=1000)["status"], "requested")
            self.assertEqual(pressure.notify(directory, self.report("critical"), 900, now=1001)["status"], "requested")
            self.assertEqual(run.call_count, 2)

    def test_observed_critical_is_retained_during_unknown_measurement(self):
        unknown_critical = pressure.analyze(row(50000) + "\ntcp4 broken", 50000, 50000)
        self.assertEqual(unknown_critical["status"], "unknown")
        with tempfile.TemporaryDirectory() as temporary, patch.object(pressure.subprocess, "run") as run:
            directory = Path(temporary)
            pressure.notify(directory, self.report("unknown"), 900, now=1000)
            self.assertEqual(pressure.notify(directory, unknown_critical, 900, now=1001)["status"], "requested")
            state = json.loads((directory / "notification.json").read_text())
            self.assertEqual(state["last_observed"]["host"], "critical")
            self.assertEqual(state["last_observed"]["measurement"], "unknown")
            self.assertEqual(pressure.notify(directory, self.report("critical"), 900, now=1002)["status"], "requested")
            self.assertEqual(run.call_count, 3)

    def test_recovery_then_recurrence_bypasses_cooldown(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(pressure.subprocess, "run") as run:
            directory = Path(temporary)
            pressure.notify(directory, self.report("critical"), 900, now=1000)
            self.assertEqual(pressure.notify(directory, self.report("ok"), 900, now=1001)["status"], "not_needed")
            self.assertEqual(pressure.notify(directory, self.report("critical"), 900, now=1002)["status"], "requested")
            self.assertEqual(run.call_count, 2)

    def test_legacy_notification_state_gets_one_fresh_alert(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(pressure.subprocess, "run") as run:
            directory = Path(temporary)
            (directory / "notification.json").write_text('{"status": "critical", "last_attempt_epoch": 1000}')
            self.assertEqual(pressure.notify(directory, self.report("critical"), 900, now=1001)["status"], "requested")
            self.assertEqual(pressure.notify(directory, self.report("critical"), 900, now=1002)["status"], "cooldown")
            self.assertEqual(json.loads((directory / "notification.json").read_text())["schema_version"], 2)
            self.assertEqual(run.call_count, 1)

    def test_malformed_notification_state_cannot_trigger_retry_storm(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            invalid = [{"status": [], "last_attempt_epoch": 1000},
                       {"status": "warning", "last_attempt_epoch": 1000,
                        "last_observed": {"host": "ok", "process": "warning", "measurement": []}}]
            for state in invalid:
                with self.subTest(state=state):
                    (directory / "notification.json").write_text(json.dumps(state))
                    with patch.object(pressure.subprocess, "run") as run:
                        with self.assertRaisesRegex(pressure.ProbeError, "notification_state_invalid"):
                            pressure.notify(directory, self.report(), 900, now=1001)
                        run.assert_not_called()

    def test_logging_precedes_notification(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            def notification(*args, **kwargs):
                self.assertTrue((directory / "latest.json").is_file())
                self.assertTrue(json.loads((directory / "alert.json").read_text())["active"])
                self.assertEqual(len(json.loads((directory / "history.json").read_text())), 1)
                return {"status": "requested"}
            with patch.object(pressure, "measure", return_value=self.report()), patch.object(pressure, "notify", side_effect=notification), redirect_stdout(io.StringIO()):
                code = pressure.main(["--state-dir", str(directory), "--notify"])
            self.assertEqual(code, 10)

    def test_corrupt_history_is_durable_unknown_not_stale_green(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "history.json").write_text("not json")
            with patch.object(pressure, "measure", return_value=self.report("ok")), redirect_stdout(io.StringIO()):
                code = pressure.main(["--state-dir", str(directory)])
            self.assertEqual(code, 30)
            self.assertEqual(json.loads((directory / "latest.json").read_text())["status"], "unknown")
            self.assertTrue(json.loads((directory / "alert.json").read_text())["active"])


class CliTests(unittest.TestCase):
    def test_script_parses_as_python39(self):
        ast.parse(SCRIPT.read_text(), feature_version=(3, 9))

    def test_offline_cli_works_without_network_or_state(self):
        result = subprocess.run([sys.executable, "-B", str(SCRIPT), "--snapshot",
                                 str(ROOT / "examples/port-pressure-snapshot.txt"), "--first", "49152", "--last", "65535"],
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["host"]["distinct_local_ports"], 3)

    def test_invalid_options_and_nonfinite_thresholds_rejected(self):
        invalid = (["--notify"], ["--first", "1"], ["--host-warning", "nan"],
                   ["--host-critical", "inf"], ["--history-limit", "121"],
                   ["--process-warning", "1001"], ["--snapshot", "example"],
                   ["--notification-cooldown", "0"])
        for args in invalid:
            with self.subTest(args=args), redirect_stdout(io.StringIO()), patch("sys.stderr", new=io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    pressure.parser_args(args)
                self.assertEqual(caught.exception.code, 2)

    def test_probe_failure_is_unknown_json_without_raw_error(self):
        output = io.StringIO()
        with patch.object(pressure, "collect", side_effect=pressure.ProbeError("probe_failed")), redirect_stdout(output):
            self.assertEqual(pressure.main([]), 30)
        report = json.loads(output.getvalue())
        self.assertEqual(report["errors"], ["probe_failed"])
        self.assertEqual(report["status"], "unknown")


if __name__ == "__main__":
    unittest.main()
