#!/usr/bin/env python3
"""Copy agent work out of macOS-cleaned temporary directories before it is deleted.

Purpose: nightly safety net for session state left in /private/tmp and the
per-user DARWIN_USER_TEMP_DIR, which macOS deletes once files are a few days
old (/usr/libexec/tmp_cleaner at 00:00 local, four days; dirhelper at 03:35
local and at boot, three days).
tmp-state-guard PR, 2026-11-08.
Inputs: roots (default /private/tmp and `getconf DARWIN_USER_TEMP_DIR`),
exclusion globs and size caps; read-only git commands in each repository and
`git ls-remote` against each remote URL (the only network use).
Outputs: rsync copies under <dest>/<root-label>/<path relative to root>/
(default ~/agent-state-backups/auto); a short summary (or --json) on stdout;
with --state-dir, latest.json, a bounded history.json and summary.txt there;
with --notify, a best-effort local macOS notification.
It never modifies, pushes, fetches into, stashes or deletes anything in a
source. Python 3.9+, standard library only; see docs/tmp-state-guard.md.
"""

import argparse
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import errno
import fcntl
import fnmatch
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlparse


SCHEMA_VERSION = 1
TOOL = "tmp-state-guard"
NOTIFICATION_TITLE = "XMechBay tmp-state guard"
EXIT_REFUSED = 2
STATUS_EXIT = {"ok": 0, "rescued": 10, "unrescued": 20, "incomplete": 30}
GB = 1000 ** 3
DAY = 86400
# Cleaner models. tmp_cleaner (/private/tmp, 00:00) runs `find -atime +3 -mtime +3
# -ctime +3`; find truncates ages to whole days, so all three timestamps must be at
# least four full days old. dirhelper (com.apple.bsd.dirhelper) cleans /var/folders
# at 03:35 and at boot, deleting files older than CLEAN_FILES_OLDER_THAN_DAYS=3.
# Which timestamps dirhelper compares is unconfirmed, so the oldest one is assumed
# to decide, which errs towards copying.
CLEANERS = {
    "tmp_cleaner": {"hour": 0, "minute": 0, "age": 4 * DAY, "oldest": False,
                    "rule": "00:00 local; deleted once atime, mtime and ctime are all at least 4 days old"},
    "dirhelper": {"hour": 3, "minute": 35, "age": 3 * DAY, "oldest": True,
                  "rule": "03:35 local and at boot; assumed deleted once any of atime, mtime, ctime is 3 days old"},
}
# Names inside a .git directory; an exclusion matching one would corrupt a copied clone.
GIT_INTERNAL_NAMES = ("objects", "refs", "logs", "hooks", "info", "pack", "packed-refs", "HEAD",
                      "ORIG_HEAD", "FETCH_HEAD", "index", "config", "modules", "worktrees", "lfs",
                      "description", "shallow", "commondir", "gitdir", "x.pack", "x.idx", "x.rev", "x.keep",
                      "COMMIT_EDITMSG", "0a", "1234567890abcdef1234567890abcdef12345678")
DEFAULT_DEST = "~/agent-state-backups/auto"
# Regenerable content, matched against file and directory names at any depth:
# skipped by the scan (reported only as counts) and excluded from every copy.
DEFAULT_EXCLUDES = (
    "pytest-of-*", "pytest-cache-files-*", "__pycache__", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", "node_modules", ".venv", "uv-cache", ".uv-cache", "uv-cache-*", ".uv_cache",
    "node-compile-cache",
)
# Matched only against top-level names under a root: operating-system service
# directories in DARWIN_USER_TEMP_DIR.
DEFAULT_TOP_EXCLUDES = ("com.apple.*",)
# Ignored files with these names do not make a repository "at risk" (they are not
# excluded from copies; only DEFAULT_EXCLUDES and --exclude are).
REGENERABLE_IGNORED = (
    "*.pyc", "*.pyo", ".DS_Store", "*.egg-info", ".eggs", ".tox", ".nox", ".coverage",
    ".coverage.*", "htmlcov", ".ipynb_checkpoints", ".hypothesis", "build", "dist", ".next",
)
LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")
NOTIFICATION_SCRIPT = '''on run argv
display notification (item 1 of argv) with title "XMechBay tmp-state guard"
end run
'''
MAX_LISTED = 20
RSYNC_TIMEOUT = 6 * 3600
STALE_REFS = "tmp-state-guard"  # refs/<this>/stale/<stamp>/... in a copy, never in a source


class Refusal(Exception):
    """A configuration the guard will not run with (exit 2)."""


# ---------------------------------------------------------------- settings

@dataclass
class Settings:
    roots: List[Tuple[str, str]]
    dest: str
    state_dir: Optional[str] = None
    dry_run: bool = False
    notify: bool = False
    max_depth: int = 7
    recent_days: float = 14.0
    entry_cap: int = 5 * GB
    run_cap: int = 30 * GB
    free_floor: int = 50 * GB
    excludes: Tuple[str, ...] = DEFAULT_EXCLUDES
    top_excludes: Tuple[str, ...] = DEFAULT_TOP_EXCLUDES
    history_limit: int = 90
    git: str = "git"
    rsync: str = "rsync"
    git_timeout: float = 300.0
    remote_timeout: float = 60.0
    errors: List[str] = field(default_factory=list)


def file_times(st: os.stat_result) -> Tuple[float, float, float]:
    """(atime, mtime, ctime) as tmp_cleaner sees them; tests replace this hook."""
    return st.st_atime, st.st_mtime, st.st_ctime


def current_time() -> float:
    return time.time()


