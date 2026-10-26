#!/usr/bin/env python3
"""Check publication inputs for private data, without printing matched values.

Inputs: tracked worktree files, reachable Git history, optional external patterns.
Output: category/location findings and scope summary; exit 1 on findings.
Serves the public infrastructure publication gate; 2026-10-25.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import subprocess
import sys
from dataclasses import dataclass


DEFAULT_MAX_BYTES = 1024 * 1024
EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@(?:[\w-]+\.)+[A-Za-z]{2,}(?![\w.-])")
GIT_IDENTITY = re.compile(r"(?m)^(?:author|committer|tagger) [^\n<>]+ <(?P<email>[^\s<>]+)> \d+ [+-]\d{4}$")
PATTERNS = {
    "personal-home-path": re.compile(
        r"(?i)(?:/(?:Users|home)/[A-Za-z0-9_.-]+(?:/|\b)|[A-Z]:[\\/](?:Users|Documents and Settings)[\\/][^\s\\/]+)"
    ),
    "local-hostname": re.compile(r"(?i)\b[A-Za-z0-9][A-Za-z0-9.-]*\.local\b"),
    "private-network-address": re.compile(
        r"(?<![\d.])(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})(?![\d.])"
    ),
    "credential-token": re.compile(
        r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|AKIA[A-Z0-9]{16}|xox[baprs]-[A-Za-z0-9-]{16,})\b"
    ),
    "private-key": re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----"),
    "credential-assignment": re.compile(
        r"(?im)^\s*[\"']?(?:api_key|access_token|refresh_token|client_secret|password)[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9_+/=-]{16,})"
    ),
}
PRIVATE_DIRECTORIES = {
    "records", "hosts", ".local", ".ssh", ".aws", "node_modules", "__pycache__",
}
PRIVATE_FILENAMES = {".env", "credentials", "credentials.json", "auth.json", "id_rsa", "id_ed25519", ".DS_Store"}
PRIVATE_SUFFIXES = {".log", ".sqlite", ".sqlite3", ".db", ".pem", ".key", ".p12", ".pfx"}


@dataclass(frozen=True, order=True)
class Finding:
    location: str
    category: str
    line: int = 0

    def render(self) -> str:
        # Locations can themselves contain private text; never print raw paths.
        return f"{self.category}: {self.location}" + (f":{self.line}" if self.line else "")


def git(root: Path, *args: str, check: bool = True) -> bytes:
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True)
    if check and result.returncode:
        raise RuntimeError(f"git {args[0]} failed (exit {result.returncode})")
    return result.stdout


def scan_text(text: str, location: str, extra: list[re.Pattern[str]], allowed_email_spans: set[tuple[int, int]] | None = None) -> set[Finding]:
    findings: set[Finding] = set()
    for category, pattern in PATTERNS.items():
        for match in pattern.finditer(text):
            findings.add(Finding(location, category, text.count("\n", 0, match.start()) + 1))
    for match in EMAIL.finditer(text):
        if not match.group().lower().endswith(".invalid") and match.span() not in (allowed_email_spans or set()):
            findings.add(Finding(location, "non-example-email", text.count("\n", 0, match.start()) + 1))
    for pattern in extra:
        for match in pattern.finditer(text):
            findings.add(Finding(location, "external-deny-pattern", text.count("\n", 0, match.start()) + 1))
    return findings


def safe_location(path: str, extra: list[re.Pattern[str]]) -> str:
    if scan_text(path, "path", extra) or any(ord(char) < 32 for char in path):
        return "[redacted path]"
    return path


def scan_path(path: str, location: str, extra: list[re.Pattern[str]]) -> set[Finding]:
    findings = scan_text(path, location, extra)
    parts = Path(path).parts
    if any(part in PRIVATE_DIRECTORIES for part in parts):
        findings.add(Finding(location, "private-or-runtime-directory"))
    name = Path(path).name
    if name in PRIVATE_FILENAMES or name.startswith(".env.") and name != ".env.example":
        findings.add(Finding(location, "private-or-runtime-file"))
    if Path(path).suffix.lower() in PRIVATE_SUFFIXES:
        findings.add(Finding(location, "private-or-runtime-file"))
    return findings


def scan_blob(data: bytes, location: str, max_bytes: int, extra: list[re.Pattern[str]], git_metadata: bool = False) -> set[Finding]:
    findings: set[Finding] = set()
    if len(data) > max_bytes:
        findings.add(Finding(location, "oversized-blob"))
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return findings | {Finding(location, "binary-or-non-utf8-blob")}
    if "\x00" in text:
        findings.add(Finding(location, "binary-or-non-utf8-blob"))
    allowed_email_spans: set[tuple[int, int]] = set()
    if git_metadata:
        # GitHub uses a service identity for synthetic PR merge commits and
        # offers public no-reply aliases. Allow these only in Git headers;
        # source files and freeform commit/tag messages remain strict.
        header = text.split("\n\n", 1)[0]
        for identity in GIT_IDENTITY.finditer(header):
            email = identity.group("email").lower()
            if email == "noreply" + "@" + "github.com" or email.endswith("@" + "users.noreply.github.com"):
                allowed_email_spans.add(identity.span("email"))
    findings.update(scan_text(text, location, extra, allowed_email_spans))
    return findings


def scan_repository(root: Path, max_bytes: int, extra: list[re.Pattern[str]], worktree_all: bool = False) -> tuple[set[Finding], dict[str, int]]:
    findings: set[Finding] = set()
    tracked = [os.fsdecode(path) for path in git(root, "ls-files", "-z").split(b"\0") if path]
    counts = {"tracked_files": len(tracked), "index_blobs": 0, "additional_worktree_files": 0, "commits": 0, "history_blobs": 0, "other_ref_objects": 0}
    if git(root, "rev-parse", "--is-shallow-repository").strip() == b"true":
        findings.add(Finding("repository", "incomplete-shallow-history"))
    for path in tracked:
        location = "worktree:" + safe_location(path, extra)
        findings.update(scan_path(path, location, extra))
        candidate = root / path
        if candidate.is_symlink():
            data = os.readlink(candidate).encode("utf-8", errors="surrogateescape")
        elif candidate.is_file():
            data = candidate.read_bytes()
        else:
            findings.add(Finding(location, "missing-or-unsupported-tracked-file"))
            continue
        findings.update(scan_blob(data, location, max_bytes, extra))
    for entry in git(root, "ls-files", "--stage", "-z").split(b"\0"):
        if not entry:
            continue
        metadata, raw_path = entry.split(b"\t", 1)
        mode, oid, stage = metadata.decode().split()
        path = os.fsdecode(raw_path)
        location = "index:" + safe_location(path, extra)
        if mode == "160000" or stage != "0":
            findings.add(Finding(location, "unsupported-index-entry"))
            continue
        findings.update(scan_blob(git(root, "cat-file", "blob", oid), location, max_bytes, extra))
        counts["index_blobs"] += 1
    if worktree_all:
        tracked_set = set(tracked)
        for directory, directories, filenames in os.walk(root, followlinks=False):
            directories[:] = [name for name in directories if name != ".git"]
            # os.walk lists symlinked directories separately and does not descend
            # into them. Audit their link text without reading outside the tree.
            link_directories = [name for name in directories if (Path(directory) / name).is_symlink()]
            for filename in filenames + link_directories:
                candidate = Path(directory) / filename
                path = candidate.relative_to(root).as_posix()
                if path in tracked_set:
                    continue
                counts["additional_worktree_files"] += 1
                location = "additional-worktree:" + safe_location(path, extra)
                findings.update(scan_path(path, location, extra))
                data = os.readlink(candidate).encode() if candidate.is_symlink() else candidate.read_bytes()
                findings.update(scan_blob(data, location, max_bytes, extra))
    commits = git(root, "rev-list", "--all").decode().splitlines()
    counts["commits"] = len(commits)
    seen_blobs: set[str] = set()
    seen_paths: set[tuple[str, str]] = set()
    for commit in commits:
        raw_commit = git(root, "cat-file", "commit", commit)
        findings.update(scan_blob(raw_commit, "commit:" + commit[:12], max_bytes, extra, git_metadata=True))
        for entry in git(root, "ls-tree", "-rz", commit).split(b"\0"):
            if not entry:
                continue
            metadata, raw_path = entry.split(b"\t", 1)
            mode, kind, raw_oid = metadata.decode().split()
            path = os.fsdecode(raw_path)
            oid = raw_oid
            location = "history:" + commit[:12] + ":" + safe_location(path, extra)
            if (path, oid) not in seen_paths:
                findings.update(scan_path(path, location, extra))
                seen_paths.add((path, oid))
            if kind != "blob":
                findings.add(Finding(location, "unsupported-git-object"))
                continue
            if oid not in seen_blobs:
                findings.update(scan_blob(git(root, "cat-file", "blob", oid), location, max_bytes, extra))
                seen_blobs.add(oid)
    # Git permits refs and annotated tags to target blobs, trees or other tags.
    # These objects need an explicit walk; commit traversal alone misses them.
    pending: list[tuple[str, str]] = []
    for raw_ref in git(root, "for-each-ref", "--format=%(refname)%00%(objecttype)%00%(objectname)").splitlines():
        raw_name, raw_kind, raw_oid = raw_ref.split(b"\0")
        name = os.fsdecode(raw_name)
        findings.update(scan_text(name, "ref:" + safe_location(name, extra), extra))
        pending.append((raw_kind.decode(), raw_oid.decode()))
    visited: set[str] = set(commits)
    while pending:
        kind, oid = pending.pop()
        if oid in visited:
            continue
        visited.add(oid)
        counts["other_ref_objects"] += 1
        location = kind + ":" + oid[:12]
        if kind == "tag":
            data = git(root, "cat-file", "tag", oid)
            findings.update(scan_blob(data, location, max_bytes, extra, git_metadata=True))
            header = data.split(b"\n\n", 1)[0].splitlines()
            target = next(line[7:].decode() for line in header if line.startswith(b"object "))
            target_kind = next(line[5:].decode() for line in header if line.startswith(b"type "))
            pending.append((target_kind, target))
        elif kind == "blob":
            if oid not in seen_blobs:
                findings.update(scan_blob(git(root, "cat-file", "blob", oid), location, max_bytes, extra))
                seen_blobs.add(oid)
        elif kind == "tree":
            for entry in git(root, "ls-tree", "-rz", oid).split(b"\0"):
                if not entry:
                    continue
                metadata, raw_path = entry.split(b"\t", 1)
                mode, child_kind, child_oid = metadata.decode().split()
                path = os.fsdecode(raw_path)
                findings.update(scan_path(path, location + ":" + safe_location(path, extra), extra))
                pending.append((child_kind, child_oid))
        else:
            findings.add(Finding(location, "unsupported-git-object"))
    counts["history_blobs"] = len(seen_blobs)
    return findings, counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument("--deny-patterns", type=Path, help="Private external UTF-8 regex file, one pattern per line; must be outside repository")
    parser.add_argument("--worktree-all", action="store_true", help="Also scan hidden, untracked and ignored worktree files, excluding Git's internal directory")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if args.max_bytes < 1:
        parser.error("--max-bytes must be positive")
    extra: list[re.Pattern[str]] = []
    if args.deny_patterns:
        path = args.deny_patterns.resolve()
        if path == root or root in path.parents:
            parser.error("--deny-patterns must be stored outside the repository")
        try:
            extra = [re.compile(line) for line in path.read_text().splitlines() if line and not line.startswith("#")]
        except (OSError, re.error):
            parser.error("cannot read or compile external deny patterns (values withheld)")
    try:
        findings, counts = scan_repository(root, args.max_bytes, extra, args.worktree_all)
    except (OSError, RuntimeError, UnicodeError):
        print("Publication check could not complete; no clean result is available.", file=sys.stderr)
        return 2
    for finding in sorted(findings):
        print(finding.render())
    print("Scope: tracked worktree files, staged index blobs and all locally reachable commit/ref/tag metadata and content.")
    print("Hidden/ignored files: " + ("included (excluding .git internals)." if args.worktree_all else "untracked files excluded; use --worktree-all for a separate local inventory."))
    print("GitHub comments, PR refs absent locally, caches, logs, artifacts and other remote surfaces require a separate audit.")
    print("Counts: " + ", ".join(f"{key}={value}" for key, value in counts.items()))
    print(f"Findings: {len(findings)}")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
