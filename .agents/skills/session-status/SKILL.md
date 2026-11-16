---
name: session-status
description: Inventory agent processes and tmux sessions on an explicitly selected SSH host, with optional screen-based status and offline fixtures.
---

# Session status

Use this skill to inspect sessions on a host the user has selected. Read the
repository instructions and fleet-control contract first. No machine aliases,
accounts, repository paths, or credentials are built in.

Run from the checkout root:

```sh
python3 .agents/skills/session-status/scripts/session_status.py --host worker.example.invalid
python3 .agents/skills/session-status/scripts/session_status.py --host worker.example.invalid --screens
python3 .agents/skills/session-status/scripts/session_status.py --snapshot examples/session-snapshot.json --screens
```

The host must be a bare hostname, IPv4 address, or simple SSH alias. Configure
the SSH user, key and port outside this repository. The remote host needs
Python 3.10 or later and `ps`; tmux is optional. SSH uses batch mode and never
answers sign-in prompts. No GUI application is opened and no session receives
input. The helper never reads authentication files, shell-account state, or
process command arguments.

By default the report contains executable identity, PID, tty and tmux pane
metadata. `--include-paths` adds working directories. Exact executable names
`claude`, `codex`, and `agy` are recognized; wrapper processes can be missed.
For example, an agent whose executable is `node` can be identified only if its
tmux pane command names the agent; a wrapper outside tmux remains unidentified.
The helper reports only the tmux server reachable through the current SSH
environment and default socket. Custom sockets, another user's server, or a
different environment may contain sessions this probe cannot see.

A missing tmux executable, a probe timeout, and an unavailable or inaccessible
server have distinct error codes. A nonzero server probe cannot distinguish no
server from insufficient access; neither proves that no sessions exist. A
running process alone yields `UNKNOWN`, never an inferred idle/working verdict.

`--screens` reads the current tmux screen and recent scrollback for recognized
agent panes. It uses the shared mac-orchestration classifier and its contract
status vocabulary. Screen evidence is heuristic and can include stale output;
inspect the intended pane before taking an action. Screen contents stay in
memory and are omitted from the final report. Processes outside tmux still
appear, but their screen status remains `UNKNOWN`.

Live metadata and screens can contain private information. Save runtime reports
only under ignored `.local/`, and never commit them. The hidden `--collect`
transport mode emits raw probe data, including requested screen text; it is an
internal SSH protocol, not a report to publish. Use the synthetic snapshot for
shareable examples. The helper does not create output files automatically.
Offline snapshots must use the documented process/pane structure and known probe
error codes. Invalid snapshots produce a generic JSON error without echoing
snapshot contents or local paths.
