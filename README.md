# XMechBay

Reusable infrastructure for observing and controlling agent sessions across
workstations. It separates host-level session control from repository task
dispatch, verifies input targets, and keeps deployment data out of source control.

The repository contains code, general operating guidance, and synthetic examples.
It does not ship host inventories, account settings, session transcripts, billing
data, or a particular organization's deployment.

## Components

| Component | Purpose |
|---|---|
| `mac-orchestration` | Read and control macOS Terminal windows; paste files into an owned tmux session with prompt checks |
| `session-status` | Read agent process and tmux inventory for an explicitly selected host |
| `fleet-session-status` | Produce a compact table per configured host, combining Terminal/tmux sessions, reported activity, observation changes and port health |
| `agy-permissions` | Inspect a supplied permission-policy file without modifying it |
| `scripts/sync_skill_mirrors.py` | Keep the agent skill directories identical, including hidden and ignored entries |
| `scripts/check_public_content.py` | Check publishable files and Git history for private-data patterns |
| `scripts/check_port_pressure.py` | Observe local TCP port pressure and retain private alerts without controlling sessions |
| `scripts/install_port_pressure_watch.py` | Preview or install a per-user macOS monitor that checks every 60 seconds |

Most tools require Python 3.10 or later; the port pressure monitor and fleet session
tables also support Python 3.9. Terminal commands require macOS and Terminal.app;
remote commands require SSH access configured by the operator. The tmux path
requires an existing tmux server on the selected host. Nothing starts remote GUI
applications, authenticates accounts, or installs host software automatically.

## Try the examples

Run these commands from the checkout root. Example domains and records are
synthetic; substitute your own values only in ignored local configuration.

```bash
python3 .agents/skills/agy-permissions/scripts/check_agy_permissions.py \
  --settings examples/permissions.json

python3 .agents/skills/session-status/scripts/session_status.py \
  --snapshot examples/session-snapshot.json --screens

S=".agents/skills/mac-orchestration/scripts/mac-term.sh"
"$S" --help

# These require a host you control and an already running tmux session.
XMECHBAY_TMUX_SESSION=fleet "$S" -H worker.example.invalid \
  tmux-paste sample-project examples/goal.txt
```

The goal example is a template to inspect before use, not permission to start
workers or spend usage. Start with one authorized target and verify the resulting
screen before repeating an operation.

## On-demand session tables

Use [the synthetic host configuration](examples/fleet-hosts.json) as the shape for
an ignored `.local/fleet-status.json`, containing your own SSH destinations.
Request either host separately or both at once:

```bash
bash scripts/fleet-status.sh laptop-a
bash scripts/fleet-status.sh laptop-b
bash scripts/fleet-status.sh laptop-a laptop-b
```

The combined command collects concurrently and prints two separate tables. Each
shows every observed logical session with its target, model, state, reported
activity, process age, observation changes, and available listener pressure.
On a local Mac, these live commands also open the same snapshot in Microsoft
Excel at **150% zoom**, with one worksheet per host, colored states, filters and
frozen headers. Workbooks are saved privately under `.local/fleet-reports/`.
Install the optional dependency with `python3 -m pip install -r requirements-excel.txt`.
Use `--no-excel` for terminal-only output; `--json`, `--list`, SSH invocations and
offline snapshots do not open Excel by default. Use `--snapshot FILE --excel` to
open a supplied snapshot locally. Nothing launches Excel on the observed hosts.
Chat reports render these as formatted Markdown tables with colored state
symbols and text labels by default. Each host keeps its own timestamp, summary
and evidence legend; copying the output into a code block hides that formatting.
Use `--all` for all configured hosts, `--list` for host keys, or `--json` for
structured details. Unknown state, failed collection and stale health evidence
remain explicit. No session input or model request is sent. See the
[fleet session status skill](.agents/skills/fleet-session-status/SKILL.md) for
evidence rules, offline snapshots and cache options.

## Configure a deployment

Keep real hostnames, project paths, account choices, and operational reports in
ignored `.local/` files or an external configuration directory. Pass a bare host
name explicitly with `-H`; no private host aliases are embedded in the tools.
The `MAC_TERM_HOST` environment variable supplies a default host when useful.
`XMECHBAY_TMUX_SESSION` selects the host-loop session and defaults to `fleet`.
`XMECHBAY_RECORDS_DIR` selects a local refusal-log directory; its default is
`.local/records/` within this checkout.

New remote coding CLI sessions use tmux where supported, with one independently
selected Terminal viewer per agent session. Stagger the windows from bottom-left
to top-right so two terminal lines remain visible at each bottom edge. See the
[viewer workflow](.agents/skills/mac-orchestration/SKILL.md#visible-viewer-for-each-session)
for identity checks, existing-session handling and safe window cleanup.

Deploy from a dedicated checkout or immutable release directory. Do development
in a separate checkout. Routing a running system through a development working
tree makes every branch switch and partial edit a live deployment.

The [fleet control contract](docs/fleet-control-contract.md) defines session
ownership, naming, prompt handling, and verification. The
[port pressure recovery guide](docs/port-pressure-recovery.md) covers monitoring,
safe session recycling, and the limits of software updates. The
[publication policy](docs/publication-policy.md) explains what may be committed
and what a local scan can and cannot establish.

## Develop and verify

```bash
python3 -B -m unittest discover -s tests -v
python3 scripts/sync_skill_mirrors.py --check
python3 scripts/check_public_content.py
```

After editing a canonical skill, run `python3 scripts/sync_skill_mirrors.py` to
update its mirror. The checker refuses unexpected destination entries; inspect
them instead of deleting them automatically.
