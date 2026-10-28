#!/usr/bin/env python3
"""User-level skill directory sync on temporary directories only (2026-10-28)."""

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location("sync_user_skills", Path(__file__).resolve().parents[1] / "scripts/sync_user_skills.py")
sync = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync)


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.canonical = root / "agents"
        self.a, self.b = root / "toolA", root / "toolB"
        self.a.mkdir()
        self.b.mkdir()
        self.repo = root / "repo-skill"
        (self.repo).mkdir()
        (self.repo / "SKILL.md").write_text("# repo\n")

    def run_sync(self, *extra):
        argv = ["--canonical", str(self.canonical), "--tool", str(self.a), "--tool", str(self.b), *extra]
        with contextlib.redirect_stdout(io.StringIO()):
            return sync.main(argv)

    def test_links_follow_to_the_real_directory_and_are_logged(self):
        (self.a / "shared").symlink_to(self.repo, target_is_directory=True)
        (self.b / "own").mkdir()
        (self.b / "own/SKILL.md").write_text("# own\n")
        (self.a / "synced").mkdir()
        self.assertEqual(self.run_sync(), 0)
        self.assertFalse(self.canonical.exists())  # dry run
        self.assertEqual(self.run_sync("--apply"), 0)
        self.assertEqual(Path(os.readlink(self.canonical / "shared")).resolve(), self.repo.resolve())
        self.assertEqual(Path(os.readlink(self.b / "shared")).resolve(), self.repo.resolve())
        self.assertEqual(Path(os.readlink(self.a / "own")).resolve(), (self.b / "own").resolve())
        self.assertFalse((self.b / "synced").exists())
        log = [json.loads(l) for l in (self.canonical / ".sync-user-skills.log").read_text().splitlines()]
        self.assertEqual(len(log), 4)
        self.assertEqual(self.run_sync("--apply"), 0)
        self.assertEqual(len((self.canonical / ".sync-user-skills.log").read_text().splitlines()), 4)

    def test_conflicting_content_is_reported_not_linked(self):
        (self.a / "x").mkdir()
        (self.a / "x/SKILL.md").write_text("# a\n")
        (self.b / "x").mkdir()
        (self.b / "x/SKILL.md").write_text("# b\n")
        self.assertEqual(self.run_sync("--apply"), 1)
        self.assertFalse((self.canonical / "x").exists())

    def test_identical_copies_are_not_a_conflict(self):
        for base in (self.a, self.b):
            (base / "same").mkdir()
            (base / "same/SKILL.md").write_text("# same\n")
        self.assertEqual(self.run_sync("--apply"), 0)
        self.assertEqual(Path(os.readlink(self.canonical / "same")).resolve(), (self.a / "same").resolve())

    def test_broken_link_is_skipped(self):
        (self.a / "gone").symlink_to(Path(self.temp.name) / "missing", target_is_directory=True)
        self.assertEqual(self.run_sync("--apply"), 0)
        self.assertFalse((self.canonical / "gone").exists())


if __name__ == "__main__":
    unittest.main()
