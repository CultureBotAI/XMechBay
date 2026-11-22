---
name: mac-orchestration
description: Inspect and control existing Terminal.app agent windows or an explicitly owned tmux session, locally or on an explicitly named SSH host.
---

# Terminal orchestration

Use this skill for requested Terminal or tmux session inspection and control.
Read the [fleet control contract](../../../docs/fleet-control-contract.md) before
sending input. Resolve `scripts/mac-term.sh` relative to this skill directory.

## Configuration

`--host HOST` (or `-H HOST`) accepts an explicit bare SSH hostname. `local` is
the default. `MAC_TERM_HOST` can supply the same value. There are no built-in
machine names or account assignments; SSH user selection belongs in the user's
private SSH configuration. Remote access uses batch mode, a connection timeout,
and SSH keepalives.

`XMECHBAY_TMUX_SESSION` names the one session this tool may control; the default
is `fleet`. Session names may contain letters, digits, underscores and hyphens.
Each named window must have exactly one pane. Other sessions, pane selectors and
ambiguous windows are refused. The tool never creates a tmux session.

Refusal, unknown-outcome and explicit-override metadata is saved under the repository's ignored
`.local/records/` directory. `XMECHBAY_RECORDS_DIR` can select a different private
directory. Logs contain host, target, command, state, timestamp, and any override
reason; they exclude screen contents and input text. Keep logs and real host
configuration out of version control.

## Safe workflow

1. Inspect `windows`, `status`, and `screen WINDOW_ID`. Status returns the
   contract vocabulary; an empty state means the screen was not recognized.
2. Confirm that the requested target and task match. Use one canary session and
   inspect its resulting screen before applying the same operation more widely.
3. Use `send WINDOW_ID TEXT` for short commands. Put longer instructions in a
   private UTF-8 file and use `paste WINDOW_ID FILE` or
   `tmux-paste WINDOW_NAME FILE`. Keep goals under 1,000 bytes; the hard limit is
   4,000 bytes. NUL, escape and other terminal control bytes are rejected.
4. Inspect the returned screen and verify the intended effect. Transport success
   alone does not prove that the agent accepted a command or resumed a goal.

Every existing-window input reads the screen immediately before sending. An
approval, question, sign-in, trust, update, empty or unreadable screen stops input.
Sign-in always belongs to the user. Use `--override-prompt 'explicit user instruction'`
only when the user expressly supplied an answer for that observed prompt. The
override is recorded before input, bound to the exact first prompted screen and
target, and expires after one attempted input. It cannot authorize another prompt
or a later Enter. Do not guess an answer to an approval or question.

File paste uses private staging files and bracketed paste, then separately guards
Enter. tmux paste validates session ownership, uses a unique buffer, and attempts
to remove both the buffer and staged file on completion or failure. No prompt text is
constructed as shell code.

Each local command, SSH command and SCP copy has a 120-second transport deadline.
`wait-file` allows its requested wait plus 120 seconds; each cleanup attempt has a
15-second deadline. These limits apply per call, not to the whole operation.
Cleanup failure produces a warning without replacing the operation's result;
temporary files or buffers may remain on an unreachable host.

A command timeout or SSH disconnect leaves its outcome unknown: the command may
already have taken effect. An unreadable screen after input likewise cannot prove
that the input failed. Neither condition triggers an automatic retry or later
input. Read the target screen and establish what happened before retrying. An SCP
failure occurs before input and is reported as such.

If a prompt blocks Enter after a paste or a later key in a sequence, earlier input
has already been attempted. The tool records an unknown outcome with the detected
prompt state and stops the remaining input. Inspect the target before continuing;
do not repeat the earlier paste or keys merely because the sequence stopped.

The helper requires Terminal to be running already and refuses to launch it.
`open DIRECTORY [COMMAND]` creates a new window in that existing app; use it only
for a user-requested session. `front` and `cascade` change the existing layout.
`quit-codex` requires an observed empty idle composer and verifies process exit.
Model menus vary between releases. Inspect each menu and use an explicitly
authorized guarded `key` operation; there is no hard-coded model-picker sequence.
The `key` command accepts named keys or a single printable character. Terminal's
`do script` appends Enter to each operation, including arrow and control keys;
inspect the result of every operation before selecting the next one.
`cascade --dry-run` prints layout moves without applying them. Numeric layout
options and window selectors are validated; directories and filenames are passed
as literal arguments. `open DIRECTORY 'COMMAND'` intentionally executes the
explicit shell command only in the newly opened Terminal window.
Review `--help` for the remaining commands. Never copy credentials between agent
homes or inspect keychains as part of orchestration.

## Session model policy

Every Codex session in the fleet runs one of two settings:

| Session | Model and effort | Profile |
| --- | --- | --- |
| Mech session (addition, discovery, review or merge inside a Mech) | GPT-6 Rosalind (Research), xhigh | `mech` |
| Claw session and infrastructure session (orchestration, fleet tooling) | GPT-6-Astra, high, Fast tier | `claw` |

Classify by the work, not the window title: an orchestrator whose title names a
Mech is an infrastructure session.

Each Codex home on each host holds two profile files. `codex -p NAME` (codex-cli
0.170) layers `$CODEX_HOME/NAME.config.toml` on top of the base config, so the
base `config.toml` stays unchanged:

```toml
# mech.config.toml
model = "gpt-rosalind-astra"
model_reasoning_effort = "xhigh"

# claw.config.toml
model = "gpt-6-astra"
model_reasoning_effort = "high"
service_tier = "priority"   # the catalog id shown as "Fast"
```

- **Starting or resuming a session:** pass the profile, for example
  `codex -p mech …` or `codex resume <id> -p claw`. Then read the footer: it
  must show `GPT-6 Rosalind (Research) xhigh` or `GPT-6-Astra high fast`.
- **Switching a live session:** use `/model gpt-rosalind-astra --reasoning=xhigh`
  or `/model gpt-6-astra --reasoning=high --speed=fast`. This lasts only for the
  session. Send it only to an empty, idle composer with no prompt, sign-in or
  question on screen, and read the footer afterwards.
- **Checking a model id:** `codex debug models` lists the model ids, effort
  levels and service tiers without making a model call.

## Synthetic example

Run from the repository root, replacing the reserved example hostname only in
your private configuration:

```bash
.agents/skills/mac-orchestration/scripts/mac-term.sh --host example.invalid status
XMECHBAY_TMUX_SESSION=fleet .agents/skills/mac-orchestration/scripts/mac-term.sh \
  --host example.invalid tmux-paste sample-project examples/goal.txt
```

The bundled goal is a fictional task. Real objectives and runtime evidence belong
under ignored local storage. No command in this skill grants permission to merge,
publish, answer approval prompts, or resume multiple usage-consuming sessions.
