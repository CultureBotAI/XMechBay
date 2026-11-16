#!/usr/bin/env python3
"""Move one repository to the agent-infrastructure target layout (2026-10-28).

Target (docs/agent-infra-contract.md): AGENTS.md is the canonical instruction
file, CLAUDE.md is a symlink to it, there is no GEMINI.md, one skills tree is a
real directory and the other path is a symlink to it (--skills-canonical picks
which; default .agents/skills).

Inputs: a repository checkout. Dry run by default: prints the plan and changes
nothing. The plan is collected and validated in full before the first change,
so a refusal (drifted copies or divergent files without --skills-from or
--instructions-from, a broken or absolute link, a nested .git inside a dropped
tree) leaves the checkout untouched. --apply performs the plan with `git mv`
and `git rm` and never commits; untracked and ignored files inside a moved or
dropped tree are moved into the kept tree and listed in the plan. Pointer-file
text that is not the "read the other file" sentence is appended to AGENTS.md
verbatim under a "carried" heading, and every dropped sentence is printed.
Output: the plan, then `git status --short` when applied.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import textwrap
from typing import Callable

AUDIT = Path(__file__).resolve().with_name("agent_infra_audit.py")
_spec = importlib.util.spec_from_file_location("agent_infra_audit", AUDIT)
audit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(audit)

READ_RE = re.compile(r"\b(read|follow|see)\b", re.IGNORECASE)
STRUCTURAL = re.compile(r"^(\s*(\d+[.)]|[|>])\s|\s{4,}\S|```|~~~)")
INSTRUCTION_PATHS = ("AGENTS.md", "CLAUDE.md", "GEMINI.md", ".agents/skills", ".claude/skills")


def git_lines(repo: Path, *args: str) -> list[str]:
    out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=False)
    return out.stdout.split("\0") if "-z" in args else out.stdout.splitlines()


class Plan:
    """Steps are collected first (reads and validation), executed later."""

    def __init__(self, repo: Path, use_git: bool) -> None:
        self.repo, self.use_git = repo, use_git
        self.steps: list[tuple[str, Callable[[], None]]] = []
        self.notes: list[str] = []
        self._tracked = set(git_lines(repo, "ls-files", "-z")) - {""} if use_git else set()

    # ----- collection helpers -------------------------------------------------
    def add(self, desc: str, fn: Callable[[], None]) -> None:
        self.steps.append((desc, fn))

    def git(self, *args: str) -> None:
        subprocess.run(["git", "-C", str(self.repo), *args], check=True)

    def tracked_name(self, rel: str) -> str | None:
        """The tracked path matching `rel`, also on a case-insensitive filesystem."""
        if rel in self._tracked or any(t.startswith(rel + "/") for t in self._tracked):
            return rel
        low = rel.lower()
        for t in self._tracked:
            if t.lower() == low:
                return t
            if t.lower().startswith(low + "/"):
                return t[: len(rel)]
        return None

    def leftovers(self, rel: str) -> list[str]:
        """Untracked and ignored paths under rel (git only), repo-relative."""
        if not self.use_git:
            return []
        found = []
        for line in git_lines(self.repo, "status", "--porcelain", "--ignored", "-z", "--", rel):
            if line[:2] in ("??", "!!"):
                found.append(line[3:].rstrip("/"))
        return sorted(found)

    # ----- steps ----------------------------------------------------------------
    def remove(self, rel: str, keep_into: str | None = None) -> None:
        extras = self.leftovers(rel)
        for extra in extras:
            if (self.repo / extra).name == ".git" or (self.repo / extra / ".git").exists():
                raise SystemExit(f"refusing: a git repository lives inside {rel} ({extra}); move it first")
        desc = f"remove {rel}"
        if extras:
            desc += f" (keeping {len(extras)} untracked/ignored path(s) under {keep_into or rel}: {', '.join(extras[:5])}{'...' if len(extras) > 5 else ''})"
            if keep_into is None:
                raise SystemExit(f"refusing: {rel} holds untracked or ignored files and nothing keeps them: {', '.join(extras[:5])}")

        def run() -> None:
            path = self.repo / rel
            tracked = self.tracked_name(rel)
            if self.use_git and tracked:
                self.git("rm", "-r", "-q", "-f", tracked)
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path.is_dir():
                if self.use_git:
                    self._merge_leftovers(path, self.repo / keep_into)
                else:
                    shutil.rmtree(path)

        self.add(desc, run)

    def move(self, src: str, dst: str) -> None:
        extras = self.leftovers(src)
        desc = f"move {src} -> {dst}" + (f" (with {len(extras)} untracked/ignored path(s))" if extras else "")

        def run() -> None:
            (self.repo / dst).parent.mkdir(parents=True, exist_ok=True)  # git rm prunes emptied parents
            tracked = self.tracked_name(src)
            if self.use_git and tracked:
                self.git("mv", tracked, dst)
                if (self.repo / src).exists():
                    self._merge_leftovers(self.repo / src, self.repo / dst)
            else:
                (self.repo / src).rename(self.repo / dst)
                if self.use_git:
                    self.git("add", dst)

        self.add(desc, run)

    @staticmethod
    def _merge_leftovers(src: Path, dst: Path) -> None:
        dst.mkdir(parents=True, exist_ok=True)
        for path in sorted(src.rglob("*")):
            target = dst / path.relative_to(src)
            if path.is_dir() and not path.is_symlink():
                target.mkdir(parents=True, exist_ok=True)
            elif target.exists() or target.is_symlink():
                raise RuntimeError(f"not overwriting {target} with the leftover {path}")
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                os.rename(path, target)
        shutil.rmtree(src)

    def symlink(self, rel: str, target: str) -> None:
        def run() -> None:
            path = self.repo / rel
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path.exists():
                raise RuntimeError(f"{rel} is still a directory; an earlier step should have removed it")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.symlink_to(target)
            if self.use_git:
                self.git("add", rel)

        self.add(f"symlink {rel} -> {target}", run)

    def append(self, rel: str, heading: str, lines: list[str]) -> None:
        def run() -> None:
            path = self.repo / rel
            text = path.read_text(encoding="utf-8")
            if not text.endswith("\n"):
                text += "\n"
            path.write_text(text + f"\n## {heading}\n\n" + "\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")
            if self.use_git:
                self.git("add", rel)

        self.add(f"append {len(lines)} carried line(s) to {rel} under '{heading}'", run)

    # ----- execution ------------------------------------------------------------
    def show(self, apply: bool) -> None:
        for desc, _ in self.steps:
            print(("APPLY " if apply else "PLAN  ") + desc)
        for note in self.notes:
            print("NOTE  " + note)

    def execute(self) -> None:
        for _, fn in self.steps:
            fn()


# ----- carried text -------------------------------------------------------------
def is_pointer_sentence(sentence: str, other: str) -> bool:
    return other.lower() in sentence.lower() and bool(READ_RE.search(sentence))


def carried_lines(path: Path, other: str) -> list[str]:
    """The file's lines verbatim, minus its H1 and the 'read <other>' sentences.

    Only plain prose paragraphs and dash bullets are filtered (and re-wrapped
    when a sentence was removed); fenced code, tables, quotes, numbered lists
    and indented blocks pass through unchanged.
    """
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    out: list[str] = []
    i, fence = 0, False
    while i < len(lines):
        line = lines[i]
        if line.lstrip().startswith(("```", "~~~")):
            fence = not fence
            out.append(line)
            i += 1
            continue
        if fence or STRUCTURAL.match(line) or not line.strip() or line.startswith("#"):
            if not line.startswith("# "):
                out.append(line.rstrip())
            i += 1
            continue
        block = [line.rstrip()]
        i += 1
        while i < len(lines) and lines[i].strip() and not lines[i].lstrip().startswith(("```", "~~~", "- ", "* ", "#")) and not STRUCTURAL.match(lines[i]):
            block.append(lines[i].rstrip())
            i += 1
        joined = " ".join(l.strip() for l in block)
        prefix = ""
        if joined.startswith(("- ", "* ")):
            prefix, joined = joined[:2], joined[2:]
        sentences = [s for s in re.split(r"(?<=[.!?])\s+(?=[A-Z`\[])", joined) if s]
        dropped = [s for s in sentences if is_pointer_sentence(s, other)]
        for s in dropped:
            print(f"DROP  from {path.name}: {s}")
        if not dropped:
            out.extend(block)
        else:
            remaining = [s for s in sentences if s not in dropped]
            if remaining:
                out.extend(textwrap.wrap(" ".join(remaining), width=80, initial_indent=prefix, subsequent_indent="  " if prefix else "", break_long_words=False, break_on_hyphens=False))
    # collapse leading/trailing/duplicate blank lines
    cleaned: list[str] = []
    for line in out:
        if not line.strip() and (not cleaned or not cleaned[-1].strip()):
            continue
        cleaned.append(line)
    while cleaned and not cleaned[-1].strip():
        cleaned.pop()
    return cleaned


# ----- planning -----------------------------------------------------------------
def converge_skills(plan: Plan, info: dict, skills_from: str | None, flip_reverse: bool, canonical: str) -> None:
    other = ".claude/skills" if canonical == ".agents/skills" else ".agents/skills"
    link_target = "../" + canonical.split("/")[0] + "/skills"
    verdict = info["verdict"]
    if verdict == "none":
        plan.notes.append("skills: none present, nothing to do")
    elif verdict in ("broken-link", "absolute-link"):
        raise SystemExit(f"skills: {verdict} ({info['kinds']}); fix the link by hand first")
    elif verdict == "unified-symlink":
        if info["canonical"] == canonical:
            plan.notes.append(f"skills: already unified (canonical {canonical})")
        elif flip_reverse:
            plan.remove(canonical)
            plan.move(other, canonical)
            plan.symlink(other, link_target)
        else:
            plan.notes.append(f"skills: unified with canonical {info['canonical']}; pass --flip-reverse to make {canonical} the real directory")
    elif verdict in ("copies-identical", "copies-drifted", "divergent"):
        source = skills_from or (canonical if verdict == "copies-identical" else None)
        if source is None:
            detail = "; ".join(f"{k}: {', '.join(v[:4])}{'...' if len(v) > 4 else ''}" for k, v in info.get("drift_detail", {}).items())
            raise SystemExit(f"skills are {verdict} ({detail or 'see audit'}); read both copies and pass --skills-from .claude/skills or .agents/skills")
        dropped = ".claude/skills" if source == ".agents/skills" else ".agents/skills"
        if verdict == "copies-drifted":
            for name, paths in info.get("drift_detail", {}).items():
                plan.notes.append(f"skills: {name} differs in {', '.join(paths)}")
            plan.notes.append(f"skills: the {dropped} copies are dropped; the {source} copies are kept")
        plan.remove(dropped, keep_into=canonical)
        if source != canonical:
            plan.move(source, canonical)
        plan.symlink(other, link_target)
    elif verdict in ("claude-only", "agents-only"):
        present = ".claude/skills" if verdict == "claude-only" else ".agents/skills"
        if present != canonical:
            plan.move(present, canonical)
        plan.symlink(other, link_target)
    else:
        raise SystemExit(f"unhandled skills verdict {verdict}")


def converge_instructions(plan: Plan, info: dict, instructions_from: str | None, keep_gemini: bool, flip_reverse: bool) -> None:
    verdict, canonical = info["verdict"], info["canonical"]
    repo = plan.repo
    gemini_kind = info.get("gemini")
    if verdict in ("broken-link", "absolute-link"):
        raise SystemExit(f"instructions: {verdict} ({info['kinds']}); fix the link by hand first")
    if verdict == "none":
        if gemini_kind == "file" and not keep_gemini:
            plan.notes.append("instructions: only GEMINI.md exists; it becomes AGENTS.md")
            plan.move("GEMINI.md", "AGENTS.md")
            plan.symlink("CLAUDE.md", "AGENTS.md")
            return
        plan.notes.append("instructions: no AGENTS.md or CLAUDE.md, nothing to do")
    elif verdict == "unified" and canonical == "AGENTS.md":
        plan.notes.append("instructions: already unified on AGENTS.md")
    elif verdict == "unified" and not flip_reverse:
        plan.notes.append("instructions: unified in the reverse direction (AGENTS.md -> CLAUDE.md); pass --flip-reverse to flip")
    elif verdict == "unified":
        plan.remove("AGENTS.md")
        plan.move("CLAUDE.md", "AGENTS.md")
        plan.symlink("CLAUDE.md", "AGENTS.md")
    elif verdict == "agents-only":
        plan.symlink("CLAUDE.md", "AGENTS.md")
    elif verdict == "claude-only":
        plan.move("CLAUDE.md", "AGENTS.md")
        plan.symlink("CLAUDE.md", "AGENTS.md")
    elif verdict in ("pointer", "copies-identical", "divergent"):
        source = instructions_from or (canonical if verdict == "pointer" else "AGENTS.md" if verdict == "copies-identical" else None)
        if source is None:
            raise SystemExit("AGENTS.md and CLAUDE.md are divergent; read both and pass --instructions-from CLAUDE.md or AGENTS.md")
        other = "CLAUDE.md" if source == "AGENTS.md" else "AGENTS.md"
        if verdict == "copies-identical":
            carried: list[str] = []
        elif verdict == "divergent":
            plan.notes.append(f"instructions: keeping {source}; the whole former {other} is carried for review")
            carried = [l.rstrip() for l in (repo / other).read_text(encoding="utf-8", errors="replace").splitlines()]
        else:
            carried = carried_lines(repo / other, source)
        plan.remove(other)
        if source == "CLAUDE.md":
            plan.move("CLAUDE.md", "AGENTS.md")
        if carried:
            plan.append("AGENTS.md", f"Carried from the former {other} (review and trim)", carried)
        plan.symlink("CLAUDE.md", "AGENTS.md")
    else:
        raise SystemExit(f"unhandled instructions verdict {verdict}")

    if gemini_kind and verdict != "none":
        if keep_gemini:
            plan.notes.append("instructions: GEMINI.md kept on request (agy reads it in addition to AGENTS.md)")
        elif gemini_kind == "pointer":
            carried = carried_lines(repo / "GEMINI.md", "AGENTS.md")
            carried = [l for l in carried if not is_pointer_sentence(l, "CLAUDE.md")]
            plan.remove("GEMINI.md")
            if carried:
                plan.append("AGENTS.md", "Antigravity (agy) notes carried from the former GEMINI.md", carried)
        elif gemini_kind.startswith("symlink"):
            plan.remove("GEMINI.md")
        else:
            raise SystemExit("GEMINI.md is a full file, not a pointer; merge it into AGENTS.md by hand or pass --keep-gemini")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("repo", type=Path)
    parser.add_argument("--apply", action="store_true", help="Perform the plan (default: print it)")
    parser.add_argument("--no-git", action="store_true", help="Use filesystem operations instead of git mv/rm (the branch and dirty-tree guards still apply inside a git checkout)")
    parser.add_argument("--skills-from", choices=(".claude/skills", ".agents/skills"), help="Which copies to keep when the two skill trees differ")
    parser.add_argument("--skills-canonical", choices=(".agents/skills", ".claude/skills"), default=".agents/skills", help="Which path is the real directory; the other becomes a symlink to it (fleets whose hub lists .claude/skills with git ls-files keep .claude/skills)")
    parser.add_argument("--instructions-from", choices=("CLAUDE.md", "AGENTS.md"), help="Which file to keep when AGENTS.md and CLAUDE.md are divergent")
    parser.add_argument("--keep-gemini", action="store_true")
    parser.add_argument("--flip-reverse", action="store_true", help="Also flip compliant reverse symlinks (skills to --skills-canonical, instructions to AGENTS.md)")
    parser.add_argument("--force", action="store_true", help="Apply even when untracked files sit under the rewritten paths (they are moved into the kept tree); modified tracked content always refuses")
    args = parser.parse_args(argv)

    repo = args.repo.resolve()
    if not repo.is_dir():
        parser.error(f"not a directory: {repo}")
    is_git = (repo / ".git").exists()
    use_git = is_git and not args.no_git
    if is_git and args.apply:
        branch = subprocess.run(["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True).stdout.strip()
        if branch in ("main", "master"):
            print("refusing to apply on main/master; create a topic branch first", file=sys.stderr)
            return 2
        dirty = subprocess.run(["git", "-C", str(repo), "status", "--porcelain", "--", *INSTRUCTION_PATHS], capture_output=True, text=True).stdout.strip()
        modified_tracked = [line for line in dirty.splitlines() if not line.startswith("??")]
        if modified_tracked:
            print("refusing to apply: modified tracked content under the paths this script rewrites (commit or stash it first):\n" + "\n".join(modified_tracked), file=sys.stderr)
            return 2
        if dirty and not args.force:
            print("refusing to apply: untracked files under the paths this script rewrites (move them, or pass --force to carry them into the kept tree):\n" + dirty, file=sys.stderr)
            return 2

    before = audit.audit_repo(repo)
    plan = Plan(repo, use_git)
    converge_skills(plan, before["skills"], args.skills_from, args.flip_reverse, args.skills_canonical)
    converge_instructions(plan, before["instructions"], args.instructions_from, args.keep_gemini, args.flip_reverse)
    plan.show(args.apply)
    if not args.apply:
        print(f"\ndry run: {len(plan.steps)} step(s) planned; re-run with --apply on a topic branch")
        return 0
    plan.execute()
    after = audit.audit_repo(repo)
    print("\nafter: " + audit.instruction_summary(after["instructions"]) + " | " + audit.skills_summary(after["skills"]) + (" | OK" if after["ok"] else " | still FIX"))
    if use_git:
        subprocess.run(["git", "-C", str(repo), "status", "--short"], check=False)
    return 0 if after["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
