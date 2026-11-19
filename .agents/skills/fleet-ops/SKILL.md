---
name: fleet-ops
description: Optimize the use of the Mech fleet. Observe every Mac's agent sessions, host observability and port-watch health, host resources (disk, swap, temporary-root growth, tmp-state guard, transcript retention) and every Mech repository's issue and PR backlog in one pass; rank what needs attention; decide whether each item belongs to the user, the claw responsible for that Mech, or XMechBay maintenance; prepare per-claw briefs and guarded dispatch commands; and synthesize systemic infrastructure, supervision, workflow and capacity conclusions with non-duplicate GitHub issue drafts (`recommend`). Use for fleet health checks, interpreting fleet session status reports, "what should the fleet do next", disk or memory pressure across hosts, stalled or idle Mech loops, PR/issue backlog triage across Mechs, delegating work to claws, and drafting systemic fleet issues.
---

# Fleet operations: observe, decide, delegate

The fleet is a set of Macs running **claw** sessions (supervisors) and **Mech**
sessions (one repository each). This skill gives one global view of activity,
resources and backlog, and routes every problem to the party that owns it. It
never acts on a Mech session directly: Mech work is delegated to the claw
responsible for that Mech, following the
[fleet control contract](../../../docs/fleet-control-contract.md). Practices
behind the thresholds and actions are in the
[fleet operations playbook](../../../docs/fleet-operations-playbook.md).

## 1. Observe (read-only)

```bash
S=.agents/skills/fleet-ops/scripts/fleet_ops.py
python3 $S observe                                   # live: sessions, resources, GitHub backlog
python3 $S observe --dashboard-url http://127.0.0.1:8765   # reuse a running fleet dashboard's /api/status
python3 $S observe --dashboard-html .local/fleet-reports/dashboard-latest.html   # reuse a saved dashboard HTML export
python3 $S observe --status-json path/to/status.json # reuse a saved /api/status JSON payload
python3 $S observe --no-issues                       # skip GitHub
# offline example with the synthetic fixtures:
python3 $S --config examples/fleet-ops.json observe --snapshot examples/fleet-snapshot.json \
  --resources-json examples/fleet-ops-resources.json --issues-json examples/fleet-ops-issues.json --out .local/fleet-ops/runs/example
```

- **Sessions** come from the `fleet-session-status` collector, a live dashboard
  (`--dashboard-url`), a rendered dashboard HTML snapshot (`--dashboard-html`,
  parsing `<script id="initial-payload">`), a saved `/api/status` JSON file
  (`--status-json`), or an offline fixture (`--snapshot`); these four session
  source flags are mutually exclusive. Screen states are observations, not proof
  of work.
- **Resources** come from `host_resources_probe.py`, run locally or piped over
  SSH to the system Python. It only reads, bounds every `du` with a timeout and
  reports a timeout as unknown, never as zero (fleet-ops raises an observation
  finding for it).
- **Backlog** is one GraphQL query per three repositories through `gh` (the
  newest 100 open PRs per repository; older drafts beyond that are not counted).
  The repository list comes from the claw fleet manifest (`fleet_manifest`), plus
  `repos` in the config.

A run directory (default `.local/fleet-ops/runs/<UTC>/`, mode 0700) keeps
`sessions.json`, `resources.json`, `issues.json`, `findings.json` and
`REPORT.md`. Show the user the report's host table and the critical and warning
findings first; the backlog rows can be long.

## 2. Decide who owns each finding

