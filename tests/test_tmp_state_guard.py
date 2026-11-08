#!/usr/bin/env python3
"""tmp-state guard on synthetic roots inside a temporary directory (2026-11-08).

Every repository, remote and backup lives under the test's own temporary
directory. The operating system's cleaned roots are patched out so that the
synthetic root stands in for /private/tmp. Runs under unittest and pytest.
"""

import contextlib
import errno
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import time
import types
import unittest
from unittest import mock


SPEC = importlib.util.spec_from_file_location(
    "tmp_state_guard", Path(__file__).resolve().parents[1] / "scripts" / "tmp_state_guard.py")
GUARD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GUARD)
REAL_FILE_TIMES = GUARD.file_times
DAY = 86400
HAVE_TOOLS = bool(shutil.which("git") and shutil.which("rsync"))


def ctime_follows_mtime(info):
    """ctime cannot be set portably; the tests let it follow the mtime they set."""
    return info.st_atime, info.st_mtime, info.st_mtime


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd)] + list(args), check=True, capture_output=True).stdout


def age(path, days):
    stamp = time.time() - days * DAY
    os.utime(path, (stamp, stamp), follow_symlinks=False)


def snapshot(*roots, metadata=True):
    """Everything about a tree except access times, which reading may update.

    metadata=False keeps mode, size, mtime and content only (for copies, whose
    directory ctime rsync may legitimately refresh)."""
    state = {}
    for root in roots:
        for directory, names, files in os.walk(root):
            for name in [""] + sorted(names) + sorted(files):
                path = os.path.join(directory, name) if name else directory
                info = os.lstat(path)
                record = (info.st_mode, info.st_size, info.st_mtime_ns)
                if metadata:
                    record += (info.st_uid, info.st_gid, info.st_ctime_ns, info.st_ino, info.st_nlink)
                if stat.S_ISLNK(info.st_mode):
                    record += (os.readlink(path),)
                elif stat.S_ISREG(info.st_mode):
                    record += (hashlib.sha256(Path(path).read_bytes()).hexdigest(),)
                elif stat.S_ISDIR(info.st_mode):
                    record += (tuple(sorted(os.listdir(path))),)
                state[path] = record
    return state


@unittest.skipUnless(HAVE_TOOLS, "git and rsync are required")
class GuardCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(os.path.realpath(self.temporary.name))
        self.root = self.base / "root"
        self.remotes = self.base / "remotes"
        self.outside = self.base / "outside"
        for path in (self.root, self.remotes, self.outside):
            path.mkdir()
        self.dest = self.base / "backups"
        self.state = self.base / "state"
        config = self.base / "gitconfig"
        config.write_text("[user]\n\tname = Synthetic\n\temail = synthetic@example.invalid\n"
                          "[init]\n\tdefaultBranch = main\n[commit]\n\tgpgsign = false\n"
                          "[advice]\n\tdetachedHead = false\n")
        environment = mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(config), "GIT_CONFIG_NOSYSTEM": "1"})
        environment.start()
        self.addCleanup(environment.stop)
        for name, value in (("system_cleaned_roots", lambda: []), ("file_times", ctime_follows_mtime)):
            patcher = mock.patch.object(GUARD, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    # -- builders

    def bare(self, name="origin.git", where=None):
        path = (where or self.remotes) / name
        subprocess.run(["git", "init", "-q", "--bare", str(path)], check=True, capture_output=True)
        return path

    def clone(self, name, remote=None, push=True, where=None):
        path = (where or self.root) / name
        if remote is None:
            subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)
        else:
            subprocess.run(["git", "clone", "-q", str(remote), str(path)], check=True, capture_output=True)
        self.commit(path, "README.md", "synthetic\n")
        if remote is not None and push:
            git(path, "push", "-q", "origin", "HEAD:refs/heads/main")
        return path

    def commit(self, repo, name, text):
        (Path(repo) / name).write_text(text)
        git(repo, "add", name)
        git(repo, "commit", "-q", "-m", "change " + name)

    def folder(self, name, days, content="notes\n", filename="notes.txt"):
        path = self.root / name
        path.mkdir(parents=True, exist_ok=True)
        target = path / filename
        target.write_text(content)
        age(target, days)
        return path

    def run_guard(self, *extra, state=True):
        argv = ["--root", "synthetic=" + str(self.root), "--dest", str(self.dest), "--free-floor-gb", "0", "--json"]
        if state:
            argv += ["--state-dir", str(self.state)]
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            code = GUARD.main(argv + list(extra))
        text = output.getvalue()
        return code, (json.loads(text) if text.strip().startswith("{") else None)

    def entry(self, report, path):
        matches = [record for record in report["entries"] if record["path"] == str(path)]
        return matches[0] if matches else None

    def copy_of(self, path):
        return self.dest / "synthetic" / Path(path).relative_to(self.root)


