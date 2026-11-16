# Fleet operations playbook

Practices for running a Mech fleet: several Macs, a claw supervisor per host and
one or more agent sessions per Mech repository. These are generic defaults used
by the [`fleet-ops`](../.agents/skills/fleet-ops/SKILL.md) thresholds and
recommendations. Review their suitability for each deployment; this document
contains no live fleet inventory or operational authorization.

## Operating loop

1. **Observe** activity, resources and backlog together (`fleet_ops.py observe`).
   A disk alarm, a stalled loop and a 150-PR queue are often one problem seen
   three ways.
2. **Decide** the owner of each finding: the user (merges, prompts, usage,
   starting supervisors), the claw responsible for the Mech (Mech work), or
   XMechBay maintenance (host health and durability).
3. **Delegate** Mech work to claws only, through a brief plus a short goal, one
   canary claw first. Never send input to a Mech session from outside its claw.
4. **Follow up**: read the claws' reports, then observe again. A finding that
   does not move after two cycles needs a different owner or an issue.

## Disk and memory

Keep every host above **100 GiB free**; below **50 GiB** stop disk-heavy work.
The tmp-state guard refuses to copy rescued work when its backup disk is below
its 50 GB floor, so a nearly full disk also removes the safety net for work left
in temporary directories.

Where the space goes, and the policy for each:

| Source | Policy |
|---|---|
| Test scratch (`pytest --basetemp`, `tempfile.mkdtemp`) | Put it under the repository's ignored `.temp/` with the issue or run id, delete it after a passing run, and keep only the result (junit, receipts). Prefer pytest's `tmp_path_retention_policy = "failed"`. Never leave multi-GiB fixtures in `/private/tmp`. |
| Leaked temporary directories | Library code must use `tempfile.TemporaryDirectory()` as a context manager or clean up in `finally`; a killed process can otherwise leave its scratch behind. |
| Campaign working trees (`.temp/<campaign>/`) | Give each campaign a retention note: owner, purpose, what is regenerable, and when it can go. Ask the owning session for that inventory before any cleanup; never delete a campaign tree from outside it. |
| Linked worktrees | Remove a worktree once its branch is merged, after moving any ignored outputs that matter. Prune registrations whose directories are gone. |
| Agent logs | Archive closed Codex rollouts older than 14 days into verified compressed archives; set Claude Code `cleanupPeriodDays` high (for example 3650) and archive transcripts, because the default deletes them after 30 days. |
| Package caches | `uv cache prune` and `npm cache clean` are safe; most of a uv cache is shared with installed environments and frees little. |
| Swap | Swap files share the data disk. Long-lived idle agent sessions keep memory paged out; close duplicates at an owned idle checkpoint and do not run several memory-heavy validations at once. |
| Time Machine local snapshots | Space from deleted files returns only as hourly snapshots expire (about a day); check reclaim the next day before deleting more. |

## Durability

- No session keeps working state in `/tmp` or `$TMPDIR`; macOS deletes it
  (`tmp_cleaner` nightly after four days untouched, `dirhelper` at 03:35 and at
  boot). Use `~/agent-work/<repo>/` or a repository's `.temp/`.
- Install the tmp-state guard on every host and read its report: a run that
  skips entries means unrescued work.
- Push or bundle unpublished commits the same day. Work kept only in a tool's
  private directory (for example a recovery clone under `~/.codex`) is invisible
  to cleanup reviews and easy to lose; move it under `~/agent-work/`.
- A publication hold needs an issue with the reason and the decision owner, so
  held work does not sit unpublished for weeks.

## Activity and utilization

- **Every claw declares its role; never two with the same role on one host.**
  Unlabelled claws on one host make delegation ambiguous; each claw should say
  which role it holds and which Mechs it covers. The proposed roles
  (coordinator, cycle supervisor, closure steward, fleet maintainer) are in
  [claw roles](claw-roles.md).
- **One responsible claw and one primary loop per Mech.** Duplicate idle
  sessions of the same Mech on another host cost memory, ports and attention.
- **Stalled goals** get a diagnosis within a day; **paused goals** get a resume
  or close decision; prompts go to the user's attention list the same day.
- A fleet where no Mech session is working is a planning gap, not a resource
  win: ask each claw for its next goal proposal.

## Backlog and review flow

Production must not outrun review. When mergeable PRs pile up by the hundred,
the merge decision, not the agents, is the bottleneck.

- Set a **work-in-progress limit** per Mech (for example 25 open PRs); above it
  the Mech stops opening PRs and works the queue.
- Group homogeneous PRs (for example record-review batches) for scheduling and
  shared checks. Independently review each PR's exact current head; sampling
  across a batch does not replace that review.
- Present green, independently reviewed PRs to the user as one decision list per
  Mech. Each merge still requires the user's explicit approval for that PR.
- When most of a Mech's PRs fail checks, fix the shared cause (CI, base branch,
  dependency) once and file it as an infrastructure issue; do not repair PR by PR.
- Close or refresh drafts untouched for 14 days, with a reason.

## Evolving the X-Mech model

Proposals, to be adopted through issues in the owning repositories:

1. **Operations in the fleet manifest.** Extend the claw fleet manifest (already
   the single source of membership) with each Mech's responsible claw, primary
   host, disk budget, retention policy and PR limit, so the dashboard, the claw
   and fleet-ops read one definition instead of local configs.
2. **Claw self-description.** Each claw writes a small machine-readable status
   file (role, host, supervised Mechs, current goal, last report) so a delegator
   can identify the right claw without reading screens.
3. **Retention metadata for working data.** A `.temp/<campaign>/RETENTION.json`
   (owner, purpose, regenerable, keep-until) makes cleanup safe and automatic
   instead of a forensic review.
4. **Capacity-aware placement.** Schedule heavy jobs (KG builds, full-corpus
   validation) on the host with free disk and memory, using the resource view.
5. **Scheduled observation.** Run `observe` nightly into private storage and
   compare runs, so trends (disk growth, backlog growth, stalls) are visible
   before they become incidents.
6. **Review capacity as a first-class resource.** Classify PRs (data records,
   code, infrastructure) with different review gates, and track review
   throughput next to agent throughput.