| Owner | Gets | Examples |
|---|---|---|
| **user** | Decisions only the user can make | merges, all three human-blocker classes across any role (`keystroke` TUI prompts including `subagent-approval` and the `agy_work_fixable` subset fixable via `agy-work -c`; `agent_message` end-of-turn plain-text questions/approvals; `pasted_prompt` / unsent composer drafts not running), usage limits, supervisor (`claw`/`mechbay`) or non-Mech stalls, starting a missing claw, dormant Mechs |
| **claw** (role-routed or explicit owner) | Mech work, read-only closure review, or host-local hygiene | `discovery_supervisor`: bounded discovery/add-record work in `discover` lane; `stewardship_supervisor`: stalls, pauses, failing PRs, PR/issue backlog batching in `steward` lane; `infrastructure_supervisor`: systemic CI cohorts (`infra` lane) and pinned bootstrap Mechs (`pinned` lane); `closure_steward`: read-only diff review of unapproved green PRs and stale-draft triage; `fleet_maintainer`: reachable same-host session/storage hygiene |
| **XMechBay maintenance** | Coordinator-owned observability, cross-host hygiene, unreachable hosts, and unmaintained hosts | unreachable hosts (`reachable: false`), failed resource probes, Coordinator GitHub query failures, cross-host duplicate-loop hygiene, and host observability/resource/durability/hygiene findings on hosts without a live `fleet_maintainer` claw |

A Mech's primary writer claw is resolved in this order:
1. `mech_owner` in the config (must reference a supervisor/writer claw);
2. a supervisor/writer claw whose `mechs` list explicitly names the Mech (`pinned` lane);
3. when any claw declares `roles` (see [`docs/claw-roles.md`](../../../docs/claw-roles.md)),
   the Mech's observed backlog lane (preferring live matching role claws before offline fallbacks):
   - `infra` (`>= pr_ci_cohort_min` and `>= pr_ci_cohort_ratio` failing PRs) -> `infrastructure_supervisor` (when `accept_infra_cohorts` is enabled; falls back to `stewardship_supervisor` / `cycle_supervisor`);
   - `steward` (`open_prs >= pr_backlog_warn`, any failing check, `open_prs > len(prs)`, or `open_issues >= issue_steward_warn` unless `discover_ignores_issue_backlog` is true) -> `stewardship_supervisor` (or `cycle_supervisor`);
   - `discover` (healthy queue below those thresholds with all open PRs observed) -> `discovery_supervisor` (or `cycle_supervisor`);
   - `unknown` (`--no-issues` or query error) -> `stewardship_supervisor` / `cycle_supervisor` (never triggers blind discovery);
4. otherwise a catch-all supervisor claw (`"mechs": ["*"]`) on the host running
   the Mech's busiest loop.

An explicit `"mechs": []` (or a claw holding only `closure_steward` /
`fleet_maintainer`) never acts as a catch-all primary Mech owner. When a live
`closure_steward` claw is configured, `user:merges` keeps merge authority while
`closure_steward` receives a read-only diff-review finding for unapproved green
PRs and owns stale-draft triage. A closure steward whose config sets
`merge_authority: "after_per_pr_review"` gets brief and goal text that let it
merge Mech-repository PRs after its own review of the exact head with green
checks; every other claw keeps the no-merge rule. With
`discover_ignores_issue_backlog`, a Mech whose only backlog is open issues stays
in the `discover` lane. Its issue finding goes to a live stewardship claw, else a
live cycle supervisor (then offline ones), as triage-only work: that claw's brief
lists the Mech as triage-only, with no record edits or PRs, so the discovery claw
remains the only writer. With no such claw in scope, the finding goes to the user
as a configuration warning, never to the discovery claw. `merge_authority` is
rejected on a claw that also holds a supervisor (writer) role, and green PRs in
`infra_repos` repositories always stay with `user:merges`. With a valid grant,
the Mech's green-PR finding goes to that closure claw instead of `user:merges`.

## 3. Plan

```bash
python3 $S plan .local/fleet-ops/runs/<run>
```

This writes `plan/briefs/<claw>.md` (the claw's findings with evidence and the
standing rules), `plan/goals/<claw>.txt` (under 1000 characters, pointing at the
brief), `plan/DECISIONS.md` (ordered into high-leverage human action buckets:
pasted/unsent prompts not running -> `agy-work -c` approval stalls -> interactive
TUI keystrokes -> plain-text agent messages awaiting reply -> supervisor/claw
stalls -> merges & fleet scope, plus prepared claw brief readiness),
`plan/MAINTENANCE.md` and `plan/DISPATCH.md` with the exact copy and paste
commands. It sends nothing.

## 3b. Interpret, recommend, and draft systemic issues (read-only)