def darwin_user_temp_dir() -> Optional[str]:
    try:
        result = subprocess.run(["/usr/bin/getconf", "DARWIN_USER_TEMP_DIR"], capture_output=True,
                                timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.decode("utf-8", "replace").strip().rstrip("/") or None


def system_cleaned_roots() -> List[str]:
    """Locations the operating system cleans; backups and state must never live there."""
    roots = ["/tmp", "/private/tmp", "/var/folders", "/private/var/folders"]
    user_temp = darwin_user_temp_dir() if sys.platform == "darwin" else None
    if user_temp:
        roots.append(user_temp)
    tmpdir = os.environ.get("TMPDIR")
    if tmpdir:
        roots.append(tmpdir)
    return sorted({os.path.realpath(path) for path in roots})


def cleaner_for(label: str) -> str:
    return "dirhelper" if label == "darwin-user-temp" else "tmp_cleaner"


def within(path: str, root: str) -> bool:
    root = root.rstrip("/") or "/"
    return path == root or path.startswith(root + "/") or root == "/"


def label_for(path: str, user_temp: Optional[str]) -> str:
    if path in (os.path.realpath("/private/tmp"), os.path.realpath("/tmp")):
        return "tmp"
    if user_temp and path == os.path.realpath(user_temp):
        return "darwin-user-temp"
    stem = re.sub(r"[^a-z0-9_-]+", "-", os.path.basename(path).lower()).strip("-")[:24] or "root"
    return stem + "-" + hashlib.sha256(path.encode()).hexdigest()[:8]


def parse_root(value: str, user_temp: Optional[str]) -> Tuple[str, str]:
    label = None
    if "=" in value:
        head, tail = value.split("=", 1)
        if LABEL_RE.match(head) and tail[:1] in ("/", "~"):
            label, value = head, tail
    path = os.path.realpath(os.path.expanduser(value))
    return (label or label_for(path, user_temp)), path


def check_refusal(settings: Settings, cleaned: List[str]) -> None:
    """Refuse destinations or state that a cleaner would delete, or that overlap a root."""
    roots = [path for _, path in settings.roots]
    labels = [label for label, _ in settings.roots]
    if len(set(labels)) != len(labels):
        raise Refusal("root labels must be unique")
    for index, first in enumerate(roots):
        for second in roots[index + 1:]:
            if within(first, second) or within(second, first):
                raise Refusal("roots must not overlap")
    protected = sorted(set(cleaned) | set(roots))
    named = [("destination", settings.dest)]
    if settings.state_dir:
        named.append(("state directory", settings.state_dir))
    for what, path in named:
        real = os.path.realpath(path)
        for root in protected:
            if within(real, root):
                raise Refusal("%s lies under a cleaned or scanned root (%s)" % (what, root))
        for root in roots:
            if within(root, real):
                raise Refusal("a scanned root lies inside the %s" % what)


# ---------------------------------------------------------------- commands

def git_environment() -> Dict[str, str]:
    env = dict(os.environ)
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_NAMESPACE",
                "GIT_ASKPASS", "SSH_ASKPASS"):
        env.pop(key, None)
    env.update({
        # Never write the index or take optional locks, also in submodules.
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never", "SSH_ASKPASS_REQUIRE": "never",
        "GIT_PAGER": "cat", "PAGER": "cat", "LC_ALL": "C",
    })
    if "GIT_SSH_COMMAND" not in env and "GIT_SSH" not in env:
        env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes -o ConnectTimeout=15"
    return env


RUNNING: List[Tuple[subprocess.Popen, bool]] = []


def stop_child(process: subprocess.Popen, own_session: bool) -> None:
    try:
        if own_session:
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except OSError:
        pass


def stop_children() -> None:
    for process, own_session in list(RUNNING):
        stop_child(process, own_session)


def run_command(command: List[str], env: Optional[Dict[str, str]] = None, cwd: Optional[str] = None,
                timeout: Optional[float] = None, data: Optional[bytes] = None,
                own_session: bool = False) -> Tuple[Optional[int], bytes, bytes]:
    """Run a child; (None, ...) on timeout or OSError.

    Children stay in the job's process group, so launchd stops them with the job.
    own_session=True (git ls-remote) detaches from any terminal so ssh or a
    credential helper cannot prompt; a timeout or a stop kills its whole group.
    """
    try:
        process = subprocess.Popen(command, stdin=subprocess.PIPE if data is not None else subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=cwd,
                                   start_new_session=own_session)
    except OSError as error:
        return None, b"", str(error).encode()
    RUNNING.append((process, own_session))
    try:
        out, err = process.communicate(data, timeout=timeout)
    except subprocess.TimeoutExpired:
        stop_child(process, own_session)
        process.communicate()
        return None, b"", b"timed out"
    finally:
        RUNNING.remove((process, own_session))
    return process.returncode, out, err


def terminate(signum, frame) -> None:
    """SIGTERM (launchd stop, logout) or SIGINT: stop children, then exit."""
    stop_children()
    raise SystemExit(128 + signum)


def redact_url(url: str) -> str:
    return re.sub(r"(://)[^/@\s]*@", r"\1***@", url)


def local_remote_path(url: str, base: str) -> Optional[str]:
    """Filesystem path of a local remote URL, or None for a network URL."""
    if url.startswith("file://"):
        return os.path.realpath(unquote(urlparse(url).path))
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", url):
        return None
    colon, slash = url.find(":"), url.find("/")
    if colon > 0 and (slash < 0 or colon < slash):
        return None  # scp-like ssh syntax, or a transport such as ext::
    return os.path.realpath(url if os.path.isabs(url) else os.path.join(base, url))


# ---------------------------------------------------------------- scan

@dataclass
class Entry:
    path: str
    root_label: str
    rel: str
    kind: str  # clone | worktree | folder | file
    depth: int
    dest: str
    parent: Optional["Entry"] = None
    children: List["Entry"] = field(default_factory=list)
    own_files: int = 0
    own_bytes: int = 0
    due_next: int = 0
    due_two: int = 0
    newest_change: float = 0.0
    transfer_files: int = 0
    transfer_bytes: int = 0
    walk_errors: int = 0
    protected: int = 0
    reasons: List[str] = field(default_factory=list)
    unknown: bool = False
    git: Dict[str, object] = field(default_factory=dict)
    rescue: Dict[str, object] = field(default_factory=dict)

    def subtree(self):
        yield self
        for child in self.children:
            yield from child.subtree()

    def total(self, attribute: str) -> int:
        return sum(getattr(node, attribute) for node in self.subtree())

    def ancestors(self):
        node = self.parent
        while node is not None:
            yield node
            node = node.parent


def matching_glob(name: str, patterns) -> Optional[str]:
    for pattern in patterns:
        if fnmatch.fnmatchcase(name, pattern):
            return pattern
    return None


def git_kind(path: str) -> Optional[str]:
    try:
        info = os.lstat(os.path.join(path, ".git"))
    except OSError:
        return None
    # A .git directory holds the commits (a clone); a .git file or link points
    # at commits stored elsewhere (a worktree, submodule or separate git dir).
    return "clone" if stat.S_ISDIR(info.st_mode) else "worktree"


def isdir_nofollow(path: str) -> bool:
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


