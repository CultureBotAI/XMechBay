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
private SSH configuration. Remote access uses batch mode and a connection timeout.

`XMECHBAY_TMUX_SESSION` names the one session this tool may control; the default
is `fleet`. Session names may contain letters, digits, underscores and hyphens.
Each named window must have exactly one pane. Other sessions, pane selectors and
ambiguous windows are refused. The tool never creates a tmux session.

Refusal and explicit-override metadata is saved under the repository's ignored
`.local/records/` directory. `XMECHBAY_RECORDS_DIR` can select a different private
directory. Logs contain host, target, command, state, timestamp, and any override
reason; they exclude screen contents and input text. Keep logs and real host
configuration out of version control.

## Visible viewer for each session

For an authorized new remote coding CLI session that supports tmux, use the
configured host-controller session with one named window and pane per repository
loop. Follow the contract's naming and ownership rules; check for an existing
live thread before starting another. GUI-only agents retain their native UI.

Keep one visible Terminal window per agent session, including coordinator and
repository agents, clearly titled for the session it displays. Reuse an existing
appropriate shell window when possible. Each tmux viewer must independently show
its intended agent: attaching several clients to a shared session is insufficient
if selecting a window in one viewer switches the others. Verify each viewer's
tty, attachment and displayed pane, and confirm that changing one view leaves
the others on their intended agents. Do not start duplicate agents to obtain
separate views. A repository title alone does not identify a running agent.

Stagger the visible Terminal windows from bottom-left to top-right, leaving
enough vertical spacing for two terminal lines to remain visible at each bottom
edge. Derive spacing from the current font and window geometry, and verify the
result on screen. On macOS, a tmux server that needs login-session keychain access
must originate in an already running Terminal app in the GUI login session.
Never launch that app from SSH.

The default tmux keys are **Ctrl+B, then W** to choose a window and **Ctrl+B,
then D** to detach a viewer. Detaching leaves agents running. Restore the view
through its existing attachment arrangement; do not launch another agent. Use
the host's configured prefix if it differs from the default.

Existing agents running directly in Terminal remain there until an authorized
idle checkpoint permits the exact-session graceful restart below. Window cleanup
does not authorize interrupting or migrating them. When asked to close redundant
windows, inspect every tab's current tty, processes and screen; close only
verified shell-only windows without child jobs. Preserve active agents, the
per-session viewers and unrelated terminals. Verify one closure before repeating
it, then confirm the retained agents and viewers are still present.

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
Enter. tmux paste validates session ownership, uses a unique buffer, and removes
both the buffer and staged file on completion or failure. No prompt text is
constructed as shell code.

The helper requires Terminal to be running already and refuses to launch it.
`open DIRECTORY [COMMAND]` creates a new window in that existing app; use it only
for a user-requested session. `front` and `cascade` change the existing layout.
`quit-codex` is a legacy Ctrl+C helper, not the preferred routine exit path.
Follow the graceful restart protocol below instead of selecting it automatically.
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

## Graceful exit and exact-session restart

1. Confirm ownership, PID, terminal, working directory, and a current idle screen
   with an empty composer, no active goal or work, and no human prompt. Do not
   interrupt working agents to create a restart checkpoint.
2. As the penultimate interaction in the shutdown sequence, acquire or reconfirm
   the exact session/conversation ID from authoritative CLI output or process
   metadata tied to that PID and terminal. Save the ID, directory, model, and
   effort privately. Check that the thread is not already live elsewhere. Error
   IDs, most-recent-thread guesses, and unsupported model assertions are invalid.
3. Immediately follow with the agent's supported `/quit` or `/exit` as the final
   shutdown command, retaining all pre-input screen guards. Inspect the resulting
   shell and verify the original PID exited; compare any printed resume ID with
   the saved ID. Input delivery alone does not establish shutdown.
4. If the exact ID or graceful exit cannot be verified, stop. Do not automatically
   fall back to Ctrl+C, SIGTERM, another signal, or a duplicate launch. A signal
   requires a separate explicit user instruction identifying the owned process
   and action; the legacy `quit-codex` helper is not routine restart permission.
5. After verified exit, use the agent's supported explicit-ID resume command from
   the same directory with the same model and effort. Verify those settings and
   the restored thread before further input. Never use `--last`/`--continue` as a
   substitute, replay a goal automatically, or answer a new prompt without its
   specific authorization.

See the [port pressure recovery guide](../../../docs/port-pressure-recovery.md)
for an AGY example and recovery verification.

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