When interpreting a fleet status or `fleet-ops` run to draw root-cause
infrastructure, supervision, workflow and capacity conclusions — and to prepare
non-duplicate GitHub issues — run `recommend`:

```bash
python3 $S recommend .local/fleet-ops/runs/<run>
# override infrastructure target repositories (xmechbay or claw):
python3 $S recommend .local/fleet-ops/runs/<run> --infra-repo xmechbay=example-org/XMechBay --infra-repo claw=example-org/claw
# offline / deterministic open-issue reconciliation (--open-issues-json and --no-github are mutually exclusive):
python3 $S recommend .local/fleet-ops/runs/<run> --open-issues-json path/to/open-issues.json
python3 $S recommend .local/fleet-ops/runs/<run> --no-github
```

`recommend` recomputes findings under the active `--config` thresholds and
aggregates per-host observability (`tmux_probe_failed`, detached `P*` session
clusters, `agent_discovery_partial`, missing/stale/degraded `port-watch`
health), claw and permission supervision readiness (composer drafts, awaiting
user replies, plain-`agy` approval stalls fixable via `agy-work` /
`R-AGY-WORK-PERMISSIONS`, multi-claw role routing, missing claws, manifest vs
`CANONICAL_MECHS` drift), workflow bottlenecks (mass CI check-failure cohorts
such as >=50% failing PRs in one repository, >100-PR GraphQL truncation,
multi-Mech publication stalls), and host capacity/durability (swap saturation +
multi-host duplicate loops + session sprawl, Codex rollout growth, low free-disk
headroom, `tmp-state` guard state, Claude 30-day transcript deletion).

It queries paginated open issues read-only across `infra_repos`
(`CultureBotAI/XMechBay`, `CultureBotAI/culturebotai-claw`) and any Mech
repository with a systemic CI cohort, then writes atomic mode-`0600` files under
`<run>/recommendations/`:

- `RECOMMENDATIONS.md`: prioritized (`P0`–`P2`) causal conclusions, concrete
  recommendations, local evidence, and `tracked` links to existing open issues.
- `ISSUE_DRAFTS.md`: public-safe GitHub issue drafts (omitting hostnames, local
  paths, private IPs, emails, tokens, and `@`-mentions) for untracked systemic
  gaps (`new_issue`), or marked `unverified_offline` / `lookup_failed` when
  GitHub was not queried or reconciliation was incomplete.
- `recommendations.json`: structured summary and full recommendation objects.
- `open_issues_snapshot.json`: cached open-issue index written when live GitHub
  reconciliation (`source_mode == "github"`) runs.

**Never call `gh issue create` automatically.** Show the user the exact target
repository, title and body for each proposed new issue and proceed only after
explicit yes/no approval in the current conversation.

## 4. Dispatch (needs the user's approval for the batch)

1. Present `DECISIONS.md` and the per-claw finding counts; get approval to
   dispatch, naming the claws.
2. Confirm each target. `DISPATCH.md` lists the live claw candidates and
   resolves one only when it is unambiguous and controllable:
   - more than one matching claw session on the host, or a `target_hint` that
     does not match exactly one candidate, leaves it unresolved. Set
     `target_hint` (a terminal id such as `10` or `T10/1`, a tty, or a tmux
     session/window selector such as `claw-primary:steward`) or per-claw
     `tmux_session` only after reading the screens and confirming which claw
     owns the Mechs;
   - a claw session with an unsent composer draft or pasted prompt not running
     (`human_block_kind: "draft"` / `"pasted_prompt"`), a human prompt
     (`APPROVAL`, `QUESTION`, `SIGN-IN`, `TRUST-PROMPT`, `UPDATE-PROMPT`, or
     `subagent-approval`), an end-of-turn agent message awaiting user reply
     (`human_block_kind: "agent_message"`), or `Goal hit usage limits` is left
     unresolved so pasting never merges a goal into unsent text or a blocked
     session;
   - a Terminal claw must be tab 1, and the plan's first command lists the
     window so you can check that its selected tab has the claw's tty, because
     `mac-term.sh paste` types into the selected tab;
   - a claw inside a tmux session other than its configured controller-owned
     session (`claws.<key>.tmux_session`, falling back to
     `hosts.<key>.tmux_session`, then `controller_tmux_session`, default
     `fleet`), or without a `tmux_window` name, is left unresolved; the plan
     never retargets `XMECHBAY_TMUX_SESSION` to an unconfigured session.
   All paths in `DISPATCH.md` are absolute, so it runs from any directory.
