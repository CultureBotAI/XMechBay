"""Fleet CLI/cache/health/local-transport tests with synthetic hosts, 2026-11-14."""

from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
from pathlib import Path
import stat
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
        "system": {"arch": "arm64", "loadavg_1m": 1.75, "disk_free_gib": 150.2, "disk_total_gib": 500.0},
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

    def test_tmux_socket_survives_normalization_and_rejects_unsafe_values(self):
        data = snapshot()
        data["sessions"][0]["target"] = {
            "transport": "tmux", "pane": "%0", "tmux_socket": "alt_server",
            "tmux_session": "review", "tmux_sessions": ["review"], "window": "SampleMech",
        }
        report = FLEET.normalize_host("a", "A", data, now=NOW)
        row = report["sessions"][0]
        self.assertEqual(row["target"]["tmux_socket"], "alt_server")
        rendered = FLEET.render_host(report)
        self.assertIn("alt\\_server:%0", rendered)
        for bad in ("default", "../escape", "/tmp/sock", "-badflag", "", 42, "has space"):
            with self.subTest(bad=bad):
                bad_data = snapshot()
                bad_data["sessions"][0]["target"] = {"transport": "tmux", "pane": "%0", "tmux_socket": bad}
                with self.assertRaises(ValueError):
                    FLEET.validate_snapshot(bad_data)
        for non_tmux in ({"transport": "terminal", "window_id": 12, "tab": 1, "tmux_socket": "alt_server"},
                         {"transport": "process", "tmux_socket": "alt_server"}):
            with self.subTest(target=non_tmux):
                bad_data = snapshot()
                bad_data["sessions"][0]["target"] = non_tmux
                with self.assertRaises(ValueError):
                    FLEET.validate_snapshot(bad_data)

    def test_normalized_report_omits_raw_screen_path_and_title(self):
        report = FLEET.normalize_host("a", "Laptop A", snapshot(), now=NOW)
        row = report["sessions"][0]
        self.assertEqual(row["listeners"]["count"], 2)
        self.assertEqual(row["repo_label"], "SampleMech")
        self.assertEqual(row["role"], "mech")
        self.assertEqual(row["project_label"], "SampleMech")
        self.assertEqual(report["system"]["arch"], "arm64")
        self.assertEqual(report["system"]["disk_free_gib"], 150.2)
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