class GitRiskTests(GuardCase):
    def test_unpushed_commit_is_rescued_as_a_working_clone(self):
        repo = self.clone("unpushed", self.bare())
        self.commit(repo, "work.txt", "unpushed work\n")
        code, report = self.run_guard()
        self.assertEqual(code, 10)
        record = self.entry(report, repo)
        self.assertEqual(record["kind"], "clone")
        self.assertEqual(record["reasons"], ["unpushed-branches"])
        self.assertEqual(record["git"]["unpushed_branches"], ["main"])
        self.assertEqual(record["git"]["unpushed_commits"], 1)
        self.assertEqual(record["rescue"]["status"], "rescued")
        copy = self.copy_of(repo)
        self.assertEqual(git(copy, "rev-parse", "HEAD"), git(repo, "rev-parse", "HEAD"))
        self.assertEqual((copy / "work.txt").read_text(), "unpushed work\n")

    def test_fully_pushed_clone_and_ancestor_branch_are_not_at_risk(self):
        repo = self.clone("pushed", self.bare())
        self.commit(repo, "second.txt", "more\n")
        git(repo, "push", "-q", "origin", "main")
        git(repo, "branch", "older", "HEAD~1")  # an ancestor of the remote head
        code, report = self.run_guard()
        self.assertEqual((code, report["status"], report["entries"]), (0, "ok", []))
        self.assertEqual(report["inventory"]["by_kind"], {"clone": 1})
        self.assertFalse(self.copy_of(repo).exists())

    def test_clone_without_remote_counts_every_branch_unpushed(self):
        repo = self.clone("local-only")
        git(repo, "branch", "side")
        code, report = self.run_guard()
        record = self.entry(report, repo)
        self.assertEqual(code, 10)
        self.assertEqual(record["git"]["unpushed_branches"], ["main", "side"])
        self.assertTrue(record["git"]["no_durable_remote"])

    def test_remote_inside_a_cleaned_root_is_not_durable(self):
        remote = self.bare("scratch-remote.git", where=self.root)
        repo = self.clone("pushed-to-scratch", remote)
        code, report = self.run_guard()
        record = self.entry(report, repo)
        self.assertEqual(record["reasons"], ["unpushed-branches"])
        self.assertIn("not durable", record["git"]["remotes"][0]["lookup"])
        self.assertIsNone(self.entry(report, remote))  # a fresh bare repository is not due
        self.assertEqual(code, 10)

    def test_detached_head_commits_are_unpushed(self):
        repo = self.clone("detached", self.bare())
        git(repo, "checkout", "-q", "--detach")
        self.commit(repo, "detached.txt", "only on HEAD\n")
        code, report = self.run_guard()
        self.assertEqual(self.entry(report, repo)["git"]["unpushed_branches"], ["(detached HEAD)"])
        self.assertEqual(code, 10)

    def test_unreachable_remote_is_unknown_and_still_rescued(self):
        repo = self.clone("unreachable", self.bare())
        git(repo, "remote", "set-url", "origin", str(self.remotes / "missing.git"))
        code, report = self.run_guard()
        record = self.entry(report, repo)
        self.assertEqual(code, 30)
        self.assertEqual(report["status"], "incomplete")
        self.assertTrue(record["unknown"])
        self.assertEqual(record["reasons"], ["unverified-branches"])
        self.assertEqual(record["git"]["unverified_branches"], ["main"])
        self.assertEqual(record["git"]["remotes"][0]["lookup"], "ls-remote failed")
        self.assertEqual(record["rescue"]["status"], "rescued")
        self.assertEqual(report["at_risk"]["unknown"], 1)

    def test_stash_in_pushed_clone_is_at_risk(self):
        repo = self.clone("stashed", self.bare())
        (repo / "README.md").write_text("edited\n")
        git(repo, "stash", "-q")
        code, report = self.run_guard()
        record = self.entry(report, repo)
        self.assertEqual(record["reasons"], ["stashes"])
        self.assertEqual(record["git"]["stashes"], 1)
        self.assertEqual(code, 10)

    def test_dirty_worktree_is_at_risk_and_clean_worktree_is_not(self):
        main = self.clone("main-repo", where=self.outside)  # commits live outside the root
        git(main, "worktree", "add", "-q", "-b", "dirty", str(self.root / "wt-dirty"))
        git(main, "worktree", "add", "-q", "-b", "clean", str(self.root / "wt-clean"))
        (self.root / "wt-dirty" / "README.md").write_text("uncommitted\n")
        code, report = self.run_guard()
        record = self.entry(report, self.root / "wt-dirty")
        self.assertEqual((record["kind"], record["reasons"]), ("worktree", ["uncommitted-changes"]))
        self.assertIsNone(self.entry(report, self.root / "wt-clean"))
        self.assertEqual(report["inventory"]["by_kind"], {"worktree": 2})
        self.assertEqual((self.copy_of(self.root / "wt-dirty") / "README.md").read_text(), "uncommitted\n")
        self.assertEqual(code, 10)

    def test_ignored_state_counts_but_regenerable_ignored_files_do_not(self):
        keeps = self.clone("ignored-state", self.bare())
        self.commit(keeps, ".gitignore", ".temp/\n__pycache__/\n")
        git(keeps, "push", "-q", "origin", "main")
        (keeps / ".temp").mkdir()
        (keeps / ".temp" / "batch.json").write_text("{}\n")
        junk = self.clone("ignored-junk", self.bare("junk.git"))
        self.commit(junk, ".gitignore", "__pycache__/\n")
        git(junk, "push", "-q", "origin", "main")
        (junk / "__pycache__").mkdir()
        (junk / "__pycache__" / "x.pyc").write_bytes(b"\0")
        code, report = self.run_guard()
        record = self.entry(report, keeps)
        self.assertEqual(record["reasons"], ["ignored-files"])
        self.assertEqual(record["git"]["ignored_samples"], [".temp/"])
        self.assertIsNone(self.entry(report, junk))
        self.assertEqual(code, 10)

    def test_repository_filters_never_run(self):
        repo = self.clone("filtered", self.bare())
        marker = self.outside / "filter-ran"
        git(repo, "config", "filter.synthetic.clean", "sh -c 'touch \"%s\"; cat'" % marker)
        git(repo, "config", "filter.synthetic.required", "true")
        self.commit(repo, ".gitattributes", "*.txt filter=synthetic\n")
        self.commit(repo, "data.txt", "v1\n")
        git(repo, "push", "-q", "origin", "main")
        marker.unlink()
        (repo / "data.txt").write_text("v2 changed\n")
        code, report = self.run_guard()
        self.assertFalse(marker.exists())
        self.assertEqual(self.entry(report, repo)["reasons"], ["uncommitted-changes"])
        self.assertEqual(code, 10)

    def test_nested_repository_is_covered_by_its_rescued_folder(self):
        session = self.folder("session", 5)
        nested = self.clone("repo", where=session)
        code, report = self.run_guard()
        self.assertEqual(self.entry(report, session)["rescue"]["status"], "rescued")
        self.assertEqual(self.entry(report, nested)["rescue"], {"status": "covered", "by": str(session)})
        self.assertTrue((self.copy_of(nested) / ".git").is_dir())
        self.assertEqual(code, 10)

    def test_depth_limit_bounds_repository_discovery(self):
        deep = self.root / "a" / "b"
        deep.mkdir(parents=True)
        self.clone("repo", where=deep)
        code, report = self.run_guard("--max-depth", "2")
        self.assertEqual(report["inventory"]["by_kind"], {"folder": 1})
        self.assertEqual(code, 0)