3. **Canary one claw**: copy its brief to the claw's host, read the screen,
   paste the goal with `mac-term.sh paste` (Terminal) or `tmux-paste` (tmux),
   read the screen again, and confirm the claw started on the brief.
4. Only then send the remaining claws, one at a time with the same checks.
5. Record what was sent where in the private run directory.

Never type into a screen that shows a prompt, never resume goals in bulk
(resumes spend usage), and never send a Mech session input directly: the claw
owns its Mechs. The guarded helpers refuse unreadable or prompting screens.

## 5. Follow up

Read each claw's `report.md`/`summary.json` (named in its brief), act on the
user decisions, run maintenance with the matching skills (`safe-delete`,
`fleet-repo-crud-cleanup`, `disk-usage-report`, the tmp-state guard installer),
and run `observe` again to confirm the findings moved. File an issue in the
right repository for every systemic cause the run exposes.

## Configuration

Copy [`examples/fleet-ops.json`](../../../examples/fleet-ops.json) to the
ignored `.local/fleet-ops.json` (or set `XMECHBAY_FLEET_OPS_CONFIG`):

- `fleet_status_config`: the `fleet-session-status` host config, relative to
  this file (hosts, SSH destinations, Python paths).
- `fleet_manifest`: path to the claw fleet manifest (`mechs:` with
  `display_name` and `github`), `repos` for additions or overrides.
- `hosts.<key>.watch`: extra directories to size on that host, such as a
  campaign's `.temp`. A watched path whose name contains a Mech is routed to
  that Mech's claw when it runs on the same host, or to the host's maintenance
  owner otherwise.
- `claws.<key>`: `host`, `label`, `repo` (the claw checkout's basename),
  `mechs` (explicit Mech names, `["*"]` for catch-all supervisors, or `[]` for
  non-catch-all role claws), optional `roles` (`discovery_supervisor`,
  `stewardship_supervisor`, `infrastructure_supervisor`, `closure_steward`,
  `fleet_maintainer`, `cycle_supervisor`), optional `accept_infra_cohorts`
  (boolean controlling whether an `infrastructure_supervisor` receives systemic
  `infra` cohorts), optional `closure_mechs` (Mech scope override when a claw
  combines a supervisor role with `closure_steward`), optional `merge_authority`
  (only `"after_per_pr_review"`, only on a `closure_steward` claw; the owner's
  explicit grant, recorded in private config), `brief_dir` on that host,
  optional `target_hint`, and optional `tmux_session` (when one host runs
  multiple controller-owned tmux sessions).
- `mech_owner`: explicit Mech-to-claw ownership overrides.
- `hosts.<key>.tmux_session`: default controller-owned tmux session on that host.
- `infra_repos`: map of infrastructure repositories (`xmechbay`, `claw`) checked
  and targeted by `recommend` (defaults to `CultureBotAI/XMechBay` and
  `CultureBotAI/culturebotai-claw`; overridable via `--infra-repo KEY=OWNER/REPO`).
- `thresholds`: override any default in `DEFAULT_THRESHOLDS` (`disk_critical_gib`,
  `disk_warn_gib`, `swap_warn_ratio`, `swap_warn_min_gib`, `tmp_root_warn_gib`,
  `watch_warn_gib`, `codex_sessions_warn_gib`, `guard_stale_hours`,
  `idle_sessions_warn`, `non_working_sessions_warn`, `blind_sessions_warn`,
  `achieved_goal_age_hours`, `pr_stale_days`, `pr_backlog_warn`,
  `issue_steward_warn`, `discover_ignores_issue_backlog`, `pr_ci_cohort_min`,
  `pr_ci_cohort_ratio`).

Keep the real config, runs, briefs and dispatch records under `.local/`; they
hold hostnames and paths. Only synthetic examples belong in Git.
