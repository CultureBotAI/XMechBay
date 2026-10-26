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
| `agy-permissions` | Inspect a supplied permission-policy file without modifying it |
| `scripts/sync_skill_mirrors.py` | Keep the agent skill directories identical, including hidden and ignored entries |
| `scripts/check_public_content.py` | Check publishable files and Git history for private-data patterns |

Python 3.10 or later is required. Terminal commands require macOS and Terminal.app;
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

## Configure a deployment

Keep real hostnames, project paths, account choices, and operational reports in
ignored `.local/` files or an external configuration directory. Pass a bare host
name explicitly with `-H`; no private host aliases are embedded in the tools.
The `MAC_TERM_HOST` environment variable supplies a default host when useful.
`XMECHBAY_TMUX_SESSION` selects the host-loop session and defaults to `fleet`.
`XMECHBAY_RECORDS_DIR` selects a local refusal-log directory; its default is
`.local/records/` within this checkout.

Deploy from a dedicated checkout or immutable release directory. Do development
in a separate checkout. Routing a running system through a development working
tree makes every branch switch and partial edit a live deployment.

The [fleet control contract](docs/fleet-control-contract.md) defines session
ownership, naming, prompt handling, and verification. The
[publication policy](docs/publication-policy.md) explains what may be committed
and what a local scan can and cannot establish.

The [multi-Claw research and roadmap](docs/research/federated-mech-fleets-2026-10-26.md)
assesses how one MechBay session could coordinate independent remote fleets toward
verified, high-value outcomes. It distinguishes existing tools from proposed
coordination services and includes a dependency-ordered implementation plan.

## Develop and verify

```bash
python3 -B -m unittest discover -s tests -v
python3 scripts/sync_skill_mirrors.py --check
python3 scripts/check_public_content.py
```

After editing a canonical skill, run `python3 scripts/sync_skill_mirrors.py` to
update its mirror. The checker refuses unexpected destination entries; inspect
them instead of deleting them automatically.
