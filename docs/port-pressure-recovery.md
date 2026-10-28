# Observe and recover from port pressure

Many long-running agent processes share one host's TCP port resources. Accumulated
listeners can leave too little capacity for new outbound connections, making
unrelated applications appear offline. A listener count identifies pressure; it
does not by itself identify a software defect or prove that every possible
connection will fail.

The monitor observes local sockets and reports pressure. It never quits an agent,
kills a process, submits a prompt, or replays a goal. Follow the
[fleet control contract](fleet-control-contract.md) for session ownership and
input checks. Keep real observations and recovery records private.

## Run a check or install the watch

Run a single check from the checkout root:

```sh
python3 -B scripts/check_port_pressure.py --state-dir .local/port-pressure
```

Add `--notify` to request a local desktop notification as well as the durable
alert record. Notifications are best-effort and may be suppressed by the desktop;
the private state directory remains the source of evidence. Nothing is sent to an
external messaging service. The monitor supports Python 3.9 or later.

Preview the macOS per-user installation before changing it:

```sh
python3 scripts/install_port_pressure_watch.py
```

The default is a dry run. Install explicitly to check every 60 seconds in the
user's login session:

```sh
python3 scripts/install_port_pressure_watch.py --install --notify
```

Omit `--notify` for records without desktop notifications. The installer uses
private application-support locations selected at runtime; deployment paths and
host identities do not belong in distributable source. Install separately on
each host that runs agents. A watch on one workstation does not observe another.

The installer copies the monitor to an immutable directory named by its source
hash, backs up an existing owned LaunchAgent configuration, and requires a fresh
durable observation from the started job. Existing state must be a private,
user-owned directory with mode `0700`; a symlink or insecure directory is refused
before changing the job. A failed startup restores the previous configuration.
Installation success verifies observation, not desktop notification delivery or
healthy pressure. Inspect the reported initial status and the private `latest.json`.

The watch runs while that user's login session is active. Sleep, logout, disk
failure, or a broken Python installation can delay or prevent checks. The installer
uses its current Python interpreter by default; `--python` can select a stable
absolute executable, and `--interval` changes the interval. A consumer should
reject a report older than two check intervals rather than assume the watch is
still running. Keep automatic dispatch paused until fresh evidence is available.

Remove the scheduled watch with:

```sh
python3 scripts/install_port_pressure_watch.py --uninstall
```

Uninstalling preserves collected data. Review and remove private records
separately when they are no longer needed.

## Interpret a result

The defaults reserve headroom before a host runs out of ports:

| State | Exit code | Default trigger or meaning |
|---|---:|---|
| `ok` | 0 | Valid observation below warning thresholds |
| `warning` | 10 | Host port occupancy reaches 50%, or a process reaches 500 listeners |
| `critical` | 20 | Host port occupancy reaches 75%, or a process reaches 1,000 listeners |
| `unknown` | 30 | The observation cannot establish a trustworthy pressure state |

These thresholds are operational guardrails, not operating-system limits or a
diagnostic test for a particular AGY bug. Keep host and process evidence together:
many moderate-sized processes can collectively exhaust a host. Counts across
addresses and IP families require care when interpreting available capacity.

Host occupancy counts distinct TCP local port numbers inside the ephemeral range
reported by the host, across all owners; duplicate numbers across addresses and
IP families count once. Process thresholds count `LISTEN` socket rows for each
exact program and PID. Missing process attribution leaves host counts available
and is marked partial. Invalid or incomplete socket measurements produce
`unknown`, even if the available rows already suggest pressure; the report keeps
that observed severity as evidence. For a complete measurement, the most severe
host or process threshold determines the overall state.

Thresholds are configurable with `--host-warning`, `--host-critical` (ratios),
`--process-warning`, and `--process-critical` (listener counts). Use `--help` to
inspect the interface before overriding defaults. With `--state-dir`, `latest.json`
contains the current report, `history.json` retains bounded compact samples, and
`alert.json` retains alert state. Local notification attempts are recorded in
`notification.json` and have a configurable cooldown; notification delivery is
not evidence that the pressure was resolved.

