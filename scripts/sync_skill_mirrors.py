#!/usr/bin/env python3
"""Copy canonical .agents skills to .claude, or check parity (2026-10-25, symlink-aware 2026-10-28).

Inputs: all files, including hidden and gitignored entries, in both skill trees.
Output: mirror differences on stdout; updated .claude files unless --check is set.
When .claude/skills is a symlink that resolves to .agents/skills the trees are
one directory (the layout in docs/agent-infra-contract.md): parity holds and
nothing is copied. Any other symlink, and unexpected destination entries,
require manual review, never deletion.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]


def inventory(root: Path) -> dict[Path, str]:
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"Expected a real skill directory: {root}")
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"Skill symlink requires manual review: {path}")
        result[path.relative_to(root)] = "directory" if path.is_dir() else "file"
    return result


def is_unified_symlink(source: Path, target: Path) -> bool:
    """True when target is a symlink resolving to the real source directory."""
    try:
        return target.is_symlink() and source.is_dir() and not source.is_symlink() and target.resolve() == source.resolve()
    except OSError:
        return False


def synchronize(source: Path, target: Path, check: bool) -> int:
    if is_unified_symlink(source, target):
        print(f"Skill mirrors are one directory: {target} -> {source}.")
        return 0
    if is_unified_symlink(target, source):
        print(f"Skill mirrors are one directory: {source} -> {target} (reverse direction).")
        return 0
    source_entries = inventory(source)
    target_entries = inventory(target)
    blockers = []
    for path, kind in target_entries.items():
        if path not in source_entries:
            blockers.append(f"Only in {target}: {path}")
        elif source_entries[path] != kind:
            blockers.append(f"File/directory mismatch: {path}")
    if blockers:
        print("\n".join(blockers), file=sys.stderr)
        return 1

    changed = []
    for path, kind in source_entries.items():
        if path not in target_entries:
            changed.append((path, kind))
        elif kind == "file" and (
            (source / path).read_bytes() != (target / path).read_bytes()
            or ((source / path).stat().st_mode & 0o111)
            != ((target / path).stat().st_mode & 0o111)
        ):
            changed.append((path, kind))
    for path, kind in changed:
        print(f"{'Different' if check else 'Copying'}: {path}")
        if not check:
            if kind == "directory":
                (target / path).mkdir(parents=True, exist_ok=True)
            else:
                (target / path).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source / path, target / path)
    if check and changed:
        return 1
    print("Skill mirrors match (including hidden and gitignored entries).")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Read-only parity check")
    args = parser.parse_args()
    try:
        return synchronize(ROOT / ".agents/skills", ROOT / ".claude/skills", args.check)
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
