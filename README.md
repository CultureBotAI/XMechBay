# XMechBay

Reusable infrastructure for observing and controlling agent sessions across
workstations. It separates host-level session control from repository task
dispatch, verifies input targets, and keeps deployment data out of source control.

The repository contains code, general operating guidance, and synthetic examples.
It does not ship host inventories, account settings, session transcripts, billing
data, or a particular organization's deployment.

This public repository is the sole maintained implementation. A private
operations workspace pins a reviewed public commit and holds its own deployment
configuration and evidence. Never push that workspace's history here. Before a
first push from any clone, follow the [publishing setup and migration
guide](docs/publication-migration.md) and install the local publication guard.

## Components

| Component | Purpose |
|---|---|
| `mac-orchestration` | Read and control macOS Terminal windows; paste files into an owned tmux session with prompt checks |
| `session-status` | Read agent process and tmux inventory for an explicitly selected host |
| `fleet-session-status` | Serve a local `127.0.0.1` HTML symbol dashboard (`scripts/fleet-dashboard.sh`) or render per-host Markdown/Excel session tables (`scripts/fleet-status.sh`) across local and SSH Macs |
| `fleet-ops` | Observe sessions, host observability/port-watch health, host resources and every Mech's issue/PR backlog in one pass, rank findings, route each to the user, the responsible claw or XMechBay maintenance, prepare per-claw briefs and guarded dispatch commands, and synthesize systemic recommendations with non-duplicate GitHub issue drafts (`recommend`; [playbook](docs/fleet-operations-playbook.md)) |
| `agy-permissions` | Inspect a supplied permission-policy file without modifying it |
| `agent-infra-unify` | Audit and converge a repository's instruction files and skill trees to the layout every agent reads |
| `scripts/agent_infra_audit.py` | Report, per checkout or user-level file set, whether Claude Code, Codex and Antigravity read the same instructions and skills |
| `scripts/agent_infra_converge.py` | Move one checkout to that layout on a topic branch, without committing |
| `scripts/sync_global_agreements.py` | Keep one common block identical across the user-level instruction files, with backups |
| `scripts/sync_user_skills.py` | Give every agent's user-level skill directory the same skills through logged symlinks |
| `scripts/sync_skill_mirrors.py` | Verify the skill trees are one directory, or keep copies identical where symlinks cannot ship |
| `scripts/check_public_content.py` | Check publishable files and Git history for private-data patterns |
| `scripts/install_publication_guard.py` | Install a clone-local pre-push guard that pins the expected repository identity and clean history root |
| `scripts/tmp_state_guard.py` | Copy unpushed, uncommitted or soon-deleted agent work out of `/private/tmp` and the per-user temporary directory before macOS cleans them; `scripts/install_tmp_state_guard.py` schedules it nightly ([guide](docs/tmp-state-guard.md)) |

Python 3.10 or later is required for the general script suite; `fleet-session-status`
(`fleet_collect.py`, `fleet_view.py`, `fleet_status.py`, `fleet_dashboard.py`),
`fleet-ops` (`fleet_ops.py`, `host_resources_probe.py`), and the tmp-state guard
(`tmp_state_guard.py`, `install_tmp_state_guard.py`) also run on Python 3.9
(stock macOS `/usr/bin/python3`) using the standard library alone. Optional
`.xlsx` workbook export in `scripts/fleet-status.sh` uses `XlsxWriter`
(`python3 -m pip install -r requirements-excel.txt`, or pass `--no-excel`). The shell
wrappers honor `XMECHBAY_PYTHON` when selecting an explicit interpreter and resolve
symlinked entrypoints cleanly. Terminal commands require macOS and Terminal.app;
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

bash scripts/fleet-status.sh --snapshot examples/fleet-snapshot.json

python3 .agents/skills/fleet-ops/scripts/fleet_ops.py --config examples/fleet-ops.json \
  observe --snapshot examples/fleet-snapshot.json --resources-json examples/fleet-ops-resources.json \
  --issues-json examples/fleet-ops-issues.json --out .local/fleet-ops/runs/example

bash scripts/fleet-dashboard.sh --snapshot examples/fleet-snapshot.json \
  --export-html /tmp/xmechbay-example-dashboard.html

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
Record the selected public commit in the private deployment manifest. Upgrade
that pin through review and an authorized canary; keep reusable fixes in public
source and local host/account choices in private configuration.

The [fleet control contract](docs/fleet-control-contract.md) defines session
ownership, naming, prompt handling, and verification. The
[publication policy](docs/publication-policy.md) explains what may be committed
and what a local scan can and cannot establish.

## Develop and verify

Use a `public/<topic>` branch and the commit-message format
`[public-source] <summary>`, including the space after the bracket.
The [publication policy](docs/publication-policy.md) defines the pre-upload
history scan, synthetic-data review and server rules. A passing CI run is a
merge check; it cannot undo disclosure from a public push.

```bash
python3 -B -m unittest discover -s tests -v
python3 scripts/sync_skill_mirrors.py --check
python3 scripts/check_public_content.py
```

`.claude/skills` is a symlink to `.agents/skills`, so a canonical skill needs no
mirroring step; the check confirms the two paths are one directory (in either
direction). A checkout made without symlink support receives stub files
instead of links; delete them and keep identical copies with `python3
scripts/sync_skill_mirrors.py`, whose checker refuses unexpected destination
entries so that they are inspected rather than deleted. The
[agent infrastructure contract](docs/agent-infra-contract.md) explains the
layout and `python3 scripts/agent_infra_audit.py .` reports compliance.
