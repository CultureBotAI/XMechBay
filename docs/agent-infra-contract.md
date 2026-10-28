# Agent infrastructure contract

One repository layout and one user-level layout that Claude Code, Codex CLI and
Antigravity (`agy`) all read, so that a rule or a skill is written once and every
agent sees the same text. Measured on 2026-10-28 against Claude Code 2.1.312,
Codex CLI 0.164 and the current `agy` build; re-measure before relying on a
discovery claim after an upgrade.

## What each agent reads

| Surface | Claude Code | Codex CLI | Antigravity (`agy`) |
|---|---|---|---|
| Repository instructions | `CLAUDE.md` (root and parent directories) | `AGENTS.md` (root and parent directories; `AGENTS.override.md` wins) | `AGENTS.md` or `GEMINI.md` at the workspace root, plus `.agents/rules/*.md` |
| Repository skills | `.claude/skills/<name>/SKILL.md` | `.agents/skills/<name>/SKILL.md` | `.agents/skills/<name>/SKILL.md` (also `_agents/`) |
| Workflows | - | - | `.agents/workflows/*.md` |
| User-level instructions | `$CLAUDE_CONFIG_DIR/CLAUDE.md` (default `~/.claude/CLAUDE.md`) | `$CODEX_HOME/AGENTS.md` (default `~/.codex/AGENTS.md`) | `~/.gemini/AGENTS.md` or `~/.gemini/GEMINI.md`, or the same names under `~/.gemini/config/` |
| User-level skills | `$CLAUDE_CONFIG_DIR/skills/` | `$CODEX_HOME/skills/` | `~/.gemini/config/skills/` |
| Memory | auto-memory per config dir and project path | `$CODEX_HOME/memories/` | per-conversation brain under `~/.gemini/antigravity-cli/` |
| Permissions and hooks | `settings.json` hooks and permission rules | approval policy and sandbox in `config.toml` | `~/.gemini/antigravity-cli/settings.json`, `~/.gemini/config/hooks.json` |

Evidence: Codex registers a repository's `.agents/skills` as a skill root and
reports it by its resolved path, so a symlink is followed; the `agy` binary
documents the `AGENTS.md`/`GEMINI.md` pair and `.agents/skills`; Claude Code reads
`CLAUDE.md` through a symlink and lists a skill once when both skill directories
resolve to the same content. Both symlinked user-level rules files are
de-duplicated by `agy` (its changelog says so); a separate `GEMINI.md` next to an
`AGENTS.md` is read twice.

## Repository layout (the target state)

```
AGENTS.md                 canonical instructions, a regular file
CLAUDE.md -> AGENTS.md    symlink, so Claude Code reads the same bytes
(no GEMINI.md)            agy reads AGENTS.md; a second file duplicates context
.agents/skills/<name>/    canonical skills, regular directories
.claude/skills -> ../.agents/skills   symlink
```

Rules:

1. **One text.** `AGENTS.md` holds everything an agent must know about the
   repository. Tool-specific notes go in short sections of the same file
   (`## Claude Code`, `## Codex`, `## Antigravity`), not in separate files, so
   each tool also sees the constraints the others work under.
2. **One skills tree.** A skill lives once under `.agents/skills/`. The
   `.claude/skills` symlink is the only mirror. Identical copies kept in step by
   a sync script are accepted where a published repository cannot ship symlinks;
   drifted copies are a defect.
3. **No pointer files.** "Read CLAUDE.md" stubs are the previous convention and
   they work only when the model follows the pointer. A symlink does not depend
   on that.
4. **Reverse direction is compliant, not preferred.** `AGENTS.md -> CLAUDE.md`
   and `.agents/skills -> ../.claude/skills` give every tool the same bytes and
   are accepted by the audit. New repositories use the forward direction; flip
   existing ones only when another change touches the files.
5. **Durable knowledge goes in the repository, not in a tool's memory.** Each
   tool's memory is a per-tool cache that no other tool reads. Anything a later
   session of any tool needs is written into `AGENTS.md`, `docs/` or a dated
   record in the repository.
6. **Enforcement stays per tool.** Hooks, permission rules and sandboxes are
   not shared; the rules they enforce are written in the common text, and each
   tool's section says what that tool enforces mechanically.

## User-level layout

The four live files are read by different tools and cannot be one file:

