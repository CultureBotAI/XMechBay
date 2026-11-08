#!/usr/bin/env python3
"""List directories the tmp-state guard cannot read in its roots, with the error kind.

Purpose: explain "walk_errors" in a tmp-state guard report (tmp-state-guard PR,
2026-11-08): whether they are permission bits (EACCES) or macOS privacy/sandbox
protection (EPERM), and under which top-level entry.
Inputs: optional root paths (default /private/tmp and DARWIN_USER_TEMP_DIR).
Outputs: TSV on stdout: errno name, top-level entry, depth, owner-is-me. Read-only;
it only lists directories (no file contents, no git, no network). Skips the same
top-level entries the guard skips (symlinks, other owners, default exclusions).
"""

import errno
import fnmatch
import os
import subprocess
import sys

EXCLUDES = ("pytest-of-*", "pytest-cache-files-*", "__pycache__", ".mypy_cache", ".pytest_cache",
            ".ruff_cache", "node_modules", ".venv", "uv-cache", ".uv-cache", "uv-cache-*", ".uv_cache",
            "node-compile-cache")
TOP_EXCLUDES = ("com.apple.*",)


def roots():
    if len(sys.argv) > 1:
        return sys.argv[1:]
    temp = subprocess.run(["/usr/bin/getconf", "DARWIN_USER_TEMP_DIR"], capture_output=True, text=True).stdout.strip()
    return ["/private/tmp", os.path.realpath(temp.rstrip("/"))]


def main():
    uid = os.getuid()
    print("errno\troot\ttop_level_entry\tdepth")
    for root in roots():
        device = os.stat(root).st_dev
        for name in sorted(os.listdir(root)):
            path = os.path.join(root, name)
            info = os.lstat(path)
            if (os.path.islink(path) or info.st_uid != uid or info.st_dev != device or not os.path.isdir(path)
                    or any(fnmatch.fnmatchcase(name, p) for p in EXCLUDES + TOP_EXCLUDES)):
                continue
            stack = [(path, 1)]
            while stack:
                directory, depth = stack.pop()
                try:
                    with os.scandir(directory) as iterator:
                        items = list(iterator)
                except OSError as error:
                    print("%s\t%s\t%s\t%d" % (errno.errorcode.get(error.errno, "?"), root, name, depth))
                    continue
                for item in items:
                    try:
                        if (item.is_dir(follow_symlinks=False) and item.stat(follow_symlinks=False).st_dev == device
                                and not any(fnmatch.fnmatchcase(item.name, p) for p in EXCLUDES)):
                            stack.append((item.path, depth + 1))
                    except OSError as error:
                        print("%s\t%s\t%s\t%d" % (errno.errorcode.get(error.errno, "?"), root, name, depth + 1))


if __name__ == "__main__":
    main()
