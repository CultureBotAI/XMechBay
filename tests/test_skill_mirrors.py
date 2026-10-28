#!/usr/bin/env python3
"""Check mirror synchronization with isolated fixtures (2026-10-25).

Inputs: synthetic skill trees in a temporary directory. Output: unittest results.
No live skills, settings, credentials, or agent sessions are modified.
"""

import contextlib
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location(
    "skill_mirrors", Path(__file__).resolve().parents[1] / "scripts/sync_skill_mirrors.py"
)
mirrors = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mirrors)


class MirrorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name) / "source"
        self.target = Path(self.temp.name) / "target"
        self.source.mkdir()
        self.target.mkdir()

    def sync(self, check=True):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return mirrors.synchronize(self.source, self.target, check)

    def test_check_detects_hidden_ignored_difference_without_writing(self):
        for root in (self.source, self.target):
            (root / ".gitignore").write_text("*.pyc\n")
            (root / ".hidden.pyc").write_bytes(b"old")
        (self.source / ".hidden.pyc").write_bytes(b"new")
        self.assertEqual(self.sync(), 1)
        self.assertEqual((self.target / ".hidden.pyc").read_bytes(), b"old")

    def test_copy_missing_file_and_executable_bit(self):
        (self.source / "skill").mkdir()
        helper = self.source / "skill/helper.sh"
        helper.write_text("#!/bin/sh\n")
        helper.chmod(0o755)
        self.assertEqual(self.sync(), 1)
        self.assertEqual(self.sync(check=False), 0)
        self.assertEqual(self.sync(), 0)
        self.assertEqual((self.target / "skill/helper.sh").stat().st_mode & 0o111, 0o111)

    def test_extra_file_is_preserved_and_blocks_sync(self):
        (self.target / "extra").write_text("keep me")
        (self.source / "new").write_text("pending")
        self.assertEqual(self.sync(check=False), 1)
        self.assertEqual((self.target / "extra").read_text(), "keep me")
        self.assertFalse((self.target / "new").exists())

    def test_symlink_is_refused_before_copy(self):
        (self.target / "redirect").symlink_to(self.source, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink requires manual review"):
            self.sync(check=False)

    def test_target_symlink_to_source_is_parity(self):
        self.target.rmdir()
        self.target.symlink_to(Path("source"), target_is_directory=True)
        (self.source / "skill").mkdir()
        (self.source / "skill/SKILL.md").write_text("x")
        self.assertEqual(self.sync(), 0)
        self.assertEqual(self.sync(check=False), 0)
        self.assertTrue(self.target.is_symlink())

    def test_target_symlink_elsewhere_is_refused(self):
        self.target.rmdir()
        elsewhere = Path(self.temp.name) / "elsewhere"
        elsewhere.mkdir()
        self.target.symlink_to(elsewhere, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "Expected a real skill directory"):
            self.sync()


if __name__ == "__main__":
    unittest.main()


class ReverseLayoutTests(MirrorTests):
    def test_source_symlink_to_target_is_parity(self):
        self.source.rmdir()
        self.source.symlink_to(Path("target"), target_is_directory=True)
        (self.target / "skill").mkdir()
        (self.target / "skill/SKILL.md").write_text("x")
        self.assertEqual(self.sync(), 0)
