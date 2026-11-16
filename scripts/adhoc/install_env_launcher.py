#!/usr/bin/env python3
"""Plan/install a launcher for a reviewed local environment loader, 2026-11-16.

Inputs: explicit executable, runtime, destination and credential-file paths.
Output: a JSON plan/receipt and, only with --apply, one mode0700 shell launcher
plus an exclusive adjacent backup when replacing a file. No credential content
is read, no runtime is executed, and no profile, PATH or session is changed.
Serves the generic environment-launcher feature; fixtures must be synthetic.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import uuid


MAX_LAUNCHER_BYTES = 1024 * 1024


class Refused(ValueError):
    """A precondition failed; do not broaden the requested installation."""


def absolute_path(value):
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or "\x00" in str(path):
        raise Refused("Paths must be absolute and must not contain '..' or NUL")
    return path


def source_file(value, executable=False):
    path = absolute_path(value)
    if not executable and path.is_symlink():
        raise Refused("Runtime must not be a symlink")
    # Package-manager interpreter symlinks are supported; pin their real file.
    resolved = path.resolve(strict=True)
    info = resolved.stat()
    if not stat.S_ISREG(info.st_mode):
        raise Refused("Interpreter and runtime must be existing regular files")
    if executable and not os.access(resolved, os.X_OK):
        raise Refused("Interpreter is not executable")
    return resolved


@contextmanager
def parent_directory(destination):
    """Walk existing directories with no-follow, keeping the final dir pinned."""
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in destination.parent.parts[1:]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o022:
            raise Refused("Destination parent must be owned by this user and not group/world writable")
        yield fd
    finally:
        os.close(fd)


def same_parent(destination, fd):
    with parent_directory(destination) as current:
        a, b = os.fstat(fd), os.fstat(current)
        if (a.st_dev, a.st_ino) != (b.st_dev, b.st_ino):
            raise Refused("Destination parent changed during installation")


def fingerprint(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, info.st_mode, info.st_uid, info.st_nlink)


def read_destination(fd, name):
    try:
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return None, None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise Refused("Destination must be a regular file with one link, never a symlink")
    if info.st_uid != os.geteuid() or info.st_size > MAX_LAUNCHER_BYTES:
        raise Refused("Destination must be owned by this user and no larger than 1 MiB")
    opened = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    with os.fdopen(opened, "rb") as stream:
        if fingerprint(os.fstat(stream.fileno())) != fingerprint(info):
            raise Refused("Destination changed before it could be read")
        content = stream.read(MAX_LAUNCHER_BYTES + 1)
        if fingerprint(os.fstat(stream.fileno())) != fingerprint(info):
            raise Refused("Destination changed while being read")
    if len(content) != info.st_size:
        raise Refused("Destination size changed while being read")
    return content, info


def absent(fd, name):
    try:
        os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    raise Refused("Backup already exists; refusing to overwrite it")


def unchanged(fd, name, content, info):
    current, observed = read_destination(fd, name)
    if current != content or ((fingerprint(observed) if observed else None)
                              != (fingerprint(info) if info else None)):
        raise Refused("Destination changed during installation")


def exclusive_write(fd, name, content, mode):
    opened = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=fd)
    with os.fdopen(opened, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fchmod(stream.fileno(), mode)
        os.fsync(stream.fileno())


def launcher_bytes(interpreter, runtime, tdm_env, cborg_shells):
    arguments = [str(interpreter), str(runtime), "--tdm-env", str(tdm_env)]
    for path in cborg_shells:
        arguments.extend(("--cborg-shell", str(path)))
    return ("#!/bin/sh\n# Generated environment launcher; contains paths only.\nexec "
            + " ".join(shlex.quote(value) for value in arguments) + ' "$@"\n').encode()


def install(destination, interpreter, runtime, tdm_env, cborg_shells, backup_tag, apply=False):
    destination = absolute_path(destination)
    interpreter, runtime = source_file(interpreter, True), source_file(runtime)
    tdm_env = absolute_path(tdm_env)
    cborg_shells = [absolute_path(path) for path in cborg_shells]
    if not cborg_shells:
        raise Refused("At least one CBORG shell path is required")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*-[0-9]{8}", backup_tag):
        raise Refused("Backup tag must contain letters/digits/hyphens and end in -YYYYMMDD")
    # Metadata-only resolution prevents installing over a loader or credential
    # file, including an alias through an existing symlink. Never open them.
    if destination.resolve(strict=False) in {
        path.resolve(strict=False) for path in [interpreter, runtime, tdm_env, *cborg_shells]
    }:
        raise Refused("Destination collides with an interpreter, runtime or credential path")
    content = launcher_bytes(interpreter, runtime, tdm_env, cborg_shells)
    if len(content) > MAX_LAUNCHER_BYTES:
        raise Refused("Generated launcher is too large")
    backup_name = destination.name + ".bak-" + backup_tag
    with parent_directory(destination) as fd:
        previous, info = read_destination(fd, destination.name)
        changed = previous != content or info is None or stat.S_IMODE(info.st_mode) != 0o700
        action = "unchanged" if not changed else ("created" if info is None else "updated")
        backup = destination.with_name(backup_name) if info is not None and changed else None
        if backup:
            absent(fd, backup_name)
        receipt = {"applied": apply, "action": action, "destination": str(destination),
                   "backup": str(backup) if backup else None, "mode": "0700",
                   "sha256": hashlib.sha256(content).hexdigest()}
        if not apply or not changed:
            return receipt
        same_parent(destination, fd)
        unchanged(fd, destination.name, previous, info)
        if backup:
            exclusive_write(fd, backup_name, previous, stat.S_IMODE(info.st_mode) & 0o777)
            saved, _ = read_destination(fd, backup_name)
            if saved != previous:
                raise Refused("Backup verification failed")
        temporary = ".env-launcher-" + uuid.uuid4().hex
        try:
            exclusive_write(fd, temporary, content, 0o700)
            same_parent(destination, fd)
            unchanged(fd, destination.name, previous, info)
            if info is None:
                # link is an atomic no-clobber publish for a new destination.
                os.link(temporary, destination.name, src_dir_fd=fd, dst_dir_fd=fd,
                        follow_symlinks=False)
                os.unlink(temporary, dir_fd=fd)
            else:
                os.replace(temporary, destination.name, src_dir_fd=fd, dst_dir_fd=fd)
            same_parent(destination, fd)
            persisted, installed = read_destination(fd, destination.name)
            if persisted != content or stat.S_IMODE(installed.st_mode) != 0o700:
                raise Refused("Installed launcher failed byte/mode verification")
        finally:
            try:
                os.unlink(temporary, dir_fd=fd)
            except FileNotFoundError:
                pass
        return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", required=True)
    parser.add_argument("--interpreter", required=True)
    parser.add_argument("--runtime", required=True)
    parser.add_argument("--tdm-env", required=True)
    parser.add_argument("--cborg-shell", action="append", required=True)
    parser.add_argument("--backup-tag", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = install(args.destination, args.interpreter, args.runtime, args.tdm_env,
                         args.cborg_shell, args.backup_tag, args.apply)
    except (OSError, ValueError, RuntimeError) as exc:
        reason = str(exc) if isinstance(exc, Refused) else "Filesystem precondition or operation failed"
        parser.exit(2, "install_env_launcher: " + reason + "\n")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
