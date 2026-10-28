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

    def test_broken_links_are_named(self):
        (self.repo / "CLAUDE.md").symlink_to("AGENTS.md")  # dangling
        self.skill(".agents/skills", "a")
        (self.repo / ".claude").mkdir()
        (self.repo / ".claude/skills").symlink_to("../nowhere", target_is_directory=True)
        result = audit.audit_repo(self.repo)
        self.assertEqual(result["instructions"]["verdict"], "broken-link")
        self.assertEqual(result["skills"]["verdict"], "broken-link")
        self.assertIn("CLAUDE.md->AGENTS.md", audit.instruction_summary(result["instructions"]))
        self.assertFalse(result["ok"])

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

    def test_modified_tracked_content_always_refuses_but_untracked_can_be_forced(self):
        self.write("CLAUDE.md", "# Rules\n")
        self.skill(".claude/skills", "a")
        self.commit()
        self.write("CLAUDE.md", "# Rules edited\n")
        self.assertEqual(self.converge("--apply"), 2)
        self.assertEqual(self.converge("--apply", "--force"), 2)
        self.assertTrue((self.repo / "CLAUDE.md").is_file())
        self.write("CLAUDE.md", "# Rules\n")
        self.write(".claude/skills/a/notes.txt", "scratch\n")
        self.assertEqual(self.converge("--apply"), 2)
        self.assertEqual(self.converge("--apply", "--force"), 0)
        self.assertEqual((self.repo / ".agents/skills/a/notes.txt").read_text(), "scratch\n")

    def test_other_dotdir_changes_do_not_block(self):
        self.write("CLAUDE.md", "# Rules\n")
        self.write(".claude/settings.local.json", "{}\n")
        self.commit()
        self.write(".claude/settings.local.json", "{\"a\": 1}\n")
        self.assertEqual(self.converge("--apply"), 0)

    def test_no_git_mode_on_plain_directory(self):
        plain = Path(self.temp.name) / "plain"
        plain.mkdir()
        (plain / "CLAUDE.md").write_text("# Rules\n")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(converge.main([str(plain), "--apply"]), 0)
        self.assertEqual(os.readlink(plain / "CLAUDE.md"), "AGENTS.md")


if __name__ == "__main__":
    unittest.main()


