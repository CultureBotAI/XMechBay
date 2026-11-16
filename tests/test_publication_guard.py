"""Synthetic pre-upload boundary regressions (2026-11-15).

Inputs: disposable Git objects, mocked GitHub metadata and temporary hooks.
Outputs: unittest assertions; never contacts GitHub or operates a live checkout.
"""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE / "scripts"))
import publication_guard as guard
import install_publication_guard as installer
sys.path.pop(0)


class PublicationGuardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.environment = patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Example Maintainer")
        self.git("config", "user.email", "maintainer@example.invalid")
        self.git("commit", "--allow-empty", "-qm", "[public-source] Synthetic clean root")
        self.clean_root = self.head()
        self.url = "https://github.com/example-org/fleet-tools.git"
        self.git("remote", "add", "origin", self.url)
        self.contract = {"schema_version": 1, "repository_id": 123456, "repository_full_name": "example-org/fleet-tools", "clean_root": self.clean_root, "remote_name": "origin"}
        self.identity = {"id": 123456, "full_name": "example-org/fleet-tools", "private": False, "visibility": "public", "fork": False, "html_url": "https://github.com/example-org/fleet-tools"}

    def git(self, *args, data=None):
        return subprocess.run(["git", "-C", str(self.root), *args], input=data, check=True, capture_output=True).stdout

    def head(self):
        return self.git("rev-parse", "HEAD").decode().strip()

    def commit(self, path="source.txt", content="Synthetic example\n", message="Add example"):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        self.git("add", "--force", path)
        self.git("commit", "-qm", "[public-source] " + message)
        return self.head()

    def update(self, oid=None, destination="refs/heads/public/example", remote_oid=guard.ZERO):
        return "refs/heads/topic {} {} {}\n".format(oid or self.head(), destination, remote_oid)

    def check(self, text=None, metadata=None, remote_name="origin", remote_url=None, deny_patterns_path=None):
        return guard.check_push(self.root, self.contract, remote_name, remote_url or self.url, self.update() if text is None else text, identity_reader=lambda _: self.identity if metadata is None else metadata, deny_patterns_path=deny_patterns_path)

    def external_patterns(self, data=b"(?i)syntheticrestrictedproject\n", bind=True):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "patterns.txt"
        path.write_bytes(data)
        if bind:
            self.contract["deny_patterns_sha256"] = hashlib.sha256(data).hexdigest()
        return path

    def test_clean_history_counts_and_unpushed_files_are_not_publication_input(self):
        self.commit()
        (self.root / ".local").mkdir()
        (self.root / ".local/private.txt").write_text("worker" + ".local")
        (self.root / "source.txt").write_text("worker" + ".local")
        counts = self.check()
        self.assertEqual(counts, {"commits": 2, "blobs": 1, "paths": 1})

    def test_actual_requested_oid_is_scanned_instead_of_current_head(self):
        bad = self.commit(content="worker" + ".local")
        self.git("checkout", "--detach", "-q", self.clean_root)
        with self.assertRaisesRegex(guard.Refusal, "local-hostname"):
            self.check(self.update(bad))

    def test_hidden_ignored_and_later_deleted_versions_still_refuse(self):
        self.commit(".gitignore", ".hidden/\n")
        self.commit(".hidden/evidence.txt", "worker" + ".local")
        self.git("rm", "-qr", ".hidden")
        self.git("commit", "-qm", "[public-source] Remove hidden fixture")
        with self.assertRaisesRegex(guard.Refusal, "local-hostname"):
            self.check()

    def test_empty_tree_names_remain_checked_after_directory_is_removed(self):
        empty = self.git("mktree", data=b"").decode().strip()
        directory = "worker" + ".local"
        tree = self.git("mktree", data=f"040000 tree {empty}\t{directory}\n".encode()).decode().strip()
        bad = self.git("commit-tree", tree, "-p", self.clean_root, data=b"[public-source] Synthetic directory\n").decode().strip()
        removed = self.git("commit-tree", empty, "-p", bad, data=b"[public-source] Remove directory\n").decode().strip()
        self.assertEqual(self.git("ls-tree", "-r", removed), b"")
        with self.assertRaisesRegex(guard.Refusal, "local-hostname") as caught:
            self.check(self.update(removed))
        self.assertNotIn(directory, str(caught.exception))

    def test_generic_empty_directory_is_scanned_without_becoming_a_blob(self):
        empty = self.git("mktree", data=b"").decode().strip()
        tree = self.git("mktree", data=f"040000 tree {empty}\texamples\n".encode()).decode().strip()
        child = self.git("commit-tree", tree, "-p", self.clean_root, data=b"[public-source] Synthetic empty directory\n").decode().strip()
        self.assertEqual(self.check(self.update(child)), {"commits": 2, "blobs": 0, "paths": 1})

    def test_commit_metadata_is_scanned_without_printing_value(self):
        address = "operator" + "@" + "example.com"
        self.commit(message="Synthetic metadata " + address)
        with self.assertRaises(guard.Refusal) as caught:
            self.check()
        self.assertIn("non-example-email", str(caught.exception))
        self.assertNotIn(address, str(caught.exception))

    def test_private_path_and_blob_values_are_not_echoed(self):
        bad_name = "host" + ".local.txt"
        token = "gh" + "p_" + "a" * 36
        self.commit(bad_name, token)
        with self.assertRaises(guard.Refusal) as caught:
            self.check()
        self.assertNotIn(bad_name, str(caught.exception))
        self.assertNotIn(token, str(caught.exception))

    def test_unrelated_root_is_rejected_even_with_clean_content(self):
        self.git("checkout", "--orphan", "unrelated", "-q")
        self.git("commit", "--allow-empty", "-qm", "Other synthetic root")
        with self.assertRaisesRegex(guard.Refusal, "ancestry"):
            self.check()

    def test_merging_unrelated_history_does_not_make_it_publishable(self):
        self.git("checkout", "--orphan", "unrelated", "-q")
        self.git("commit", "--allow-empty", "-qm", "Other synthetic root")
        self.git("checkout", "main", "-q")
        self.git("merge", "--allow-unrelated-histories", "unrelated", "-m", "Merge synthetic roots")
        with self.assertRaisesRegex(guard.Refusal, "ancestry"):
            self.check()

    def test_tags_and_other_destination_namespaces_are_rejected(self):
        for ref in ["refs/tags/example", "refs/notes/example", "refs/heads/private/example", "refs/heads/public/", "refs/heads/public/Uppercase"]:
            with self.subTest(ref=ref), self.assertRaises(guard.Refusal):
                self.check(self.update(destination=ref))

    def test_noncommit_object_cannot_be_uploaded_as_allowed_branch(self):
        blob = self.git("hash-object", "-w", "--stdin", data=b"Synthetic text").decode().strip()
        with self.assertRaisesRegex(guard.Refusal, "direct commit"):
            self.check(self.update(blob))

    def test_main_delete_refused_but_scoped_branch_delete_allowed(self):
        for destination, allowed in [("refs/heads/main", False), ("refs/heads/public/example", True)]:
            text = "(delete) {} {} {}\n".format(guard.ZERO, destination, self.clean_root)
            if allowed:
                self.assertEqual(self.check(text)["commits"], 0)
            else:
                with self.assertRaises(guard.Refusal):
                    self.check(text)

    def test_multiple_updates_are_all_checked(self):
        good = self.clean_root
        bad = self.commit(content="worker" + ".local")
        with self.assertRaisesRegex(guard.Refusal, "local-hostname"):
            self.check(self.update(good, "refs/heads/main") + self.update(bad))

    def test_malformed_and_duplicate_stdin_fail_closed(self):
        for text in ["not a ref\n", self.update().replace(self.head(), "invalid"), self.update() * 2, "\n"]:
            with self.subTest(text=text[:12]), self.assertRaises(guard.Refusal):
                self.check(text)

    def test_unknown_or_literal_url_remote_name_is_rejected(self):
        for name in ["other", self.url]:
            with self.subTest(name=name), self.assertRaisesRegex(guard.Refusal, "named remote"):
                self.check(remote_name=name)

    def test_pushurl_and_insteadof_rewrites_are_checked(self):
        self.git("remote", "set-url", "--push", "origin", "https://github.com/example-org/other.git")
        with self.assertRaisesRegex(guard.Refusal, "URL"):
            self.check()
        self.git("config", "--unset", "remote.origin.pushurl")
        self.git("config", "url.https://example.invalid/.pushInsteadOf", "https://github.com/")
        with self.assertRaisesRegex(guard.Refusal, "URL"):
            self.check()

    def test_second_push_destination_is_rejected(self):
        self.git("remote", "set-url", "--add", "--push", "origin", self.url)
        self.git("remote", "set-url", "--add", "--push", "origin", self.url)
        with self.assertRaisesRegex(guard.Refusal, "URL"):
            self.check()

    def test_remote_argument_must_match_effective_configured_url(self):
        with self.assertRaisesRegex(guard.Refusal, "URL"):
            self.check(remote_url="git" + "@" + "github.com:example-org/fleet-tools.git")

    def test_repo_id_name_visibility_and_fork_are_all_pinned(self):
        cases = [{"id": 654321}, {"full_name": "example-org/renamed"}, {"private": True}, {"visibility": "private"}, {"fork": True}, {"html_url": "https://example.invalid/repository"}, {"id": True}]
        for change in cases:
            with self.subTest(change=change), self.assertRaisesRegex(guard.Refusal, "identity"):
                self.check(metadata={**self.identity, **change})

    def test_github_lookup_pins_host_and_refuses_redirect_and_error(self):
        response = subprocess.CompletedProcess([], 0, b"HTTP/2.0 200 OK\r\ncontent-type: application/json\r\n\r\n" + json.dumps(self.identity).encode(), b"")
        with patch.object(guard.subprocess, "run", return_value=response) as run:
            self.assertEqual(guard.github_metadata(self.contract), self.identity)
            self.assertIn("--hostname", run.call_args.args[0])
            self.assertIn("github.com", run.call_args.args[0])
        for response in [subprocess.CompletedProcess([], 0, b"HTTP/2.0 301 Moved\nlocation: https://example.invalid\n\n{}", b""), subprocess.CompletedProcess([], 1, b"", b"private error"), subprocess.CompletedProcess([], 0, b"invalid", b"")]:
            with patch.object(guard.subprocess, "run", return_value=response), self.assertRaises(guard.Refusal):
                guard.github_metadata(self.contract)

    def test_shallow_grafted_replaced_and_partial_history_refuse(self):
        gitdir = self.root / ".git"
        cases = [gitdir / "shallow", gitdir / "info/grafts"]
        for target in cases:
            target.write_text(self.clean_root + "\n")
            with self.assertRaises(guard.Refusal):
                self.check()
            target.unlink()
        bad = self.commit(content="worker" + ".local")
        self.git("replace", bad, self.clean_root)
        with self.assertRaisesRegex(guard.Refusal, "Replacement"):
            self.check()
        self.git("replace", "-d", bad)
        self.git("config", "remote.origin.promisor", "true")
        with self.assertRaisesRegex(guard.Refusal, "Partial"):
            self.check()

    def test_graft_environment_is_refused(self):
        with patch.dict(os.environ, {"GIT_GRAFT_FILE": "synthetic-grafts"}), self.assertRaisesRegex(guard.Refusal, "Grafted"):
            self.check()

    def test_submodule_and_oversized_blob_fail_closed(self):
        self.git("update-index", "--add", "--cacheinfo", "160000," + self.clean_root + ",dependency")
        self.git("commit", "-qm", "[public-source] Synthetic gitlink")
        with self.assertRaisesRegex(guard.Refusal, "unsupported tree"):
            self.check()
        self.git("reset", "--hard", self.clean_root)
        self.commit(content="x" * (guard.scanner.DEFAULT_MAX_BYTES + 1))
        with self.assertRaisesRegex(guard.Refusal, "oversized"):
            self.check()

    def test_contract_requires_exact_schema_and_unique_keys(self):
        contract_path = self.root / "contract.json"
        contract_path.write_text('{"schema_version":1,"schema_version":1}')
        with self.assertRaises(guard.Refusal):
            guard.load_contract(contract_path)
        for change in [{"extra": True}, {"repository_id": True}, {"clean_root": "short"}, {"remote_name": "other"}, {"deny_patterns_sha256": None}, {"deny_patterns_sha256": "short"}, {"deny_patterns_sha256": True}]:
            with self.subTest(change=change), self.assertRaises(guard.Refusal):
                guard.validate_contract({**self.contract, **change})

    def test_external_patterns_cover_ignored_and_later_deleted_blob_versions(self):
        self.commit(".gitignore", ".hidden/\n")
        self.commit(".hidden/data.txt", "SyntheticRestrictedProject\n")
        self.git("rm", "-qr", ".hidden")
        self.git("commit", "-qm", "[public-source] Remove synthetic fixture")
        self.check()  # Generic users retain the no-list contract.
        path = self.external_patterns()
        with self.assertRaisesRegex(guard.Refusal, "external-deny-pattern") as caught:
            self.check(deny_patterns_path=path)
        self.assertNotIn("SyntheticRestrictedProject", str(caught.exception))

    def test_external_patterns_cover_commit_messages_and_identity_headers(self):
        path = self.external_patterns()
        for metadata in ("message", "author"):
            with self.subTest(metadata=metadata):
                self.git("reset", "--hard", self.clean_root)
                self.git("config", "user.name", "SyntheticRestrictedProject" if metadata == "author" else "Example Maintainer")
                self.commit(message="SyntheticRestrictedProject" if metadata == "message" else "Example")
                with self.assertRaisesRegex(guard.Refusal, "external-deny-pattern"):
                    self.check(deny_patterns_path=path)

    def test_external_patterns_cover_later_deleted_empty_tree_paths(self):
        path = self.external_patterns()
        empty = self.git("mktree", data=b"").decode().strip()
        tree = self.git("mktree", data=f"040000 tree {empty}\tSyntheticRestrictedProject\n".encode()).decode().strip()
        first = self.git("commit-tree", tree, "-p", self.clean_root, data=b"[public-source] Add directory\n").decode().strip()
        removed = self.git("commit-tree", empty, "-p", first, data=b"[public-source] Remove directory\n").decode().strip()
        with self.assertRaisesRegex(guard.Refusal, "external-deny-pattern"):
            self.check(self.update(removed), deny_patterns_path=path)

    def test_external_patterns_cover_both_source_and_destination_refs(self):
        path = self.external_patterns()
        for update in (
            self.update(destination="refs/heads/public/syntheticrestrictedproject"),
            self.update().replace("refs/heads/topic", "refs/heads/syntheticrestrictedproject"),
        ):
            with self.subTest(update=update), self.assertRaisesRegex(guard.Refusal, "ref contains") as caught:
                self.check(update, deny_patterns_path=path)
            self.assertNotIn("syntheticrestrictedproject", str(caught.exception))

    def test_configured_patterns_fail_closed_on_missing_changed_or_malformed_payload(self):
        path = self.external_patterns()
        path.unlink()
        with self.assertRaises(guard.Refusal):
            self.check(deny_patterns_path=path)
        path.write_bytes(b"different-pattern\n")
        with self.assertRaisesRegex(guard.Refusal, "differs"):
            self.check(deny_patterns_path=path)
        for invalid in (b"[synthetic-private-invalid", b"\xff", b"# comments only\n\n", b"a\0b", b"x" * (guard.MAX_DENY_PATTERN_BYTES + 1)):
            path.write_bytes(invalid)
            self.contract["deny_patterns_sha256"] = hashlib.sha256(invalid).hexdigest()
            with self.subTest(length=len(invalid)), self.assertRaises(guard.Refusal) as caught:
                self.check(deny_patterns_path=path)
            self.assertNotIn("synthetic-private-invalid", str(caught.exception))

    def test_installer_requires_external_patterns_and_rejects_symlink_alias(self):
        inside = self.root / "patterns.txt"
        inside.write_text("synthetic-pattern\n")
        before = (self.root / ".git/config").read_bytes()
        with self.assertRaisesRegex(guard.Refusal, "outside"):
            installer.install(self.root, self.contract, SOURCE, inside)
        external = self.external_patterns(bind=False)
        external.unlink()
        external.symlink_to(inside)
        with self.assertRaisesRegex(guard.Refusal, "outside"):
            installer.install(self.root, self.contract, SOURCE, external)
        self.assertEqual((self.root / ".git/config").read_bytes(), before)
        self.assertFalse((self.root / ".git/xmechbay-publication").exists())

    def test_installer_pins_exact_pattern_bytes_and_refuses_changed_installation(self):
        path = self.external_patterns(b"# Synthetic list\n\n(?i)syntheticrestrictedproject\n", bind=False)
        original = path.read_bytes()
        self.assertTrue(installer.install(self.root, self.contract, SOURCE, path))
        bundle = self.root / ".git/xmechbay-publication"
        payload = bundle / guard.DENY_PATTERN_FILE
        installed = guard.load_contract(bundle / "contract.json")
        self.assertEqual(installed["deny_patterns_sha256"], hashlib.sha256(original).hexdigest())
        self.assertEqual(payload.read_bytes(), original)
        self.assertEqual(payload.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("deny_patterns_sha256", self.contract)
        self.assertFalse(installer.install(self.root, self.contract, SOURCE, path))
        path.write_bytes(b"different-pattern\n")
        self.assertEqual(payload.read_bytes(), original)
        with self.assertRaisesRegex(guard.Refusal, "differs"):
            installer.install(self.root, self.contract, SOURCE, path)
        path.write_bytes(original)
        payload.unlink()
        with self.assertRaisesRegex(guard.Refusal, "differs"):
            installer.install(self.root, self.contract, SOURCE, path)

    def test_bound_contract_cannot_install_without_matching_payload_or_use_unbound_file(self):
        path = self.external_patterns(bind=False)
        with self.assertRaisesRegex(guard.Refusal, "not bound"):
            self.check(deny_patterns_path=path)
        self.contract["deny_patterns_sha256"] = "1" * 64
        with self.assertRaisesRegex(guard.Refusal, "required"):
            installer.install(self.root, self.contract, SOURCE)
        with self.assertRaisesRegex(guard.Refusal, "differ"):
            installer.install(self.root, self.contract, SOURCE, path)

    def test_installed_hook_reads_pinned_patterns_and_refuses_missing_or_changed_copy(self):
        path = self.external_patterns(bind=False)
        installer.install(self.root, self.contract, SOURCE, path)
        bundle = self.root / ".git/xmechbay-publication"
        payload = bundle / guard.DENY_PATTERN_FILE
        mock_bin = self.root / "mock-bin"
        mock_bin.mkdir()
        mock_gh = mock_bin / "gh"
        response = "HTTP/2.0 200 OK\ncontent-type: application/json\n\n" + json.dumps(self.identity)
        mock_gh.write_text("#!/usr/bin/env python3\nimport sys\nsys.stdout.write(" + repr(response) + ")\n")
        mock_gh.chmod(0o700)
        environment = {**os.environ, "PATH": str(mock_bin) + os.pathsep + os.environ["PATH"]}

        def hook():
            return subprocess.run([str(bundle / "hooks/pre-push"), "origin", self.url], cwd=self.root, input=self.update(), capture_output=True, text=True, env=environment)

        self.assertEqual(hook().returncode, 0)
        path.unlink()  # The original private source is not a runtime dependency.
        self.commit(content="SyntheticRestrictedProject\n")
        result = hook()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("external-deny-pattern", result.stderr)
        self.assertNotIn("SyntheticRestrictedProject", result.stderr + result.stdout)
        payload.write_bytes(b"different-pattern\n")
        result = hook()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("differs", result.stderr)
        payload.unlink()
        self.assertNotEqual(hook().returncode, 0)

    def test_unmarked_commit_and_merge_commit_refuse_before_upload(self):
        self.git("commit", "--allow-empty", "-qm", "Unmarked synthetic commit")
        with self.assertRaisesRegex(guard.Refusal, "message prefix"):
            self.check()
        self.git("reset", "--hard", self.clean_root)
        child = self.commit()
        tree = self.git("rev-parse", "HEAD^{tree}").decode().strip()
        merge = self.git("commit-tree", tree, "-p", child, "-p", self.clean_root, data=b"[public-source] Synthetic merge\n").decode().strip()
        with self.assertRaisesRegex(guard.Refusal, "linear"):
            self.check(self.update(merge))

    def test_local_prefix_rule_requires_space(self):
        tree = self.git("rev-parse", "HEAD^{tree}").decode().strip()
        for message, allowed in [("[public-source] Synthetic summary", True), ("[public-source]Missing space", False), ("[public-source]\tTab", False)]:
            with self.subTest(message=message):
                child = self.git("commit-tree", tree, "-p", self.clean_root, data=(message + "\n").encode()).decode().strip()
                if allowed:
                    self.assertEqual(self.check(self.update(child))["commits"], 2)
                else:
                    with self.assertRaisesRegex(guard.Refusal, "message prefix"):
                        self.check(self.update(child))

    def test_non_fast_forward_and_unknown_advertised_commit_refuse(self):
        child = self.commit()
        with self.assertRaises(guard.Refusal):
            self.check(self.update(self.clean_root, remote_oid=child))
        with self.assertRaises(guard.Refusal):
            self.check(self.update(child, remote_oid="1" * 40))
        self.assertEqual(self.check(self.update(child, remote_oid=self.clean_root))["commits"], 2)

    def test_installer_pins_files_backs_up_config_and_is_idempotent(self):
        original = (self.root / ".git/config").read_bytes()
        self.assertTrue(installer.install(self.root, self.contract, SOURCE))
        bundle = self.root / ".git/xmechbay-publication"
        self.assertEqual((bundle / "publication_guard.py").read_bytes(), (SOURCE / "scripts/publication_guard.py").read_bytes())
        self.assertEqual(json.loads((bundle / "contract.json").read_text()), self.contract)
        backups = list((self.root / ".git").glob("config.bak-publication-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)
        self.assertFalse(installer.install(self.root, self.contract, SOURCE))
        (bundle / "check_public_content.py").write_text("changed")
        with self.assertRaisesRegex(guard.Refusal, "differs"):
            installer.install(self.root, self.contract, SOURCE)

    def test_installer_does_not_replace_other_hooks(self):
        hook = self.root / ".git/hooks/pre-push"
        hook.write_text("#!/bin/sh\nexit 1\n")
        with self.assertRaisesRegex(guard.Refusal, "Existing hooks"):
            installer.install(self.root, self.contract, SOURCE)
        self.assertEqual(hook.read_text(), "#!/bin/sh\nexit 1\n")

    def test_installer_rejects_configured_hookspath(self):
        self.git("config", "core.hooksPath", "custom-hooks")
        with self.assertRaisesRegex(guard.Refusal, "hooksPath"):
            installer.install(self.root, self.contract, SOURCE)

    def test_installed_hook_executes_pinned_copies_with_mock_identity(self):
        installer.install(self.root, self.contract, SOURCE)
        bundle = self.root / ".git/xmechbay-publication"
        mock_bin = self.root / "mock-bin"
        mock_bin.mkdir()
        mock_gh = mock_bin / "gh"
        response = "HTTP/2.0 200 OK\ncontent-type: application/json\n\n" + json.dumps(self.identity)
        mock_gh.write_text("#!/usr/bin/env python3\nimport sys\nsys.stdout.write(" + repr(response) + ")\n")
        mock_gh.chmod(0o700)
        environment = {**os.environ, "PATH": str(mock_bin) + os.pathsep + os.environ["PATH"]}
        # A hostile worktree module cannot replace the installed scanner.
        (self.root / "check_public_content.py").write_text("raise RuntimeError('wrong module')\n")
        result = subprocess.run([str(bundle / "hooks/pre-push"), "origin", self.url], cwd=self.root, input=self.update(), capture_output=True, text=True, env=environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Publication guard passed", result.stdout)
        result = subprocess.run([str(bundle / "hooks/pre-push"), "origin", self.url], cwd=self.root, input=self.update(destination="refs/tags/blocked"), capture_output=True, text=True, env=environment)
        self.assertNotEqual(result.returncode, 0)


class PublicationRulesetContractTests(unittest.TestCase):
    """Pin intended ref protections; these do not prove live server enforcement."""

    def read_ruleset(self, filename, target):
        ruleset = json.loads((SOURCE / "config/publication" / filename).read_text())
        self.assertEqual(ruleset["target"], target)
        self.assertEqual(ruleset["enforcement"], "active")
        self.assertEqual(ruleset["bypass_actors"], [])
        rules = {item["type"]: item for item in ruleset["rules"]}
        self.assertEqual(len(rules), len(ruleset["rules"]))
        return ruleset, rules

    def test_all_branch_rules_do_not_claim_metadata_enforcement(self):
        ruleset, rules = self.read_ruleset("all-branches.json", "branch")
        self.assertEqual(ruleset["conditions"]["ref_name"], {"include": ["~ALL"], "exclude": []})
        self.assertEqual(set(rules), {"non_fast_forward", "required_linear_history"})

    def test_namespace_fence_covers_creation_and_update_with_recursive_exclusion(self):
        ruleset, rules = self.read_ruleset("blocked-branches.json", "branch")
        # GitHub uses Ruby File.fnmatch with FNM_PATHNAME: * alone misses
        # nested public branches. A live canary must exercise these patterns.
        self.assertEqual(ruleset["conditions"]["ref_name"], {
            "include": ["~ALL"],
            "exclude": ["refs/heads/main", "refs/heads/public/**/*"],
        })
        self.assertEqual(set(rules), {"creation", "update"})
        self.assertIs(rules["update"]["parameters"]["update_allows_fetch_and_merge"], False)
        # No deletion rule: remove an accidentally created disallowed branch.

    def test_tags_disallow_creation_update_and_deletion(self):
        ruleset, rules = self.read_ruleset("all-tags.json", "tag")
        self.assertEqual(ruleset["conditions"]["ref_name"], {"include": ["~ALL"], "exclude": []})
        self.assertEqual(set(rules), {"creation", "update", "deletion"})
        self.assertIs(rules["update"]["parameters"]["update_allows_fetch_and_merge"], False)

    def test_main_preserves_review_and_required_ci_without_deletion_or_rewrite(self):
        ruleset, rules = self.read_ruleset("main.json", "branch")
        self.assertEqual(ruleset["conditions"]["ref_name"], {"include": ["refs/heads/main"], "exclude": []})
        self.assertEqual(set(rules), {
            "deletion", "non_fast_forward", "required_linear_history",
            "pull_request", "required_status_checks",
        })
        self.assertIs(rules["pull_request"]["parameters"]["required_review_thread_resolution"], True)
        checks = rules["required_status_checks"]["parameters"]
        self.assertIs(checks["do_not_enforce_on_create"], False)
        self.assertIs(checks["strict_required_status_checks_policy"], True)
        self.assertEqual(checks["required_status_checks"], [
            {"context": "checks"}, {"context": "Python 3.9 stdlib checks"},
        ])


if __name__ == "__main__":
    unittest.main()
