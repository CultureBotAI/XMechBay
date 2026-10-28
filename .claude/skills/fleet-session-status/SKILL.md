---
name: fleet-session-status
description: Produce compact, evidence-labelled session status tables for one or more configured laptops or workstations. Use for on-demand Claw, Mech, or agent fleet status and progress checks; each host receives its own table.
---

# Fleet session tables

Use the shared collector and renderer to report one row per logical agent session,
including duplicate sessions in the same repository. Preserve a separate table
for each requested host. This is observation: do not send `/btw`, ask a model for
status, answer prompts, start workers, quit sessions, or resume goals to obtain a
report. Follow the [fleet control contract](../../../docs/fleet-control-contract.md)
if a later request authorizes control.

## Run the requested host commands

Run from the checkout root. Resolve host keys through the private configuration;
do not assume that a machine nickname is a universal SSH alias.

```bash
# Each command returns its own host table.
bash scripts/fleet-status.sh laptop-a --config .local/fleet-status.json
bash scripts/fleet-status.sh laptop-b --config .local/fleet-status.json

# Collect concurrently, then print the same two tables separately.
bash scripts/fleet-status.sh laptop-a laptop-b --config .local/fleet-status.json
bash scripts/fleet-status.sh --all --config .local/fleet-status.json
```

Configuration defaults to `.local/fleet-status.json` in the repository;
`XMECHBAY_FLEET_CONFIG` or `--config` selects another private file. Use `--list`
to inspect configured host keys. The [synthetic configuration](../../../examples/fleet-hosts.json)
shows the schema. Keep actual SSH destinations, labels, paths, caches and reports
in ignored local storage. A relative `state_dir` is resolved beside the config.

Use `--json` for structured details and `--no-cache` when comparison state should
not be retained. `--snapshot FILE` renders supplied offline collector evidence
without SSH. Prefer the existing commands over creating ad hoc collectors.

```bash
bash scripts/fleet-status.sh --snapshot examples/fleet-snapshot.json
```

This bundled fixture is fictional and uses fixed historical timestamps; stale
labels are intentional, and its rows must never be presented as live observations.

## Present the evidence compactly

Keep the generated host summary, every session row, and its short legend. The
table shows target, agent/model, state and UI timer, latest reported activity,
process age and observation changes, and available listener counts. Highlight
human prompts or failed coverage in a brief note after the affected table; do not
replace all sessions with a selected highlights list.

- `~` marks screen-reported activity, not independently verified completion.
  A goal state, a model assertion, or a changing screen does not prove useful work.
  Do not invent percentages or call a completed goal a verified repository result.
- `Up` is process age. A goal/turn timer comes from the UI. `Δ` compares normalized
  observations; unchanged output is not proof of a stall, and the first sample has
  no change history.
- Missing or unreadable screens remain unknown. Current composer/footer and
  active human prompts take precedence over conversation history. Do not promote
  an old network error or historical user question into the current state.
- Keep minimized/background Terminal tabs, tmux panes, and unmatched processes
  visible when collected. Preserve distinct agents sharing a repository or tty;
  a tmux viewer is not another agent session.
- An ID from a recognized explicit-ID invocation is launch/resume-ID evidence
  only; an interactive resume may have changed the current session. Tag it as a
  launch ID and retain `session_id_current_verified: false` in JSON. Never use
  it as the authoritative current ID for a restart without fresh CLI confirmation.
  Never substitute an error UUID, title, latest-conversation cache entry, or a
  model's unsupported guess.
- Port-watch data has its own observation time and coverage. A PID absent from
  its bounded process list means unknown listeners, not zero. Separate host port
  pressure from one process needing maintenance. A stale/missing health report
  is not healthy, and SSH reachability does not establish internet connectivity.
- Keep a failed-host block and its coverage errors when another host succeeds.
  Never present a failed probe as an empty host with zero sessions.

The report is an on-demand observation, not a subscription or permission to spend
model/API usage. Local comparison state supports later requested checks; no
automatic session action follows from a status or alert.
