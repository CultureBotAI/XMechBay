"""Fleet CLI/cache/health evidence tests with synthetic hosts, 2026-10-27."""

from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).parents[1] / ".agents/skills/fleet-session-status/scripts/fleet_status.py"
SPEC = importlib.util.spec_from_file_location("fleet_status_test", SCRIPT)
FLEET = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FLEET)
NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)


def snapshot():
    return {"schema_version": 1, "observed_at": NOW.isoformat(), "sessions": [{
        "agent": "agy", "pid": 101, "ppid": 100, "tty": "ttys001",
        "started_at": (NOW - timedelta(hours=1)).isoformat(), "age_seconds": 3600,
        "cwd": "/work/SampleMech", "screen": "Completed example review.\n>\n? for shortcuts Gemini Example · high",
        "title": "", "target": {"transport": "terminal", "window_id": 12, "tab": 1},
        "session_id": "12345678-1234-1234-1234-123456789abc", "session_id_source": "argv", "errors": []
    }], "coverage": {"processes": "complete", "terminal": "complete", "tmux": "unavailable"},
        "errors": [], "health": {"error": None, "report": {
            "schema_version": 1, "observed_at": (NOW - timedelta(seconds=10)).isoformat(),
            "status": "ok", "host": {"status": "ok", "occupancy_ratio": 0.02},
            "process_status": "ok", "attribution": {"status": "partial"},
            "largest_processes": [{"program": "agy", "pid": 101, "listen_sockets": 2, "status": "ok"}], "offenders": []}}}