class Scanner:
    def __init__(self, settings: Settings, now: float, times: Callable):
        self.settings = settings
        self.now = now
        self.times = times
        self.use_cleaner("tmp_cleaner")
        self.skipped = {"symlink": 0, "foreign_owner": 0, "other_filesystem": 0, "special": 0}
        self.excluded: Dict[str, int] = {}
        self.hardlinks = set()
        self.uid = os.getuid()

    def use_cleaner(self, name: str) -> None:
        model = CLEANERS[name]
        runs = next_cleaner_runs(self.now, model["hour"], model["minute"])
        self.deadlines = tuple(run - model["age"] for run in runs)
        self.oldest = model["oldest"]

    def exclude(self, name: str, top: bool = False) -> bool:
        pattern = matching_glob(name, self.settings.excludes + (self.settings.top_excludes if top else ()))
        if pattern is not None:
            self.excluded[pattern] = self.excluded.get(pattern, 0) + 1
            return True
        return False

    def scan_root(self, label: str, root: str) -> List[Entry]:
        entries: List[Entry] = []
        try:
            root_info = os.stat(root)
            if not stat.S_ISDIR(root_info.st_mode):
                raise NotADirectoryError(root)
            names = sorted(os.listdir(root))
        except OSError:
            self.settings.errors.append("root_unreadable:" + label)
            return entries
        dest_root = os.path.join(self.settings.dest, label)
        self.use_cleaner(cleaner_for(label))
        for name in names:
            self.hardlinks = set()  # each entry is copied by its own rsync call
            path = os.path.join(root, name)
            try:
                info = os.lstat(path)
            except FileNotFoundError:
                continue
            except OSError:
                self.settings.errors.append("entry_unreadable:" + label)
                continue
            if stat.S_ISLNK(info.st_mode):
                self.skipped["symlink"] += 1
                continue
            if self.exclude(name, top=True):
                continue
            if info.st_uid != self.uid:
                self.skipped["foreign_owner"] += 1
                continue
            if info.st_dev != root_info.st_dev:
                self.skipped["other_filesystem"] += 1
                continue
            dest = os.path.join(dest_root, name)
            if stat.S_ISREG(info.st_mode):
                entry = Entry(path, label, name, "file", 1, dest)
                self.account(entry, info, dest_root if isdir_nofollow(dest_root) else None, name)
            elif stat.S_ISDIR(info.st_mode):
                entry = Entry(path, label, name, git_kind(path) or "folder", 1, dest)
                self.walk(entry, root_info.st_dev, root)
            else:
                self.skipped["special"] += 1
                continue
            entries.append(entry)
        return entries

    @staticmethod
    def unreadable(owner: Entry, error: OSError) -> None:
        # EPERM is macOS privacy or sandbox protection (TemporaryItems and service
        # directories in DARWIN_USER_TEMP_DIR): nothing running as this user can
        # read it, so it is reported but does not make the observation incomplete.
        # ENOENT/ENOTDIR: removed by a live session during the walk; nothing to lose.
        if error.errno in (errno.ENOENT, errno.ENOTDIR):
            return
        if error.errno == errno.EPERM:
            owner.protected += 1
        else:
            owner.walk_errors += 1

    def walk(self, top: Entry, device: int, root: str) -> None:
        stack = [(top.path, top, top.depth, top.dest if isdir_nofollow(top.dest) else None)]
        while stack:
            directory, owner, depth, dest_dir = stack.pop()
            try:
                with os.scandir(directory) as iterator:
                    items = list(iterator)
            except OSError as error:
                self.unreadable(owner, error)
                continue
            for item in items:
                try:
                    info = item.stat(follow_symlinks=False)
                except OSError as error:
                    self.unreadable(owner, error)
                    continue
                if stat.S_ISLNK(info.st_mode):
                    continue  # rsync copies the link itself; its target is never read
                if self.exclude(item.name):
                    continue
                if stat.S_ISDIR(info.st_mode):
                    if info.st_dev != device:
                        self.skipped["other_filesystem"] += 1
                        continue
                    child_dest = os.path.join(dest_dir, item.name) if dest_dir else None
                    if child_dest and not isdir_nofollow(child_dest):
                        child_dest = None
                    kind = git_kind(item.path) if depth < self.settings.max_depth and item.name != ".git" else None
                    if kind:
                        node = Entry(item.path, top.root_label, os.path.relpath(item.path, root), kind,
                                     depth + 1, os.path.join(self.settings.dest, top.root_label,
                                                             os.path.relpath(item.path, root)), parent=owner)
                        owner.children.append(node)
                        stack.append((item.path, node, depth + 1, child_dest))
                    else:
                        stack.append((item.path, owner, depth + 1, child_dest))
                elif stat.S_ISREG(info.st_mode):
                    self.account(owner, info, dest_dir, item.name)

    def account(self, owner: Entry, info: os.stat_result, dest_dir: Optional[str], name: str) -> None:
        size = info.st_size
        if info.st_nlink > 1:
            key = (info.st_dev, info.st_ino)
            if key in self.hardlinks:
                size = 0
            self.hardlinks.add(key)
        owner.own_files += 1
        owner.own_bytes += size
        if info.st_size > 0:  # an empty file holds no work to lose
            stamps = self.times(info)
            deciding = min(stamps) if self.oldest else max(stamps)
            if deciding <= self.deadlines[0]:
                owner.due_next += 1
            if deciding <= self.deadlines[1]:
                owner.due_two += 1
            owner.newest_change = max(owner.newest_change, stamps[1])
        copied = None
        if dest_dir:
            try:
                copied = os.lstat(os.path.join(dest_dir, name))
            except OSError:
                copied = None
        if (copied is None or not stat.S_ISREG(copied.st_mode) or copied.st_size != info.st_size
                or int(copied.st_mtime) != int(info.st_mtime)):
            owner.transfer_files += 1
            owner.transfer_bytes += size


def next_cleaner_runs(now: float, hour: int = 0, minute: int = 0) -> Tuple[float, float]:
    """The next two local HH:MM times after now (mktime normalises day overflow and DST)."""
    local = time.localtime(now)

    def at(offset: int) -> float:
        return time.mktime((local.tm_year, local.tm_mon, local.tm_mday + offset, hour, minute, 0, 0, 0, -1))

    offset = 0 if at(0) > now else 1
    return at(offset), at(offset + 1)


# ---------------------------------------------------------------- git

