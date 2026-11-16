#!/usr/bin/env python3
"""Refuse unsafe Git uploads before object transfer (2026-11-15).

Inputs: Git pre-push arguments/stdin and a locally installed publication contract.
Outputs: redacted refusal or scope counts; reads Git objects and GitHub metadata.
The installer pins this script and its scanner outside the changing worktree.
Local hooks are a backstop, not server-side authorization or a secrecy proof.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import check_public_content as scanner


OID = re.compile(r"[0-9a-f]{40}\Z")
ZERO = "0" * 40
REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9][A-Za-z0-9._-]*\Z")
CONTRACT_KEYS = {"schema_version", "repository_id", "repository_full_name", "clean_root", "remote_name"}
DENY_PATTERN_FILE = "deny-patterns.txt"
MAX_DENY_PATTERN_BYTES = 1024 * 1024


class Refusal(Exception):
    """A bounded diagnostic containing no remote URL, path or matched content."""


def git(root: Path, *args: str, optional: bool = False) -> bytes:
    environment = os.environ.copy()
    environment.update(GIT_NO_REPLACE_OBJECTS="1", GIT_OPTIONAL_LOCKS="0", GIT_NO_LAZY_FETCH="1", GIT_TERMINAL_PROMPT="0")
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, env=environment)
    if result.returncode and not (optional and result.returncode == 1):
        raise Refusal("Git inspection failed; publication was not approved.")
    return result.stdout


def validate_contract(contract: dict) -> dict:
    if not isinstance(contract, dict) or not CONTRACT_KEYS <= set(contract) or set(contract) - CONTRACT_KEYS - {"deny_patterns_sha256"}:
        raise Refusal("Publication contract has an unsupported schema.")
    if type(contract["schema_version"]) is not int or contract["schema_version"] != 1:
        raise Refusal("Publication contract version is unsupported.")
    if type(contract["repository_id"]) is not int or contract["repository_id"] < 1:
        raise Refusal("Publication contract requires a positive repository ID.")
    if not isinstance(contract["repository_full_name"], str) or not REPOSITORY.fullmatch(contract["repository_full_name"]):
        raise Refusal("Publication contract requires a canonical repository name.")
    if not isinstance(contract["clean_root"], str) or not OID.fullmatch(contract["clean_root"]) or contract["clean_root"] == ZERO:
        raise Refusal("Publication contract requires a complete root commit ID.")
    if contract["remote_name"] != "origin":
        raise Refusal("Only the explicitly configured origin remote is supported.")
    if "deny_patterns_sha256" in contract and (not isinstance(contract["deny_patterns_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", contract["deny_patterns_sha256"])):
        raise Refusal("Publication contract requires a complete deny-pattern digest.")
    return contract


def compile_deny_patterns(data: bytes) -> list[re.Pattern[str]]:
    """Use UTF-8 regex lines, ignoring blank lines and lines beginning with #."""
    if len(data) > MAX_DENY_PATTERN_BYTES or b"\0" in data:
        raise Refusal("Deny-pattern payload is invalid or oversized; values withheld.")
    try:
        patterns = [re.compile(line) for line in data.decode("utf-8").splitlines()
                    if line.strip() and not line.startswith("#")]
    except (UnicodeError, re.error, OverflowError, RecursionError):
        raise Refusal("Deny-pattern payload cannot be decoded or compiled; values withheld.") from None
    if not patterns:
        raise Refusal("Configured deny-pattern payload contains no patterns.")
    return patterns


def read_deny_pattern_data(path: Path) -> bytes:
    try:
        if path.is_symlink() or not path.is_file():
            raise Refusal("Deny-pattern payload must be a readable regular file.")
        with path.open("rb") as handle:
            data = handle.read(MAX_DENY_PATTERN_BYTES + 1)
    except OSError:
        raise Refusal("Deny-pattern payload is absent or unreadable; values withheld.") from None
    compile_deny_patterns(data)
    return data


def load_deny_patterns(contract: dict, path: Path | None = None) -> list[re.Pattern[str]]:
    digest = contract.get("deny_patterns_sha256")
    if digest is None:
        if path is not None:
            raise Refusal("Deny-pattern payload is not bound by the publication contract.")
        return []
    data = read_deny_pattern_data(path if path is not None else Path(__file__).with_name(DENY_PATTERN_FILE))
    if hashlib.sha256(data).hexdigest() != digest:
        raise Refusal("Deny-pattern payload differs from the publication contract.")
    return compile_deny_patterns(data)


def load_contract(path: Path) -> dict:
    def unique_keys(items):
        value = {}
        for key, item in items:
            if key in value:
                raise Refusal("Publication contract contains duplicate keys.")
            value[key] = item
        return value
    try:
        return validate_contract(json.loads(path.read_text(), object_pairs_hook=unique_keys))
    except (OSError, ValueError, UnicodeError):
        raise Refusal("Publication contract is absent or unreadable.") from None