class ReviewRegressionTests(RepoCase):
    """Behaviours the adversarial review of PR #20 found untested."""

    def git_out(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True, text=True, env=GIT_ENV).stdout

    def test_dropped_copy_keeps_untracked_and_ignored_files(self):
        self.write("AGENTS.md", "# Repo\n")
        (self.repo / "CLAUDE.md").symlink_to("AGENTS.md")
        self.skill(".agents/skills", "x", "# keep\n")
        self.skill(".claude/skills", "x", "# keep\n")
        self.write(".gitignore", ".local/\n")
        self.commit()
        self.write(".claude/skills/x/.local/record.txt", "live record\n")
        self.write(".claude/skills/x/draft.md", "untracked draft\n")
        self.assertEqual(self.converge("--apply", "--skills-from", ".agents/skills"), 2)  # untracked under the paths: refuse without --force
        with contextlib.redirect_stdout(io.StringIO()) as out:
            converge.main([str(self.repo), "--skills-from", ".agents/skills"])
        self.assertIn("keeping 2 untracked/ignored path(s)", out.getvalue())
        self.assertEqual(self.converge("--apply", "--force", "--skills-from", ".agents/skills"), 0)
        self.assertEqual((self.repo / ".agents/skills/x/.local/record.txt").read_text(), "live record\n")
        self.assertEqual((self.repo / ".agents/skills/x/draft.md").read_text(), "untracked draft\n")

    def test_nested_git_inside_dropped_tree_refuses_before_any_change(self):
        self.write("AGENTS.md", "# Repo\n")
        (self.repo / "CLAUDE.md").symlink_to("AGENTS.md")
        self.skill(".agents/skills", "x")
        self.skill(".claude/skills", "x")
        self.commit()
        nested = self.repo / ".claude/skills/scratch"
        nested.mkdir()
        subprocess.run(["git", "init", "-q", str(nested)], check=True, env=GIT_ENV)
        with self.assertRaises(SystemExit):
            self.converge("--apply", "--force")
        self.assertTrue((self.repo / ".claude/skills/x/SKILL.md").is_file())
        self.assertFalse((self.repo / ".claude/skills").is_symlink())

    def test_refusal_in_the_instructions_phase_changes_nothing(self):
        self.write("CLAUDE.md", "# A long real file\n\n- rule one\n")
        self.write("AGENTS.md", "# Another real file\n\n- rule two\n")
        self.skill(".claude/skills", "a")
        self.commit()
        with self.assertRaises(SystemExit):
            self.converge("--apply")
        self.assertFalse((self.repo / ".agents").exists())
        self.assertFalse((self.repo / ".claude/skills").is_symlink())
        self.assertEqual(self.git_out("status", "--porcelain"), "")

    def test_gemini_only_repo_becomes_agents_md(self):
        self.write("GEMINI.md", "# Rules for agy\n\n- keep it\n")
        self.commit()
        self.assertEqual(self.converge("--apply"), 0)
        self.assertEqual((self.repo / "AGENTS.md").read_text(), "# Rules for agy\n\n- keep it\n")
        self.assertEqual(os.readlink(self.repo / "CLAUDE.md"), "AGENTS.md")
        self.assertFalse((self.repo / "GEMINI.md").exists())
        self.assertTrue(audit.audit_repo(self.repo)["ok"])

    def test_no_git_keeps_the_branch_and_dirty_guards(self):
        self.git("switch", "-q", "main")
        self.write("CLAUDE.md", "# Rules\n")
        self.commit()
        self.assertEqual(self.converge("--apply", "--no-git"), 2)
        self.git("switch", "-q", "topic")
        self.write("CLAUDE.md", "# Rules edited\n")
        self.assertEqual(self.converge("--apply", "--no-git"), 2)
        self.assertTrue((self.repo / "CLAUDE.md").is_file())

    def test_carried_text_keeps_structure_and_whole_words(self):
        pointer = (
            "# Repo\n\nRead CLAUDE.md for the rules. The hook in CLAUDE.md is already enforced.\n"
            "Overseeing CLAUDE.md edits is the owner's job.\n\n"
            "1. Run `make`.\n2. Then test.\n\n```bash\nmake test\nmake lint\n```\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n> quoted\n"
        )
        self.write("AGENTS.md", pointer)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            lines = converge.carried_lines(self.repo / "AGENTS.md", "CLAUDE.md")
        text = "\n".join(lines)
        prose = " ".join(text.split())
        self.assertNotIn("Read CLAUDE.md for the rules.", prose)
        self.assertIn("The hook in CLAUDE.md is already enforced.", prose)
        self.assertIn("Overseeing CLAUDE.md edits is the owner's job.", prose)
        self.assertIn("1. Run `make`.\n2. Then test.", text)
        self.assertIn("```bash\nmake test\nmake lint\n```", text)
        self.assertIn("| a | b |\n|---|---|\n| 1 | 2 |", text)
        self.assertIn("> quoted", text)
        self.assertIn("DROP  from AGENTS.md: Read CLAUDE.md for the rules.", out.getvalue())

    def test_short_real_file_that_mentions_the_other_is_not_a_pointer(self):
        self.write("AGENTS.md", "# Repo\n\nClaude Code also gets a CLAUDE.md copy.\n\n- real rule\n")
        self.write("CLAUDE.md", "# Rules\n\n- another real rule\n")
        self.assertEqual(audit.audit_repo(self.repo)["instructions"]["verdict"], "divergent")
        self.write("AGENTS.md", "# Repo\n\nRead CLAUDE.md for the rules.\n")
        self.assertEqual(audit.audit_repo(self.repo)["instructions"]["verdict"], "pointer")

    def test_absolute_symlink_target_is_a_defect(self):
        self.write("AGENTS.md", "# Repo\n")
        (self.repo / "CLAUDE.md").symlink_to(self.repo / "AGENTS.md")
        result = audit.audit_repo(self.repo)
        self.assertEqual(result["instructions"]["verdict"], "absolute-link")
        self.assertFalse(result["ok"])
        self.commit()
        with self.assertRaises(SystemExit):
            self.converge("--apply")

    def test_ignored_cache_does_not_make_copies_drift(self):
        self.write("AGENTS.md", "# Repo\n")
        (self.repo / "CLAUDE.md").symlink_to("AGENTS.md")
        self.skill(".agents/skills", "a")
        self.skill(".claude/skills", "a")
        self.write(".gitignore", "__pycache__/\n")
        self.commit()
        self.write(".claude/skills/a/__pycache__/x.pyc", "cache")
        self.assertEqual(audit.audit_repo(self.repo)["skills"]["verdict"], "copies-identical")
        self.write(".claude/skills/a/extra.md", "untracked but real content\n")
        info = audit.audit_repo(self.repo)["skills"]
        self.assertEqual(info["verdict"], "copies-drifted")
        self.assertEqual(info["drift_detail"], {"a": ["a/extra.md (only in .claude)"]})

    def test_case_insensitive_tracked_name_is_moved_with_git(self):
        self.write("Claude.md", "# Rules\n")
        self.commit()
        if not (self.repo / "CLAUDE.md").exists():
            self.skipTest("case-sensitive filesystem")
        self.assertEqual(self.converge("--apply"), 0)
        tracked = self.git_out("ls-files").split()
        self.assertIn("AGENTS.md", tracked)
        self.assertIn("CLAUDE.md", tracked)
        self.assertNotIn("Claude.md", tracked)
        self.assertEqual(os.readlink(self.repo / "CLAUDE.md"), "AGENTS.md")
        self.assertTrue(audit.audit_repo(self.repo)["ok"])