class GitInspector:
    def __init__(self, settings: Settings, cleaned: List[str]):
        self.settings = settings
        self.cleaned = cleaned
        self.env = git_environment()
        self.remote_cache: Dict[str, Tuple[Optional[set], Optional[str]]] = {}
        self.timeouts = 0
        self.regenerable = tuple(settings.excludes) + REGENERABLE_IGNORED
        self.clone_gitdirs: set = set()  # .git directories of clones found in this scan

    def git(self, path: str, *args: str, data: Optional[bytes] = None, config: Tuple[str, ...] = ()):
        env = dict(self.env, GIT_CEILING_DIRECTORIES=os.path.dirname(path))
        command = [self.settings.git, "-c", "core.fsmonitor=false"] + list(config) + ["-C", path] + list(args)
        return run_command(command, env=env, timeout=self.settings.git_timeout, data=data)

    def filter_overrides(self, path: str) -> Tuple[str, ...]:
        """Disable every configured clean/process filter (git-lfs, for one).

        `git status` runs a clean filter on files whose stat data changed, and
        git-lfs's clean filter writes into .git/lfs. With filters off, such a
        file reads as modified, which errs towards rescuing it.
        """
        code, out, _ = self.git(path, "config", "--name-only", "--get-regexp", r"^filter\.")
        drivers = set()
        for name in out.decode("utf-8", "replace").split() if code == 0 else []:
            if name.startswith("filter.") and name.count(".") >= 2:
                drivers.add(name[len("filter."):].rsplit(".", 1)[0])
        overrides: List[str] = []
        for driver in sorted(drivers):
            overrides += ["-c", "filter.%s.clean=" % driver, "-c", "filter.%s.process=" % driver,
                          "-c", "filter.%s.required=false" % driver]
        return tuple(overrides)

    def remote_refs(self, url: str) -> Tuple[Optional[set], Optional[str]]:
        """Commit ids advertised by a remote, cached per URL; (None, error) when unknown."""
        if url not in self.remote_cache and self.timeouts >= 3:
            return None, "ls-remote skipped after repeated timeouts"
        if url not in self.remote_cache:
            # Run outside the repository so repository config cannot redirect the transport.
            code, out, _ = run_command([self.settings.git, "-c", "protocol.ext.allow=never", "ls-remote", url],
                                       env=self.env, cwd="/", timeout=self.settings.remote_timeout,
                                       own_session=True)
            if code == 0:
                ids = set()
                for line in out.decode("utf-8", "replace").splitlines():
                    sha = line.split("\t", 1)[0].strip()
                    if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha):
                        ids.add(sha)
                self.remote_cache[url] = (ids, None)
            else:
                self.timeouts += code is None
                self.remote_cache[url] = (None, "ls-remote timed out" if code is None else "ls-remote failed")
        return self.remote_cache[url]

    def inspect(self, entry: Entry) -> None:
        info = entry.git
        errors: List[str] = []
        code, out, _ = self.git(entry.path, "rev-parse", "--show-toplevel")
        top = os.path.realpath(out.decode("utf-8", "replace").strip()) if code == 0 else None
        if top != os.path.realpath(entry.path):
            errors.append("not a repository root" if code == 0 else "rev-parse failed")
            self.finish(entry, errors, 0, 0, 0, [], [])
            return
        dirty, ignored = 0, []
        # Explicit flags override status.showUntrackedFiles and submodule ignore settings.
        code, out, _ = self.git(entry.path, "status", "--porcelain=v1", "-z", "--ignored",
                                "--untracked-files=normal", "--ignore-submodules=none",
                                config=self.filter_overrides(entry.path))
        if code == 0:
            dirty, ignored = self.parse_status(out)
        else:
            errors.append("status failed")
        stashes, unpushed, unknown, extra = 0, [], [], []
        full_check = entry.kind == "clone"
        if entry.kind == "worktree":
            full_check, extra = self.worktree_commits(entry, errors, info)
        if full_check:
            code, out, _ = self.git(entry.path, "stash", "list", "--format=%H")
            if code == 0:
                stashes = len(out.split())
            else:
                errors.append("stash list failed")
            unpushed, unknown = self.branches(entry, top, errors, info)
        self.finish(entry, errors, dirty, len(ignored), stashes, unpushed, unknown, extra)
        if ignored:
            info["ignored_samples"] = ignored[:5]

    def worktree_commits(self, entry: Entry, errors: List[str], info: Dict[str, object]):
        """Where a .git file's commits live; (run the clone checks?, extra reasons).

        A worktree of a clone found in this scan is covered by that clone, which
        counts its worktrees' detached HEADs. A git dir inside a scanned or cleaned
        root (a submodule's .git/modules/<name>, a --separate-git-dir) is as
        temporary as a clone, so it gets the clone checks. A git dir elsewhere is
        durable, but commits only on a detached HEAD are reachable from nothing.
        """
        code, out, _ = self.git(entry.path, "rev-parse", "--git-common-dir")
        if code != 0:
            errors.append("rev-parse --git-common-dir failed")
            return False, []
        common = os.path.realpath(os.path.join(entry.path, out.decode("utf-8", "replace").strip()))
        info["git_dir"] = common
        if any(within(common, root) for root in self.cleaned):
            if common in self.clone_gitdirs:
                return False, []
            info["git_dir_in_cleaned_root"] = True
            return True, []
        if self.git(entry.path, "symbolic-ref", "-q", "HEAD")[0] == 1:
            code, out, _ = self.git(entry.path, "rev-list", "-n", "1", "HEAD", "--not", "--branches", "--tags",
                                    "--remotes")
            if code != 0:
                errors.append("rev-list failed")
            elif out.strip():
                code, head, _ = self.git(entry.path, "rev-parse", "HEAD")
                info["detached_head"] = head.decode().strip() if code == 0 else None
                return False, ["detached-commits"]
        return False, []

    def parse_status(self, out: bytes) -> Tuple[int, List[str]]:
        fields = out.split(b"\0")
        dirty, ignored, index = 0, [], 0
        while index < len(fields):
            record = fields[index]
            index += 1
            if len(record) < 4:
                continue
            code, name = record[:2], os.fsdecode(record[3:])
            if code[:1] in (b"R", b"C"):
                index += 1  # -z puts a rename's original path in the next field
            if code == b"!!":
                parts = [part for part in name.rstrip("/").split("/") if part]
                if not any(matching_glob(part, self.regenerable) for part in parts):
                    ignored.append(name)
            else:
                dirty += 1
        return dirty, ignored

    def branches(self, entry: Entry, top: str, errors: List[str], info: Dict[str, object]):
        code, out, _ = self.git(entry.path, "for-each-ref", "--format=%(objectname) %(refname)", "refs/heads")
        if code != 0:
            errors.append("for-each-ref failed")
            return [], []
        tips: Dict[str, List[str]] = {}
        for line in out.decode("utf-8", "replace").splitlines():
            sha, _, ref = line.partition(" ")
            tips.setdefault(sha, []).append(ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref)
        if self.git(entry.path, "symbolic-ref", "-q", "HEAD")[0] == 1:
            code, out, _ = self.git(entry.path, "rev-parse", "-q", "--verify", "HEAD^{commit}")
            if code == 0:
                tips.setdefault(out.decode().strip(), []).append("(detached HEAD)")
        # Other worktrees' detached HEADs keep their commits only in this git dir.
        code, out, _ = self.git(entry.path, "worktree", "list", "--porcelain")
        for block in out.decode("utf-8", "replace").split("\n\n")[1:] if code == 0 else []:
            fields = dict((line.split(" ", 1) + [""])[:2] for line in block.splitlines() if line)
            if "detached" in fields and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", fields.get("HEAD", "")):
                label = "(detached HEAD of worktree %s)" % os.path.basename(fields.get("worktree", "?"))
                names = tips.setdefault(fields["HEAD"], [])
                if label not in names and "(detached HEAD)" not in names:
                    names.append(label)
        remotes, durable, failures = [], set(), 0
        code, out, _ = self.git(entry.path, "remote")
        names = out.decode("utf-8", "replace").split() if code == 0 else []
        if code != 0:
            errors.append("remote failed")
        for name in names:
            urls = []
            for extra in ([], ["--push"]):
                code, out, _ = self.git(entry.path, "remote", "get-url", "--all", *extra, name)
                if code == 0:
                    urls.extend(line for line in out.decode("utf-8", "replace").splitlines() if line.strip())
            for url in dict.fromkeys(urls):
                record = {"name": name, "url": redact_url(url)}
                local = local_remote_path(url, top)
                if local is not None and any(within(local, root) for root in self.cleaned):
                    record["lookup"] = "not durable (inside a cleaned root)"
                else:
                    refs, error = self.remote_refs(url if local is None else local)
                    if refs is None:
                        failures += 1
                        record["lookup"] = error
                    else:
                        durable |= refs
                        record["lookup"] = "ok"
                remotes.append(record)
        info["remotes"] = remotes
        pending = [sha for sha in tips if sha not in durable]
        reachable_unpushed = None
        if pending and durable:
            check = b"".join(sha.encode() + b"\n" for sha in sorted(durable))
            code, out, _ = self.git(entry.path, "cat-file", "--batch-check", data=check)
            local = [parts[0] for parts in (line.split() for line in out.decode("utf-8", "replace").splitlines())
                     if len(parts) == 3 and parts[1] == "commit"] if code == 0 else []
            if code != 0:
                errors.append("cat-file failed")
            if local:
                spec = "".join(sha + "\n" for sha in pending) + "".join("^" + sha + "\n" for sha in local)
                code, out, _ = self.git(entry.path, "rev-list", "--stdin", data=spec.encode())
                if code == 0:
                    reachable_unpushed = set(out.decode().split())
                    info["unpushed_commits"] = len(reachable_unpushed)
                else:
                    errors.append("rev-list failed")
        unpushed, unknown = [], []
        for sha in pending:
            if reachable_unpushed is not None and sha not in reachable_unpushed:
                continue  # an ancestor of a remote ref that exists locally
            # Not provably on a reachable remote. With a failed lookup it is unknown.
            (unknown if failures else unpushed).extend(tips[sha])
        if not durable and not failures:
            info["no_durable_remote"] = True
        info["branches"] = sum(len(names) for names in tips.values())
        return sorted(unpushed), sorted(unknown)

    def finish(self, entry: Entry, errors, dirty, ignored, stashes, unpushed, unknown, extra=()) -> None:
        info = entry.git
        info.update({"uncommitted": dirty, "ignored_entries": ignored})
        if entry.kind == "clone" or info.get("git_dir_in_cleaned_root"):
            info.update({"stashes": stashes, "unpushed_branches": unpushed[:MAX_LISTED],
                         "unpushed_branch_count": len(unpushed),
                         "unverified_branches": unknown[:MAX_LISTED],
                         "unverified_branch_count": len(unknown)})
        if errors:
            info["errors"] = errors
            entry.unknown = True
            entry.reasons.append("git-error")
        if unpushed:
            entry.reasons.append("unpushed-branches")
        if unknown:
            entry.unknown = True
            entry.reasons.append("unverified-branches")
        if dirty:
            entry.reasons.append("uncommitted-changes")
        if stashes:
            entry.reasons.append("stashes")
        if ignored:
            entry.reasons.append("ignored-files")
        entry.reasons.extend(extra)


