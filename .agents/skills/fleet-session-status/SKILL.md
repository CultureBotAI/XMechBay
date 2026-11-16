---
name: fleet-session-status
description: Serve the local 127.0.0.1 MechBay HTML symbol dashboard or produce compact, evidence-labelled session status tables across configured Macs (local and SSH). Use for on-demand MechBay, Claw, and Mech session status and telemetry checks.
---

# Fleet session tables and local HTML dashboard

Use the shared collector, role classifier, and renderers to report one row per
logical agent session across configured Macs, including duplicate sessions and
boss-worker worktrees in the same repository. This is strictly read-only
observation: do not send `/btw`, ask a model for status, answer prompts, start
workers, quit sessions, or resume goals to obtain a report. Follow the
[fleet control contract](../../../docs/fleet-control-contract.md) if a later
request authorizes control.

## Local HTML symbol dashboard (`127.0.0.1`)

Launch the local HTML dashboard from the checkout root to view **MechBay**,
**Claw**, and **Mech** sessions across all configured Macs as three aligned
tables of colored symbols, with an **Update** button (`POST /api/refresh`),
host telemetry cards, and an opt-in auto-refresh selector (`Off` by default):

```bash
# Serve on http://127.0.0.1:8765/ and open in the local browser on macOS
bash scripts/fleet-dashboard.sh --open --config .local/fleet-status.json

# Export a private (0600) self-contained static HTML snapshot without serving
bash scripts/fleet-dashboard.sh --config .local/fleet-status.json \
  --export-html .local/fleet-reports/dashboard-snapshot.html

# Preview the synthetic multi-host fixture offline
bash scripts/fleet-dashboard.sh --snapshot examples/fleet-snapshot.json
```

Dashboard guarantees:
- Binds exclusively to loopback (`127.0.0.1`, `localhost`, or `::1`), sets a
  strict `Content-Security-Policy` with zero external requests, and validates
  `Host`, `Origin`, and a per-process `X-MechBay-Token` on `POST /api/refresh`.
  When an open tab hits `403 invalid_csrf_token` after a local server restart,
  the UI reloads once (with a 30s loop cooldown), restores view/filter state,
  and completes the refresh—or surfaces a manual **Reload page** action if
  session storage or the cooldown prevents automatic reload.
- Coalesces concurrent **Update** clicks into one single-flight collection sweep
  and never opens Excel or sends input to sessions.
- Surfaces a dedicated top-of-page **Needs Human Input** spotlight banner (plus
  browser-tab count badge, clickable per-Mac input pills, and keyboard shortcut
  `i`) listing every live session waiting on a blocking prompt (`APPROVAL`,
  `QUESTION`, `SIGN-IN`, `TRUST-PROMPT`, `UPDATE-PROMPT`, including numbered or
  cursor-highlighted TUI menus and Codex `Reviewing approval request` waits) or
  an unsent single-/multi-line composer draft, sorted prompt-first and longest
  wait first, with one-click jump-and-expand to the target row.
- Groups sessions by role (`1 · MechBay Sessions`, `2 · Claw Sessions`,
  `3 · Mech Sessions`) by default, placing companion or unmatched repositories
  in an **Other Observed Sessions** drawer that stays collapsed when clear but
  auto-expands (with an input-count badge) whenever an `other`-role session
  needs human input, so host totals reconcile 100%, with instant client-side
  switches to **By Mac** and **Project × Mac Matrix** views.
- Supports `"transport": "local"` in `.local/fleet-status.json` for probing the
  local Mac directly without SSH, and retains dimmed `last_reachable` cached
  rows stamped with their age when a remote laptop is unreachable on another
  network.

## Terminal Markdown and Excel host tables

```bash
# Each command returns its own host table.
bash scripts/fleet-status.sh workstation-local --config .local/fleet-status.json --no-excel
bash scripts/fleet-status.sh laptop-a laptop-b --config .local/fleet-status.json
bash scripts/fleet-status.sh --all --config .local/fleet-status.json
```

Configuration defaults to `.local/fleet-status.json` in the repository;
`XMECHBAY_FLEET_CONFIG` or `--config` selects another private file. Use `--list`
to inspect configured host keys. The [synthetic configuration](../../../examples/fleet-hosts.json)
shows both `"transport": "local"` and SSH host schemas, plus optional
`role_overrides`. Keep actual SSH destinations, labels, paths, caches and
reports in ignored local storage. A relative `state_dir` is resolved beside the
config.

On the local Mac, a live `fleet-status.sh` command also opens Microsoft Excel at
**150%** display zoom by default (one worksheet per requested host in a unique
private `.local/fleet-reports/` workbook). Use `--no-excel` for terminal-only
output or `--json` for normalized JSON. Install the optional Excel exporter
dependency with `python3 -m pip install -r requirements-excel.txt`.

```bash
bash scripts/fleet-status.sh --snapshot examples/fleet-snapshot.json
```

The bundled fixture is fictional and uses fixed historical timestamps; its rows
must never be presented as live observations.

## Evidence and presentation rules

Render generated Markdown tables directly in chat, never inside a code block.
Keep the renderer's colored state symbols beside their text labels: green
active, white idle, yellow human input, orange paused, red stalled/quota, blue
achieved, and black unknown. Color is a scanning aid; geometric glyphs and text
labels carry the same distinction.

- `~` marks screen-reported activity, not independently verified completion.
  A goal state, a model assertion, or a changing screen does not prove useful work.
- `Up` is process age. A goal/turn timer comes from the UI. `Δ` compares normalized
  observations; unchanged output is not proof of a stall, and the first sample has
  no change history.
- Missing or unreadable screens remain unknown. Current composer/footer and
  active human prompts take precedence over conversation history.
- Keep minimized/background Terminal tabs, tmux panes, and unmatched processes
  visible when collected. Preserve distinct agents sharing a repository or tty;
  a tmux viewer is not another agent session.
- An ID from a recognized explicit-ID invocation is launch/resume-ID evidence
  only (`session_id_current_verified: false`). Reconfirm the current ID before a
  restart or control operation.
- Port-watch data has its own observation time and coverage. A PID absent from
  its bounded process list means unknown listeners, not zero.
- Keep a failed-host block and its coverage errors when another host succeeds.
  Never present a failed probe as an empty host with zero sessions.
