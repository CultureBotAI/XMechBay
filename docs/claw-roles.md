# Roles for a supervised fleet

This is a reusable role design. Each deployment chooses its role holders,
supported agent types, cycle stages and authorization scopes in private
configuration. This document establishes no live assignment, service readiness
or permission to merge, restart, resume or send input.

## Responsibilities

A role is a documented responsibility held by an identified session. A small
deployment can give several compatible roles to one session. Multiple sessions
must not claim the same ownership boundary without an explicit handoff.

| Role | Config key (`roles`) | Responsibility | Boundary |
| --- | --- | --- | --- |
| Coordinator | *(coordinator session)* | Observe activity, resources and backlog; route findings; prepare a decision list and briefs for the owning supervisors | Does not perform domain work or send input to another supervisor's workers |
| Cycle supervisor | `cycle_supervisor` | Track all configured stages for its assigned repositories in smaller single-lane deployments; verify closure receipts; diagnose stalls and escalate prompts | Controls only explicitly bound sessions; does not acquire merge or restart authority from its role |
| Discovery supervisor | `discovery_supervisor` | Run bounded `source_discovery` and net-new `add_records` (`add/...` branches and discovery saturation ledgers) for repositories in the `discover` lane | Does not edit existing records, schemas or workflows; stops opening PRs when a repository reaches its open-PR cap or fails checks |
| Stewardship supervisor | `stewardship_supervisor` | Run `record_review`, `resolve_review_findings`, `open_issue_triage` and non-cohort failing-PR repair for repositories in the `steward` lane | Pauses net-new record additions until open PRs and status checks clear; does not merge PRs |
| Infrastructure supervisor | `infrastructure_supervisor` | Fix shared CI, schema and workflow causes once on `main`, maintain shared exporters/infrastructure tasks, and bootstrap explicitly assigned repositories (`infra` lane) | Does not mass-edit scientific claims across open PR branches when one base-branch fix resolves the cohort |
| Closure steward | `closure_steward` | Review mergeable green PR heads and triage stale drafts read-only via `gh`; file defect issues; record verified merge candidates for the operator | Never merges, pushes to another session's branch, substitutes batch sampling for per-PR diff review, or edits repository checkouts |
| Fleet maintainer | `fleet_maintainer` | Run read-only space/session inventories first, clean approved regenerable scratch on its host, refresh checkouts at idle checkpoints, and retire duplicate/completed loops via graceful exit | Does not change scientific content, touch unreachable hosts, or treat generic maintenance guidance as permission to remove data |

The owning repository session remains responsible for its changes and evidence.
A closure receipt should identify the reviewed head, checks, findings and their
disposition, and any human decision still required. A supervisor verifies the
receipt against the actual repository state before advancing the cycle.