# ---------------------------------------------------------------- rescue

def free_bytes(path: str) -> int:
    probe = path
    while not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    return shutil.disk_usage(probe).free


def rsync_entry(entry: Entry, settings: Settings) -> Tuple[Optional[int], str]:
    os.makedirs(os.path.dirname(entry.dest), mode=0o700, exist_ok=True)
    command = [settings.rsync, "-aHx"] + ["--exclude=" + pattern for pattern in settings.excludes]
    if entry.kind == "file":
        command += [entry.path, entry.dest]
    else:
        command += [entry.path + "/", entry.dest + "/"]
    code, _, err = run_command(command, timeout=RSYNC_TIMEOUT)
    return code, err.decode("utf-8", "replace")


def only_vanished(code: Optional[int], detail: str) -> bool:
    """rsync 24 means files vanished; openrsync reports that as 23 with ENOENT lines."""
    if code == 24:
        return True
    lines = [line for line in detail.splitlines()
             if line.strip() and not line.lower().startswith(("rsync error:", "rsync warning:"))]
    return code == 23 and bool(lines) and all("No such file or directory" in line for line in lines)


def read_packed_refs(path: str) -> Dict[str, str]:
    refs: Dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                parts = line.split()
                if len(parts) == 2 and not line.startswith(("#", "^")):
                    refs[parts[1]] = parts[0]
    except OSError:
        pass
    return refs


def reconcile_refs(source_git: str, copy_git: str, stamp: str) -> int:
    """In the copy only: retire loose refs that the source no longer has as loose files.

    rsync never deletes, so once the source packs a ref (git pack-refs, git gc) the
    copy's stale loose file would shadow the updated packed-refs. A stale ref whose
    packed value is the same is removed; any other is moved under
    refs/tmp-state-guard/stale/<stamp>/ so its commits stay reachable. The source
    is only read.
    """
    refs_dir = os.path.join(copy_git, "refs")
    if not isdir_nofollow(refs_dir) or not isdir_nofollow(source_git):
        return 0
    packed = read_packed_refs(os.path.join(source_git, "packed-refs"))
    changed = 0
    for directory, names, files in os.walk(refs_dir):
        rel_dir = os.path.relpath(directory, copy_git)
        names[:] = [name for name in names if os.path.join(rel_dir, name) != os.path.join("refs", STALE_REFS)]
        for name in files:
            rel = os.path.join(rel_dir, name)
            if os.path.lexists(os.path.join(source_git, rel)):
                continue
            path = os.path.join(directory, name)
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as stream:
                    value = stream.read(4096).strip()
                if packed.get(rel.replace(os.sep, "/")) == value:
                    os.unlink(path)
                else:
                    target = os.path.join(refs_dir, STALE_REFS, "stale", stamp, os.path.relpath(rel, "refs"))
                    os.makedirs(os.path.dirname(target), mode=0o700, exist_ok=True)
                    os.replace(path, target)
                changed += 1
            except OSError:
                continue
    return changed


