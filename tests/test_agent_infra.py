#!/usr/bin/env python3
"""Audit and converge agent infrastructure on synthetic repositories (2026-10-28).

Inputs: temporary Git repositories built per test. Output: unittest results.
No real checkout, agent settings, or user-level file is read or modified.
"""

import contextlib
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


audit = load("agent_infra_audit")
converge = load("agent_infra_converge")

GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "HOME": tempfile.gettempdir()}


class RepoCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.write(".gitattributes", "")
        self.commit()
        self.git("switch", "-q", "-c", "topic")

    def commit(self):
        self.git("add", "-A")
        self.git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "--allow-empty", "-m", "fixture")

    def git(self, *args):
        subprocess.run(["git", "-C", str(self.repo), *args], check=True, env=GIT_ENV, capture_output=True)

    def write(self, rel, text="content\n"):
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def skill(self, tree, name, text="# skill\n"):
        self.write(f"{tree}/{name}/SKILL.md", text)

    def converge(self, *argv):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return converge.main([str(self.repo), *argv])


class AuditTests(RepoCase):
    def test_claude_only_layout(self):
        self.write("CLAUDE.md", "# Rules\n")
        self.skill(".claude/skills", "a")
        result = audit.audit_repo(self.repo)
        self.assertFalse(result["ok"])
        self.assertEqual(result["instructions"]["verdict"], "claude-only")
        self.assertEqual(result["skills"]["verdict"], "claude-only")

    def test_pointer_and_reverse_symlink_are_named(self):
        self.write("CLAUDE.md", "# Rules\n\nlong\n")
        self.write("AGENTS.md", "# Repo\n\nRead CLAUDE.md for the rules.\n")
        self.skill(".claude/skills", "a")
        (self.repo / ".agents").mkdir()
        (self.repo / ".agents/skills").symlink_to("../.claude/skills", target_is_directory=True)
        result = audit.audit_repo(self.repo)
        self.assertEqual((result["instructions"]["verdict"], result["instructions"]["canonical"]), ("pointer", "CLAUDE.md"))
        self.assertEqual((result["skills"]["verdict"], result["skills"]["canonical"]), ("unified-symlink", ".claude/skills"))
        self.assertFalse(result["ok"])

    def test_drifted_copies_list_skill_names(self):
        self.skill(".agents/skills", "same")
        self.skill(".claude/skills", "same")
        self.skill(".agents/skills", "drift", "# a\n")
        self.skill(".claude/skills", "drift", "# b\n")
        self.skill(".claude/skills", "only-claude")
        self.write("AGENTS.md", "# Repo\n")
        (self.repo / "CLAUDE.md").symlink_to("AGENTS.md")
        result = audit.audit_repo(self.repo)
        self.assertEqual(result["skills"]["verdict"], "copies-drifted")
        self.assertEqual(result["skills"]["drifted"], ["drift", "only-claude"])
        self.assertEqual(result["instructions"]["verdict"], "unified")

    def test_target_layout_is_ok_and_gemini_breaks_it(self):
        self.write("AGENTS.md", "# Repo\n")
        (self.repo / "CLAUDE.md").symlink_to("AGENTS.md")
        self.skill(".agents/skills", "a")
        (self.repo / ".claude").mkdir()
        (self.repo / ".claude/skills").symlink_to("../.agents/skills", target_is_directory=True)
        self.assertTrue(audit.audit_repo(self.repo)["ok"])
        self.write("GEMINI.md", "# Repo\n\nRead AGENTS.md.\n")
        result = audit.audit_repo(self.repo)
        self.assertFalse(result["ok"])
        self.assertEqual(result["instructions"]["gemini"], "pointer")

    def test_scan_skips_linked_worktrees_by_default(self):
        self.write("CLAUDE.md", "# Rules\n")
        self.commit()
        worktree = Path(self.temp.name) / "wt"
        self.git("worktree", "add", "-q", str(worktree), "-b", "wt")
        found = audit.find_repos(Path(self.temp.name), 2)
        self.assertEqual(found, [self.repo.resolve()])
        self.assertEqual(audit.find_repos(Path(self.temp.name), 2, include_worktrees=True), [self.repo.resolve(), worktree.resolve()])

    def test_global_common_block_drift(self):
        a = Path(self.temp.name) / "a.md"
        b = Path(self.temp.name) / "b.md"
        a.write_text(f"# A\n\n{audit.COMMON_BEGIN}\nrule\n{audit.COMMON_END}\n")
        b.write_text(f"# B\n{audit.COMMON_BEGIN}\nrule\n{audit.COMMON_END}\nextra\n")
        self.assertTrue(audit.audit_global([str(a), str(b)])["ok"])
        b.write_text(f"{audit.COMMON_BEGIN}\nother\n{audit.COMMON_END}\n")
        result = audit.audit_global([str(a), str(b)])
        self.assertFalse(result["ok"])
        self.assertEqual(result["distinct_common_blocks"], 2)