def allowed_head(ref: str) -> bool:
    return ref == "refs/heads/main" or bool(re.fullmatch(r"refs/heads/public/[a-z0-9][a-z0-9._/-]*", ref))


def parse_updates(text: str) -> list[tuple[str, str, str, str]]:
    updates = []
    destinations = set()
    for line in text.splitlines():
        fields = line.split()
        if len(fields) != 4:
            raise Refusal("Malformed pre-push input.")
        local_ref, local_oid, remote_ref, remote_oid = fields
        if not OID.fullmatch(local_oid) or not OID.fullmatch(remote_oid):
            raise Refusal("Pre-push input contains an invalid object ID.")
        if not allowed_head(remote_ref) or remote_ref in destinations:
            raise Refusal("Destination ref is outside the publication branch policy.")
        if local_oid == ZERO and (remote_ref == "refs/heads/main" or local_ref != "(delete)" or remote_oid == ZERO):
            raise Refusal("Main deletion or malformed deletion is not permitted.")
        if local_oid != ZERO and local_ref == "(delete)":
            raise Refusal("Malformed deletion input.")
        destinations.add(remote_ref)
        updates.append((local_ref, local_oid, remote_ref, remote_oid))
    return updates


def verify_history_available(root: Path) -> None:
    if git(root, "rev-parse", "--is-shallow-repository").strip() != b"false":
        raise Refusal("Shallow history cannot establish publication ancestry.")
    graft_path = Path(os.fsdecode(git(root, "rev-parse", "--git-path", "info/grafts").strip()))
    if not graft_path.is_absolute():
        graft_path = root / graft_path
    if graft_path.exists() or os.environ.get("GIT_GRAFT_FILE"):
        raise Refusal("Grafted history is not supported for publication.")
    if git(root, "for-each-ref", "--format=%(refname)", "refs/replace").strip() or os.environ.get("GIT_REPLACE_REF_BASE"):
        raise Refusal("Replacement history is not supported for publication.")
    if git(root, "config", "--get", "extensions.partialclone", optional=True).strip() or git(root, "config", "--get-regexp", r"remote\..*\.promisor", optional=True).strip():
        raise Refusal("Partial clones cannot establish complete publication content.")


def verify_destination(root: Path, contract: dict, remote_name: str, remote_url: str) -> None:
    if remote_name != contract["remote_name"]:
        raise Refusal("The push does not target the approved named remote.")
    name = contract["repository_full_name"]
    ssh_user = "git" + "@"
    accepted = {f"https://github.com/{name}", f"https://github.com/{name}.git", ssh_user + f"github.com:{name}.git", "ssh://" + ssh_user + f"github.com/{name}.git"}
    # get-url expands both insteadOf and pushInsteadOf; --all catches a second
    # push destination instead of silently validating only the first one.
    effective = git(root, "remote", "get-url", "--push", "--all", remote_name).decode().splitlines()
    if len(effective) != 1 or effective[0] not in accepted or remote_url not in accepted or remote_url != effective[0]:
        raise Refusal("Push URL or URL rewrite does not identify the approved endpoint.")


