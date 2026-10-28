#!/usr/bin/env python3
"""Audit agent instruction files and skill trees for Claude Code, Codex and agy (2026-10-28).

Inputs: repository paths (positional) or --scan ROOT [--max-depth N] to find Git
checkouts; --global to audit the user-level files instead. Reads only; follows
no symlink outside the repository for content, reports link targets as text.
Output: a text table (default) or --json. Exit 1 when any audited repository is
not in the target state described in docs/agent-infra-contract.md.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md", "GEMINI.md")
SKILL_DIRS = (".agents/skills", ".claude/skills")
COMMON_BEGIN = "<!-- common-agreements:begin -->"
COMMON_END = "<!-- common-agreements:end -->"
POINTER_MAX_LINES = 12
DEFAULT_GLOBAL_FILES = (
    "~/.claude/CLAUDE.md",
    "~/.codex/AGENTS.md",
    "~/.codex-pro/AGENTS.md",
    "~/.gemini/AGENTS.md",
)


def kind_of(path: Path) -> str:
    """'missing', 'file', 'dir', 'symlink:<target>' or 'broken-symlink:<target>'."""
    if path.is_symlink():
        target = os.readlink(path)
        return f"{'symlink' if path.exists() else 'broken-symlink'}:{target}"
    if not path.exists():
        return "missing"
    return "dir" if path.is_dir() else "file"


def same_file(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve() and a.exists()
    except OSError:
        return False


def tree_digest(root: Path) -> dict[str, str]:
    """Relative path -> sha256 (files) or 'dir', including hidden entries."""
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        if path.is_symlink():
            result[rel] = "symlink:" + os.readlink(path)
        elif path.is_dir():
            result[rel] = "dir"
        else:
            result[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def skill_names(root: Path) -> list[str]:
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir() and (p / "SKILL.md").is_file())


def is_pointer(path: Path, other_name: str) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    lines = [line for line in text.splitlines() if line.strip()]
    return len(lines) <= POINTER_MAX_LINES and other_name in text


def classify_instructions(repo: Path) -> dict:
    kinds = {name: kind_of(repo / name) for name in INSTRUCTION_FILES}
    agents, claude, gemini = (repo / n for n in INSTRUCTION_FILES)
    verdict, canonical = "none", None
    if any(k.startswith("broken-symlink") for k in (kinds["AGENTS.md"], kinds["CLAUDE.md"])):
        verdict = "broken-link"
    elif kinds["AGENTS.md"] != "missing" and kinds["CLAUDE.md"] != "missing":
        if kinds["CLAUDE.md"].startswith("symlink") and same_file(claude, agents):
            verdict, canonical = "unified", "AGENTS.md"
        elif kinds["AGENTS.md"].startswith("symlink") and same_file(agents, claude):
            verdict, canonical = "unified", "CLAUDE.md"
        elif kinds["AGENTS.md"] == "file" and kinds["CLAUDE.md"] == "file":
            if agents.read_bytes() == claude.read_bytes():
                verdict = "copies-identical"
            elif is_pointer(agents, "CLAUDE.md") and not is_pointer(claude, "AGENTS.md"):
                verdict, canonical = "pointer", "CLAUDE.md"
            elif is_pointer(claude, "AGENTS.md") and not is_pointer(agents, "CLAUDE.md"):
                verdict, canonical = "pointer", "AGENTS.md"
            else:
                verdict = "divergent"
        else:
            verdict = "divergent"
    elif kinds["AGENTS.md"] != "missing":
        verdict, canonical = "agents-only", "AGENTS.md"
    elif kinds["CLAUDE.md"] != "missing":
        verdict, canonical = "claude-only", "CLAUDE.md"
    result = {"kinds": kinds, "verdict": verdict, "canonical": canonical}
    if kinds["GEMINI.md"] != "missing":
        result["gemini"] = "pointer" if kinds["GEMINI.md"] == "file" and is_pointer(gemini, "AGENTS.md") else kinds["GEMINI.md"].split(":")[0]
    return result


def classify_skills(repo: Path) -> dict:
    agents, claude = (repo / d for d in SKILL_DIRS)
    kinds = {d: kind_of(repo / d) for d in SKILL_DIRS}
    names = {d: skill_names(repo / d) for d in SKILL_DIRS}
    drifted: list[str] = []
    if any(kinds[d].startswith("broken-symlink") for d in SKILL_DIRS):
        verdict, canonical = "broken-link", None
    elif kinds[".agents/skills"] == "missing" and kinds[".claude/skills"] == "missing":
        verdict, canonical = "none", None
    elif kinds[".claude/skills"].startswith("symlink") and same_file(claude, agents):
        verdict, canonical = "unified-symlink", ".agents/skills"
    elif kinds[".agents/skills"].startswith("symlink") and same_file(agents, claude):
        verdict, canonical = "unified-symlink", ".claude/skills"
    elif kinds[".agents/skills"] == "dir" and kinds[".claude/skills"] == "dir":
        a, c = tree_digest(agents), tree_digest(claude)
        if a == c:
            verdict, canonical = "copies-identical", ".agents/skills"
        else:
            verdict, canonical = "copies-drifted", None
            drifted = sorted({p.split("/")[0] for p in set(a) ^ set(c)} | {p.split("/")[0] for p in set(a) & set(c) if a[p] != c[p]})
    elif kinds[".agents/skills"] != "missing" and kinds[".claude/skills"] == "missing":
        verdict, canonical = "agents-only", ".agents/skills"
    elif kinds[".claude/skills"] != "missing" and kinds[".agents/skills"] == "missing":
        verdict, canonical = "claude-only", ".claude/skills"
    else:
        verdict, canonical = "divergent", None
    return {"kinds": kinds, "counts": {d: len(n) for d, n in names.items()}, "verdict": verdict, "canonical": canonical, "drifted": drifted}


def audit_repo(repo: Path) -> dict:
    instructions = classify_instructions(repo)
    skills = classify_skills(repo)
    ok = (
        instructions["verdict"] in ("unified", "none")
        and "gemini" not in instructions
        and skills["verdict"] in ("unified-symlink", "copies-identical", "none")
    )
    relevant = instructions["verdict"] != "none" or skills["verdict"] != "none"
    return {"repo": str(repo), "ok": ok, "relevant": relevant, "instructions": instructions, "skills": skills}


def find_repos(root: Path, max_depth: int, include_worktrees: bool = False) -> list[Path]:
    """Git checkouts under root; linked worktrees (a .git *file*) only on request."""
    found = []
    root = root.resolve()
    for dirpath, dirnames, _ in os.walk(root):
        depth = len(Path(dirpath).relative_to(root).parts)
        git = Path(dirpath) / ".git"
        if git.is_dir() or (include_worktrees and git.is_file()):
            found.append(Path(dirpath))
        if depth >= max_depth:
            dirnames[:] = []
        dirnames[:] = [d for d in dirnames if d != ".git"]
    return sorted(found)


def common_block(text: str) -> str | None:
    start, end = text.find(COMMON_BEGIN), text.find(COMMON_END)
    if start < 0 or end < 0 or end < start:
        return None
    return text[start + len(COMMON_BEGIN):end].strip()


def audit_global(files: list[str]) -> dict:
    rows, digests = [], set()
    for name in files:
        path = Path(name).expanduser()
        kind = kind_of(path)
        row = {"file": name, "kind": kind, "lines": None, "common": None}
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace")
            row["lines"] = len(text.splitlines())
            block = common_block(text)
            if block is not None:
                row["common"] = hashlib.sha256(block.encode()).hexdigest()[:12]
                digests.add(row["common"])
        rows.append(row)
    missing = [r["file"] for r in rows if r["common"] is None]
    return {"files": rows, "ok": not missing and len(digests) == 1, "without_common_block": missing, "distinct_common_blocks": len(digests)}


def instruction_summary(info: dict) -> str:
    text = info["verdict"]
    if text == "broken-link":
        text += "(" + ", ".join(f"{n}->{k.split(':', 1)[1]}" for n, k in info["kinds"].items() if k.startswith("broken")) + ")"
    if info["canonical"] and info["verdict"] in ("unified", "pointer"):
        text += f"({info['canonical']})" + ("(reverse)" if info["verdict"] == "unified" and info["canonical"] == "CLAUDE.md" else "")
    if "gemini" in info:
        text += f" gemini:{info['gemini']}"
    return text


def skills_summary(info: dict) -> str:
    text = info["verdict"]
    if text == "broken-link":
        text += "(" + ", ".join(f"{n}->{k.split(':', 1)[1]}" for n, k in info["kinds"].items() if k.startswith("broken")) + ")"
    if info["verdict"] == "unified-symlink" and info["canonical"] == ".claude/skills":
        text += "(reverse)"
    if info["drifted"]:
        text += "[" + ",".join(info["drifted"][:4]) + ("..." if len(info["drifted"]) > 4 else "") + "]"
    counts = info["counts"]
    return f"{text} {counts['.agents/skills']}/{counts['.claude/skills']}"


def print_table(results: list[dict], base: Path | None) -> None:
    width = max([len(display(r["repo"], base)) for r in results] + [4])
    print(f"{'repo':<{width}}  {'status':<6}  {'instructions':<34}  skills(.agents/.claude)")
    for r in results:
        print(f"{display(r['repo'], base):<{width}}  {'OK' if r['ok'] else 'FIX':<6}  {instruction_summary(r['instructions']):<34}  {skills_summary(r['skills'])}")
    fix = sum(1 for r in results if not r["ok"])
    print(f"\n{len(results)} repositories, {len(results) - fix} OK, {fix} to fix")


def display(repo: str, base: Path | None) -> str:
    if base is None:
        return repo
    try:
        return str(Path(repo).resolve().relative_to(base.resolve())) or "."
    except ValueError:
        return repo


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("repos", nargs="*", help="Repository checkouts to audit")
    parser.add_argument("--scan", type=Path, help="Find Git checkouts under this directory")
    parser.add_argument("--max-depth", type=int, default=4)
    parser.add_argument("--all", action="store_true", help="Include repositories with no agent files")
    parser.add_argument("--worktrees", action="store_true", help="With --scan, also include linked worktrees")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--global", dest="global_audit", action="store_true", help="Audit user-level files instead")
    parser.add_argument("--global-file", action="append", help="User-level file to include (repeatable)")
    args = parser.parse_args(argv)

    if args.global_audit:
        result = audit_global(args.global_file or list(DEFAULT_GLOBAL_FILES))
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            for row in result["files"]:
                print(f"{row['file']:<28} {row['kind']:<10} lines={row['lines']!s:<5} common={row['common'] or '-'}")
            print(f"\n{'OK' if result['ok'] else 'FIX'}: {result['distinct_common_blocks']} distinct common block(s); without block: {result['without_common_block'] or 'none'}")
        return 0 if result["ok"] else 1

    repos = [Path(r) for r in args.repos]
    if args.scan:
        repos.extend(find_repos(args.scan, args.max_depth, args.worktrees))
    if not repos:
        parser.error("give repository paths or --scan ROOT")
    results = [audit_repo(r) for r in repos]
    if not args.all:
        results = [r for r in results if r["relevant"]]
    if args.json:
        print(json.dumps(results, indent=2))
    else:
        print_table(results, args.scan)
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
