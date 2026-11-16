#!/usr/bin/env python3
"""Give every agent's user-level skill directory the same skills (2026-10-28).

Inputs: --tool DIR for each agent's user skill directory (defaults: Claude Code
~/.claude/skills, Codex ~/.codex/skills and ~/.codex-pro/skills, Antigravity
~/.gemini/config/skills) and --canonical DIR (default ~/.agents/skills), the one
directory that lists every skill once. Each skill's real directory stays where
it is; the canonical directory and every tool directory that lacks the skill
get a symlink to that real directory. Names excluded by default: `synced`
(claude.ai-managed) and `.system` (Codex-managed); --exclude adds to them. Only
directories holding a SKILL.md count as skills. A name that resolves to
different real directories with different content in two places is a conflict:
reported, never touched.
Output: a table (or --json); with --apply the symlinks are created and the
list of created links is appended to --manifest (default
<canonical>/.sync-user-skills.log) so they can be removed again.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import sys

DEFAULT_TOOLS = (
    str(Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")) / "skills"),
    str(Path(os.environ.get("CODEX_HOME", "~/.codex")) / "skills"),
    "~/.gemini/config/skills",
)
DEFAULT_CANONICAL = "~/.agents/skills"
DEFAULT_EXCLUDE = ("synced", ".system")


def tree_digest(root: Path) -> str:
    h = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        h.update(str(path.relative_to(root)).encode())
        if path.is_symlink():
            h.update(os.readlink(path).encode())
        elif path.is_file():
            h.update(path.read_bytes())
    return h.hexdigest()


def real_dir(entry: Path) -> Path | None:
    """The directory an entry stands for, or None when it is broken or not a directory."""
    try:
        target = entry.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    return target if target.is_dir() else None


def plan(canonical: Path, tools: list[Path], exclude: set[str]) -> dict:
    names: dict[str, dict] = {}
    for base in [canonical, *tools]:
        if not base.is_dir():
            continue
        for entry in sorted(base.iterdir()):
            if entry.name in exclude or entry.name.startswith("."):
                continue
            target = real_dir(entry)
            rec = names.setdefault(entry.name, {"real": None, "present": {}, "conflict": None, "broken": [], "skipped": []})
            if target is None:
                (rec["skipped"] if (entry.is_file() and not entry.is_symlink()) else rec["broken"]).append(str(entry))
                continue
            if not (target / "SKILL.md").is_file():
                rec["skipped"].append(str(entry))  # a directory without SKILL.md is not a skill
                continue
            rec["present"][str(base)] = str(target)
            if rec["real"] is None:
                rec["real"] = str(target)
            elif Path(rec["real"]) != target and tree_digest(Path(rec["real"])) != tree_digest(target):
                rec["conflict"] = f"{rec['real']} vs {target}"
    actions = []
    for name, rec in sorted(names.items()):
        if rec["conflict"] or rec["real"] is None:
            continue
        if rec["skipped"] and not rec["present"]:
            continue
        for base in [canonical, *tools]:
            entry = base / name
            if entry.exists() or entry.is_symlink():
                continue
            actions.append({"link": str(entry), "target": rec["real"]})
    return {"names": names, "actions": actions}


def apply(actions: list[dict], manifest: Path) -> None:
    stamp = dt.datetime.now().isoformat(timespec="seconds")
    with manifest.open("a", encoding="utf-8") as log:
        for action in actions:
            link = Path(action["link"])
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(action["target"], target_is_directory=True)
            log.write(json.dumps({"created": stamp, **action}) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--canonical", default=DEFAULT_CANONICAL)
    parser.add_argument("--tool", action="append", help="Agent skill directory (repeatable; default: the four known ones)")
    parser.add_argument("--exclude", action="append", help="Name to leave alone, in addition to the defaults (repeatable)")
    parser.add_argument("--no-default-exclude", action="store_true", help=f"Do not exclude {', '.join(DEFAULT_EXCLUDE)} by default")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--manifest", help="Where created links are logged (default <canonical>/.sync-user-skills.log)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    canonical = Path(args.canonical).expanduser()
    tools = [Path(t).expanduser() for t in (args.tool or DEFAULT_TOOLS)]
    exclude = set(args.exclude or ()) | (set() if args.no_default_exclude else set(DEFAULT_EXCLUDE))
    result = plan(canonical, tools, exclude)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        bases = [canonical, *tools]
        width = max([len(n) for n in result["names"]] + [5])
        print(f"{'skill':<{width}}  " + "  ".join(f"{b.parent.name}/{b.name}"[-18:].rjust(18) for b in bases))
        for name, rec in sorted(result["names"].items()):
            cells = []
            for b in bases:
                e = b / name
                if str(b) in rec["present"]:
                    cells.append("real" if not e.is_symlink() else "link")
                elif any(a["link"] == str(e) for a in result["actions"]):
                    cells.append("+link")
                else:
                    cells.append("broken" if str(e) in rec["broken"] else "file/no-skill" if str(e) in rec["skipped"] else "-")
            flag = "  CONFLICT " + rec["conflict"] if rec["conflict"] else ""
            print(f"{name:<{width}}  " + "  ".join(c.rjust(18) for c in cells) + flag)
        print(f"\n{len(result['actions'])} symlink(s) to create" + ("" if args.apply else " (dry run; pass --apply)"))
    conflicts = [n for n, r in result["names"].items() if r["conflict"]]
    if args.apply and result["actions"]:
        manifest = Path(args.manifest).expanduser() if args.manifest else canonical / ".sync-user-skills.log"
        canonical.mkdir(parents=True, exist_ok=True)
        apply(result["actions"], manifest)
        print(f"created {len(result['actions'])} symlink(s); logged in {manifest}")
    return 1 if conflicts else 0


if __name__ == "__main__":
    raise SystemExit(main())