def rescue(entries: List[Entry], settings: Settings) -> None:
    budget = 0
    done = set()
    for entry in sorted(entries, key=lambda item: (item.depth, item.path)):
        cover = next((parent for parent in entry.ancestors() if id(parent) in done), None)
        if cover is not None:
            entry.rescue = {"status": "covered", "by": cover.path}
            done.add(id(entry))
            continue
        size, transfer = entry.total("own_bytes"), entry.total("transfer_bytes")
        files = entry.total("transfer_files")
        if size > settings.entry_cap:
            entry.rescue = {"status": "skipped", "reason": "entry-cap"}
            continue
        if budget + transfer > settings.run_cap:
            entry.rescue = {"status": "skipped", "reason": "run-cap"}
            continue
        try:
            free = free_bytes(settings.dest) - (budget if settings.dry_run else 0)
        except OSError:
            entry.rescue = {"status": "failed", "reason": "free-space-unknown"}
            continue
        if transfer and free - transfer < settings.free_floor:
            entry.rescue = {"status": "skipped", "reason": "free-disk-floor", "free_bytes": free}
            continue
        if settings.dry_run:
            entry.rescue = {"status": "would-copy" if files else "up-to-date"}
            budget += transfer
            done.add(id(entry))
            continue
        try:
            code, detail = rsync_entry(entry, settings)
        except OSError:
            code, detail = None, "could not create the destination"
        exists = os.path.lexists(entry.dest)
        vanished = code != 0 and only_vanished(code, detail)
        if (code == 0 or vanished) and exists:
            entry.rescue = {"status": "rescued" if files else "up-to-date", "rsync_exit": code}
            if vanished:
                entry.rescue["vanished_during_copy"] = True
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            retired = sum(reconcile_refs(os.path.join(node.path, ".git"), os.path.join(node.dest, ".git"), stamp)
                          for node in entry.subtree() if node.kind == "clone")
            if retired:
                entry.rescue["stale_refs_retired"] = retired
            budget += transfer
            done.add(id(entry))
        else:
            tail = " | ".join(detail.strip().splitlines()[-3:])
            entry.rescue = {"status": "failed", "reason": "rsync", "rsync_exit": code, "detail": tail}


# ---------------------------------------------------------------- report

def iso(epoch: float, local: bool = False, precise: bool = False) -> str:
    moment = datetime.fromtimestamp(epoch, timezone.utc)
    return (moment.astimezone() if local else moment).isoformat(timespec="microseconds" if precise else "seconds")


def entry_record(entry: Entry) -> Dict[str, object]:
    record = {
        "path": entry.path, "root": entry.root_label, "relative_path": entry.rel, "kind": entry.kind,
        "reasons": entry.reasons, "unknown": entry.unknown,
        "files": entry.total("own_files"), "bytes": entry.total("own_bytes"),
        "transfer_files": entry.total("transfer_files"), "transfer_bytes": entry.total("transfer_bytes"),
        "unreadable_dirs": entry.total("walk_errors"), "protected_dirs": entry.total("protected"),
        "dest": entry.dest, "rescue": entry.rescue,
    }
    if entry.kind in ("folder", "file"):
        record.update({"due_next_run": entry.due_next, "due_within_two_runs": entry.due_two,
                       "newest_change": iso(entry.newest_change) if entry.newest_change else None})
    if entry.git:
        record["git"] = entry.git
    return record


def build_report(settings: Settings, now: float, scanner: Scanner, tops: List[Entry], at_risk: List[Entry],
                 phase: str) -> Dict[str, object]:
    nodes = [node for top in tops for node in top.subtree()]
    kinds: Dict[str, int] = {}
    risk_kinds: Dict[str, int] = {}
    for node in nodes:
        kinds[node.kind] = kinds.get(node.kind, 0) + 1
    for node in at_risk:
        risk_kinds[node.kind] = risk_kinds.get(node.kind, 0) + 1
    outcomes: Dict[str, int] = {}
    for node in at_risk:
        state = str(node.rescue.get("status", "pending"))
        outcomes[state] = outcomes.get(state, 0) + 1
    uncovered = [node for node in at_risk if node.rescue.get("status") != "covered"]
    walk_errors = sum(node.walk_errors for node in nodes)
    unknown = [node for node in nodes if node.unknown]
    errors = list(settings.errors)
    if walk_errors:
        errors.append("walk_errors:%d" % walk_errors)
    if unknown:
        errors.append("unknown_entries:%d" % len(unknown))
    if phase != "complete":
        status = "running"
    elif errors:
        status = "incomplete"
    elif any(node.rescue.get("status") in ("skipped", "failed") for node in at_risk):
        status = "unrescued"
    elif any(node.rescue.get("status") in ("rescued", "would-copy") for node in at_risk):
        status = "rescued"
    else:
        status = "ok"
    cleaners = []
    for label, _ in settings.roots:
        model = CLEANERS[cleaner_for(label)]
        runs = next_cleaner_runs(now, model["hour"], model["minute"])
        cleaners.append({"root": label, "cleaner": cleaner_for(label), "rule": model["rule"],
                         "next_runs": [iso(run, local=True) for run in runs]})
    return {
        "schema_version": SCHEMA_VERSION, "tool": TOOL, "phase": phase,
        "observed_at": iso(now, precise=True),
        "finished_at": iso(current_time(), precise=True) if phase == "complete" else None,
        "dry_run": settings.dry_run, "status": status, "exit_code": STATUS_EXIT.get(status),
        "roots": [{"label": label, "path": path} for label, path in settings.roots],
        "dest": settings.dest,
        "cleaners": cleaners,
        "settings": {"max_depth": settings.max_depth, "recent_days": settings.recent_days,
                     "entry_cap_bytes": settings.entry_cap, "run_cap_bytes": settings.run_cap,
                     "free_floor_bytes": settings.free_floor, "excludes": list(settings.excludes),
                     "top_level_excludes": list(settings.top_excludes)},
        "inventory": {"top_level_entries": len(tops), "by_kind": kinds},
        "skipped": dict(scanner.skipped, protected_dirs=sum(node.protected for node in nodes),
                        excluded=dict(sorted(scanner.excluded.items()))),
        "at_risk": {"entries": len(at_risk), "by_kind": risk_kinds, "unknown": len(unknown),
                    "bytes": sum(node.total("own_bytes") for node in uncovered),
                    "transfer_bytes": sum(node.total("transfer_bytes") for node in uncovered),
                    "outcomes": outcomes},
        "entries": [entry_record(node) for node in at_risk],
        # Where the walk could not read: the reason a run is incomplete, or
        # privacy-protected directories that are only reported.
        "unreadable": [{"path": top.path, "unreadable_dirs": top.total("walk_errors"),
                        "protected_dirs": top.total("protected")}
                       for top in tops if top.total("walk_errors") or top.total("protected")][:MAX_LISTED],
        "errors": errors,
        "notification": {"status": "disabled"},
    }


