---
name: agent-infra-unify
description: Audit and converge a repository's agent instruction files (AGENTS.md, CLAUDE.md, GEMINI.md) and skill trees (.agents/skills, .claude/skills) to the one layout that Claude Code, Codex CLI and Antigravity all read. Use when a repo has drifted skill copies, pointer stubs, a Claude-only layout, or when onboarding a repository for several coding agents.
---

# Unify agent infrastructure in a repository

Read `docs/agent-infra-contract.md` first. It states what each agent reads, the
target layout, and the compliance states; this skill is the procedure.

## 1. Measure before touching anything

```bash
python3 scripts/agent_infra_audit.py <repo>            # one repository
python3 scripts/agent_infra_audit.py --scan <dir>       # every checkout below <dir>
python3 scripts/agent_infra_audit.py --global           # the user-level files
```

`FIX` rows name the defect: `pointer(CLAUDE.md)` means a stub `AGENTS.md` points
at the real `CLAUDE.md`; `copies-drifted[...]` lists skills whose two copies
differ; `gemini:pointer` means a redundant `GEMINI.md`.

## 2. Decide what is authoritative when copies disagree

The converge script refuses to pick. Read both copies of every drifted skill,
and both instruction files when they are `divergent`. Prefer the copy the
repository's own tooling writes (for the Mech fleet that is `.claude/skills`, the
one claw renders into) and the copy with the newer substantive history
(`git log -- <path>`), not the longer or the more common one. Record what the
dropped copy had that the kept one lacks; that goes in the pull request.

## 3. Converge on a topic branch

```bash
git -C <repo> switch -c chore/agent-infra-unify
python3 scripts/agent_infra_converge.py <repo>                                  # plan
python3 scripts/agent_infra_converge.py <repo> --apply [--skills-from .claude/skills] [--instructions-from CLAUDE.md] [--skills-canonical .claude/skills]
python3 scripts/agent_infra_audit.py <repo>                                     # must print OK
```

Pass `--skills-canonical .claude/skills` for a fleet whose hub lists that path
with `git ls-files` (the Mech fleet does). The plan is validated before anything
changes and lists every untracked or ignored file it will move and every
sentence it drops.

Then open `AGENTS.md`: trim the "Carried from the former ..." section the script
appended (keep facts that are still true, delete "read CLAUDE.md" prose) and
replace references to `.claude/skills/` paths with `.agents/skills/` where the
text describes where skills live. Run the repository's own checks. Commit, push,
open the pull request; merging is the owner's decision.

## 4. Verify with the agents themselves (canary)

In the converged checkout, run each agent once, non-interactively, with the same
prompt, and compare:

```bash
claude -p 'Without using tools: give the first heading of the project instructions you were given, and list the project skill names available to you.'
codex exec 'Without using tools: give the first heading of the project instructions you were given, and list the project skill names available to you.'
agy -p 'Without using tools: give the first heading of the project instructions you were given, and list the project skill names available to you.'
```

All three must quote the `AGENTS.md` heading and the same skill names. Do this
for the first repository of a batch only; converge the rest after it passes.

## What this skill does not do

It does not merge two divergent instruction files into one text: that is
editing, done by hand with both files open. It does not touch user-level files
(`scripts/sync_global_agreements.py` does that, with backups). It never commits
or merges.
