#!/usr/bin/env python3
"""Move one repository to the agent-infrastructure target layout (2026-10-28).

Target (docs/agent-infra-contract.md): AGENTS.md is the canonical instruction
file, CLAUDE.md is a symlink to it, there is no GEMINI.md, .agents/skills holds
the skills and .claude/skills is a symlink to it.

Inputs: a repository checkout. Dry run by default: prints the plan and changes
nothing. --apply performs the plan with `git mv`/`git rm` (or plain filesystem
operations with --no-git) and never commits. It refuses to choose between
drifted skill copies or divergent instruction files unless --skills-from or
--instructions-from names the copy to keep; lines of a removed pointer file that
are not the "read the other file" sentence are appended to AGENTS.md under a
"carried" heading so the reviewer can trim rather than recover them.
Output: the plan, then `git status --short` when applied.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys

AUDIT = Path(__file__).resolve().with_name("agent_infra_audit.py")
_spec = importlib.util.spec_from_file_location("agent_infra_audit", AUDIT)
audit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(audit)

READ_WORDS = ("read", "follow", "see")
INSTRUCTION_PATHS = ("AGENTS.md", "CLAUDE.md", "GEMINI.md", ".agents", ".claude")


class Plan:
    def __init__(self, repo: Path, use_git: bool, apply: bool) -> None:
        self.repo, self.use_git, self.apply = repo, use_git, apply
        self.steps: list[str] = []
        self.notes: list[str] = []

    def log(self, text: str) -> None:
        self.steps.append(text)
        print(("APPLY " if self.apply else "PLAN  ") + text)

    def git(self, *args: str) -> None:
        subprocess.run(["git", "-C", str(self.repo), *args], check=True)

    def tracked(self, rel: str) -> bool:
        if not self.use_git:
            return False
        out = subprocess.run(["git", "-C", str(self.repo), "ls-files", "--error-unmatch", rel], capture_output=True)
        return out.returncode == 0

    def remove(self, rel: str) -> None:
        self.log(f"remove {rel}")
        if not self.apply:
            return
        path = self.repo / rel
        if self.tracked(rel):
            self.git("rm", "-r", "-q", "-f", rel)
            if path.exists() or path.is_symlink():
                self._fs_remove(path)
        else:
            self._fs_remove(path)

    @staticmethod
    def _fs_remove(path: Path) -> None:
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)

    def move(self, src: str, dst: str) -> None:
        self.log(f"move {src} -> {dst}")
        if not self.apply:
            return
        (self.repo / dst).parent.mkdir(parents=True, exist_ok=True)  # git rm prunes emptied parents
        if self.tracked(src):
            self.git("mv", src, dst)
            if (self.repo / src).exists():  # untracked leftovers (ignored files) inside a moved directory
                self._merge_leftovers(self.repo / src, self.repo / dst)
        else:
            (self.repo / src).rename(self.repo / dst)

    @staticmethod
    def _merge_leftovers(src: Path, dst: Path) -> None:
        for path in list(src.rglob("*")):
            target = dst / path.relative_to(src)
            if path.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            elif not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                path.rename(target)
        shutil.rmtree(src, ignore_errors=True)

    def symlink(self, rel: str, target: str) -> None:
        self.log(f"symlink {rel} -> {target}")
        if not self.apply:
            return
        path = self.repo / rel
        if path.is_symlink() or path.exists():
            self._fs_remove(path)
        path.symlink_to(target)
        if self.use_git:
            self.git("add", rel)

    def append(self, rel: str, heading: str, lines: list[str]) -> None:
        self.log(f"append {len(lines)} carried line(s) to {rel} under '{heading}'")
        if not self.apply:
            return
        path = self.repo / rel
        text = path.read_text(encoding="utf-8")
        if not text.endswith("\n"):
            text += "\n"
        path.write_text(text + f"\n## {heading}\n\n" + "\n".join(lines) + "\n", encoding="utf-8")
        if self.use_git:
            self.git("add", rel)


def carried_lines(path: Path, other: str) -> list[str]:
    """Lines of a pointer file worth keeping: not the heading, not 'read <other>'."""
    kept = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("# "):
            continue
        lowered = stripped.lower()
        if other.lower() in lowered and any(w in lowered for w in READ_WORDS) and len(stripped) < 160:
            continue
        kept.append(line.rstrip())
    return kept


def converge_skills(plan: Plan, info: dict, skills_from: str | None, flip_reverse: bool = False) -> None:
    verdict = info["verdict"]
    if verdict == "none":
        plan.notes.append("skills: none present, nothing to do")
        return
    if verdict == "unified-symlink":
        if info["canonical"] == ".claude/skills" and flip_reverse:
            plan.remove(".agents/skills")
            plan.move(".claude/skills", ".agents/skills")
            plan.symlink(".claude/skills", "../.agents/skills")
        else:
            plan.notes.append(f"skills: already unified (canonical {info['canonical']})")
        return
    if verdict in ("copies-identical", "copies-drifted", "divergent"):
        source = skills_from or (".agents/skills" if verdict == "copies-identical" else None)
        if source is None:
            raise SystemExit(f"skills are {verdict} ({', '.join(info['drifted']) or 'see audit'}); pass --skills-from .claude/skills or .agents/skills")
        other = ".claude/skills" if source == ".agents/skills" else ".agents/skills"
        if verdict == "copies-drifted":
            plan.notes.append(f"skills: dropping the {other} copies; differing skills: {', '.join(info['drifted'])}")
        plan.remove(other)
        if source == ".claude/skills":
            plan.move(".claude/skills", ".agents/skills")
        plan.symlink(".claude/skills", "../.agents/skills")
        return
    if verdict == "claude-only":
        if (plan.repo / ".agents").exists() and not (plan.repo / ".agents").is_dir():
            raise SystemExit(".agents exists and is not a directory")
        (plan.repo / ".agents").mkdir(exist_ok=True) if plan.apply else None
        plan.move(".claude/skills", ".agents/skills")
        plan.symlink(".claude/skills", "../.agents/skills")
        return
    if verdict == "agents-only":
        if plan.apply:
            (plan.repo / ".claude").mkdir(exist_ok=True)
        plan.symlink(".claude/skills", "../.agents/skills")
        return
    raise SystemExit(f"unhandled skills verdict {verdict}")


def converge_instructions(plan: Plan, info: dict, instructions_from: str | None, keep_gemini: bool, flip_reverse: bool = False) -> None:
    verdict, canonical = info["verdict"], info["canonical"]
    repo = plan.repo
    if verdict == "none":
        plan.notes.append("instructions: no AGENTS.md or CLAUDE.md, nothing to do")
    elif verdict == "unified" and canonical == "AGENTS.md":
        plan.notes.append("instructions: already unified on AGENTS.md")
    elif verdict == "unified" and not flip_reverse:  # reverse direction: AGENTS.md -> CLAUDE.md
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
        carried = carried_lines(repo / other, source) if verdict != "copies-identical" else []
        if verdict == "divergent":
            plan.notes.append(f"instructions: keeping {source}; the whole former {other} is carried for review")
            carried = [line.rstrip() for line in (repo / other).read_text(encoding="utf-8", errors="replace").splitlines()]
        if plan.apply:
            carried_text = list(carried)  # captured before removal
        plan.remove(other)
        if source == "CLAUDE.md":
            plan.move("CLAUDE.md", "AGENTS.md")
        if carried:
            plan.append("AGENTS.md", f"Carried from the former {other} (review and trim)", carried if not plan.apply else carried_text)
        plan.symlink("CLAUDE.md", "AGENTS.md")
    else:
        raise SystemExit(f"unhandled instructions verdict {verdict}")

    if "gemini" in info and not keep_gemini:
        gemini = repo / "GEMINI.md"
        if info["gemini"] == "pointer":
            carried = carried_lines(gemini, "AGENTS.md") + ([] if "CLAUDE.md" not in gemini.read_text(encoding="utf-8", errors="replace") else [])
            carried = [l for l in carried if not ("claude.md" in l.lower() and any(w in l.lower() for w in READ_WORDS))]
            plan.remove("GEMINI.md")
            if carried:
                plan.append("AGENTS.md", "Antigravity (agy) notes carried from the former GEMINI.md", carried)
        elif info["gemini"].startswith("symlink"):
            plan.remove("GEMINI.md")
        else:
            raise SystemExit("GEMINI.md is a full file, not a pointer; merge it into AGENTS.md by hand or pass --keep-gemini")
    elif "gemini" in info:
        plan.notes.append("instructions: GEMINI.md kept on request (agy will read it in addition to AGENTS.md)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("repo", type=Path)
    parser.add_argument("--apply", action="store_true", help="Perform the plan (default: print it)")
    parser.add_argument("--no-git", action="store_true", help="Use filesystem operations instead of git mv/rm")
    parser.add_argument("--skills-from", choices=(".claude/skills", ".agents/skills"))
    parser.add_argument("--instructions-from", choices=("CLAUDE.md", "AGENTS.md"))
    parser.add_argument("--keep-gemini", action="store_true")
    parser.add_argument("--flip-reverse", action="store_true", help="Also flip compliant reverse symlinks to the forward direction")
    parser.add_argument("--force", action="store_true", help="Apply even when the rewritten paths have uncommitted changes")
    args = parser.parse_args(argv)

    repo = args.repo.resolve()
    if not repo.is_dir():
        parser.error(f"not a directory: {repo}")
    use_git = not args.no_git and (repo / ".git").exists()
    if use_git and subprocess.run(["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True).stdout.strip() in ("main", "master") and args.apply:
        print("refusing to apply on main/master; create a topic branch first", file=sys.stderr)
        return 2

    if use_git and args.apply and not args.force:
        dirty = subprocess.run(["git", "-C", str(repo), "status", "--porcelain", "--", *INSTRUCTION_PATHS], capture_output=True, text=True).stdout.strip()
        if dirty:
            print("refusing to apply: uncommitted changes under the paths this script rewrites (commit or stash them, or pass --force):\n" + dirty, file=sys.stderr)
            return 2

    before = audit.audit_repo(repo)
    plan = Plan(repo, use_git, args.apply)
    converge_skills(plan, before["skills"], args.skills_from, args.flip_reverse)
    converge_instructions(plan, before["instructions"], args.instructions_from, args.keep_gemini, args.flip_reverse)
    for note in plan.notes:
        print("NOTE  " + note)
    if not args.apply:
        print(f"\ndry run: {len(plan.steps)} step(s) planned; re-run with --apply on a topic branch")
        return 0
    after = audit.audit_repo(repo)
    print("\nafter: " + audit.instruction_summary(after["instructions"]) + " | " + audit.skills_summary(after["skills"]) + (" | OK" if after["ok"] else " | still FIX"))
    if use_git:
        subprocess.run(["git", "-C", str(repo), "status", "--short"], check=False)
    return 0 if after["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