class FolderAndSkipTests(GuardCase):
    def test_folder_rule_follows_the_cleaner_and_recency(self):
        due = self.folder("due", 5)
        self.folder("fresh", 0.05)
        self.folder("stale", 20)
        self.folder("only-empty-files", 5, content="")
        report_file = self.root / "report.json"
        report_file.write_text("{}\n")
        age(report_file, 6)
        code, report = self.run_guard()
        self.assertEqual(sorted(record["path"] for record in report["entries"]), [str(due), str(report_file)])
        record = self.entry(report, due)
        self.assertEqual((record["kind"], record["reasons"]), ("folder", ["files-due"]))
        self.assertEqual((record["due_next_run"], record["due_within_two_runs"]), (1, 1))
        copied = self.copy_of(due) / "notes.txt"
        self.assertEqual(copied.read_text(), "notes\n")
        self.assertEqual(int(copied.stat().st_mtime), int((due / "notes.txt").stat().st_mtime))
        self.assertEqual(self.copy_of(report_file).read_text(), "{}\n")
        self.assertEqual(self.entry(report, report_file)["kind"], "file")
        self.assertEqual(code, 10)

    def test_fresh_ctime_keeps_a_file_alive(self):
        folder = self.folder("recently-changed-metadata", 5)
        with mock.patch.object(GUARD, "file_times", REAL_FILE_TIMES):  # real ctime is "now"
            code, report = self.run_guard()
        self.assertIsNone(self.entry(report, folder))
        self.assertEqual(code, 0)

    def test_cleaner_deadlines_truncate_to_whole_days(self):
        now = time.mktime((2026, 11, 8, 21, 30, 0, 0, 0, -1))
        first, second = GUARD.next_cleaner_runs(now)
        self.assertEqual(time.localtime(first)[:6], (2026, 11, 9, 0, 0, 0))
        self.assertEqual(time.localtime(second)[:6], (2026, 11, 10, 0, 0, 0))
        settings = GUARD.Settings(roots=[], dest=str(self.dest))
        scanner = GUARD.Scanner(settings, now, lambda info: (info.stamp, info.stamp, info.stamp))
        entry = GUARD.Entry(str(self.root), "synthetic", "x", "folder", 1, str(self.dest / "x"))
        for stamp in (first - 4 * DAY, first - 4 * DAY + 1, second - 4 * DAY + 1):
            info = types.SimpleNamespace(st_size=1, st_nlink=1, st_dev=1, st_ino=stamp, st_mtime=stamp, stamp=stamp)
            scanner.account(entry, info, None, "f")
        self.assertEqual((entry.due_next, entry.due_two), (1, 2))

    def test_symlinked_entries_are_skipped_not_followed(self):
        target = self.clone("moved-work", where=self.outside)  # at risk if it were followed
        (self.root / "moved-work").symlink_to(target, target_is_directory=True)
        session = self.folder("session", 5)
        (session / "pointer").symlink_to(self.outside / "moved-work" / "README.md")
        code, report = self.run_guard()
        self.assertEqual(report["skipped"]["symlink"], 1)
        self.assertEqual([record["path"] for record in report["entries"]], [str(session)])
        self.assertFalse(os.path.lexists(self.copy_of(self.root / "moved-work")))
        self.assertTrue((self.copy_of(session) / "pointer").is_symlink())  # copied as a link only
        self.assertEqual(code, 10)

    def test_exclusions_are_skipped_counted_and_never_copied(self):
        self.folder("pytest-of-someone/pytest-1", 5)
        self.folder("com.apple.service", 5)
        session = self.folder("session", 5)
        for name in ("__pycache__", "node_modules", "scratch-cache", "com.apple.inner"):
            (session / name).mkdir()
            (session / name / "file").write_text("x\n")
        code, report = self.run_guard("--exclude", "scratch-cache")
        self.assertEqual([record["path"] for record in report["entries"]], [str(session)])
        self.assertEqual(report["skipped"]["excluded"],
                         {"__pycache__": 1, "com.apple.*": 1, "node_modules": 1, "pytest-of-*": 1, "scratch-cache": 1})
        self.assertNotIn("pytest-of-someone", json.dumps(report))
        copy = self.copy_of(session)
        self.assertEqual(sorted(os.listdir(copy)), ["com.apple.inner", "notes.txt"])
        self.assertEqual(code, 10)

    def test_unreadable_directories_are_named_and_only_eperm_is_tolerated(self):
        protected = self.folder("service", 0.1) / "inner"
        locked = self.folder("locked", 0.1) / "inner"
        protected.mkdir()
        locked.mkdir()
        real_scandir = os.scandir

        def scandir(path):
            if str(path) == str(protected):
                raise PermissionError(errno.EPERM, "Operation not permitted")
            if str(path) == str(locked):
                raise PermissionError(errno.EACCES, "Permission denied")
            return real_scandir(path)

        with mock.patch.object(GUARD.os, "scandir", scandir):
            code, report = self.run_guard()
        self.assertEqual((code, report["status"]), (30, "incomplete"))
        self.assertEqual(report["errors"], ["walk_errors:1"])
        self.assertEqual(report["unreadable"], [
            {"path": str(self.root / "locked"), "unreadable_dirs": 1, "protected_dirs": 0},
            {"path": str(self.root / "service"), "unreadable_dirs": 0, "protected_dirs": 1}])
        shutil.rmtree(self.root / "locked")
        with mock.patch.object(GUARD.os, "scandir", scandir):
            code, report = self.run_guard()
        self.assertEqual((code, report["skipped"]["protected_dirs"]), (0, 1))

    def test_bad_exclusions_are_rejected(self):
        for pattern in ("a/b", ".*", "+ keep"):
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                GUARD.parse_args(["--exclude", pattern])


