# Fleet control contract

This contract separates session mechanics, task ownership, and reporting. A
deployment assigns these roles to its own tools; it does not need a particular
organization, host naming scheme, repository layout, or model provider.

## Ownership

| Layer | Responsibility | Evidence |
|---|---|---|
| Host controller | Inspect hosts, locate sessions, and perform authorized input in host-loop windows | Private operational log |
| Task dispatcher | Create isolated task worktrees and own the workers it launches | Task workspace |
| Repository supervisor | Observe repository progress and dispatch only within its documented agent/tty boundary | Supervisor workspace |
| Reporting interface | Consume status and progress without controlling sessions | Published summaries containing no private inputs |

The controller does not acquire ownership of a worker by being able to reach its
host. A supervisor must positively identify the intended process and terminal;
an unknown agent type, ambiguous process, or unrelated tmux pane is observe-only.

## Session naming and placement

- The host controller owns one configured tmux session. The default name is
  `fleet`; `XMECHBAY_TMUX_SESSION` changes it. Its windows use repository basenames,
  such as `sample-project`, with documented suffixes for distinct loops.
- A task dispatcher owns separate sessions named `<slug>-<task-id>`, using
  lowercase letters, digits, and hyphens, with one isolated worktree per task.
- Before creation, list existing sessions and relevant host-loop windows. Refuse
  an existing exact target name. Do not replace a session to resolve a collision.
- Start a task dispatcher on the host where its workers run. The host controller
  may start its authorized host-loop context, then only observe dispatcher-owned
  workers; it does not send keys to or kill those workers directly.
- On macOS, a tmux server that needs GUI-login keychain access must originate in
  that login session. An SSH-origin server may not share that access. Do not
  launch a remote GUI application to repair the security context.
- Treat window IDs as transient selectors. Verify the current tty, working
  directory, process, and screen before using a previously recorded target.

## Input and prompts

| Target | Input owner |
|---|---|
| Configured host-loop windows | Host controller |
| Dispatcher-owned workers | Dispatcher that created them |
| Unrecognized or ambiguous session | None until its identity and ownership are established |
| Approval, question, trust, update, or sign-in screen | Human decision required |

Read the screen before input and inspect it afterward. A successful API call is
not proof that the command was submitted or completed. Use a file for goals and
long prompts; use literal short commands and separately guarded key operations
for interactive input. Never interpolate prompt text into shell code.

An explicit instruction to answer a particular prompt may authorize one input.
The override is bound to the first checked prompt and target and expires before
that input is attempted. It cannot authorize a later prompt, be armed by an idle
screen, bypass an unreadable screen, or change ownership. Sign-in is always left
to the user. Compound operations stop if a subsequent prompt needs authorization.

Prompt detection uses visible text and is conservative. It cannot eliminate races
in a changing interface; post-input inspection remains necessary. Direct tmux or
AppleScript commands outside the helper do not inherit the helper's checks.

## Status

Screen-derived states are:

`Pursuing goal`, `Goal paused`, `Goal stalled`, `Goal achieved`,
`Goal hit usage limits`, `Working`, `idle`, `APPROVAL`, `QUESTION`, `SIGN-IN`,
`TRUST-PROMPT`, and `UPDATE-PROMPT`.

`idle` requires an observed empty composer and no goal. A drafted composer or
unrecognized screen is unknown. A process-only inventory cannot prove progress
or idleness; retain an unknown state rather than inferring one from a process
name. Keep the evidence source clear when combining reports.

Host health is separate from a session's goal state. A `critical` or `unknown`
port-pressure result holds admission of new work on that host until a fresh,
usable observation clears the hold. Controllers honor the hold before launches
and at safe dispatch checkpoints; they do not interrupt active work, dismiss
human prompts, or stop processes automatically. A `warning` calls for inspection
and planning an owned, idle checkpoint. The
[port pressure recovery guide](port-pressure-recovery.md) defines the monitoring
and verified recovery procedure; the monitor reports state but does not enforce
dispatch decisions.

## Operations and evidence

Keep real goal files and logs in a private deployment workspace. Record the target,
authorized action, verification, and any pending human decision there. Logs can
contain personal paths or prompt text even when the source code is generic.
Do not commit them here; publish only a separately reviewed aggregate or synthetic
example.

Resuming goals and launching workers may spend usage. Start one authorized canary,
verify its end-to-end result, and obtain the appropriate authorization before a
larger run. Stopping one loop must not kill a shared tmux server. Never resume a
thread already live in another session.
