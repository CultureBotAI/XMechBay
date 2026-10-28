"""Synthetic privacy-gate checks; inputs are disposable Git repositories.

Output: unittest assertions. Serves the publication gate; 2026-10-25.
"""

import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SPEC = importlib.util.spec_from_file_location("public_content", Path(__file__).resolve().parents[1] / "scripts" / "check_public_content.py")
gate = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = gate
SPEC.loader.exec_module(gate)


class PublicationGateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.git("init", "-q")
        self.git("config", "user.name", "Example Maintainer")
        self.git("config", "user.email", "maintainer@example.invalid")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True).stdout

    def git_input(self, data, *args):
        return subprocess.run(["git", "-C", str(self.root), *args], input=data, check=True, capture_output=True).stdout

    def write(self, path, content):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content.encode() if isinstance(content, str) else content)

    def scan(self, all_files=False, max_bytes=1024 * 1024, extra=None):
        return gate.scan_repository(self.root, max_bytes, extra or [], all_files)

    def categories(self, findings):
        return {finding.category for finding in findings}

    def test_generic_configuration_and_reserved_examples_pass(self):
        self.write("README.md", "maintainer@example.invalid\nssh agent@example.invalid\n/usr/bin/python3\n${HOME}/workspaces/${PROJECT}\n")
        self.git("add", ".")
        self.git("commit", "-qm", "Add generic example")
        findings, counts = self.scan()
        self.assertEqual(findings, set())
        self.assertEqual(counts["commits"], 1)

    def test_private_patterns_are_detected_without_echoing_values(self):
        values = [
            "/" + "Users/" + "example-user/private",
            "host" + ".local",
            "user" + "@" + "example.com",
            "gh" + "p_" + "a" * 36,
            "-----BEGIN " + "PRIVATE KEY-----",
            "192" + ".168.1.22",
        ]
        findings = gate.scan_blob("\n".join(values).encode(), "fixture", 1024, [])
        self.assertEqual(len(findings), len(values))
        rendered = "\n".join(finding.render() for finding in findings)
        for value in values:
            self.assertNotIn(value, rendered)

    def test_deleted_history_still_fails(self):
        self.write("example.txt", "/" + "home/" + "example-user/private")
        self.git("add", ".")
        self.git("commit", "-qm", "Old example")
        self.git("rm", "example.txt")
        self.git("commit", "-qm", "Remove example")
        findings, counts = self.scan()
        self.assertIn("personal-home-path", self.categories(findings))
        self.assertEqual(counts["commits"], 2)

    def test_staged_private_content_cannot_hide_behind_clean_worktree(self):
        self.write("example.txt", "/" + "home/" + "example-user/private")
        self.git("add", ".")
        self.write("example.txt", "Generic example\n")
        findings, _ = self.scan()
        self.assertTrue(any(finding.location.startswith("index:") and finding.category == "personal-home-path" for finding in findings))

    def test_other_branch_and_tag_metadata_are_checked(self):
        self.write("README.md", "Generic example\n")
        self.git("add", ".")
        self.git("commit", "-qm", "Base")
        self.git("branch", "other")
        self.git("config", "user.email", "user" + "@" + "example.com")
        self.git("tag", "-am", "Example tag", "example")
        findings, _ = self.scan()
        self.assertIn("non-example-email", self.categories(findings))
        self.assertTrue(any(finding.location.startswith("tag:") for finding in findings))

    def test_github_privacy_aliases_are_allowed_only_in_git_identity_headers(self):
        service = "noreply" + "@" + "github.com"
        alias = "123+example-user" + "@" + "users.noreply.github.com"
        commit = f"tree {'0' * 40}\nauthor Example <{alias}> 1234567890 +0000\ncommitter GitHub <{service}> 1234567890 +0000\n\nSynthetic merge\n"
        self.assertFalse(gate.scan_blob(commit.encode(), "commit", 2048, [], git_metadata=True))
        tag = f"object {'0' * 40}\ntype commit\ntag example\ntagger Example <{alias}> 1234567890 +0000\n\nTag fixture\n"
        self.assertFalse(gate.scan_blob(tag.encode(), "tag", 2048, [], git_metadata=True))
        self.assertIn("non-example-email", self.categories(gate.scan_blob(commit.encode(), "source", 2048, [])))
        self.assertIn("non-example-email", self.categories(gate.scan_blob((commit + service).encode(), "commit", 2048, [], git_metadata=True)))
        self.assertIn("non-example-email", self.categories(gate.scan_blob((commit + f"author Example <{alias}> 1234567890 +0000").encode(), "commit", 2048, [], git_metadata=True)))

    def test_hidden_and_ignored_files_require_explicit_inventory(self):
        self.write(".gitignore", ".local/\n")
        self.git("add", ".gitignore")
        self.write(".local/session.txt", "host" + ".local")
        findings, counts = self.scan()
        self.assertFalse(findings)
        self.assertEqual(counts["additional_worktree_files"], 0)
        findings, counts = self.scan(all_files=True)
        self.assertIn("local-hostname", self.categories(findings))
        self.assertIn("private-or-runtime-directory", self.categories(findings))
        self.assertEqual(counts["additional_worktree_files"], 1)

    def test_local_file_name_is_not_a_hostname(self):
        self.write("note.md", "edit .claude/settings.local.json and config.local.yaml")
        self.git("add", ".")
        self.assertNotIn("local-hostname", self.categories(self.scan()[0]))
        self.write("note.md", "ssh host" + ".local")
        self.git("add", ".")
        self.assertIn("local-hostname", self.categories(self.scan()[0]))

    def test_symlinked_directories_are_audited_without_traversal(self):
        outside = self.root.parent / (self.root.name + "-outside")
        outside.mkdir()
        self.addCleanup(outside.rmdir)
        (self.root / ("host" + ".local")).symlink_to(outside, target_is_directory=True)
        findings, counts = self.scan(all_files=True)
        self.assertIn("local-hostname", self.categories(findings))
        self.assertEqual(counts["additional_worktree_files"], 1)

    def test_noncommit_refs_and_nested_tags_are_checked(self):
        blob = self.git_input(("/" + "home/" + "example-user/private").encode(), "hash-object", "-w", "--stdin").decode().strip()
        tree = self.git_input(f"100644 blob {blob}\tfixture.txt\n".encode(), "mktree").decode().strip()
        self.git("tag", "-am", "Tree fixture", "inner", tree)
        self.git("tag", "-am", "Nested fixture", "outer", "inner")
        self.git("tag", "-d", "inner")
        self.git("update-ref", "refs/fixtures/direct-blob", blob)
        findings, counts = self.scan()
        self.assertIn("personal-home-path", self.categories(findings))
        self.assertEqual(counts["commits"], 0)
        self.assertEqual(counts["history_blobs"], 1)
        self.assertEqual(counts["other_ref_objects"], 4)

    def test_binary_oversize_and_private_data_categories(self):
        self.write("records/session.bin", b"\x00\xff" + b"a" * 200)
        self.git("add", ".")
        findings, _ = self.scan(max_bytes=100)
        self.assertEqual(self.categories(findings), {"binary-or-non-utf8-blob", "oversized-blob", "private-or-runtime-directory"})

    def test_private_external_patterns_redact_paths(self):
        patterns = [gate.re.compile("private-" + "fixture")]
        self.write("private-fixture.txt", "private-" + "fixture")
        self.git("add", ".")
        findings, _ = self.scan(extra=patterns)
        self.assertIn("external-deny-pattern", self.categories(findings))
        self.assertTrue(all("private-fixture" not in finding.render() for finding in findings))

    def test_pattern_file_inside_repo_is_rejected(self):
        with self.assertRaises(SystemExit) as caught:
            gate.main(["--root", str(self.root), "--deny-patterns", str(self.root / "patterns.txt")])
        self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