class RescueLimitTests(GuardCase):
    def test_entry_cap_skips_the_entry_loudly(self):
        big = self.folder("big", 5, content="x" * 2000)
        code, report = self.run_guard("--entry-cap-gb", "0.000001")
        self.assertEqual(self.entry(report, big)["rescue"], {"status": "skipped", "reason": "entry-cap"})
        self.assertEqual((code, report["status"]), (20, "unrescued"))
        self.assertFalse(self.copy_of(big).exists())

    def test_run_cap_stops_after_the_budget(self):
        first = self.folder("a-first", 5, content="x" * 600)
        second = self.folder("b-second", 5, content="y" * 600)
        code, report = self.run_guard("--run-cap-gb", "0.000001")
        self.assertEqual(self.entry(report, first)["rescue"]["status"], "rescued")
        self.assertEqual(self.entry(report, second)["rescue"], {"status": "skipped", "reason": "run-cap"})
        self.assertEqual(code, 20)

    def test_free_disk_floor_skips_copies(self):
        folder = self.folder("needs-space", 5)
        code, report = self.run_guard("--free-floor-gb", "1000000000")
        rescue = self.entry(report, folder)["rescue"]
        self.assertEqual((rescue["status"], rescue["reason"]), ("skipped", "free-disk-floor"))
        self.assertEqual(code, 20)
        self.assertFalse(self.dest.exists())

    def test_failed_copy_is_reported_as_not_rescued(self):
        folder = self.folder("session", 5)
        code, report = self.run_guard("--rsync", shutil.which("false") or "/usr/bin/false")
        rescue = self.entry(report, folder)["rescue"]
        self.assertEqual((rescue["status"], rescue["reason"], rescue["rsync_exit"]), ("failed", "rsync", 1))
        self.assertEqual((code, report["status"]), (20, "unrescued"))

    def test_destination_or_state_under_a_cleaned_root_is_refused(self):
        self.folder("session", 5)
        code, report = self.run_guard("--dest", str(self.root / "backups"))
        self.assertEqual((code, report), (2, None))
        self.assertFalse((self.root / "backups").exists())
        self.assertFalse(self.state.exists())
        with mock.patch.object(GUARD, "system_cleaned_roots", lambda: [str(self.base)]):
            self.assertEqual(self.run_guard()[0], 2)
        argv = ["--root", str(self.root), "--dest", str(self.dest), "--state-dir", str(self.root / "state")]
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(GUARD.main(argv), 2)
        self.assertFalse(self.dest.exists())

    def test_insecure_state_directory_is_refused(self):
        self.state.mkdir(mode=0o755)
        os.chmod(self.state, 0o755)
        self.assertEqual(self.run_guard()[0], 2)

    def test_dry_run_reports_and_copies_nothing(self):
        repo = self.clone("unpushed", self.bare())
        self.commit(repo, "work.txt", "w\n")
        code, report = self.run_guard("--dry-run")
        self.assertEqual(code, 10)
        self.assertTrue(report["dry_run"])
        self.assertEqual(self.entry(report, repo)["rescue"], {"status": "would-copy"})
        self.assertGreater(report["at_risk"]["transfer_bytes"], 0)
        self.assertFalse(self.dest.exists())
        self.assertEqual(json.loads((self.state / "latest.json").read_text())["dry_run"], True)

    def test_second_run_is_idempotent_and_third_picks_up_changes(self):
        repo = self.clone("unpushed", self.bare())
        self.commit(repo, "work.txt", "w\n")
        self.assertEqual(self.run_guard()[0], 10)
        copy = snapshot(self.dest, metadata=False)
        code, report = self.run_guard()
        self.assertEqual((code, report["status"]), (0, "ok"))
        self.assertEqual(self.entry(report, repo)["rescue"]["status"], "up-to-date")
        self.assertEqual(report["at_risk"]["transfer_bytes"], 0)
        self.assertEqual(snapshot(self.dest, metadata=False), copy)
        (repo / "work.txt").write_text("changed and longer\n")
        code, report = self.run_guard()
        self.assertEqual(code, 10)
        self.assertEqual((self.copy_of(repo) / "work.txt").read_text(), "changed and longer\n")
        self.assertEqual(len(json.loads((self.state / "history.json").read_text())), 3)

    def test_sources_are_byte_and_stat_identical_after_a_rescue(self):
        remote = self.bare()
        unpushed = self.clone("unpushed", remote)
        self.commit(unpushed, "work.txt", "w\n")
        stashed = self.clone("stashed", self.bare("other.git"))
        (stashed / "README.md").write_text("edited\n")
        git(stashed, "stash", "-q")
        (stashed / "README.md").write_text("dirty too\n")
        main = self.clone("main-repo", where=self.outside)
        git(main, "worktree", "add", "-q", "-b", "wt", str(self.root / "wt"))
        (self.root / "wt" / "new.txt").write_text("untracked\n")
        session = self.folder("session", 5)
        (session / "__pycache__").mkdir()
        (session / "__pycache__" / "x.pyc").write_bytes(b"\0\1")
        os.link(session / "notes.txt", session / "hardlink.txt")
        (self.root / "link").symlink_to(main, target_is_directory=True)
        before = snapshot(self.root, self.outside, self.remotes)
        # Hashing the files for the snapshot may move their atime (Linux relatime);
        # this test is about the source staying unchanged, so judge age by mtime.
        with mock.patch.object(GUARD, "file_times", lambda info: (info.st_mtime,) * 3):
            code, report = self.run_guard()
        self.assertEqual(code, 10)
        self.assertEqual(report["at_risk"]["entries"], 4)
        self.assertEqual(snapshot(self.root, self.outside, self.remotes), before)