| File | Read by |
|---|---|
| `~/.claude/CLAUDE.md` (shared into other Claude config dirs by symlink) | Claude Code |
| `~/.codex/AGENTS.md`, `~/.codex-pro/AGENTS.md` | Codex CLI, one per account home |
| `~/.gemini/AGENTS.md` | Antigravity |

They share one **common block** delimited by markers:

```
<!-- common-agreements:begin -->
...
<!-- common-agreements:end -->
```

`scripts/sync_global_agreements.py --common <file> --target <file>...` writes the
common block into each target between the markers (creating them at the top of
the file when absent), backing each target up as `<file>.bak-common-<YYYYMMDD>`
first. `--check` reports drift and changes nothing. Text outside the markers is
the tool-specific part and is never touched.

User-level skills: `scripts/sync_user_skills.py` lists every skill once in a
canonical directory (default `~/.agents/skills`) and gives each tool's skill
directory a symlink for every skill it lacks, pointing at the skill's real
directory wherever that lives. Names with different content in two places are
reported as conflicts and left alone; created links are logged so they can be
removed. Dry run by default.

## Compliance states reported by the audit

Instructions: `unified` (symlink pair, canonical named), `pointer` (two real
files, one is a short stub naming the other), `copies-identical`, `divergent`,
`claude-only`, `agents-only`, `none`. `gemini:<kind>` is appended when a
`GEMINI.md` exists.

Skills: `unified-symlink` (either direction), `copies-identical`,
`copies-drifted` (with the drifted skill names), `claude-only`, `agents-only`,
`none`.

A repository is **OK** when instructions are `unified` (either direction) or
absent, with no `GEMINI.md`, and skills are `unified-symlink` (either direction),
`copies-identical` or `none`. Everything else is **FIX**. The audit marks the
reverse direction `(reverse)`; `agent_infra_converge.py --flip-reverse` turns it
forward when a change already touches those files.

## Converging a repository

```bash
python3 scripts/agent_infra_audit.py <repo>                 # measure
python3 scripts/agent_infra_converge.py <repo>              # plan, changes nothing
python3 scripts/agent_infra_converge.py <repo> --apply      # on a topic branch
python3 scripts/agent_infra_audit.py <repo>                 # verify OK
```

The converge script refuses to choose between drifted copies or divergent
instruction files; pass `--skills-from` or `--instructions-from` after reading
both. It never commits. Lines from a removed pointer or `GEMINI.md` that are not
the "read the other file" sentence are appended to `AGENTS.md` under a "carried"
heading for the reviewer to trim, so nothing is lost silently.

Canary first: converge one repository, open its pull request, and run each of
the three agents non-interactively in that checkout asking for the first heading
of its instructions and the list of repository skills. Only when all three
answer with the canonical text and the same skill names, converge the rest, one
pull request per repository. Merging is the owner's decision.

## Known exceptions

- A published repository that must work on checkouts without symlink support
  keeps identical copies and a sync check in CI (this repository did until
  2026-10-28; its CI now accepts the symlink).
- Governance tooling that reads `CLAUDE.md` by name keeps working: a symlink
  reads as the canonical file. Tooling that *writes* into `.claude/skills` also
  keeps working, because writes follow the symlink into `.agents/skills`.
- Code that opens path components with `O_NOFOLLOW` (a hardened path walker
  such as a Mech's `scripts/record_review.py`) cannot reach a skill through
  the symlinked tree. Configuration it reads (for example
  `conf/record_review.yaml`) must name the real directory; CI catches the
  other case with `NotADirectoryError`.
- Validators that refuse every symlink (a `safe_path` that walks components
  with `is_symlink()`, an inventory that treats a symlinked input as missing)
  reject saved records that name `CLAUDE.md` or an `.agents/skills` route.
  Give them an allowlist of exactly the two entry-point links with their
  contracted targets (`CLAUDE.md -> AGENTS.md`, `.agents/skills ->
  .claude/skills` or the forward pair), never a blanket "symlinks inside the
  repository are fine": the tests that refuse symlinked data files must keep
  passing.
- Tooling that lists `.claude/skills` with `git ls-files` sees only the symlink
  entry when that path is a link. A fleet whose hub inventories its members that
  way keeps `.claude/skills` as the real directory and `.agents/skills` as the
  symlink (`agent_infra_converge.py --skills-canonical .claude/skills`); the
  audit reports it as `unified-symlink(reverse)` and OK.
