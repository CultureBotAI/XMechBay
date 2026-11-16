#!/usr/bin/env python3
"""2026-11-16: Supply one literal env-file token to an MCP header helper.

Inputs: explicit private env file and variable name. Default/check output is a
configured boolean; --emit-headers writes secret JSON only to nonterminal stdout
owned by the caller (normally the MCP client's pipe). No shell evaluation,
inherited credentials, network access, or file writes. Runtime activation work.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat
import sys


MAX_BYTES = 64 * 1024
ERROR = "mcp-env-headers: refused unsafe or invalid input"
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
TOKEN = re.compile(r"[A-Za-z0-9._~+/-]+=*\Z")
ASSIGNMENT = re.compile(r"(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)[ \t]*=[ \t]*(.*)\Z")


class Refused(ValueError):
    """Never attach input data to this exception."""


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Refused()


def _snapshot(info):
    return (info.st_dev, info.st_ino, info.st_uid, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _read_private_file(filename: str) -> str:
    """Walk without following links and read a bounded, owner-only regular file."""
    path = Path(filename)
    parts = path.parts[1:] if path.is_absolute() else path.parts
    if not parts or ".." in parts:
        raise Refused()
    directory = os.open("/" if path.is_absolute() else ".",
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    descriptor = None
    try:
        for component in parts[:-1]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=directory)
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                or before.st_nlink != 1 or stat.S_IMODE(before.st_mode) & 0o077
                or before.st_size > MAX_BYTES):
            raise Refused()
        chunks = []
        remaining = MAX_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        if len(content) > MAX_BYTES or _snapshot(before) != _snapshot(os.fstat(descriptor)):
            raise Refused()
        return content.decode("utf-8")
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory)


def read_private_file(filename: str) -> str:
    """Suppress filesystem/decoder exception details as well as CLI diagnostics."""
    try:
        return _read_private_file(filename)
    except (OSError, ValueError, UnicodeError):
        pass
    raise Refused()


def _literal(raw: str) -> str:
    if raw.startswith(("'", '"')):
        end = raw.find(raw[0], 1)
        if end < 0 or not re.fullmatch(r"[ \t]*(?:#.*)?", raw[end + 1:]):
            raise Refused()
        value = raw[1:end]
    else:
        match = re.fullmatch(r"([^ \t]*)(?:[ \t]+(?:#.*)?)?", raw)
        if not match:
            raise Refused()
        value = match[1]
        if "'" in value or '"' in value:
            raise Refused()
    if any(char in value for char in "$`\\;&|<>"):
        raise Refused()
    return value


def selected_token(content: str, variable: str) -> str | None:
    """Accept literal assignments only; reject duplicate or malformed entries."""
    if not NAME.fullmatch(variable):
        raise Refused()
    if any((ord(char) < 32 and char not in "\n\t") or ord(char) == 127
           for char in content):
        raise Refused()
    seen = set()
    selected = None
    for line in content.split("\n"):
        stripped = line.strip(" \t")
        if not stripped or stripped.startswith("#"):
            continue
        match = ASSIGNMENT.fullmatch(stripped)
        if not match or match[1] in seen:
            raise Refused()
        seen.add(match[1])
        value = _literal(match[2])
        if match[1] == variable:
            if not value:
                continue
            if not TOKEN.fullmatch(value):
                raise Refused()
            selected = value
    return selected


def main(argv=None) -> int:
    parser = Parser(description=__doc__)
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--variable", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="Report configured boolean only (default)")
    mode.add_argument("--emit-headers", action="store_true", help="Write secret JSON to the client pipe")
    try:
        args = parser.parse_args(argv)
        if args.emit_headers and sys.stdout.isatty():
            raise Refused()
        token = selected_token(read_private_file(args.env_file), args.variable)
        if args.emit_headers:
            if token is None:
                raise Refused()
            result = {"Authorization": "Bearer " + token}
        else:
            result = {"configured": token is not None}
        print(json.dumps(result))
        return 0
    except (OSError, ValueError, UnicodeError):
        print(ERROR, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
