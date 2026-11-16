---
name: agy-permissions
description: Audit an explicitly supplied permissions JSON file and inspect rule-match evidence without changing settings or accessing live accounts.
---

# Permission audit

Use this skill to inspect permission-rule structure or investigate a command
prompt. Ask the user to select a settings file when the input is unknown; do
not search home directories, read authentication files, or discover accounts.
Start with the synthetic example:

```sh
python3 .agents/skills/agy-permissions/scripts/check_agy_permissions.py --settings examples/permissions.json
python3 .agents/skills/agy-permissions/scripts/check_agy_permissions.py --settings examples/permissions.json --check-command 'git status --short'
```

For private settings, use an explicitly supplied local path. The helper is
read-only: it never executes the checked command, changes rules, writes
backups, or applies a policy. Reports omit settings paths, command contents
and rule contents; zero-based locations identify rules for local inspection.
Store any live report or input only under ignored `.local/`.

The audit validates `permissions.allow`, `permissions.ask`, and
`permissions.deny` as lists of strings. It counts command/path/regex rules,
flags duplicate rules and malformed or unrecognized syntax, and reports the
size of the serialized rules. This size is **not** the size of a generated
sandbox profile and cannot establish whether a platform limit will be hit.

Command checks inspect `command(...)` rules by default; use `--tool unsandboxed`
for that separate rule family. Literal payloads receive exact string comparison.
`regex:` payloads use Python `re.fullmatch` in a time-limited child process.
Multiline checks identify cases that would match only with Python's DOTALL
flag. Python regex syntax and behavior differ from RE2 and other engines;
these are approximations, not authoritative validation of the agent's engine.

For multiline input use `--check-command-file` with an explicitly selected
local UTF-8 file. Match evidence is reported separately for allow, ask, and
deny; no combined allowed/denied verdict is asserted. Agent policy precedence,
shell expansion, command decomposition, file-path matching, and sandbox
generation can change the actual outcome. Reproduce a prompt in the selected
agent only when the user authorizes that action.

Do not install the synthetic example as an operational policy. It demonstrates
the format and test cases, not a recommended permission boundary.
