"""Verify reversible local monitor installation; synthetic inputs, 2026-10-27."""

import importlib.util
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import plistlib
import sys
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "install_port_pressure_watch", Path(__file__).parents[1] / "scripts" / "install_port_pressure_watch.py"
)
INSTALL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INSTALL)


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.root = self.home / "private-monitor"
        self.source = self.home / "source.py"
        self.content = b"# synthetic monitor\n"
        self.source.write_bytes(self.content)
        self.plan = INSTALL.make_plan(self.home, self.root, Path(sys.executable), self.source, True)
        self.report = {"schema_version": 1, "status": "ok", "observed_at": datetime.now(timezone.utc).isoformat()}

    def old_install(self):
        previous = dict(self.plan["plist"])
        previous["StartInterval"] = 180
        path = Path(self.plan["plist_path"])
        INSTALL.atomic_write(path, plistlib.dumps(previous))
        return path, path.read_bytes()

    def test_plan_is_read_only_and_uses_immutable_release(self):
        self.assertFalse(self.root.exists())
        self.assertIn(self.plan["script_sha256"], self.plan["script"])
        arguments = self.plan["plist"]["ProgramArguments"]
        self.assertEqual(arguments[1], "-B")
        self.assertIn("--notify", arguments)
        self.assertEqual(self.plan["plist"]["StartInterval"], 60)
        self.assertTrue(self.plan["plist"]["RunAtLoad"])
        self.assertEqual(self.plan["plist"]["StandardOutPath"], "/dev/null")

    @patch.object(INSTALL, "wait_for_report")
    @patch.object(INSTALL, "launchctl")
    def test_install_copies_source_and_loads_only_its_job(self, launchctl, wait_for_report):
        launchctl.side_effect = [True, False, True]
        wait_for_report.return_value = self.report
        result = INSTALL.install(self.plan, self.content, 501)
        self.assertEqual(result["action"], "installed")
        self.assertEqual(Path(self.plan["script"]).read_bytes(), self.content)
        self.assertEqual(Path(self.plan["script"]).stat().st_mode & 0o777, 0o600)
        self.assertEqual([c.args[:1] for c in launchctl.call_args_list], [("print",), ("print",), ("bootstrap",)])
        self.assertEqual(launchctl.call_args_list[-1].args[1], "gui/501")

    @patch.object(INSTALL, "wait_for_report")
    @patch.object(INSTALL, "launchctl")
    def test_update_preserves_old_plist(self, launchctl, wait_for_report):
        path, previous = self.old_install()
        launchctl.side_effect = [True, True, True, True]
        wait_for_report.return_value = self.report
        result = INSTALL.install(self.plan, self.content, 501)
        self.assertEqual(Path(result["backup"]).read_bytes(), previous)
        self.assertEqual(plistlib.loads(path.read_bytes())["StartInterval"], 60)
        self.assertEqual(launchctl.call_args_list[2].args, ("bootout", "gui/501/" + INSTALL.LABEL))

    @patch.object(INSTALL, "launchctl")
    def test_failed_upgrade_restores_loaded_previous_configuration(self, launchctl):
        path, previous = self.old_install()
        launchctl.side_effect = [True, True, True, RuntimeError("bootstrap failed"), True]
        with self.assertRaisesRegex(RuntimeError, "bootstrap failed"):
            INSTALL.install(self.plan, self.content, 501)
        self.assertEqual(path.read_bytes(), previous)
        self.assertEqual(launchctl.call_args_list[-1].args, ("bootstrap", "gui/501", str(path)))
        self.assertFalse((self.root / "installation.json").exists())

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
        self.assertEqual(launchctl.call_count, 2)

    @patch.object(INSTALL, "launchctl")
    def test_insecure_state_is_rejected_before_job_changes(self, launchctl):
        state = Path(self.plan["state_dir"])
        self.root.mkdir(mode=0o700)
        state.mkdir(mode=0o755)
        with self.assertRaisesRegex(RuntimeError, "mode 0700"):
            INSTALL.install(self.plan, self.content, 501)
        launchctl.assert_not_called()

    @patch.object(INSTALL, "launchctl")
    def test_symlinked_state_is_rejected_before_job_changes(self, launchctl):
        self.root.mkdir(mode=0o700)
        Path(self.plan["state_dir"]).symlink_to(self.home, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, "symlinks"):
            INSTALL.install(self.plan, self.content, 501)
        launchctl.assert_not_called()

    @patch.object(INSTALL, "wait_for_report", side_effect=RuntimeError("no fresh report"))
    @patch.object(INSTALL, "launchctl")
    def test_startup_without_evidence_unloads_new_job_and_restores_old(self, launchctl, wait_for_report):
        path, previous = self.old_install()
        launchctl.return_value = True
        with self.assertRaisesRegex(RuntimeError, "no fresh report"):
            INSTALL.install(self.plan, self.content, 501)
        self.assertEqual(path.read_bytes(), previous)
        self.assertEqual([call.args[0] for call in launchctl.call_args_list],
                         ["print", "print", "bootout", "bootstrap", "bootout", "bootstrap"])

    def test_startup_evidence_must_be_fresh(self):
        state = self.home / "report-state"
        state.mkdir()
        started = datetime.now(timezone.utc)
        stale = dict(self.report, observed_at=(started - timedelta(seconds=5)).isoformat())
        (state / "latest.json").write_text(json.dumps(stale))
        with self.assertRaisesRegex(RuntimeError, "no fresh durable report"):
            INSTALL.wait_for_report(state, started, timeout=0.01)
        fresh = dict(self.report, observed_at=(started + timedelta(seconds=1)).isoformat())
        (state / "latest.json").write_text(json.dumps(fresh))
        self.assertEqual(INSTALL.wait_for_report(state, started, timeout=0.1), fresh)

    @patch.object(INSTALL, "launchctl")
    def test_uninstall_preserves_evidence_and_backup(self, launchctl):
        path, previous = self.old_install()
        evidence = self.root / "state" / "latest.json"
        INSTALL.atomic_write(evidence, b'{"status":"ok"}\n')
        result = INSTALL.uninstall(self.home, self.root, 501)
        self.assertFalse(path.exists())
        self.assertTrue(evidence.exists())
        self.assertEqual(Path(result["backup"]).read_bytes(), previous)
        self.assertEqual(launchctl.call_args_list[-1].args, ("bootout", "gui/501/" + INSTALL.LABEL))


if __name__ == "__main__":
    unittest.main()