**Merge authority is explicit and specific to the current task.** Green CI,
historical approval, a role assignment or a policy document does not authorize
a merge. Without the owner's approval, record the PR and decision needed, mark
the closure as held, and route it to the owner's decision list. Subsequent work
may proceed only if the configured cycle permits that held state. Related
external discussion: [held closure handling](https://github.com/CultureBotAI/culturebotai-claw/issues/1248).

## Two-fleet workload split and exclusive-writer contract

Larger deployments can split domain supervision into two complementary fleets so
record production never starves review, repair and infrastructure maintenance:

1. **Discovery and add-record fleet (`discovery_supervisor`):** network- and
   literature-bound `source_discovery` and net-new `add_records` work, writing
   only new record files on `add/...` branches and updating discovery saturation
   ledgers in dedicated discovery worktrees.
2. **Stewardship, closure and infrastructure fleet (`stewardship_supervisor`,
   `infrastructure_supervisor`, `closure_steward`, `fleet_maintainer`):**
   validation- and test-heavy `record_review`, `resolve_review_findings`,
   `open_issue_triage`, failing-PR repair, shared CI/exporter infrastructure,
   read-only green-PR diff review, and host session/storage hygiene.

To prevent cross-session branch collisions and duplicate loops across hosts,
routing enforces **at most one active writer lane per repository** from observed
backlog state (unless overridden by `mech_owner` or an explicit `claws.<key>.mechs`
assignment):

| Lane | Entry condition | Active writer | Concurrent read-only / hygiene lanes |
| --- | --- | --- | --- |
| `infra` | Systemic check-failure cohort (`>= pr_ci_cohort_min` and `>= pr_ci_cohort_ratio` failing PRs) or explicitly listed bootstrap repository | `infrastructure_supervisor` | `closure_steward` (stale-draft classification via `gh`), `fleet_maintainer` (duplicate-loop inventory) |
| `steward` | Open PRs `>= pr_backlog_warn`, any non-cohort failing check, or open issues `>= issue_steward_warn` | `stewardship_supervisor` | `closure_steward` (read-only diff review of green PRs via `gh`) |
| `discover` | Open PRs `< pr_backlog_warn`, zero failing checks, and open issues `< issue_steward_warn` | `discovery_supervisor` | `closure_steward` (read-only diff review of green PRs via `gh`) |
| `unknown` | Backlog unobserved (`--no-issues` or query failure) | Existing host `stewardship_supervisor` / `cycle_supervisor` (never triggers blind discovery) | Coordinator / `fleet_maintainer` restores backlog observation |

**Workload-matched host placement.** Because `discover`, `steward` and `infra`
are mutually exclusive per repository and `closure_steward` operates read-only
through GitHub metadata and diffs, heterogeneous multi-host deployments can bind
CPU-intensive lanes (`stewardship_supervisor`, `infrastructure_supervisor`) to
higher-compute hosts while placing I/O- and API-bound lanes
(`discovery_supervisor`, `closure_steward`, and host-local `fleet_maintainer`)
on lighter hosts. Claws holding only non-writer roles configure `"mechs": []` so
they never capture repositories as catch-all owners.

## Session ownership and capability checks

Before assigning work, read the session's current screen and available reports,
confirm its role with the operator, and record its binding privately. Bindings
include the relevant socket/window, session/pane, process, tty and thread.
Unknown or ambiguous sessions remain observe-only.

Check that the installed supervisor supports the selected agent and transport.
Support for one Terminal application does not imply support for another agent
or for tmux. Use the [fleet control contract](fleet-control-contract.md) for
prompt handling, ownership and verification; raw terminal commands do not inherit
the helper's checks.

| Contract layer | Role mapping | Input boundary |
| --- | --- | --- |
| Reporting interface | Coordinator's observation and planning | No session input |
| Host controller | An explicitly authorized controller | Only identified host-loop targets, with before/after checks |
| Repository supervisor | Cycle supervisor | Only the sessions in its verified supervisor map |
| Task dispatcher | The session that launched workers | Only its owned workers |

Fleet-wide roles may need host-local actions. An SSH process may have a different
environment or authentication context from a logged-in agent. Verify the required
context instead of assuming credentials or service access propagate. Provisioning
credentials and restarting sessions require the operator's specific authorization;
keep those actions and receipts outside public source.

## Declaring cycle stages

Map each configured stage to the repository's actual skill or mark it
`not_applicable` with a reason. Do not infer capability from a repository name or
assume that every project uses the same skill names. A manifest can resolve
generic stages such as discovery, record addition, graph curation and record
review to the appropriate local implementation.

Define evidence for completion alongside the stage:

- Discovery and record addition need a ledger of searched sources, candidate
  records and deduplication decisions. A statement that no new records were found
  is limited to the declared search frontier and available sources.
- Graph curation needs before/after coverage and evidence for edited assertions;
  records with no applicable graph should say so.
- Record review needs validated, versioned findings tied to the target records.
  Choose batch sizes to keep each PR reviewable and retain independent review of
  each exact head.
- Publication needs the source/destination checks in the
  [publication policy](publication-policy.md), plus the repository's own checks
  and required human decisions.

Related external design discussions cover [stage-to-skill mappings](https://github.com/CultureBotAI/culturebotai-claw/issues/1246),
[discovery evidence](https://github.com/CultureBotAI/culturebotai-claw/issues/1247)
and [review work-in-progress limits](https://github.com/CultureBotAI/culturebotai-claw/issues/1230).
These references describe design work, not the capabilities or state of a live deployment.

## Optional research services

Search, literature-access and chemical-database services can support discovery,
curation and record review when the operator has the required access. The
[library-service guide](lbnl-tdm-and-cborg-mcp.md) describes one integration family;
using this role design does not require that institution or those services.

The maintainer verifies configuration and runs an authorized provider canary.
The supervisor arranges any approved environment refresh at a safe checkpoint.
The coordinator places dependent work only where readiness is verified. The
closure steward checks that publication contains permitted claims and citations,
rather than private credentials, licensed full text or operational receipts.

The read-only `scripts/adhoc/lbnl_readiness_inventory.py` reports configured
names, booleans and modes. Its output belongs in private storage. Configuration
presence is not proof that a provider works; a canary verifies only what it
actually exercised.

## Adoption

1. Select role holders and record their ownership boundaries privately.
2. Verify the installed tools support the intended agents and transports.
3. Map cycle stages to skills and define their closure evidence.
4. Test routing and receipts with synthetic fixtures, then one authorized live
   canary using the intended launcher and environment.
5. Review the result before expanding the rollout or changing source pins.

External discussions of [supervisor self-description](https://github.com/CultureBotAI/culturebotai-claw/issues/1227)
and [manifest operations fields](https://github.com/CultureBotAI/culturebotai-claw/issues/1228)
may inform a deployment's design. Historical inventories, research reports,
runtime bindings and prior authorization records remain private evidence; they
are not prerequisites to use this generic guide.
