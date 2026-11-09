#!/usr/bin/env bash
# Merge a user-approved, ordered list of pull requests through a GitHub merge queue
# strictly one at a time.
#
# Some repositories' required merge-group checks fail an entry that is stacked behind
# another (for example an audit that insists the base is already on the protected
# branch), so each PR is queued only after the previous one has merged. Before queuing,
# the PR must still be at its expected (reviewed) head, MERGEABLE and CLEAN; otherwise
# the script stops and says why. It never enables auto-merge, never force-pushes and
# never retries a PR that left the queue.
#
# Usage: serial_merge_queue.sh OWNER/REPO PR:HEAD_PREFIX [PR:HEAD_PREFIX ...]
# Environment:
#   GH                  gh executable (default: gh)
#   INTERVAL            seconds between queue polls (default 45)
#   MAX_POLLS           queue polls per PR before giving up (default 80)
#   MERGEABILITY_TRIES  reads of mergeability after a base change (default 6)
#   MERGEABILITY_WAIT   seconds between those reads (default 10)
# Output: one line per event, a final line per PR (merge commit or the reason it
# stopped), and "all merged" on success. Exit status 0 only when every PR merged.
set -uo pipefail
if [ "$#" -lt 2 ]; then
  echo "usage: $0 OWNER/REPO PR:HEAD_PREFIX [PR:HEAD_PREFIX ...]" >&2
  exit 2
fi
REPO="$1"; shift
case "$REPO" in */*) ;; *) echo "usage: OWNER/REPO expected, got '$REPO'" >&2; exit 2;; esac
OWNER="${REPO%%/*}"; NAME="${REPO##*/}"
GH="${GH:-gh}"
INTERVAL="${INTERVAL:-45}"; MAX_POLLS="${MAX_POLLS:-80}"
TRIES="${MERGEABILITY_TRIES:-6}"; WAIT="${MERGEABILITY_WAIT:-10}"

pr_state() {
  "$GH" pr view "$1" -R "$REPO" --json headRefOid,mergeable,mergeStateStatus,state \
    --jq '"\(.headRefOid) \(.mergeable) \(.mergeStateStatus) \(.state)"'
}

for spec in "$@"; do
  case "$spec" in *:?*) ;; *) echo "STOP: '$spec' is not PR:HEAD_PREFIX"; exit 1;; esac
  pr="${spec%%:*}"; want="${spec#*:}"
  case "$pr" in ''|*[!0-9]*) echo "STOP: '$pr' is not a PR number"; exit 1;; esac
  case "$want" in *[!0-9a-fA-F]*) echo "STOP: '$want' is not a hexadecimal head prefix"; exit 1;; esac
  if [ "${#want}" -lt 7 ]; then echo "STOP: head prefix '$want' is shorter than 7 characters"; exit 1; fi

  read -r head mergeable status state <<<"$(pr_state "$pr")"
  if [ "${state:-}" = MERGED ]; then echo "#$pr already MERGED"; continue; fi
  case "${head:-}" in "$want"*) ;; *) echo "STOP #$pr: head ${head:-?} is not the reviewed $want"; exit 1;; esac
  # Mergeability is computed lazily after the base branch changes; read it a few times.
  for ((t = 1; t < TRIES; t++)); do
    [ "$mergeable" = MERGEABLE ] && [ "$status" = CLEAN ] && break
    sleep "$WAIT"
    read -r head mergeable status state <<<"$(pr_state "$pr")"
  done
  case "$head" in "$want"*) ;; *) echo "STOP #$pr: head changed to $head while waiting"; exit 1;; esac
  if [ "$mergeable" != MERGEABLE ] || [ "$status" != CLEAN ]; then
    echo "STOP #$pr: $mergeable $status (needs a rebase or its checks)"; exit 1
  fi

  if ! "$GH" pr merge "$pr" -R "$REPO" --squash --match-head-commit "$head" >/dev/null 2>&1; then
    echo "STOP #$pr: gh pr merge refused"; exit 1
  fi
  echo "#$pr queued at ${head:0:8}"
  st=""
  for ((i = 1; i <= MAX_POLLS; i++)); do
    sleep "$INTERVAL"
    out=$("$GH" api graphql -f query="{repository(owner:\"$OWNER\",name:\"$NAME\"){pullRequest(number:$pr){state mergeCommit{oid} mergeQueueEntry{state}}}}" \
      --jq '.data.repository.pullRequest|"\(.state) \(.mergeCommit.oid // "-") \(.mergeQueueEntry.state // "none")"')
    read -r st mc q <<<"$out"
    if [ "$st" = MERGED ]; then echo "#$pr MERGED ${mc:0:8}"; break; fi
    if [ "$q" = none ]; then echo "STOP #$pr: left the queue without merging"; exit 1; fi
  done
  [ "$st" = MERGED ] || { echo "STOP #$pr: not merged after $MAX_POLLS polls"; exit 1; }
done
echo "all merged"