class HealthAndIdentityTests(unittest.TestCase):
    def test_observed_tmux_memberships_survive_normalization(self):
        data = snapshot()
        data["sessions"][0]["target"] = {"transport": "tmux", "pane": "%1",
            "tmux_session": "fleet", "tmux_sessions": ["fleet", "viewer"], "window": "SampleMech"}
        row = FLEET.normalize_host("a", "A", data, now=NOW)["sessions"][0]
        self.assertEqual(row["target"]["tmux_sessions"], ["fleet", "viewer"])
        for invalid in ("fleet", ["fleet", {"unexpected": "payload"}], None):
            data["sessions"][0]["target"]["tmux_sessions"] = invalid
            with self.assertRaises(ValueError):
                FLEET.validate_snapshot(data)

    def test_normalized_report_omits_raw_screen_path_and_title(self):
        report = FLEET.normalize_host("a", "Laptop A", snapshot(), now=NOW)
        row = report["sessions"][0]
        self.assertEqual(row["listeners"]["count"], 2)
        self.assertEqual(row["repo_label"], "SampleMech")
        self.assertNotIn("screen", row)
        self.assertNotIn("cwd", row)
        self.assertNotIn("title", row)
        self.assertNotIn("/work/", json.dumps(report))
        self.assertEqual(row["session_id_source"], "argv")

    def test_absent_pid_is_unknown_not_zero(self):
        data = snapshot()
        data["health"]["report"]["largest_processes"] = []
        report = FLEET.normalize_host("a", "A", data, now=NOW)
        self.assertIsNone(report["sessions"][0]["listeners"]["count"])

    def test_health_older_than_process_cannot_match_reused_pid(self):
        data = snapshot()
        data["sessions"][0]["started_at"] = (NOW - timedelta(seconds=2)).isoformat()
        report = FLEET.normalize_host("a", "A", data, now=NOW)
        self.assertIsNone(report["sessions"][0]["listeners"]["count"])

    def test_stale_health_keeps_age_and_does_not_supply_counts(self):
        data = snapshot()
        data["health"]["report"]["observed_at"] = (NOW - timedelta(minutes=5)).isoformat()
        report = FLEET.normalize_host("a", "A", data, now=NOW)
        self.assertEqual(report["health"]["status"], "stale")
        self.assertEqual(report["health"]["age_seconds"], 300)
        self.assertIsNone(report["sessions"][0]["listeners"]["count"])

    def test_future_or_invalid_health_is_unknown(self):
        for changes in ({"observed_at": (NOW + timedelta(minutes=1)).isoformat()}, {"schema_version": 8}):
            data = snapshot()
            data["health"]["report"].update(changes)
            report = FLEET.normalize_host("a", "A", data, now=NOW)
            self.assertEqual(report["health"]["status"], "unknown")

    def test_malformed_nested_health_status_is_unknown_without_losing_sessions(self):
        data = snapshot()
        data["health"]["report"]["process_status"] = []
        data["health"]["report"]["largest_processes"][0]["status"] = []
        report = FLEET.normalize_host("a", "A", data, now=NOW)
        self.assertEqual(report["health"]["process_status"], "unknown")
        self.assertEqual(len(report["sessions"]), 1)
        self.assertFalse(report["sessions"][0]["session_id_current_verified"])

    def test_unverified_pane_remains_visible_without_fake_pid(self):
        data = snapshot()
        data["sessions"][0].update(pid=None, started_at=None, age_seconds=None,
            session_id=None, session_id_source=None, screen="", errors=["agent_process_unverified"],
            target={"transport": "tmux", "pane": "%3", "tmux_session": "fleet", "window": "SampleMech"})
        report = FLEET.normalize_host("a", "A", data, now=NOW)
        self.assertEqual(len(report["sessions"]), 1)
        self.assertEqual(report["sessions"][0]["status"], "UNKNOWN")

    def test_unproven_id_and_none_screen_are_rejected(self):
        for change in ({"session_id_source": "title"}, {"screen": None}):
            data = snapshot()
            data["sessions"][0].update(change)
            with self.assertRaises(ValueError):
                FLEET.validate_snapshot(data)


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / "cache"

    def report(self, seconds=0, change=None):
        data = snapshot()
        data["observed_at"] = (NOW + timedelta(seconds=seconds)).isoformat()
        if change:
            data["sessions"][0].update(change)
        return FLEET.normalize_host("a", "A", data, now=NOW + timedelta(seconds=seconds))

    def test_first_unchanged_changed_are_observation_deltas(self):
        first = self.report()
        FLEET.compare_cache(first, self.directory, "worker-a.example.invalid")
        self.assertEqual(first["sessions"][0]["delta"]["kind"], "first")
        second = self.report(60)
        FLEET.compare_cache(second, self.directory, "worker-a.example.invalid")
        self.assertEqual(second["sessions"][0]["delta"], {"kind": "unchanged", "seconds": 60, "sample_gap_seconds": 60})
        changed = self.report(90, {"screen": "Reviewed 12 examples.\n>\n? for shortcuts Gemini Example · high"})
        FLEET.compare_cache(changed, self.directory, "worker-a.example.invalid")
        self.assertEqual(changed["sessions"][0]["delta"]["kind"], "changed")
        stored = (self.directory / "a.json").read_text()
        self.assertNotIn("Reviewed", stored)
        self.assertNotIn("SampleMech", stored)
        self.assertEqual((self.directory / "a.json").stat().st_mode & 0o777, 0o600)

    def test_pid_reuse_or_changed_host_is_new_observation(self):
        first = self.report()
        FLEET.compare_cache(first, self.directory, "worker-a.example.invalid")
        reused = self.report(60, {"started_at": (NOW + timedelta(seconds=30)).isoformat()})
        FLEET.compare_cache(reused, self.directory, "worker-a.example.invalid")
        self.assertEqual(reused["sessions"][0]["delta"]["kind"], "first")
        moved = self.report(90)
        FLEET.compare_cache(moved, self.directory, "worker-b.example.invalid")
        self.assertEqual(moved["sessions"][0]["delta"]["kind"], "first")

    def test_parser_upgrade_does_not_claim_session_change(self):
        first = self.report()
        FLEET.compare_cache(first, self.directory, "worker-a.example.invalid")
        parser_directory = Path(self.temporary.name) / "changed-parser"
        parser_directory.mkdir()
        (parser_directory / "fleet_view.py").write_text("# synthetic different parser revision\n")
        later = self.report(30)
        with patch.object(FLEET, "SCRIPT_DIR", parser_directory):
            FLEET.compare_cache(later, self.directory, "worker-a.example.invalid")
        self.assertEqual(later["sessions"][0]["delta"]["kind"], "first")

    def test_corrupt_or_nonprivate_cache_does_not_hide_live_state(self):
        self.directory.mkdir(mode=0o700)
        (self.directory / "a.json").write_text("not-json")
        report = self.report()
        FLEET.compare_cache(report, self.directory, "worker-a.example.invalid")
        self.assertEqual(report["cache_error"], "comparison_cache_unavailable")
        self.assertTrue(report["reachable"])

    def test_incomplete_inventory_cannot_invent_departures_or_erase_baseline(self):
        first = self.report()
        FLEET.compare_cache(first, self.directory, "worker-a.example.invalid")
        baseline = (self.directory / "a.json").read_bytes()
        for coverage in ({"processes": "unknown"}, {"processes": "partial"},
                         {"processes": "complete", "agent_discovery": "partial"}):
            incomplete = self.report(30)
            incomplete["coverage"].update(coverage)
            incomplete["sessions"] = []
            FLEET.compare_cache(incomplete, self.directory, "worker-a.example.invalid")
            self.assertNotIn("changes", incomplete)
            self.assertEqual(incomplete["cache_error"], "comparison_incomplete_inventory")
            self.assertEqual((self.directory / "a.json").read_bytes(), baseline)
        final = self.report(60)
        FLEET.compare_cache(final, self.directory, "worker-a.example.invalid")
        self.assertEqual(final["sessions"][0]["delta"]["kind"], "unchanged")
        self.assertEqual(final["changes"]["departed"], 0)


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "config.json"
        self.config = {"schema_version": 1, "hosts": {
            "a": {"host": "worker-a.example.invalid", "label": "Laptop A"},
            "b": {"host": "worker-b.example.invalid", "label": "Laptop B"}}, "aliases": {"first": "a"}}
        self.path.write_text(json.dumps(self.config))

    def test_aliases_and_combined_selection_preserve_order_without_duplicates(self):
        self.assertEqual(FLEET.selected_keys(["b", "first", "a"], self.config["hosts"], self.config["aliases"]), ["b", "a"])

    @patch.dict(FLEET.os.environ, {}, clear=True)
    @patch.object(FLEET.sys, "platform", "darwin")
    @patch.object(FLEET, "open_excel")
    @patch("fleet_excel.export_reports")
    @patch.object(FLEET, "fetch_snapshot", side_effect=lambda host: snapshot())
    def test_live_tables_open_same_collection_in_excel_at_150(self, fetch, export, opened):
        export.return_value = Path(self.temporary.name) / "fleet.xlsx"
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = FLEET.main(["a", "b", "--config", str(self.path), "--no-cache"])
        self.assertEqual(code, 0)
        self.assertEqual(fetch.call_count, 2)
        reports = export.call_args.args[0]
        self.assertEqual([report["key"] for report in reports], ["a", "b"])
        self.assertEqual(out.getvalue().count("| Session · target |"), 2)
        self.assertEqual(reports[0]["observed_at"], snapshot()["observed_at"])
        self.assertEqual(export.call_args.kwargs["zoom"], 150)
        opened.assert_called_once_with(export.return_value, zoom=150)

    @patch.dict(FLEET.os.environ, {}, clear=True)
    @patch.object(FLEET.sys, "platform", "darwin")
    @patch.object(FLEET, "open_excel")
    @patch("fleet_excel.export_reports")
    @patch.object(FLEET, "fetch_snapshot", side_effect=lambda host: snapshot())
    def test_terminal_json_and_list_modes_do_not_export(self, fetch, export, opened):
        for option in ("--no-excel", "--json", "--list"):
            with self.subTest(option=option), redirect_stdout(io.StringIO()):
                self.assertEqual(FLEET.main(["a", "--config", str(self.path), "--no-cache", option]), 0)
        export.assert_not_called()
        opened.assert_not_called()

    @patch.dict(FLEET.os.environ, {"SSH_CONNECTION": "synthetic"}, clear=True)
    @patch.object(FLEET.sys, "platform", "darwin")
    @patch("fleet_excel.export_reports")
    @patch.object(FLEET, "fetch_snapshot", side_effect=lambda host: snapshot())
    def test_ssh_invocation_does_not_default_to_gui(self, fetch, export):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(FLEET.main(["a", "--config", str(self.path), "--no-cache"]), 0)
        export.assert_not_called()

    @patch.dict(FLEET.os.environ, {}, clear=True)
    @patch.object(FLEET.sys, "platform", "darwin")
    @patch.object(FLEET, "open_excel", side_effect=RuntimeError("synthetic_open_failure"))
    @patch("fleet_excel.export_reports")
    @patch.object(FLEET, "fetch_snapshot", side_effect=lambda host: snapshot())
    def test_gui_failure_preserves_terminal_table_and_reports_failure(self, fetch, export, opened):
        export.return_value = Path(self.temporary.name) / "fleet.xlsx"
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(FLEET.main(["a", "--config", str(self.path), "--no-cache"]), 1)
        self.assertIn("| Session · target |", out.getvalue())
        self.assertIn("synthetic_open_failure", err.getvalue())
        self.assertIn(str(export.return_value), err.getvalue())

    @patch.dict(FLEET.os.environ, {}, clear=True)
    @patch.object(FLEET.sys, "platform", "darwin")
    @patch.object(FLEET, "open_excel")
    @patch("fleet_excel.export_reports")
    @patch.object(FLEET, "fetch_snapshot", side_effect=[snapshot(), RuntimeError("ssh_probe_failed")])
    def test_failed_host_is_in_workbook_and_preserves_collection_exit_code(self, fetch, export, opened):
        export.return_value = Path(self.temporary.name) / "fleet.xlsx"
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(FLEET.main(["a", "b", "--config", str(self.path), "--no-cache"]), 2)
        self.assertEqual(len(export.call_args.args[0]), 2)
        self.assertFalse(export.call_args.args[0][1]["reachable"])

    @patch.dict(FLEET.os.environ, {}, clear=True)
    @patch.object(FLEET.sys, "platform", "darwin")
    @patch.object(FLEET.subprocess, "run")
    def test_excel_open_uses_literal_arguments_and_verifies_identity_and_zoom(self, run):
        path = (Path(self.temporary.name) / "quoted ' workbook.xlsx").resolve()
        run.return_value.stdout = str(path) + "\n150\n"
        FLEET.open_excel(path)
        self.assertEqual(run.call_args.args[0][-2:], [str(path), "150"])
        self.assertNotIn("shell", run.call_args.kwargs)
        run.return_value.stdout = str(path) + "\n100\n"
        with self.assertRaises(RuntimeError):
            FLEET.open_excel(path)

    @patch.dict(FLEET.os.environ, {"SSH_TTY": "synthetic"}, clear=True)
    @patch.object(FLEET.sys, "platform", "darwin")
    @patch.object(FLEET.subprocess, "run")
    def test_excel_opener_refuses_remote_gui_launch(self, run):
        with self.assertRaises(RuntimeError):
            FLEET.open_excel(Path(self.temporary.name) / "fleet.xlsx")
        run.assert_not_called()

    def test_ssh_options_and_unconfigured_hosts_are_refused(self):
        self.config["hosts"]["a"]["host"] = "-oProxyCommand=bad"
        self.path.write_text(json.dumps(self.config))
        with self.assertRaises(ValueError):
            FLEET.load_config(self.path)
        with self.assertRaises(ValueError):
            FLEET.selected_keys(["unknown"], self.config["hosts"])

    @patch.object(FLEET, "fetch_snapshot")
    def test_combined_command_keeps_success_when_one_host_fails(self, fetch):
        def get(host):
            if host["host"].startswith("worker-b"):
                raise RuntimeError("ssh_probe_failed")
            data = snapshot()
            data["observed_at"] = datetime.now(timezone.utc).isoformat()
            return data
        fetch.side_effect = get
        out = io.StringIO()
        with redirect_stdout(out):
            result = FLEET.main(["a", "b", "--config", str(self.path), "--no-cache", "--json"])
        self.assertEqual(result, 2)
        hosts = json.loads(out.getvalue())["hosts"]
        self.assertTrue(hosts[0]["reachable"])
        self.assertFalse(hosts[1]["reachable"])
        self.assertEqual(len(hosts[0]["sessions"]), 1)

    @patch.object(FLEET, "fetch_snapshot")
    def test_combined_command_isolates_malformed_remote_metadata(self, fetch):
        def get(host):
            data = snapshot()
            data["observed_at"] = datetime.now(timezone.utc).isoformat()
            if host["host"].startswith("worker-b"):
                data["sessions"][0]["agent"] = []
            return data
        fetch.side_effect = get
        out = io.StringIO()
        with redirect_stdout(out):
            code = FLEET.main(["--all", "--config", str(self.path), "--no-cache", "--json"])
        self.assertEqual(code, 2)
        reports = json.loads(out.getvalue())["hosts"]
        self.assertTrue(reports[0]["reachable"])
        self.assertFalse(reports[1]["reachable"])

    @patch.object(FLEET, "fetch_snapshot")
    def test_combined_command_preserves_sessions_with_unknown_health_details(self, fetch):
        def get(host):
            data = snapshot()
            data["observed_at"] = datetime.now(timezone.utc).isoformat()
            if host["host"].startswith("worker-b"):
                data["health"]["report"]["process_status"] = []
            return data
        fetch.side_effect = get
        out = io.StringIO()
        with redirect_stdout(out):
            code = FLEET.main(["a", "b", "--config", str(self.path), "--no-cache", "--json"])
        self.assertEqual(code, 0)
        reports = json.loads(out.getvalue())["hosts"]
        self.assertTrue(all(report["reachable"] for report in reports))
        self.assertEqual(reports[1]["health"]["process_status"], "unknown")

    @patch.object(FLEET, "fetch_snapshot", side_effect=AssertionError("must remain offline"))
    def test_offline_fixture_prints_two_separate_host_blocks(self, fetch):
        fixture = Path(self.temporary.name) / "snapshot.json"
        fixture.write_text(json.dumps({"schema_version": 1, "hosts": {
            "a": {"label": "Laptop A", "snapshot": snapshot()},
            "b": {"label": "Laptop B", "snapshot": snapshot()}}}))
        out = io.StringIO()
        with redirect_stdout(out):
            result = FLEET.main(["--snapshot", str(fixture)])
        self.assertEqual(result, 0)
        self.assertEqual(out.getvalue().count("| Session · target |"), 2)
        self.assertEqual(out.getvalue().count("OFFLINE snapshot"), 2)
        fetch.assert_not_called()

    @patch.object(FLEET.subprocess, "run")
    def test_remote_health_path_is_one_literal_argument(self, run):
        host = {"host": "worker.example.invalid", "health_report": "/work/a 'quoted'; literal.json"}
        run.return_value.returncode = 1
        run.return_value.stdout = b""
        with self.assertRaises(RuntimeError):
            FLEET.fetch_snapshot(host)
        command = run.call_args.args[0]
        self.assertEqual(FLEET.shlex.split(command[-1])[-1], host["health_report"])
        self.assertEqual(command[-2], host["host"])
        self.assertNotIn("shell", run.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