class ReportTests(GuardCase):
    def test_empty_root_is_ok_and_state_is_private_and_bounded(self):
        for _ in range(3):
            code, report = self.run_guard("--history-limit", "2")
        self.assertEqual((code, report["status"], report["phase"]), (0, "ok", "complete"))
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o700)
        self.assertEqual(len(json.loads((self.state / "history.json").read_text())), 2)
        self.assertIn("status: ok (exit 0)", (self.state / "summary.txt").read_text())
        self.assertEqual(json.loads((self.state / "latest.json").read_text())["observed_at"], report["observed_at"])

    def test_summary_is_printed_without_json(self):
        self.folder("session", 5)
        argv = ["--root", "synthetic=" + str(self.root), "--dest", str(self.dest), "--free-floor-gb", "0", "--dry-run"]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(GUARD.main(argv), 10)
        text = output.getvalue()
        self.assertIn("(dry run)", text)
        self.assertIn("at risk: 1 (folder 1)", text)
        self.assertIn("would-copy  folder", text)

    def test_notification_is_local_and_only_when_needed(self):
        calls = []
        with mock.patch.object(GUARD, "run_command", lambda command, **kw: calls.append(command) or (0, b"", b"")):
            quiet = {"exit_code": 0}
            self.assertEqual(GUARD.notify(quiet), {"status": "not_needed"})
            loud = {"exit_code": 10, "status": "rescued", "dry_run": False,
                    "at_risk": {"entries": 2, "unknown": 0, "outcomes": {"rescued": 2}}}
            self.assertEqual(GUARD.notify(loud), {"status": "requested"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "/usr/bin/osascript")
        self.assertIn('with title "XMechBay tmp-state guard"', calls[0][2])
        self.assertEqual(calls[0][3], "rescued: 2 at risk, 2 copied, 0 not rescued, 0 unknown")

    def test_remote_urls_are_redacted_and_classified(self):
        self.assertEqual(GUARD.redact_url("https://user:secret@example.invalid/r.git"), "https://***@example.invalid/r.git")
        self.assertIsNone(GUARD.local_remote_path("git@example.invalid:org/repo.git", "/base"))
        self.assertIsNone(GUARD.local_remote_path("ext::sh -c true", "/base"))
        self.assertEqual(GUARD.local_remote_path("../remote.git", "/base/repo"), os.path.realpath("/base/remote.git"))
        self.assertEqual(GUARD.local_remote_path("file:///base/r.git", "/x"), os.path.realpath("/base/r.git"))


class ReviewRegressionTests(GuardCase):
    """One test per finding of the PR #54 review (issues #55-#66)."""

    def test_fresh_atime_alone_keeps_a_file_alive(self):  # 55
        folder = self.root / "read-recently"
        folder.mkdir()
        target = folder / "notes.txt"
        target.write_text("still read\n")
        os.utime(target, (time.time(), time.time() - 5 * DAY))
        code, report = self.run_guard()
        self.assertEqual((code, report["entries"]), (0, []))

    def test_detached_commit_in_worktree_of_durable_repo_is_reported(self):  # 56
        main = self.clone("main-repo", self.bare(), where=self.outside)
        git(main, "worktree", "add", "-q", "--detach", str(self.root / "review"))
        self.commit(self.root / "review", "review.txt", "only on a detached HEAD\n")
        code, report = self.run_guard()
        record = self.entry(report, self.root / "review")
        self.assertEqual(record["reasons"], ["detached-commits"])
        self.assertEqual(record["git"]["detached_head"], git(self.root / "review", "rev-parse", "HEAD").decode().strip())
        self.assertEqual(code, 10)

    def test_clone_counts_its_worktrees_detached_heads(self):  # 56
        repo = self.clone("clone", self.bare())
        git(repo, "worktree", "add", "-q", "--detach", str(self.outside / "elsewhere"))
        self.commit(self.outside / "elsewhere", "w.txt", "w\n")
        code, report = self.run_guard()
        record = self.entry(report, repo)
        self.assertEqual(record["git"]["unpushed_branches"], ["(detached HEAD of worktree elsewhere)"])
        self.assertEqual(code, 10)

    def test_unpushed_submodule_commit_is_copied_with_its_superproject(self):  # 57
        sub_remote = self.bare("sub.git")
        self.clone("seed", sub_remote, where=self.outside)
        sup = self.clone("super", self.bare("super.git"))
        git(sup, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub_remote), "sub")
        git(sup, "commit", "-q", "-m", "add submodule")
        self.commit(sup / "sub", "sub.txt", "unpushed in the submodule\n")
        git(sup, "add", "sub")
        git(sup, "commit", "-q", "-m", "record submodule")
        git(sup, "push", "-q", "origin", "main")
        lost = git(sup / "sub", "rev-parse", "HEAD").decode().strip()
        code, report = self.run_guard()
        record = self.entry(report, sup / "sub")
        self.assertEqual(record["reasons"], ["unpushed-branches"])
        self.assertTrue(record["git"]["git_dir_in_cleaned_root"])
        holder = self.entry(report, sup)
        self.assertEqual(holder["reasons"], ["git-dir-of-at-risk-checkout"])
        self.assertEqual(holder["rescue"]["status"], "rescued")
        self.assertEqual(record["rescue"], {"status": "covered", "by": str(sup)})
        modules = self.copy_of(sup) / ".git" / "modules" / "sub"
        subprocess.run(["git", "--git-dir", str(modules), "cat-file", "-e", lost], check=True)
        self.assertEqual(code, 10)

    def test_per_user_temp_root_uses_the_dirhelper_model(self):  # 58
        now = time.mktime((2026, 11, 8, 21, 30, 0, 0, 0, -1))
        first, second = GUARD.next_cleaner_runs(now, 3, 35)
        self.assertEqual(time.localtime(first)[:5], (2026, 11, 9, 3, 35))
        self.assertEqual(time.localtime(second)[:5], (2026, 11, 10, 3, 35))
        early = time.mktime((2026, 11, 8, 1, 0, 0, 0, 0, -1))
        self.assertEqual(time.localtime(GUARD.next_cleaner_runs(early, 3, 35)[0])[:5], (2026, 11, 8, 3, 35))
        other = self.base / "user-temp"
        for root in (self.root, other):
            folder = root / "work"
            folder.mkdir(parents=True)
            (folder / "f.txt").write_text("written days ago, read today\n")
            os.utime(folder / "f.txt", (time.time(), time.time() - 5 * DAY))
        code, report = self.run_guard("--root", "darwin-user-temp=" + str(other))
        self.assertEqual([record["path"] for record in report["entries"]], [str(other / "work")])
        self.assertEqual([item["cleaner"] for item in report["cleaners"]], ["tmp_cleaner", "dirhelper"])
        self.assertEqual(code, 10)

    def test_untracked_files_are_seen_despite_config(self):  # 59
        repo = self.clone("hides-untracked", self.bare())
        git(repo, "config", "status.showUntrackedFiles", "no")
        (repo / "notes.md").write_text("new work\n")
        code, report = self.run_guard()
        self.assertEqual(self.entry(report, repo)["reasons"], ["uncommitted-changes"])
        self.assertEqual(code, 10)

    def test_copy_follows_packed_refs_and_keeps_retired_tips(self):  # 60
        repo = self.clone("packs-refs")
        git(repo, "branch", "side")
        self.assertEqual(self.run_guard()[0], 10)
        old = git(repo, "rev-parse", "main").decode().strip()
        self.commit(repo, "c.txt", "C\n")
        new = git(repo, "rev-parse", "main").decode().strip()
        git(repo, "branch", "-q", "-D", "side")
        git(repo, "pack-refs", "--all")
        before = snapshot(self.root)
        code, report = self.run_guard()
        self.assertEqual(snapshot(self.root), before)
        copy = self.copy_of(repo)
        self.assertEqual(git(copy, "rev-parse", "main").decode().strip(), new)
        retired = git(copy, "for-each-ref", "--format=%(refname) %(objectname)", "refs/tmp-state-guard").decode()
        self.assertEqual(sorted(line.split("/")[-1] for line in retired.splitlines()), ["main " + old, "side " + old])
        self.assertEqual(self.entry(report, repo)["rescue"]["stale_refs_retired"], 2)

    def fake_rsync(self, message):
        script = self.base / "fake-rsync"
        script.write_text('#!/bin/sh\nfor a; do last=$a; done\nmkdir -p "$last"\necho "%s" >&2\n'
                          'echo "rsync error: some files could not be transferred (code 23)" >&2\nexit 23\n' % message)
        script.chmod(0o755)
        return str(script)

    def test_files_vanishing_mid_copy_count_as_rescued(self):  # 61
        folder = self.folder("live", 5)
        fake = self.fake_rsync("rsync: stat: /live/gone.tmp: No such file or directory")
        code, report = self.run_guard("--rsync", fake)
        rescue = self.entry(report, folder)["rescue"]
        self.assertEqual((rescue["status"], rescue["vanished_during_copy"]), ("rescued", True))
        self.assertEqual(code, 10)
        fake = self.fake_rsync("rsync: open: /live/secret: Permission denied")
        code, report = self.run_guard("--rsync", fake)
        self.assertEqual((self.entry(report, folder)["rescue"]["status"], code), ("failed", 20))

    def test_directory_vanishing_during_the_walk_is_not_an_error(self):  # 62
        gone = self.folder("busy", 0.1) / "build-tmp"
        gone.mkdir()
        real_scandir = os.scandir

        def scandir(path):
            if str(path) == str(gone):
                raise FileNotFoundError(errno.ENOENT, "No such file or directory")
            return real_scandir(path)

        with mock.patch.object(GUARD.os, "scandir", scandir):
            code, report = self.run_guard()
        self.assertEqual((code, report["errors"], report["unreadable"]), (0, [], []))

    def test_hard_links_count_in_every_entry_that_copies_them(self):  # 63
        first = self.folder("a", 5, content="x" * 1000)
        second = self.root / "b"
        second.mkdir()
        os.link(first / "notes.txt", second / "notes.txt")
        code, report = self.run_guard()
        self.assertEqual([self.entry(report, path)["bytes"] for path in (first, second)], [1000, 1000])

    def test_free_disk_floor_does_not_block_up_to_date_copies(self):  # 64
        repo = self.clone("unpushed")
        self.assertEqual(self.run_guard()[0], 10)
        code, report = self.run_guard("--free-floor-gb", "1000000000")
        self.assertEqual((code, self.entry(report, repo)["rescue"]["status"]), (0, "up-to-date"))

    def test_children_are_stopped_on_timeout_and_on_request(self):  # 65
        started = time.monotonic()
        code, _, err = GUARD.run_command(["sh", "-c", "sleep 30 & sleep 30"], timeout=0.5, own_session=True)
        self.assertEqual((code, err), (None, b"timed out"))
        self.assertLess(time.monotonic() - started, 10)
        import threading
        result = []
        worker = threading.Thread(target=lambda: result.append(GUARD.run_command(["sleep", "30"])))
        worker.start()
        deadline = time.monotonic() + 5
        while not GUARD.RUNNING and time.monotonic() < deadline:
            time.sleep(0.01)
        GUARD.stop_children()
        worker.join(10)
        self.assertFalse(worker.is_alive())
        self.assertNotEqual(result[0][0], 0)

    def test_exclusions_matching_git_internals_are_rejected(self):  # 66
        for pattern in ("logs", "*.pack", "objects", "[0-9a-f][0-9a-f]"):
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                GUARD.parse_args(["--exclude", pattern])
        self.assertEqual(GUARD.parse_args(["--exclude", "big-data"]).exclude, ["big-data"])


if __name__ == "__main__":
    unittest.main()
