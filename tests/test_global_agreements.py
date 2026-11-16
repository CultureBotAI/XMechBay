#!/usr/bin/env python3
"""Common-block splicing for user-level agent files, on temporary files only (2026-10-28)."""

import contextlib
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location("sync_global_agreements", Path(__file__).resolve().parents[1] / "scripts/sync_global_agreements.py")
sync = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync)


class SpliceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        self.common = self.dir / "common.md"
        self.common.write_text("## Rule\n\n- do the thing\n")

    def run_sync(self, *targets, check=False):
        argv = ["--common", str(self.common), "--date", "20260101"] + [a for t in targets for a in ("--target", str(t))] + (["--check"] if check else [])
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return sync.main(argv)

    def test_insert_after_heading_and_keep_tool_text(self):
        target = self.dir / "AGENTS.md"
        target.write_text("# Global\n\n## Tool only\n\n- keep\n")
        self.assertEqual(self.run_sync(target, check=True), 1)
        self.assertEqual(self.run_sync(target), 0)
        text = target.read_text()
        self.assertTrue(text.startswith("# Global\n\n" + sync.BEGIN))
        self.assertIn("- do the thing\n" + sync.END + "\n\n## Tool only\n\n- keep\n", text)
        self.assertTrue((self.dir / "AGENTS.md.bak-common-20260101").exists())
        self.assertEqual(self.run_sync(target, check=True), 0)

    def test_replace_existing_block_only(self):
        target = self.dir / "CLAUDE.md"
        target.write_text(f"# G\n\n{sync.BEGIN}\nold\n{sync.END}\n\n## Hooks\n\ntext\n")
        self.assertEqual(self.run_sync(target), 0)
        text = target.read_text()
        self.assertNotIn("old", text)
        self.assertIn("- do the thing", text)
        self.assertTrue(text.endswith("## Hooks\n\ntext\n"))

    def test_second_run_refuses_to_overwrite_backup(self):
        target = self.dir / "AGENTS.md"
        target.write_text("# G\n")
        self.assertEqual(self.run_sync(target), 0)
        self.common.write_text("changed\n")
        self.assertEqual(self.run_sync(target), 2)

    def test_missing_target_is_created(self):
        target = self.dir / "new.md"
        self.assertEqual(self.run_sync(target), 0)
        self.assertTrue(target.read_text().startswith(sync.BEGIN))


if __name__ == "__main__":
    unittest.main()


class ValidateBeforeWriteTests(SpliceTests):
    def test_backup_collision_on_one_target_writes_none(self):
        one = self.dir / "one.md"
        two = self.dir / "two.md"
        one.write_text("# One\n")
        two.write_text("# Two\n")
        (self.dir / "two.md.bak-common-20260101").write_text("old backup\n")
        self.assertEqual(self.run_sync(one, two), 2)
        self.assertEqual(one.read_text(), "# One\n")

    def test_lone_marker_is_refused(self):
        one = self.dir / "one.md"
        one.write_text(f"# One\n{sync.END}\n")
        self.assertEqual(self.run_sync(one), 2)
        self.assertEqual(one.read_text(), f"# One\n{sync.END}\n")