class ComparisonAndFallbackTests(unittest.TestCase):
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

    def test_last_reachable_cache_attaches_stale_sessions_when_host_goes_unreachable(self):
        live_snap = snapshot()
        live_snap["observed_at"] = datetime.now(timezone.utc).isoformat()
        with patch.object(FLEET, "fetch_snapshot", return_value=live_snap):
            first = FLEET.collect_host("m5", {"host": "worker-a.example.invalid", "label": "M5 Laptop"}, 120, self.directory)
        self.assertTrue(first["reachable"])
        last_file = self.directory / "m5.last.json"
        self.assertTrue(last_file.is_file())
        self.assertEqual(stat.S_IMODE(last_file.stat().st_mode), 0o600)

        with patch.object(FLEET, "fetch_snapshot", side_effect=RuntimeError("ssh_probe_timed_out")):
            second = FLEET.collect_host("m5", {"host": "worker-a.example.invalid", "label": "M5 Laptop"}, 120, self.directory)
        self.assertFalse(second["reachable"])
        self.assertEqual(second["sessions"], [])
        self.assertIn("last_reachable", second)
        self.assertEqual(len(second["last_reachable"]["sessions"]), 1)
        cached_row = second["last_reachable"]["sessions"][0]
        self.assertTrue(cached_row["stale_cached"])
        self.assertEqual(cached_row["delta"]["kind"], "stale_cached")
        self.assertTrue(cached_row["listeners"]["stale"])
        self.assertIsNone(cached_row["listeners"]["count"])
        self.assertEqual(second["last_reachable"]["health"]["status"], "stale")

        # Incomplete process probes or stale snapshots must not clobber m5.last.json.
        broken = dict(first, sessions=[], coverage={"processes": "unknown"}, errors=["process_probe_timed_out"])
        FLEET.save_last_reachable(broken, self.directory)
        stale_rep = dict(first, sessions=[], snapshot_stale=True)
        FLEET.save_last_reachable(stale_rep, self.directory)
        reloaded = FLEET.load_last_reachable("m5", self.directory)
        self.assertEqual(len(reloaded["sessions"]), 1)

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

    def test_partial_agent_discovery_computes_per_pid_deltas_without_inventing_departures(self):
        snap1 = snapshot()
        second_sess = dict(
            snap1["sessions"][0],
            pid=202,
            tty="ttys002",
            cwd="/work/OtherMech",
            target={"transport": "terminal", "window_id": 13, "tab": 1},
        )
        snap1["sessions"].append(second_sess)
        r1 = FLEET.normalize_host("a", "A", snap1, now=NOW)
        FLEET.compare_cache(r1, self.directory, "worker-a.example.invalid")
        self.assertEqual([s["delta"]["kind"] for s in r1["sessions"]], ["first", "first"])

        # Partial discovery sees only PID 101 (unchanged); PID 202 baseline must survive.
        r2 = self.report(30)
        r2["coverage"]["agent_discovery"] = "partial"
        FLEET.compare_cache(r2, self.directory, "worker-a.example.invalid")
        self.assertEqual(r2["cache_error"], "comparison_incomplete_inventory")
        self.assertNotIn("changes", r2)
        self.assertEqual(r2["sessions"][0]["delta"], {"kind": "unchanged", "seconds": 30, "sample_gap_seconds": 30})
        rendered_r2 = FLEET.render_host(r2)
        self.assertIn("departures uncounted", rendered_r2)

        # Partial discovery sees PID 101 screen change -> delta is "changed".
        r3 = self.report(60, {"screen": "Updated progress line.\n>\n? for shortcuts Gemini Example · high"})
        r3["coverage"]["agent_discovery"] = "partial"
        FLEET.compare_cache(r3, self.directory, "worker-a.example.invalid")
        self.assertEqual(r3["sessions"][0]["delta"]["kind"], "changed")

        # Full discovery returns with both PID 101 and PID 202 (unchanged since t=0).
        snap4 = snapshot()
        snap4["observed_at"] = (NOW + timedelta(seconds=90)).isoformat()
        snap4["sessions"][0]["screen"] = "Updated progress line.\n>\n? for shortcuts Gemini Example · high"
        snap4["sessions"].append(dict(second_sess))
        r4 = FLEET.normalize_host("a", "A", snap4, now=NOW + timedelta(seconds=90))
        FLEET.compare_cache(r4, self.directory, "worker-a.example.invalid")
        by_pid = {s["pid"]: s for s in r4["sessions"]}
        self.assertEqual(by_pid[101]["delta"]["kind"], "unchanged")
        self.assertEqual(by_pid[202]["delta"], {"kind": "unchanged", "seconds": 90, "sample_gap_seconds": 90})
        self.assertEqual(r4["changes"]["departed"], 0)

    def test_unreadable_screen_or_no_cache_yields_unavailable_delta(self):
        uncached = self.report(0)
        self.assertEqual(uncached["sessions"][0]["delta"], {"kind": "unavailable", "seconds": None})

        r1 = self.report(0)
        FLEET.compare_cache(r1, self.directory, "worker-a.example.invalid")
        self.assertEqual(r1["sessions"][0]["delta"]["kind"], "first")

        # Transient screen failure must report unavailable without clobbering prior fingerprint.
        blind = self.report(30, {"screen": "", "errors": ["screen_unavailable"]})
        FLEET.compare_cache(blind, self.directory, "worker-a.example.invalid")
        self.assertEqual(blind["sessions"][0]["delta"], {"kind": "unavailable", "seconds": None})

        recovered = self.report(60)
        FLEET.compare_cache(recovered, self.directory, "worker-a.example.invalid")
        self.assertEqual(recovered["sessions"][0]["delta"], {"kind": "unchanged", "seconds": 60, "sample_gap_seconds": 30})

    def test_unreadable_screen_backward_clock_skew_resets_cache_entry_cleanly(self):
        r1 = self.report(60)
        FLEET.compare_cache(r1, self.directory, "worker-a.example.invalid")
        self.assertEqual(r1["sessions"][0]["delta"]["kind"], "first")

        # Backward clock step (t=30 < changed=60 and seen=60) during screen_unavailable
        # must not write changed_at > seen_at into cache.
        skewed_blind = self.report(30, {"screen": "", "errors": ["screen_unavailable"]})
        FLEET.compare_cache(skewed_blind, self.directory, "worker-a.example.invalid")
        self.assertEqual(skewed_blind["sessions"][0]["delta"], {"kind": "unavailable", "seconds": None})
        cached = json.loads((self.directory / "a.json").read_text())["sessions"]
        entry = next(iter(cached.values()))
        self.assertIsNone(entry["fingerprint"])
        self.assertIsNone(entry["changed_at"])
        self.assertEqual(entry["seen_at"], (NOW + timedelta(seconds=30)).isoformat())

        recovered = self.report(35)
        FLEET.compare_cache(recovered, self.directory, "worker-a.example.invalid")
        self.assertEqual(recovered["sessions"][0]["delta"], {"kind": "first", "seconds": None})

    def test_partial_discovery_cache_cap_retains_current_sessions_despite_future_skewed_previous(self):
        FLEET.compare_cache(self.report(-120), self.directory, "worker-a.example.invalid")
        seed = json.loads((self.directory / "a.json").read_text())
        stale_sessions = {}
        for idx in range(250):
            stale_sessions[f"{2000 + idx}:Mon Nov 14 08:00:00 2026"] = {
                "fingerprint": f"valid-{idx}",
                "changed_at": (NOW - timedelta(seconds=60)).isoformat(),
                "seen_at": (NOW - timedelta(seconds=10)).isoformat(),
            }
        for idx in range(15):
            stale_sessions[f"{5000 + idx}:Mon Nov 14 08:00:00 2026"] = {
                "fingerprint": f"future-{idx}",
                "changed_at": (NOW + timedelta(seconds=3600)).isoformat(),
                "seen_at": (NOW + timedelta(seconds=3600)).isoformat(),
            }
        seed["sessions"] = stale_sessions
        FLEET.atomic_json(self.directory / "a.json", seed)

        r1 = self.report(0)
        r1["coverage"]["agent_discovery"] = "partial"
        FLEET.compare_cache(r1, self.directory, "worker-a.example.invalid")
        self.assertEqual(r1["sessions"][0]["delta"]["kind"], "first")

        stored = json.loads((self.directory / "a.json").read_text())["sessions"]
        self.assertEqual(len(stored), 256)
        live_key = f"{r1['sessions'][0]['pid']}:{r1['sessions'][0]['started_at']}"
        self.assertIn(live_key, stored)
        retained_valid = sum(1 for k in stored if k.startswith(("2", "3", "4")))
        self.assertEqual(retained_valid, 250)

        r2 = self.report(30)
        r2["coverage"]["agent_discovery"] = "partial"
        FLEET.compare_cache(r2, self.directory, "worker-a.example.invalid")
        self.assertEqual(r2["sessions"][0]["delta"], {"kind": "unchanged", "seconds": 30, "sample_gap_seconds": 30})


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "config.json"
        self.config = {"schema_version": 1, "hosts": {
            "a": {"host": "worker-a.example.invalid", "label": "Laptop A"},
            "b": {"host": "worker-b.example.invalid", "label": "Laptop B"}}, "aliases": {"first": "a"}}
        self.path.write_text(json.dumps(self.config))

    def test_local_transport_does_not_require_ssh_host_and_invokes_collector_directly(self):
        local_cfg = {"schema_version": 1, "hosts": {
            "m1": {"transport": "local", "label": "M1 Workstation", "python": "/usr/bin/python3"}
        }, "role_overrides": {"custom-repo": {"role": "mech", "project": "CustomMech"}}}
        cfg_path = Path(self.temporary.name) / "local-config.json"
        cfg_path.write_text(json.dumps(local_cfg))
        loaded = FLEET.load_config(cfg_path)
        self.assertEqual(loaded["hosts"]["m1"]["transport"], "local")

        live_snap = snapshot()
        live_snap["observed_at"] = datetime.now(timezone.utc).isoformat()
        with patch.object(FLEET.subprocess, "run") as run_mock:
            run_mock.return_value.returncode = 0
            run_mock.return_value.stdout = json.dumps(live_snap).encode("utf-8")
            fetched = FLEET.fetch_snapshot(loaded["hosts"]["m1"])
        self.assertEqual(fetched["observed_at"], live_snap["observed_at"])
        called_cmd = run_mock.call_args.args[0]
        self.assertNotEqual(called_cmd[0], "ssh")
        self.assertTrue(called_cmd[2].endswith("fleet_collect.py"))

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

    def test_ensure_private_dir_creates_0700_intermediate_parents_and_rejects_foreign_symlink(self):
        nested = Path(self.temporary.name) / ".local" / "fleet-status-cache"
        resolved = FLEET.ensure_private_dir(nested)
        self.assertEqual(FLEET.stat.S_IMODE((Path(self.temporary.name) / ".local").lstat().st_mode), 0o700)
        self.assertEqual(FLEET.stat.S_IMODE(resolved.lstat().st_mode), 0o700)

        target_dir = Path(self.temporary.name) / "real-target"
        target_dir.mkdir(mode=0o700)
        link_dir = Path(self.temporary.name) / "foreign-link"
        link_dir.symlink_to(target_dir)
        real_lstat = Path.lstat

        def fake_lstat(path_obj):
            st = real_lstat(path_obj)
            if path_obj.name == "foreign-link":
                class ForeignStat:
                    st_mode = st.st_mode
                    st_uid = FLEET.os.getuid() + 1000
                return ForeignStat()
            return st

        with patch.object(Path, "lstat", fake_lstat):
            with self.assertRaisesRegex(ValueError, "directory_symlink_untrusted"):
                FLEET.ensure_private_dir(link_dir / "sub")

    @patch.object(FLEET, "load_config", side_effect=RuntimeError("unexpected_runtime_failure"))
    def test_main_catches_runtime_error_with_structured_json(self, _mock_cfg):
        err = io.StringIO()
        with patch("sys.stderr", err):
            code = FLEET.main(["--config", str(self.path)])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err.getvalue())["error"], "unexpected_runtime_failure")

    def test_symlinked_fleet_wrappers_resolve_root_and_honor_xmechbay_python(self):
        repo_root = Path(__file__).resolve().parents[1]
        bin_a = Path(self.temporary.name) / "bin-a"
        bin_b = Path(self.temporary.name) / "bin-b"
        decoy = Path(self.temporary.name) / "cdpath-decoy"
        (decoy / "scripts").mkdir(parents=True)
        (decoy / "bin-a").mkdir(parents=True)
        (decoy / "bin-b").mkdir(parents=True)
        bin_a.mkdir()
        bin_b.mkdir()
        env = dict(
            FLEET.os.environ,
            XMECHBAY_PYTHON=FLEET.sys.executable,
            CDPATH=f".:{decoy}",
        )
        for wrapper_name in ("fleet-status.sh", "fleet-dashboard.sh"):
            direct = FLEET.subprocess.run(
                ["/bin/bash", f"scripts/{wrapper_name}", "--help"],
                cwd=repo_root,
                env=env,
                text=True,
                capture_output=True,
                timeout=15,
            )
            self.assertEqual(direct.returncode, 0, direct.stderr)
            self.assertIn("--snapshot", direct.stdout)

            hop1 = bin_b / wrapper_name
            hop1.symlink_to(repo_root / "scripts" / wrapper_name)
            hop2 = bin_a / wrapper_name
            hop2.symlink_to(Path("..") / "bin-b" / wrapper_name)
            proc = FLEET.subprocess.run(
                ["/bin/bash", f"bin-a/{wrapper_name}", "--help"],
                cwd=self.temporary.name,
                env=env,
                text=True,
                capture_output=True,
                timeout=15,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("--snapshot", proc.stdout)

    def test_skip_permissions_validation_and_normalization(self):
        for valid in (True, False, None):
            snap = snapshot()
            snap["sessions"][0]["agent"] = "agy"
            snap["sessions"][0]["skip_permissions"] = valid
            report = FLEET.normalize_host("a", "Host A", snap)
            self.assertIs(report["sessions"][0]["skip_permissions"], valid)

        for invalid in ("false", 1, []):
            bad = snapshot()
            bad["sessions"][0]["skip_permissions"] = invalid
            with self.assertRaisesRegex(ValueError, "snapshot_identity_invalid"):
                FLEET.validate_snapshot(bad)

        unverified = snapshot()
        unverified["sessions"][0].update({
            "pid": None,
            "started_at": None,
            "session_id": None,
            "skip_permissions": False,
            "errors": ["agent_process_unverified"],
        })
        with self.assertRaisesRegex(ValueError, "snapshot_process_invalid"):
            FLEET.validate_snapshot(unverified)

    def test_tailscale_peer_matching_fallback_and_host_key_alias_roaming(self):
        ts_self_ip = ".".join(["100", "74", "1", "10"])
        ts_m5_ip = ".".join(["100", "73", "2", "20"])
        ts_intel_ip = ".".join(["100", "82", "3", "30"])
        ts_suffix = "sample-net." + "ts.net."
        raw_ts = {
            "BackendState": "Running",
            "Self": {
                "HostName": "workstation",
                "DNSName": "m1." + ts_suffix,
                "TailscaleIPs": [ts_self_ip],
                "Online": True,
            },
            "Peer": {
                "k1": {
                    "HostName": "Laptop M5",
                    "DNSName": "m5." + ts_suffix,
                    "TailscaleIPs": [ts_m5_ip],
                    "Online": True,
                },
                "k2": {
                    "HostName": "Laptop Intel",
                    "DNSName": "intel." + ts_suffix,
                    "TailscaleIPs": [ts_intel_ip],
                    "Online": False,
                },
            },
        }
        parsed = FLEET.parse_tailscale_status_json(raw_ts)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["self"]["ipv4"], ts_self_ip)
        aliases = {"intel": "intelmbpro"}

        # 1. Direct key match (m5) + off-LAN primary 255 -> Tailscale IPv4 with HostKeyAlias=<primary>
        m5_cfg = {"host": "worker-m5.example.invalid", "label": "M5 Laptop"}
        enriched_m5 = FLEET.enrich_host_with_tailscale("m5", m5_cfg, aliases=aliases, ts_status=parsed)
        self.assertIn(ts_m5_ip, enriched_m5["_auto_tailscale_targets"])

        live_snap = snapshot()
        live_snap["observed_at"] = datetime.now(timezone.utc).isoformat()
        seen_cmds = []

        class Res:
            def __init__(self, rc, stdout=b"", stderr=b""):
                self.returncode = rc
                self.stdout = stdout
                self.stderr = stderr

        def fake_ssh_run(cmd, **kwargs):
            seen_cmds.append(cmd)
            dest = cmd[-2]
            if dest == "worker-m5.example.invalid":
                return Res(255, stderr=b"ssh: Could not resolve hostname")
            self.assertIn("HostKeyAlias=worker-m5.example.invalid", cmd)
            return Res(0, stdout=json.dumps(live_snap).encode("utf-8"))

        cache_dir = Path(self.temporary.name) / "roam-cache"
        # First sample on LAN:
        lan_snap = dict(live_snap)
        with patch.object(FLEET, "fetch_snapshot", return_value=lan_snap):
            first = FLEET.collect_host("m5", m5_cfg, 120, cache_dir)
        self.assertEqual(first["route"], "ssh")
        self.assertEqual(first["sessions"][0]["delta"]["kind"], "first")

        # Second sample roaming over Tailscale: preserves comparison cache identity (delta == unchanged)
        with patch.object(FLEET.subprocess, "run", side_effect=fake_ssh_run):
            second = FLEET.collect_host("m5", m5_cfg, 120, cache_dir, aliases=aliases, ts_status=parsed)
        self.assertTrue(second["reachable"])
        self.assertEqual(second["route"], "tailscale")
        self.assertEqual(second["sessions"][0]["delta"]["kind"], "unchanged")
        self.assertIn("Tailscale SSH observed", FLEET.render_host(second))
        self.assertEqual(seen_cmds[0][-2], "worker-m5.example.invalid")
        self.assertEqual(seen_cmds[1][-2], ts_m5_ip)

        # 2. Alias match (intel -> intelmbpro) when Online is False appends tailscale_peer_offline
        intel_cfg = {"host": "worker-intel.example.invalid", "label": "Intel Laptop"}
        with patch.object(
            FLEET.subprocess, "run", return_value=Res(255, stderr=b"Could not resolve hostname")
        ) as off_mock:
            offline_rep = FLEET.collect_host(
                "intelmbpro", intel_cfg, 120, cache_dir, aliases=aliases, ts_status=parsed
            )
        self.assertFalse(offline_rep["reachable"])
        self.assertEqual(offline_rep["errors"], ["ssh_probe_failed", "tailscale_peer_offline"])
        # Offline peer targets were skipped, so only the primary host was attempted
        self.assertEqual(off_mock.call_count, 1)

        # 3. Host key verification failure (even without IDENTIFICATION HAS CHANGED) never drops HostKeyAlias (#29)
        mitm_cfg = {"host": "worker-m5.example.invalid", "tailscale_host": ts_m5_ip}
        mitm_calls = []

        def fake_mitm_run(cmd, **kwargs):
            mitm_calls.append(cmd)
            if cmd[-2] == "worker-m5.example.invalid":
                return Res(255, stderr=b"Could not resolve hostname")
            return Res(255, stderr=b"Host key verification failed.")

        with patch.object(FLEET.subprocess, "run", side_effect=fake_mitm_run):
            with self.assertRaisesRegex(RuntimeError, "ssh_host_key_unverified"):
                FLEET.fetch_snapshot(mitm_cfg)
        self.assertEqual(len(mitm_calls), 2)
        self.assertIn("HostKeyAlias=worker-m5.example.invalid", mitm_calls[1])

        # 4. Ambiguous implicit peer match (2 peers sharing lookup name) returns None (#29)
        ambig_ts = {
            "peers": [
                {"short_name": "dup", "dns_name": "dup-1.sample-net.invalid", "ips": [], "online": True, "targets": [ts_m5_ip]},
                {"short_name": "dup", "dns_name": "dup-2.sample-net.invalid", "ips": [], "online": True, "targets": [ts_intel_ip]},
            ]
        }
        self.assertIsNone(FLEET.match_tailscale_peer("dup", {"host": "dup-lan.example.invalid"}, ts_status=ambig_ts))


if __name__ == "__main__":
    unittest.main()