class ConvergeTests(RepoCase):
    def test_dry_run_changes_nothing(self):
        self.write("CLAUDE.md", "# Rules\n")
        self.skill(".claude/skills", "a")
        self.commit()
        self.assertEqual(self.converge(), 0)
        self.assertTrue((self.repo / "CLAUDE.md").is_file())
        self.assertFalse((self.repo / "AGENTS.md").exists())
        self.assertFalse((self.repo / ".agents").exists())

    def test_claude_only_repo_reaches_target_with_git(self):
        self.write("CLAUDE.md", "# Rules\n")
        self.skill(".claude/skills", "a", "# a\n")
        self.write(".claude/skills/a/.ignored", "untracked\n")
        self.write(".gitignore", ".ignored\n")
        self.commit()
        self.assertEqual(self.converge("--apply"), 0)
        self.assertTrue(audit.audit_repo(self.repo)["ok"])
        self.assertEqual(os.readlink(self.repo / "CLAUDE.md"), "AGENTS.md")
        self.assertEqual((self.repo / "AGENTS.md").read_text(), "# Rules\n")
        self.assertEqual(os.readlink(self.repo / ".claude/skills"), "../.agents/skills")
        self.assertEqual((self.repo / ".claude/skills/a/SKILL.md").read_text(), "# a\n")
        self.assertEqual((self.repo / ".agents/skills/a/.ignored").read_text(), "untracked\n")

    def test_pointer_and_drifted_copies_require_choice_then_carry_lines(self):
        self.write("CLAUDE.md", "# Rules\n\n- real rule\n")
        self.write("AGENTS.md", "# Repo\n\nRead CLAUDE.md for the rules.\n\n- Skills live in .claude/skills/.\n")
        self.write("GEMINI.md", "# Repo\n\nRead AGENTS.md.\n\n- agy: audits are read-only.\n")
        self.skill(".claude/skills", "a", "# keep\n")
        self.skill(".agents/skills", "a", "# stub\n")
        self.commit()
        with self.assertRaises(SystemExit):
            self.converge("--apply")
        self.assertTrue((self.repo / "GEMINI.md").is_file())  # refused before any change
        self.assertEqual(self.converge("--apply", "--skills-from", ".claude/skills"), 0)
        result = audit.audit_repo(self.repo)
        self.assertTrue(result["ok"], result)
        text = (self.repo / "AGENTS.md").read_text()
        self.assertIn("- real rule", text)
        self.assertIn("## Carried from the former AGENTS.md (review and trim)", text)
        self.assertIn("- Skills live in .claude/skills/.", text)
        self.assertNotIn("Read CLAUDE.md", text)
        self.assertIn("## Antigravity (agy) notes carried from the former GEMINI.md", text)
        self.assertIn("- agy: audits are read-only.", text)
        self.assertEqual((self.repo / ".agents/skills/a/SKILL.md").read_text(), "# keep\n")
        self.assertFalse((self.repo / "GEMINI.md").exists())

    def test_identical_copies_keep_agents_and_recreate_pruned_claude_dir(self):
        self.write("AGENTS.md", "# Repo\n")
        (self.repo / "CLAUDE.md").symlink_to("AGENTS.md")
        self.skill(".agents/skills", "a")
        self.skill(".claude/skills", "a")
        self.commit()
        self.assertEqual(self.converge("--apply"), 0)
        self.assertTrue(audit.audit_repo(self.repo)["ok"])
        self.assertEqual(os.readlink(self.repo / ".claude/skills"), "../.agents/skills")

    def test_reverse_symlinks_are_flipped(self):
        self.write("CLAUDE.md", "# Rules\n")
        (self.repo / "AGENTS.md").symlink_to("CLAUDE.md")
        self.skill(".claude/skills", "a")
        (self.repo / ".agents").mkdir()
        (self.repo / ".agents/skills").symlink_to("../.claude/skills", target_is_directory=True)
        self.commit()
        self.assertTrue(audit.audit_repo(self.repo)["ok"])  # reverse direction is compliant
        self.assertEqual(self.converge("--apply"), 0)
        self.assertTrue((self.repo / "AGENTS.md").is_symlink())  # untouched without --flip-reverse
        self.assertEqual(self.converge("--apply", "--flip-reverse"), 0)
        self.assertTrue(audit.audit_repo(self.repo)["ok"])
        self.assertTrue((self.repo / "AGENTS.md").is_file() and not (self.repo / "AGENTS.md").is_symlink())
        self.assertTrue((self.repo / ".agents/skills").is_dir() and not (self.repo / ".agents/skills").is_symlink())

    def test_reverse_canonical_keeps_claude_real_for_fleet_tooling(self):
        self.write("CLAUDE.md", "# Rules\n")
        self.write("AGENTS.md", "# Repo\n\nRead CLAUDE.md.\n")
        self.skill(".claude/skills", "a", "# keep\n")
        self.skill(".agents/skills", "a", "# stub\n")
        self.commit()
        self.assertEqual(self.converge("--apply", "--skills-canonical", ".claude/skills", "--skills-from", ".claude/skills"), 0)
        result = audit.audit_repo(self.repo)
        self.assertTrue(result["ok"], result)
        self.assertEqual(os.readlink(self.repo / ".agents/skills"), "../.claude/skills")
        self.assertFalse((self.repo / ".claude/skills").is_symlink())
        self.assertEqual((self.repo / ".agents/skills/a/SKILL.md").read_text(), "# keep\n")
        self.assertEqual(os.readlink(self.repo / "CLAUDE.md"), "AGENTS.md")
        tracked = subprocess.run(["git", "-C", str(self.repo), "ls-files", "--", ".claude/skills"], capture_output=True, text=True, env=GIT_ENV).stdout.split()
        self.assertEqual(tracked, [".claude/skills/a/SKILL.md"])

    def test_flip_to_reverse_from_forward(self):
        self.write("AGENTS.md", "# Repo\n")
        (self.repo / "CLAUDE.md").symlink_to("AGENTS.md")
        self.skill(".agents/skills", "a", "# keep\n")
        (self.repo / ".claude").mkdir()
        (self.repo / ".claude/skills").symlink_to("../.agents/skills", target_is_directory=True)
        self.commit()
        self.assertEqual(self.converge("--apply", "--skills-canonical", ".claude/skills", "--flip-reverse"), 0)
        self.assertTrue(audit.audit_repo(self.repo)["ok"])
        self.assertFalse((self.repo / ".claude/skills").is_symlink())
        self.assertEqual(os.readlink(self.repo / ".agents/skills"), "../.claude/skills")
        self.assertEqual((self.repo / ".claude/skills/a/SKILL.md").read_text(), "# keep\n")

    def test_refuses_on_main(self):
        self.git("switch", "-q", "main")
        self.write("CLAUDE.md", "# Rules\n")
        self.commit()
        self.assertEqual(self.converge("--apply"), 2)
        self.assertTrue((self.repo / "CLAUDE.md").is_file())

    def test_refuses_dirty_paths_unless_forced(self):
        self.write("CLAUDE.md", "# Rules\n")
        self.commit()
        self.write("CLAUDE.md", "# Rules edited\n")
        self.assertEqual(self.converge("--apply"), 2)
        self.assertTrue((self.repo / "CLAUDE.md").is_file())
        self.assertEqual(self.converge("--apply", "--force"), 0)
        self.assertEqual((self.repo / "AGENTS.md").read_text(), "# Rules edited\n")

    def test_no_git_mode_on_plain_directory(self):
        plain = Path(self.temp.name) / "plain"
        plain.mkdir()
        (plain / "CLAUDE.md").write_text("# Rules\n")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(converge.main([str(plain), "--apply"]), 0)
        self.assertEqual(os.readlink(plain / "CLAUDE.md"), "AGENTS.md")


if __name__ == "__main__":
    unittest.main()
