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

# Also listen on this Mac's own Tailscale interface (never 0.0.0.0 / LAN)
bash scripts/fleet-dashboard.sh --tailscale --config .local/fleet-status.json

# Export a private (0600) self-contained static HTML snapshot without serving
bash scripts/fleet-dashboard.sh --config .local/fleet-status.json \
  --export-html .local/fleet-reports/dashboard-snapshot.html

# Preview the synthetic multi-host fixture offline
bash scripts/fleet-dashboard.sh --snapshot examples/fleet-snapshot.json
```

Dashboard guarantees:
- Binds exclusively to loopback (`127.0.0.1`, `localhost`, or `::1`) by default—or,
  when `--tailscale` is passed, additionally to this Mac's verified Tailscale
  interface IP (never `0.0.0.0` or LAN)—sets a strict `Content-Security-Policy`
  with zero external requests, validates `Host`, `Origin`, and a per-process
  `X-MechBay-Token` on `POST /api/refresh` and `POST /api/focus-terminal`, and
  restricts `POST /api/focus-terminal` to loopback browser clients.
  When an open tab hits `403 invalid_csrf_token` after a local server restart,
  the UI reloads once (with a 30s loop cooldown), restores view/filter state,
  and completes the refresh or pending M1 Terminal jump—or surfaces a manual
  **Reload page** action if session storage or the cooldown prevents automatic
  reload.
- Clicking a live local **M1** session row (or M1 spotlight card / matrix pill /
  detail action) switches local `Terminal.app` directly to that exact window and
  tab (`POST /api/focus-terminal`, matching live `tty` and active `tmux`
  pane/window so recycled window IDs, grouped `xmb-view-*` viewers, or multi-window
  sessions never focus a tab displaying a different session, un-minimizing if
  needed, and refusing if `Terminal.app` is not already running) while sending
  **zero input** to any session; clicking `▸`/`▾` on an M1 row toggles details in
  place without switching apps, and non-M1 (`m5`, `intel`, remote SSH, cached, or
  snapshot) rows never attempt local `Terminal.app` switching.
- Coalesces concurrent **Update** clicks into one single-flight collection sweep
  and never opens Excel or sends input to sessions.
- Detects active turns (`Working`, `🟢 ▶ work`) from dedicated live TUI cues across
  all three CLIs—Codex interruptible status lines (`(<elapsed> · esc to interrupt)`),
  Claude Code active spinners (`✽ … (<elapsed> · tokens)`), foreground bash hints,
  and live subagent progress trees below the footer, plus Antigravity braille
  spinners (`⣷ …`), running task strips, and `esc to cancel` active-turn footers—while
  keeping human prompts (`APPROVAL`, `QUESTION`, etc.) at higher precedence and never
  promoting historical prose or completed `· done` turns.
- Surfaces a dedicated top-of-page **Needs Human Input** spotlight banner (plus
  browser-tab count badge, clickable per-Mac input pills, sub-category filter
  chips, and keyboard shortcut `i`) that cleanly separates three distinct
  human-blocker classes:
  1. **Interactive TUI questions & keystrokes (`keystroke`):** `APPROVAL`,
     `QUESTION`, `SIGN-IN`, `TRUST-PROMPT`, and `UPDATE-PROMPT` modals, showing
     both the prompt question and the exact keystrokes required (`⌨ 1 or Enter
     allow · 2 deny · f diff · ↑/↓`, `ctrl+k approve · alt+j manage`,
     `1–N or ↑/↓ + Enter`, etc.), plus an explicit **`⚡ Fixable by agy-work`**
     subset badge and filter (`agy-work -c`) when OS `ps` evidence proves an
     `agy` session on `APPROVAL` was launched without
     `--dangerously-skip-permissions` (`skip_permissions: false`), distinguished
     from `agy` sessions already running `agy-work` (`skip_permissions: true`,
     stopped on a standing Ask rule) and from `QUESTION`/non-`agy` prompts.
  2. **Coding-agent plain-text messages (`agent_message`):** sessions idle at an
     empty composer whose final assistant paragraph asks a question or requests
     explicit confirmation/action in plain text (`💬 Type reply + Enter`), while
     excluding Esc-interrupted turns (`Interrupted · What should ... do instead?`)
     and generic polite sign-offs.
  3. **Pasted / unsent prompts that are NOT running (`pasted_prompt`):** single-
     or multi-line text or `[Pasted text ...]` sitting unsubmitted in the
     composer (`⌨ Press Enter to run · clear to discard`), surfacing the
     sanitized composer prompt (`❯ ...`) rather than stale prior-turn output.
- Groups sessions by role (`1 · MechBay Sessions`, `2 · Claw Sessions`,
  `3 · Mech Sessions`) by default, placing companion or unmatched repositories
  in an **Other Observed Sessions** drawer that stays collapsed when clear but
  auto-expands (with an input-count badge) whenever an `other`-role session
  needs human input, so host totals reconcile 100%, with instant client-side
  switches to **By Mac** and **Project × Mac Matrix** views.
- Supports `"transport": "local"` in `.local/fleet-status.json` for probing the
  local Mac directly without SSH, automatically falls back from off-LAN `.local`
  hostnames to Tailscale peers (`tailscale_host`, `fallback_hosts`, or automatic
  `tailscale status --json` peer discovery with OpenSSH `HostKeyAlias` pinned to
  the canonical configured host so `known_hosts` stays strict and `Δ` comparison
  history survives roaming), badges Tailscale-connected host cards (`🔗 Tailscale`),
  and retains dimmed `last_reachable` cached rows stamped with their age when a
  remote laptop is genuinely offline.

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
