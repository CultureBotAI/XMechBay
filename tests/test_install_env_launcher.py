"""Synthetic filesystem and real shell-argument checks for the installer."""
import contextlib
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


SOURCE = Path(__file__).resolve().parents[1] / "scripts/adhoc/install_env_launcher.py"
SPEC = importlib.util.spec_from_file_location("install_env_launcher", SOURCE)
INSTALL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INSTALL)


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.destination = self.root / "environment-command"
        self.runtime = self.root / "loader ' $(not-a-command).py"
        self.runtime.write_text(
            "import json, sys\n"
            "print(json.dumps(sys.argv[1:]))\n"
            "if sys.argv[-1] == 'exit-37': sys.exit(37)\n"
        )
        self.interpreter = Path(sys.executable).resolve()
        self.tdm = self.root / "missing tdm ' $(touch credential-pwned);.env"
        self.shells = [self.root / "missing shell.env", self.root / 'other "shell".env']
        self.tag = "example-20990101"

    def install(self, **changes):
        arguments = dict(destination=self.destination, interpreter=self.interpreter,
                         runtime=self.runtime, tdm_env=self.tdm, cborg_shells=self.shells,
                         backup_tag=self.tag)
        arguments.update(changes)
        return INSTALL.install(**arguments)

    def backup(self):
        return self.destination.with_name(self.destination.name + ".bak-" + self.tag)

    def test_default_plan_does_not_write_or_read_credential_files(self):
        before = set(self.root.iterdir())
        receipt = self.install()
        self.assertFalse(receipt["applied"])
        self.assertEqual(receipt["action"], "created")
        self.assertEqual(set(self.root.iterdir()), before)
        self.assertFalse(self.tdm.exists())
        self.assertFalse(any(path.exists() for path in self.shells))

    def test_real_launcher_keeps_shell_metacharacters_empty_arguments_and_double_dash(self):
        self.install(apply=True)
        forwarded = ["--", "child program", "", "$(touch forwarded-pwned)",
                     "quote'\";*", "line one\nline two", "--", "λ"]
        result = subprocess.run([str(self.destination), *forwarded], cwd=self.root,
                                text=True, capture_output=True, check=True)
        expected = ["--tdm-env", str(self.tdm)]
        for path in self.shells:
            expected.extend(("--cborg-shell", str(path)))
        self.assertEqual(json.loads(result.stdout), expected + forwarded)
        self.assertFalse((self.root / "credential-pwned").exists())
        self.assertFalse((self.root / "forwarded-pwned").exists())
        self.assertEqual(stat.S_IMODE(self.destination.stat().st_mode), 0o700)

    def test_child_exit_status_is_preserved(self):
        self.install(apply=True)
        result = subprocess.run([str(self.destination), "--", "exit-37"],
                                capture_output=True, check=False)
        self.assertEqual(result.returncode, 37)

    def test_interpreter_symlink_is_resolved_and_pinned(self):
        linked = self.root / "python link"
        linked.symlink_to(self.interpreter)
        self.install(interpreter=linked, apply=True)
        self.assertNotIn(str(linked), self.destination.read_text())
        linked.unlink()
        result = subprocess.run([str(self.destination)], text=True,
                                capture_output=True, check=True)
        self.assertIn("--tdm-env", json.loads(result.stdout))

    def test_update_preserves_previous_bytes_and_permissions(self):
        previous = b"#!/bin/sh\nexit 9\n"
        self.destination.write_bytes(previous)
        self.destination.chmod(0o750)
        plan = self.install()
        self.assertEqual(plan["backup"], str(self.backup()))
        self.assertEqual(self.destination.read_bytes(), previous)
        self.assertFalse(self.backup().exists())
        receipt = self.install(apply=True)
        self.assertEqual(receipt["action"], "updated")
        self.assertEqual(self.backup().read_bytes(), previous)
        self.assertEqual(stat.S_IMODE(self.backup().stat().st_mode), 0o750)
        self.assertEqual(stat.S_IMODE(self.destination.stat().st_mode), 0o700)

    def test_identical_install_is_noop_even_with_previous_backup(self):
        self.destination.write_text("old launcher")
        self.install(apply=True)
        observed = self.destination.stat()
        result = self.install(apply=True)
        self.assertEqual(result["action"], "unchanged")
        self.assertIsNone(result["backup"])
        self.assertEqual(INSTALL.fingerprint(observed), INSTALL.fingerprint(self.destination.stat()))

    def test_same_bytes_wrong_mode_are_backed_up_before_correction(self):
        self.install(apply=True)
        before = self.destination.read_bytes()
        self.destination.chmod(0o644)
        self.install(apply=True)
        self.assertEqual(self.backup().read_bytes(), before)
        self.assertEqual(stat.S_IMODE(self.backup().stat().st_mode), 0o644)
        self.assertEqual(stat.S_IMODE(self.destination.stat().st_mode), 0o700)

    def test_backup_collision_refuses_without_touching_existing_file(self):
        self.destination.write_text("previous")
        for kind in ("regular", "directory", "symlink"):
            with self.subTest(kind=kind):
                if kind == "regular":
                    self.backup().write_text("keep backup")
                elif kind == "directory":
                    self.backup().mkdir()
                else:
                    self.backup().symlink_to(self.root / "nonexistent")
                with self.assertRaisesRegex(INSTALL.Refused, "Backup already exists"):
                    self.install(apply=True)
                self.assertEqual(self.destination.read_text(), "previous")
                if kind == "directory":
                    self.backup().rmdir()
                else:
                    self.backup().unlink()

    def test_symlink_directory_fifo_and_hardlinked_destination_are_refused(self):
        target = self.root / "existing"
        target.write_text("preserve target")
        for kind in ("symlink", "directory", "fifo", "hardlink"):
            with self.subTest(kind=kind):
                if kind == "symlink":
                    self.destination.symlink_to(target)
                elif kind == "directory":
                    self.destination.mkdir()
                elif kind == "fifo":
                    os.mkfifo(self.destination)
                else:
                    os.link(target, self.destination)
                with self.assertRaises(INSTALL.Refused):
                    self.install(apply=True)
                self.assertEqual(target.read_text(), "preserve target")
                self.assertFalse(self.backup().exists())
                if kind == "directory":
                    self.destination.rmdir()
                else:
                    self.destination.unlink()

    def test_symlinked_missing_and_writable_parents_are_refused(self):
        linked = self.root / "linked"
        linked.symlink_to(self.root, target_is_directory=True)
        for parent in (linked, self.root / "missing"):
            with self.subTest(parent=parent):
                with self.assertRaises(OSError):
                    self.install(destination=parent / "launcher", apply=True)
        self.root.chmod(0o777)
        with self.assertRaisesRegex(INSTALL.Refused, "not group/world writable"):
            self.install(apply=True)
        self.root.chmod(0o700)
        self.assertFalse(self.destination.exists())

    def test_input_aliases_and_runtime_symlink_are_refused(self):
        for path in (self.runtime, self.interpreter, self.tdm, *self.shells):
            with self.subTest(path=path):
                with self.assertRaisesRegex(INSTALL.Refused, "collides"):
                    self.install(destination=path, apply=True)
        linked = self.root / "linked-runtime"
        linked.symlink_to(self.runtime)
        with self.assertRaisesRegex(INSTALL.Refused, "Runtime must not be a symlink"):
            self.install(runtime=linked, apply=True)

    def test_invalid_inputs_are_rejected_before_any_write(self):
        cases = [{"destination": Path("relative")}, {"tdm_env": Path("relative")},
                 {"runtime": self.root}, {"interpreter": self.runtime},
                 {"backup_tag": "missing-date"}, {"backup_tag": "../example-20990101"},
                 {"cborg_shells": []}]
        for changed in cases:
            with self.subTest(changed=changed), self.assertRaises(INSTALL.Refused):
                self.install(apply=True, **changed)
        with self.assertRaises(FileNotFoundError):
            self.install(runtime=self.root / "absent.py", apply=True)
        self.assertFalse(self.destination.exists())

    def test_concurrent_update_is_preserved_and_original_backup_retained(self):
        self.destination.write_text("original")
        original_write = INSTALL.exclusive_write

        def intervene(fd, name, content, mode):
            original_write(fd, name, content, mode)
            if name.startswith(".env-launcher-"):
                self.destination.write_text("concurrent change")

        with patch.object(INSTALL, "exclusive_write", side_effect=intervene):
            with self.assertRaisesRegex(INSTALL.Refused, "changed during"):
                self.install(apply=True)
        self.assertEqual(self.destination.read_text(), "concurrent change")
        self.assertEqual(self.backup().read_text(), "original")
        self.assertFalse(list(self.root.glob(".env-launcher-*")))

    def test_new_destination_created_at_publish_is_not_overwritten(self):
        original_link = os.link

        def intervene(*args, **kwargs):
            self.destination.write_text("concurrent creation")
            return original_link(*args, **kwargs)

        with patch.object(INSTALL.os, "link", side_effect=intervene):
            with self.assertRaises(FileExistsError):
                self.install(apply=True)
        self.assertEqual(self.destination.read_text(), "concurrent creation")
        self.assertFalse(list(self.root.glob(".env-launcher-*")))

    def test_cli_defaults_to_plan(self):
        argv = ["--destination", str(self.destination), "--interpreter", str(self.interpreter),
                "--runtime", str(self.runtime), "--tdm-env", str(self.tdm),
                "--cborg-shell", str(self.shells[0]), "--backup-tag", self.tag]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(INSTALL.main(argv), 0)
        self.assertFalse(json.loads(output.getvalue())["applied"])
        self.assertFalse(self.destination.exists())


if __name__ == "__main__":
    unittest.main()
