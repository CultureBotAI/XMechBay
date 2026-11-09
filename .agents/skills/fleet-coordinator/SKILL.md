---
name: fleet-coordinator
description: Coordinate several long-running agent sessions (worker sessions on one or more hosts) as the single coordinator. Assign scoped work streams through a claims file, write and deliver briefs, watch for finished turns, verify reports, keep durable records, bring decisions to the user with recommendations, and carry out only what the user approves. Use when one session must direct other agent sessions, serialize their merges and deployments, or hand coordination to another session.
---

# Fleet coordinator

The coordinator directs; it does not do the workers' tasks. Exactly one
coordinator drives a fleet at a time. Two sessions sending input to the same
worker conflict, so a handoff ends the old coordinator's input before the new one
starts.

Read first: `docs/fleet-control-contract.md` (ownership, naming, input rules),
the `mac-orchestration` skill (guarded input to Terminal and tmux), the
`session-status` skill (inventory) and `docs/tmp-state-guard.md`. Hosts,
accounts and repository paths are never built in. They come from the user and
from private deployment notes kept outside this repository.

## The loop

1. **Observe.** Read each worker's screen with the guarded reader, plus any
   deployment monitors the user named. A watcher that waits for a turn to end
   can misread a pause, such as a worker waiting on a subagent or a timer, as
   idle. Read the screen again before acting.
2. **Read the report, not the screen.** At the end of a turn, workers name a
   report or receipt file. Open it, then verify its key claims against the
   source of truth: PR heads, checks and merge state from the forge, process
   identity from `ps`/`lsof`, and "unpushed" from `git ls-remote`, not from
   local remote-tracking refs, which go stale.
3. **Record.** Append a dated entry to the private operations record: what was
   sent, what came back, what was verified, what is pending. Never rewrite
   history; add a correction line instead.
4. **Decide with the user.** Present each decision with a recommendation and the
   reason for it. Approval is per action: a merge approval covers that PR at
   that head, and a deployment approval covers that deployment.
5. **Act, then verify the side effect.** Immediately before acting, re-check
   that the state matches what was approved: the same head, still mergeable,
   the worker still idle, the prompt unchanged. Then confirm the effect: the PR
   is in the queue, the session accepted the input, the file exists and is not
   empty.

## Streams and claims

Give every worker one scoped stream, and record it in a claims file kept
outside any repository checkout. `templates/claims.md` is a template. Each
stream lists:
- the worker;
- its host;
- what it owns (repositories, files, PRs);
- what it must not touch.

Every brief tells the worker to read the claims file before editing a shared
file. Workers do not pick up tasks by themselves; they act only on briefs.
Update the claims file whenever a stream starts, changes or finishes.

## Briefs

A brief is a short file, at most 4,000 bytes, saved in the private operations
directory and delivered with the guarded paste. `templates/brief.md` is a
template. It states:
- the user's decision, quoted;
- context the worker cannot know;
- numbered scope;
- the rules (worktrees in durable storage, no merges, no input to other
  sessions, no mentions);
- exactly what to report.

End with "then stop". Never put deployment secrets in a brief.

## Prompts belong to the user

- Never type into a window showing an approval, question, sign-in, trust or
  update prompt. The guard refuses, and its refusal is the answer.
- The guard's explicit override is for one prompt the user explicitly told you
  to answer. It binds to the screen the guard first observes and allows one
  input. On a screen that keeps changing (a working session), it cannot bind,
  so wait for the turn to end.
- A worker's queued questions can often be read from its session log instead
  of by opening the question panel. Asynchronous questions accept an ordinary
  message as the answer, once the user has given it.

## Merges and deployments

- **Merging is the user's decision for each PR.** Re-check the reviewed head
  before queuing.
- **Merge queues can fail an entry stacked behind another,** for example when a
  required audit insists the base is already on the protected branch. Queue
  one PR at a time with `scripts/serial_merge_queue.sh`. It stops when a later
  PR is no longer at its reviewed head or no longer merges cleanly.
- **When several merged PRs touch the same component,** have their combined
  change reviewed independently before one deployment. Deploy once, verify, and
  keep the rollback.
- **Merges can arrive from the user's account outside the coordination,** for
  example from another session or by hand. Never assume a cached list of open
  PRs is current.

## Long-running workers

- Canary first: one unit end to end, verified, before a batch.
- Recycle a leaking or stuck session only at a verified idle point, by exact
  session identity, with a graceful exit. Never send a signal without the
  user's explicit permission for that process. Rebind any monitors after a
  restart.
- Watch resource pressure (for example listener counts) as an early warning,
  not as restart authority by itself.

## Durability

No working state goes in `/tmp`, `/private/tmp` or `$TMPDIR`, the
coordinator's own scratch included: macOS cleans those, and agent scratchpads
can be wiped when their session restarts. Keep evidence, briefs and records in
durable storage such as `~/agent-work/<repo>/`. Workers push committed work the
same day. The tmp-state guard (`docs/tmp-state-guard.md`) is a nightly safety
net, not a substitute.

## Verification habits

- A claim that something is absent needs an ignore-inclusive search (`rg -uu`,
  `find`) and a fresh fetch of the remote first.
- Count what was actually written, not what a command reported.
- Name where each fact came from, and its time.

## Handing off

When coordination moves to another session, write a private handoff covering:
- the reading list;
- the rules learned the hard way;
- each stream's worker, host, current brief and state;
- pending user decisions;
- open risks.

`templates/handoff.md` is a template. Deliver it, confirm the new coordinator
has read it, then stop sending input from the old one.