def github_metadata(contract: dict) -> dict:
    try:
        result = subprocess.run(["gh", "api", "--hostname", "github.com", "--include", "repos/" + contract["repository_full_name"]], capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        raise Refusal("GitHub identity could not be verified.") from None
    if result.returncode:
        raise Refusal("GitHub identity lookup failed.")
    try:
        header, body = result.stdout.replace(b"\r\n", b"\n").split(b"\n\n", 1)
        if not re.fullmatch(rb"HTTP/\S+ 200(?: [^\n]*)?", header.splitlines()[0]) or re.search(rb"(?im)^location:", header):
            raise Refusal("GitHub returned a redirect or unexpected response.")
        return json.loads(body)
    except (ValueError, IndexError, UnicodeError):
        raise Refusal("GitHub identity response is invalid.") from None


def verify_github_identity(contract: dict, metadata: dict) -> None:
    if not isinstance(metadata, dict) or type(metadata.get("id")) is not int or metadata.get("id") != contract["repository_id"] or metadata.get("full_name") != contract["repository_full_name"] or metadata.get("private") is not False or metadata.get("visibility") != "public" or metadata.get("fork") is not False or metadata.get("html_url") != "https://github.com/" + contract["repository_full_name"]:
        raise Refusal("Repository identity, name, visibility or network does not match the publication contract.")


def scan_updates(root: Path, contract: dict, updates: list[tuple[str, str, str, str]], extra: list[re.Pattern[str]]) -> dict[str, int]:
    verify_history_available(root)
    tips = sorted({item[1] for item in updates if item[1] != ZERO})
    for local_ref, _, destination, _ in updates:
        git(root, "check-ref-format", destination)
        if scanner.scan_text(destination, "destination-ref", extra) or scanner.scan_text(local_ref, "source-ref", extra):
            raise Refusal("Publication ref contains a prohibited content pattern; values withheld.")
    for tip in tips:
        if git(root, "cat-file", "-t", tip).strip() != b"commit":
            raise Refusal("Publication refs must target direct commit objects.")
    for _, tip, _, previous in updates:
        if tip != ZERO and previous != ZERO:
            # Refuse absent old objects as well: fetch the actual destination
            # before establishing that this update is a fast-forward.
            if git(root, "cat-file", "-t", previous).strip() != b"commit":
                raise Refusal("The advertised previous commit cannot be verified.")
            git(root, "merge-base", "--is-ancestor", previous, tip)
    if not tips:
        return {"commits": 0, "blobs": 0, "paths": 0}
    commits = git(root, "rev-list", "--topo-order", *tips, "--").decode().splitlines()
    roots = set(git(root, "rev-list", "--max-parents=0", *tips, "--").decode().splitlines())
    if roots != {contract["clean_root"]}:
        raise Refusal("Pushed ancestry is not rooted exclusively in the approved clean commit.")
    blobs = set()
    paths = set()
    findings = set()
    for commit in commits:
        raw_commit = git(root, "cat-file", "commit", commit)
        header, separator, message = raw_commit.partition(b"\n\n")
        if not separator or not message.startswith(b"[public-source] "):
            raise Refusal("Every pushed commit must use the public-source message prefix.")
        if sum(line.startswith(b"parent ") for line in header.splitlines()) > 1:
            raise Refusal("Only linear public-source history may be uploaded.")
        findings.update(scanner.scan_blob(raw_commit, "commit:" + commit[:12], scanner.DEFAULT_MAX_BYTES, extra, git_metadata=True))
        # Include tree entries: -r alone omits empty directories, whose names
        # are still uploaded as Git metadata and need the same path checks.
        for entry in git(root, "ls-tree", "-rtz", "--full-tree", commit).split(b"\0"):
            if not entry:
                continue
            metadata, raw_path = entry.split(b"\t", 1)
            mode, kind, oid = metadata.decode().split()
            path = os.fsdecode(raw_path)
            # Output never includes path strings or content; object IDs and
            # categories suffice to locate the finding during private review.
            location = kind + ":" + oid[:12]
            if (path, oid) not in paths:
                findings.update(scanner.scan_path(path, location, extra))
                paths.add((path, oid))
            if kind == "tree" and mode == "040000":
                continue
            if kind != "blob" or mode not in {"100644", "100755", "120000"}:
                raise Refusal("Pushed history contains an unsupported tree entry.")
            if oid not in blobs:
                findings.update(scanner.scan_blob(git(root, "cat-file", "blob", oid), location, scanner.DEFAULT_MAX_BYTES, extra))
                blobs.add(oid)
    if findings:
        categories = ", ".join(sorted({finding.category for finding in findings}))
        raise Refusal("Pushed history failed content checks: " + categories + ". Values withheld.")
    return {"commits": len(commits), "blobs": len(blobs), "paths": len(paths)}


def check_push(root: Path, contract: dict, remote_name: str, remote_url: str, stdin: str, identity_reader=github_metadata, deny_patterns_path: Path | None = None) -> dict[str, int]:
    validate_contract(contract)
    extra = load_deny_patterns(contract, deny_patterns_path)
    updates = parse_updates(stdin)
    verify_destination(root, contract, remote_name, remote_url)
    counts = scan_updates(root, contract, updates, extra)
    verify_github_identity(contract, identity_reader(contract))
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("remote_name")
    parser.add_argument("remote_url")
    args = parser.parse_args(argv)
    try:
        root = Path(os.fsdecode(git(Path.cwd(), "rev-parse", "--show-toplevel").strip()))
        contract = load_contract(args.contract)
        deny_path = args.contract.parent / DENY_PATTERN_FILE if "deny_patterns_sha256" in contract else None
        counts = check_push(root, contract, args.remote_name, args.remote_url, sys.stdin.read(), deny_patterns_path=deny_path)
    except (Refusal, OSError, ValueError, UnicodeError) as error:
        message = str(error) if isinstance(error, Refusal) else "Publication inspection could not complete; values withheld."
        print("Publication refused: " + message, file=sys.stderr)
        return 1
    print("Publication guard passed: " + ", ".join(f"{key}={value}" for key, value in counts.items()) + ".")
    return 0


if __name__ == "__main__":
    sys.exit(main())
