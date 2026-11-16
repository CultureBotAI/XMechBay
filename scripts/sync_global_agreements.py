#!/usr/bin/env python3
"""Keep one common block of working agreements identical across user-level agent files (2026-10-28).

Inputs: --common FILE holding the shared text, and one or more --target files
(for example ~/.claude/CLAUDE.md, ~/.codex/AGENTS.md, ~/.gemini/AGENTS.md). The
block is delimited in each target by the markers below; text outside them is the
tool-specific part and is never changed. Output: --check reports which targets
differ and exits 1; otherwise each differing target is backed up next to itself
as <file>.bak-common-<YYYYMMDD> and rewritten. A target without markers gets the
block inserted after its first heading line (or at the top).
"""

from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path
import shutil
import sys

BEGIN = "<!-- common-agreements:begin -->"
END = "<!-- common-agreements:end -->"


def render(common: str) -> str:
    return f"{BEGIN}\n{common.strip()}\n{END}\n"


def splice(text: str, common: str) -> str:
    block = render(common)
    start, end = text.find(BEGIN), text.find(END)
    if start >= 0 and end > start:
        tail = text[end + len(END):]
        return text[:start] + block + ("\n" + tail.lstrip("\n") if tail.strip() else "")
    lines = text.splitlines(keepends=True)
    if lines and lines[0].startswith("# "):
        head, rest = lines[0], "".join(lines[1:])
        return head + "\n" + block + ("\n" + rest.lstrip("\n") if rest.strip() else "")
    return block + ("\n" + text if text.strip() else "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--common", type=Path, required=True)
    parser.add_argument("--target", type=Path, action="append", required=True)
    parser.add_argument("--check", action="store_true", help="Report drift, write nothing")
    parser.add_argument("--date", default=dt.date.today().strftime("%Y%m%d"), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    common = args.common.read_text(encoding="utf-8")
    if BEGIN in common or END in common:
        print("the common file must not contain the markers", file=sys.stderr)
        return 2
    # Validate every target before writing any, so a refusal never leaves a partial set.
    work = []
    for target in args.target:
        target = target.expanduser()
        current = target.read_text(encoding="utf-8") if target.exists() else ""
        if current.count(BEGIN) != current.count(END) or current.count(BEGIN) > 1 or (BEGIN in current and current.find(BEGIN) > current.find(END)):
            print(f"malformed markers in {target}; fix by hand", file=sys.stderr)
            return 2
        wanted = splice(current, common)
        backup = target.with_name(f"{target.name}.bak-common-{args.date}") if target.exists() else None
        if wanted != current and not args.check and backup is not None and backup.exists():
            print(f"backup exists, not overwriting: {backup}", file=sys.stderr)
            return 2
        work.append((target, current, wanted, backup))
    drift = 0
    for target, current, wanted, backup in work:
        if wanted == current:
            print(f"ok       {target}")
            continue
        drift += 1
        if args.check:
            print(f"differs  {target}")
            continue
        if backup is not None:
            shutil.copy2(target, backup)
            print(f"updated  {target} (backup {backup.name})")
        else:
            print(f"created  {target}")
        target.write_text(wanted, encoding="utf-8")
    return 1 if args.check and drift else 0


if __name__ == "__main__":
    raise SystemExit(main())