def human_bytes(value: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1000 or unit == "TB":
            return ("%d %s" % (value, unit)) if unit == "B" else ("%.1f %s" % (value, unit))
        value /= 1000.0
    return str(value)


def summary_text(report: Dict[str, object]) -> str:
    risk = report["at_risk"]
    lines = ["XMechBay tmp-state guard, %s%s" % (report["observed_at"], " (dry run)" if report["dry_run"] else ""),
             "status: %s (exit %d)" % (report["status"], report["exit_code"])]
    kinds = ", ".join("%s %d" % item for item in sorted(risk["by_kind"].items())) or "none"
    lines.append("at risk: %d (%s); unknown: %d" % (risk["entries"], kinds, risk["unknown"]))
    outcomes = ", ".join("%s %d" % item for item in sorted(risk["outcomes"].items())) or "nothing to do"
    lines.append("rescue: " + outcomes)
    verb = "would copy" if report["dry_run"] else "copy"
    lines.append("%s %s of %s in at-risk entries" % (verb, human_bytes(risk["transfer_bytes"]),
                                                     human_bytes(risk["bytes"])))
    skipped = report["skipped"]
    lines.append("skipped top-level: %d symlinks, %d other owners, %d other filesystems; excluded paths: %d; "
                 "privacy-protected dirs: %d" % (skipped["symlink"], skipped["foreign_owner"],
                                                 skipped["other_filesystem"], sum(skipped["excluded"].values()),
                                                 skipped["protected_dirs"]))
    for record in report["entries"][:MAX_LISTED]:
        rescue = record["rescue"]
        state = str(rescue.get("status", "pending"))
        if rescue.get("reason"):
            state += " (" + str(rescue["reason"]) + ")"
        lines.append("  %s  %s  %s  %s  [%s]" % (state, record["kind"], human_bytes(record["bytes"]),
                                                record["path"], ", ".join(record["reasons"])))
    if len(report["entries"]) > MAX_LISTED:
        lines.append("  ... %d more in latest.json" % (len(report["entries"]) - MAX_LISTED))
    for record in report["unreadable"]:
        lines.append("unreadable: %s (%d unreadable, %d privacy-protected)"
                     % (record["path"], record["unreadable_dirs"], record["protected_dirs"]))
    for error in report["errors"]:
        lines.append("error: " + error)
    lines.append("copies: " + str(report["dest"]))
    return "\n".join(lines) + "\n"


def notify(report: Dict[str, object]) -> Dict[str, str]:
    if report["exit_code"] == 0:
        return {"status": "not_needed"}
    risk = report["at_risk"]
    outcomes = risk["outcomes"]
    message = ("%s: %d at risk, %d copied, %d not rescued, %d unknown%s" % (
        report["status"], risk["entries"], outcomes.get("rescued", 0) + outcomes.get("would-copy", 0),
        outcomes.get("skipped", 0) + outcomes.get("failed", 0), risk["unknown"],
        " (dry run)" if report["dry_run"] else ""))
    code, _, _ = run_command(["/usr/bin/osascript", "-e", NOTIFICATION_SCRIPT, message], timeout=10)
    return {"status": "requested" if code == 0 else "failed"}


# ---------------------------------------------------------------- state

def validate_private_dir(path: str) -> None:
    os.makedirs(path, mode=0o700, exist_ok=True)
    info = os.lstat(path)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        raise Refusal("state directory must be a private directory (mode 0700) owned by this user")


@contextmanager
def run_lock(path: Optional[str]):
    """Exclusive non-blocking lock on a file; None means no lock (a dry run without state)."""
    if path is None:
        yield
        return
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refusal("another tmp-state guard run holds the lock")
        yield
    finally:
        os.close(descriptor)


def atomic_write(path: str, data: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".tmp-state-guard-", dir=os.path.dirname(path))
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def persist(state_dir: str, report: Dict[str, object], history_limit: int) -> None:
    atomic_write(os.path.join(state_dir, "latest.json"), (json.dumps(report, indent=2) + "\n").encode())
    if report["phase"] != "complete":
        return
    history_path = os.path.join(state_dir, "history.json")
    history: List[object] = []
    try:
        if not os.path.islink(history_path):
            with open(history_path, "rb") as stream:
                loaded = json.loads(stream.read(8 * 1024 * 1024))
            history = loaded if isinstance(loaded, list) else []
    except (OSError, ValueError):
        history = []
    risk = report["at_risk"]
    history.append({"observed_at": report["observed_at"], "status": report["status"],
                    "exit_code": report["exit_code"], "dry_run": report["dry_run"],
                    "at_risk": risk["entries"], "by_kind": risk["by_kind"], "outcomes": risk["outcomes"],
                    "transfer_bytes": risk["transfer_bytes"], "errors": report["errors"]})
    history = history[-history_limit:]
    atomic_write(history_path, (json.dumps(history, indent=2) + "\n").encode())
    atomic_write(os.path.join(state_dir, "summary.txt"), summary_text(report).encode())


# ---------------------------------------------------------------- main

def observe_and_rescue(settings: Settings, cleaned: List[str], times: Optional[Callable] = None,
                       now: Optional[float] = None) -> Dict[str, object]:
    now = current_time() if now is None else now
    scanner = Scanner(settings, now, times or file_times)
    tops: List[Entry] = []
    for label, root in settings.roots:
        tops.extend(scanner.scan_root(label, root))
    # A remote inside a scanned or cleaned root is as temporary as the clone.
    inspector = GitInspector(settings, sorted(set(cleaned) | {path for _, path in settings.roots}))
    nodes = [node for top in tops for node in top.subtree()]
    inspector.clone_gitdirs = {os.path.realpath(os.path.join(node.path, ".git"))
                               for node in nodes if node.kind == "clone"}
    recent = now - settings.recent_days * DAY
    for top in tops:
        if top.kind in ("folder", "file") and top.due_two and top.newest_change >= recent:
            top.reasons.append("files-due")
    for node in nodes:
        if node.kind in ("clone", "worktree"):
            inspector.inspect(node)
    for node in nodes:
        # The commits of an at-risk checkout whose git dir sits elsewhere in a root
        # are copied with the entry that holds that git dir.
        git_dir = node.git.get("git_dir")
        if not (node.reasons and node.git.get("git_dir_in_cleaned_root")) or within(git_dir, node.path):
            continue
        holders = [other for other in nodes if within(git_dir, other.path)]
        if holders:
            holder = max(holders, key=lambda other: len(other.path))
            if "git-dir-of-at-risk-checkout" not in holder.reasons:
                holder.reasons.append("git-dir-of-at-risk-checkout")
        else:
            node.git.setdefault("errors", []).append("git dir is not inside a scanned entry and is not copied")
            node.unknown = True
    at_risk = [node for node in nodes if node.reasons]
    if settings.state_dir:
        persist(settings.state_dir, build_report(settings, now, scanner, tops, at_risk, "observed"),
                settings.history_limit)
    rescue(at_risk, settings)
    return build_report(settings, now, scanner, tops, at_risk, "complete")


def gigabytes(value: str) -> int:
    number = float(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must not be negative")
    return int(number * GB)


def exclude_glob(value: str) -> str:
    if not value or "/" in value or value[:2] in ("+ ", "- ") or value.startswith("!"):
        raise argparse.ArgumentTypeError("an exclusion is a file-name glob without '/'")
    if fnmatch.fnmatchcase(".git", value):
        raise argparse.ArgumentTypeError("an exclusion must not match .git")
    if any(fnmatch.fnmatchcase(name, value) for name in GIT_INTERNAL_NAMES):
        raise argparse.ArgumentTypeError("an exclusion must not match names inside .git (it would corrupt copies)")
    return value


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", action="append", metavar="[LABEL=]PATH",
                        help="directory to guard (repeatable); default /private/tmp and DARWIN_USER_TEMP_DIR")
    parser.add_argument("--dest", default=DEFAULT_DEST, help="backup directory (default %(default)s)")
    parser.add_argument("--state-dir", help="private directory for latest.json, history.json, summary.txt")
    parser.add_argument("--dry-run", action="store_true", help="report what would be copied; copy nothing")
    parser.add_argument("--notify", action="store_true", help="request a local macOS notification when exit != 0")
    parser.add_argument("--json", action="store_true", help="print the full JSON report instead of the summary")
    parser.add_argument("--max-depth", type=int, default=7, help="repository search depth (default 7)")
    parser.add_argument("--recent-days", type=float, default=14.0,
                        help="non-git entries count only if a file changed within this many days")
    parser.add_argument("--entry-cap-gb", type=gigabytes, default=5 * GB, help="per-entry size cap (default 5)")
    parser.add_argument("--run-cap-gb", type=gigabytes, default=30 * GB, help="per-run copy cap (default 30)")
    parser.add_argument("--free-floor-gb", type=gigabytes, default=50 * GB,
                        help="keep at least this much free on the backup disk (default 50)")
    parser.add_argument("--exclude", action="append", type=exclude_glob, default=[], metavar="GLOB",
                        help="extra file-name glob to skip in scans and copies (repeatable)")
    parser.add_argument("--exclude-top", action="append", type=exclude_glob, default=[], metavar="GLOB",
                        help="extra glob matched only against top-level names under a root (repeatable)")
    parser.add_argument("--no-default-excludes", action="store_true",
                        help="drop the built-in exclusion lists; --exclude and --exclude-top still apply")
    parser.add_argument("--history-limit", type=int, default=90)
    parser.add_argument("--git", default=shutil.which("git") or "/usr/bin/git")
    parser.add_argument("--rsync", default="/usr/bin/rsync" if os.path.exists("/usr/bin/rsync")
                        else (shutil.which("rsync") or "rsync"))
    parser.add_argument("--git-timeout", type=float, default=300.0)
    parser.add_argument("--remote-timeout", type=float, default=60.0)
    args = parser.parse_args(argv)
    if not 1 <= args.max_depth <= 32:
        parser.error("--max-depth must be between 1 and 32")
    if not 1 <= args.history_limit <= 1000:
        parser.error("--history-limit must be between 1 and 1000")
    if args.recent_days <= 0:
        parser.error("--recent-days must be positive")
    return args


def settings_from_args(args) -> Settings:
    errors: List[str] = []
    user_temp = darwin_user_temp_dir() if sys.platform == "darwin" else None
    if args.root:
        roots = [parse_root(value, user_temp) for value in args.root]
    else:
        roots = [("tmp", os.path.realpath("/private/tmp" if sys.platform == "darwin" else "/tmp"))]
        if user_temp:
            roots.append(("darwin-user-temp", os.path.realpath(user_temp)))
        elif sys.platform == "darwin":
            errors.append("darwin_user_temp_dir_unavailable")
    excludes = () if args.no_default_excludes else DEFAULT_EXCLUDES
    excludes = tuple(dict.fromkeys(excludes + tuple(args.exclude)))
    top_excludes = () if args.no_default_excludes else DEFAULT_TOP_EXCLUDES
    top_excludes = tuple(dict.fromkeys(top_excludes + tuple(args.exclude_top)))
    return Settings(
        roots=roots, dest=os.path.abspath(os.path.expanduser(args.dest)),
        state_dir=os.path.abspath(os.path.expanduser(args.state_dir)) if args.state_dir else None,
        dry_run=args.dry_run, notify=args.notify, max_depth=args.max_depth, recent_days=args.recent_days,
        entry_cap=args.entry_cap_gb, run_cap=args.run_cap_gb, free_floor=args.free_floor_gb,
        excludes=excludes, top_excludes=top_excludes, history_limit=args.history_limit, git=args.git, rsync=args.rsync,
        git_timeout=args.git_timeout, remote_timeout=args.remote_timeout, errors=errors)


def main(argv=None) -> int:
    args = parse_args(argv)
    previous = {signum: signal.signal(signum, terminate) for signum in (signal.SIGTERM, signal.SIGINT)}
    try:
        return run(args)
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def run(args) -> int:
    settings = settings_from_args(args)
    try:
        cleaned = system_cleaned_roots()
        check_refusal(settings, cleaned)
        lock = None
        if settings.state_dir:
            validate_private_dir(settings.state_dir)
            lock = os.path.join(settings.state_dir, "run.lock")
        elif not settings.dry_run:
            os.makedirs(settings.dest, mode=0o700, exist_ok=True)
            lock = os.path.join(settings.dest, ".tmp-state-guard.lock")
        with run_lock(lock):
            report = observe_and_rescue(settings, cleaned)
            if settings.notify:
                report["notification"] = notify(report)
            if settings.state_dir:
                persist(settings.state_dir, report, settings.history_limit)
    except Refusal as error:
        print("tmp-state guard refused: " + str(error), file=sys.stderr)
        return EXIT_REFUSED
    except OSError as error:
        print("tmp-state guard failed: " + str(error), file=sys.stderr)
        return STATUS_EXIT["incomplete"]
    sys.stdout.write(json.dumps(report, indent=2) + "\n" if args.json else summary_text(report))
    return int(report["exit_code"])


if __name__ == "__main__":
    sys.exit(main())