At `warning`, inspect the evidence and arrange an idle checkpoint with the session
owner. At `critical` or `unknown`, hold new admissions on that host. Apply the hold
at safe launch and dispatch checkpoints; leave active work and human decisions
undisturbed. A missing, stale, or unreadable observation must not be treated as
`ok`. The monitor does not wire itself into a dispatcher or make that decision
for one.

## Recycle an owned AGY session safely

1. Confirm the current process, terminal, working directory, and ownership. Read
   the current screen. Proceed only at a confirmed idle checkpoint with no
   approval, question, trust, update, or sign-in prompt and no active goal or work.
   A process listing or a long unchanged screen is insufficient evidence.
2. Make the penultimate interaction in the shutdown sequence a read-only
   acquisition or confirmation of the exact conversation ID from authoritative
   CLI output or process metadata tied to the current process and terminal.
   Record it privately with the working directory, model, and effort. Verify that
   this conversation is not already live elsewhere. An error ID, newest-thread
   timestamp, or model's unsupported assertion is not identity evidence. If the
   exact ID cannot be established, stop without sending an exit command.
3. Immediately follow that identity confirmation with the final shutdown command,
   `/quit` or `/exit`, through the authorized, guarded input transport. The screen
   guard still applies immediately before input. Inspect the result, compare any
   printed resume ID with the saved ID, and verify both the returned shell and the
   exit of the original PID. Successful input delivery alone does not prove exit.
4. If graceful exit cannot be verified, stop. Do not automatically substitute
   Ctrl+C, SIGTERM, another signal, or a duplicate launch. Signal-based recovery
   requires a separate explicit user instruction naming the owned process and
   action; it is not part of routine restarting. Never interrupt working agents,
   terminate an unrelated worker or process group, or stop a shared tmux server.
5. Only after verified exit, resume the saved conversation from the same working
   directory with the same model and effort. For example, using values populated
   from the verified private record:

   ```sh
   cd -- "$SESSION_WORKDIR"
   agy --conversation "$SESSION_ID" --model "$SESSION_MODEL" --effort "$SESSION_EFFORT"
   ```

   Check the resulting conversation, directory, model, and effort before further
   input. If any saved setting cannot be restored, leave recovery pending rather
   than silently substituting another thread, model, or effort.
6. Recheck listener pressure and network access. Record the before/after result
   privately. Resume is not permission to replay a goal or launch new workers;
   respect the existing work authorization and inspect any new prompt.

AGY documents `/quit` as an alias of `/exit`. Its `--conversation` flag resumes an
explicit ID; `--continue` and `-c` select the most recent conversation and are
unsuitable when recovering a specific fleet session. Ctrl+D has context-dependent
behavior: it exits only with an empty prompt and otherwise deletes text.
[Official CLI reference](https://antigravity.google/docs/cli/reference/)

## Updates and recurrence

Inspect the installed release notes with `agy changelog`; this standalone command
does not require an agent prompt. The supported explicit update command is
`agy update`. AGY also has a background self-updater. Inspect the version of the
running executable as well as the installed file: a replacement on disk does not
replace already running processes. Use an authorized idle checkpoint to restart
onto an updated executable, then verify the running version.
[Changelog command](https://antigravity.google/docs/cli/commands/changelog),
[self-updater documentation](https://antigravity.google/docs/cli/troubleshooting/)

Release 1.4.8 fixes a memory leak involving background tasks left by plugin
sidecars. Its notes do not claim a socket or listener leak fix; 1.4.7 likewise does
not document one. An older 1.1.12 fix addressed connections leaked by
`read_url_content`, but that does not establish a remedy for pressure observed in
a later version. Updating is maintenance, not proof that recurrence is prevented.
[1.4.8 release](https://github.com/google-antigravity/antigravity-cli/releases/tag/1.4.8),
[1.4.7 release](https://github.com/google-antigravity/antigravity-cli/releases/tag/1.4.7),
[official changelog](https://antigravity.google/docs/changelog?tab=cli)

Continue observing growth under the actual workload after recovery. An idle
restart can release accumulated resources without fixing the underlying defect.
Use sustained low pressure and a matching upstream fix as evidence; do not
introduce automatic quits, process termination, goal replay, or weaker sandbox
settings as an unverified workaround.
