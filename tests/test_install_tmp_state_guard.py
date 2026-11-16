#!/usr/bin/env python3
"""Reversible tmp-state guard LaunchAgent installation; mocked launchctl, 2026-11-08."""

import contextlib
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
import os
from pathlib import Path
import plistlib
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
SPEC = importlib.util.spec_from_file_location("install_tmp_state_guard", SCRIPTS / "install_tmp_state_guard.py")
INSTALL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INSTALL)


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(os.path.realpath(self.temporary.name))
        self.root = self.home / "private-guard"
        self.dest = self.home / "agent-state-backups" / "auto"
        self.source = self.home / "tmp_state_guard.py"
        self.content = b"# synthetic guard\n"
        self.source.write_bytes(self.content)
        self.plan = INSTALL.make_plan(self.home, self.root, Path("/usr/bin/python3"), self.source, self.dest, True)
        self.report = {"schema_version": 1, "tool": "tmp-state-guard", "phase": "observed", "status": "running",
                       "observed_at": datetime.now(timezone.utc).isoformat(), "at_risk": {"entries": 0}}

    def old_install(self):
        previous = dict(self.plan["plist"])
        previous["StartCalendarInterval"] = {"Hour": 20, "Minute": 0}
        path = Path(self.plan["plist_path"])
        INSTALL.atomic_write(path, plistlib.dumps(previous))
        return path, path.read_bytes()

    def test_plan_is_read_only_daily_and_architecture_neutral(self):
        self.assertFalse(self.root.exists())
        plist = self.plan["plist"]
        self.assertEqual(plist["Label"], "org.xmechbay.tmp-state-guard")
        self.assertIn(self.plan["script_sha256"], self.plan["script"])
        self.assertEqual(plist["StartCalendarInterval"], {"Hour": 21, "Minute": 30})
        self.assertNotIn("StartInterval", plist)
        self.assertTrue(plist["RunAtLoad"])
        self.assertEqual(plist["EnvironmentVariables"], {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"})
        arguments = plist["ProgramArguments"]
        self.assertEqual(arguments[:2], ["/usr/bin/python3", "-B"])
        self.assertEqual(arguments[arguments.index("--dest") + 1], str(self.dest))
        self.assertIn("--notify", arguments)
        self.assertNotIn("--dry-run", arguments)
        rendered = plistlib.dumps(plist).decode()
        for prefix in ("/opt/homebrew", "/usr/local"):
            self.assertNotIn(prefix, rendered)

    def test_extra_interval_and_exclusions_are_passed_through(self):
        plan = INSTALL.make_plan(self.home, self.root, Path("/usr/bin/python3"), self.source, self.dest,
                                 interval_hours=6, excludes=["big-data"], top_excludes=["MSau_*"])
        self.assertEqual(plan["plist"]["StartInterval"], 6 * 3600)
        arguments = plan["plist"]["ProgramArguments"]
        self.assertEqual(arguments[-4:], ["--exclude", "big-data", "--exclude-top", "MSau_*"])

    def test_real_plan_refuses_backups_in_a_cleaned_location(self):
        source = SCRIPTS / "tmp_state_guard.py"
        with self.assertRaisesRegex(ValueError, "destination lies under"):
            INSTALL.check_locations(source, Path("/private/tmp/backups"), self.root / "state")
        with self.assertRaisesRegex(ValueError, "state directory lies under"):
            INSTALL.check_locations(source, Path("/nonexistent-guard-test/backups"), Path("/tmp/guard-state"))

    def test_main_prints_a_plan_without_changes(self):
        output = io.StringIO()
        argv = ["--root", str(self.root), "--dest", str(self.home / "copies"), "--python", sys.executable]
        # The temporary home is itself under a cleaned root; location checks have their own test.
        with patch.object(Path, "home", return_value=self.home), patch.object(INSTALL, "check_locations"), \
                contextlib.redirect_stdout(output):
            self.assertEqual(INSTALL.main(argv), 0)
        plan = json.loads(output.getvalue())
        self.assertEqual(plan["action"], "plan")
        self.assertFalse(self.root.exists())
        self.assertFalse(Path(plan["plist_path"]).exists())

    @patch.object(INSTALL, "wait_for_report")
    @patch.object(INSTALL, "launchctl")
    def test_install_copies_source_and_loads_only_its_job(self, launchctl, wait_for_report):
        launchctl.side_effect = [True, False, True]
        wait_for_report.return_value = self.report
        result = INSTALL.install(self.plan, self.content, 501)
        self.assertEqual(result["action"], "installed")
        self.assertEqual(result["first_phase"], "observed")
        self.assertEqual(Path(self.plan["script"]).read_bytes(), self.content)
        self.assertEqual(Path(self.plan["script"]).stat().st_mode & 0o777, 0o600)
        self.assertEqual(Path(self.plan["state_dir"]).stat().st_mode & 0o777, 0o700)
        self.assertEqual([c.args[:1] for c in launchctl.call_args_list], [("print",), ("print",), ("bootstrap",)])
        self.assertEqual(launchctl.call_args_list[-1].args[1], "gui/501")

    @patch.object(INSTALL, "wait_for_report")
    @patch.object(INSTALL, "launchctl")
    def test_update_backs_up_the_old_plist(self, launchctl, wait_for_report):
        path, previous = self.old_install()
        launchctl.side_effect = [True, True, True, True]
        wait_for_report.return_value = self.report
        result = INSTALL.install(self.plan, self.content, 501)
        self.assertEqual(Path(result["backup"]).read_bytes(), previous)
        self.assertIn(".bak-tmp-state-guard-", result["backup"])
        self.assertEqual(plistlib.loads(path.read_bytes())["StartCalendarInterval"], {"Hour": 21, "Minute": 30})
        self.assertEqual(launchctl.call_args_list[2].args, ("bootout", "gui/501/" + INSTALL.LABEL))

    @patch.object(INSTALL, "wait_for_report", side_effect=RuntimeError("no fresh observation"))
    @patch.object(INSTALL, "launchctl")
    def test_startup_without_evidence_unloads_new_job_and_restores_old(self, launchctl, wait_for_report):
        path, previous = self.old_install()
        launchctl.return_value = True
        with self.assertRaisesRegex(RuntimeError, "no fresh observation"):
            INSTALL.install(self.plan, self.content, 501)
        self.assertEqual(path.read_bytes(), previous)
        self.assertEqual([call.args[0] for call in launchctl.call_args_list],
                         ["print", "print", "bootout", "bootstrap", "bootout", "bootstrap"])
        self.assertFalse((self.root / "installation.json").exists())

    @patch.object(INSTALL, "wait_for_report", side_effect=RuntimeError("no fresh observation"))
    @patch.object(INSTALL, "launchctl")
    def test_rollback_restores_the_old_plist_even_if_bootout_fails(self, launchctl, wait_for_report):
        path, previous = self.old_install()
        launchctl.side_effect = [True, True, True, True, RuntimeError("bootout failed"), True]
        with self.assertRaisesRegex(RuntimeError, "no fresh observation"):
            INSTALL.install(self.plan, self.content, 501)
        self.assertEqual(path.read_bytes(), previous)
        self.assertEqual(launchctl.call_args_list[-1].args, ("bootstrap", "gui/501", str(path)))

    @patch.object(INSTALL, "wait_for_report", side_effect=RuntimeError("no fresh observation"))
    @patch.object(INSTALL, "launchctl")
    def test_failed_first_install_leaves_no_plist(self, launchctl, wait_for_report):
        launchctl.side_effect = [True, False, True, True]
        with self.assertRaises(RuntimeError):
            INSTALL.install(self.plan, self.content, 501)
        self.assertFalse(Path(self.plan["plist_path"]).exists())

    @patch.object(INSTALL, "launchctl")
    def test_foreign_job_is_never_unloaded(self, launchctl):
        path = Path(self.plan["plist_path"])
        INSTALL.atomic_write(path, plistlib.dumps({"Label": "unrelated", "ProgramArguments": ["program"]}))
        with self.assertRaisesRegex(RuntimeError, "does not match"):
            INSTALL.install(self.plan, self.content, 501)
        launchctl.assert_not_called()

    @patch.object(INSTALL, "launchctl")
    def test_loaded_label_without_owned_plist_is_refused(self, launchctl):
        launchctl.side_effect = [True, True]
        with self.assertRaisesRegex(RuntimeError, "without our owned plist"):
            INSTALL.install(self.plan, self.content, 501)

    @patch.object(INSTALL, "launchctl")
    def test_insecure_or_symlinked_state_is_rejected_before_job_changes(self, launchctl):
        state = Path(self.plan["state_dir"])
        self.root.mkdir(mode=0o700)
        state.mkdir(mode=0o755)
        os.chmod(state, 0o755)
        with self.assertRaisesRegex(RuntimeError, "mode 0700"):
            INSTALL.install(self.plan, self.content, 501)
        state.rmdir()
        state.symlink_to(self.home, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, "symlinks"):
            INSTALL.install(self.plan, self.content, 501)
        launchctl.assert_not_called()

    def test_startup_evidence_must_be_fresh_and_from_this_guard(self):
        state = self.home / "report-state"
        state.mkdir()
        started = datetime.now(timezone.utc)
        stale = dict(self.report, observed_at=(started - timedelta(seconds=5)).isoformat())
        (state / "latest.json").write_text(json.dumps(stale))
        with self.assertRaisesRegex(RuntimeError, "no fresh observation"):
            INSTALL.wait_for_report(state, started, timeout=0.01)
        other = dict(self.report, tool="port-pressure", observed_at=(started + timedelta(seconds=1)).isoformat())
        (state / "latest.json").write_text(json.dumps(other))
        with self.assertRaisesRegex(RuntimeError, "no fresh observation"):
            INSTALL.wait_for_report(state, started, timeout=0.01)
        fresh = dict(self.report, observed_at=(started + timedelta(seconds=1)).isoformat())
        (state / "latest.json").write_text(json.dumps(fresh))
        self.assertEqual(INSTALL.wait_for_report(state, started, timeout=0.1), fresh)

    @patch.object(INSTALL, "launchctl")
    def test_uninstall_preserves_state_and_backup(self, launchctl):
        path, previous = self.old_install()
        evidence = self.root / "state" / "latest.json"
        INSTALL.atomic_write(evidence, b'{"status":"ok"}\n')
        launchctl.return_value = True
        result = INSTALL.uninstall(self.home, self.root, 501)
        self.assertFalse(path.exists())
        self.assertTrue(evidence.exists())
        self.assertEqual(Path(result["backup"]).read_bytes(), previous)
        self.assertEqual(launchctl.call_args_list[-1].args, ("bootout", "gui/501/" + INSTALL.LABEL))


if __name__ == "__main__":
    unittest.main()
