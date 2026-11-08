# Rescue agent work from cleaned temporary directories

macOS deletes old files from two temporary locations without asking:

- `/private/tmp` (`/tmp`). `/usr/libexec/tmp_cleaner`, started by the launchd
  job `com.apple.tmp_cleaner` at 00:00 local time, runs
  `find -dx . -fstype local -type f -atime +3 -mtime +3 -ctime +3 -delete`.
  `find` truncates ages to whole days, so a file goes once its access,
  modification and change times are all at least four days old. Empty
  directories older than three days go too.
- The per-user temporary directory, `getconf DARWIN_USER_TEMP_DIR` (the usual
  `$TMPDIR`, under `/private/var/folders`, where `mktemp -d` and Python's
  `tempfile` write by default). `/usr/libexec/dirhelper`, started by
  `com.apple.bsd.dirhelper` at 03:35 local time **and at every boot**, deletes
  files older than three days there (`CLEAN_FILES_OLDER_THAN_DAYS=3`). Which
  timestamps it compares is not documented.

Agent sessions are told not to keep state in either place. The tmp-state guard
is the safety net for when one does anyway: it runs every evening before the
cleaner and copies anything at risk to `~/agent-state-backups/auto/`.

## What it does

One run of `scripts/tmp_state_guard.py`:

1. **Scans** each root (default `/private/tmp` and `DARWIN_USER_TEMP_DIR`;
   `--root [LABEL=]PATH` replaces them). It stays on the root's filesystem,
   never follows a symbolic link, and skips top-level entries owned by another
   user. A symbolic link left in a root (agents leave them pointing at moved
   work) is counted and skipped; a link inside a copied entry is copied as a
   link, never followed.
2. **Classifies git repositories** found within `--max-depth` (default 7). A
   `.git` directory means a clone; a `.git` file means a worktree, submodule or
   separate git dir, whose commits live in the git dir it points to.
   - A clone is at risk if a local branch tip, its detached `HEAD` or the
     detached `HEAD` of one of its worktrees is not on a remote, if it has
     uncommitted or untracked changes (whatever `status.showUntrackedFiles`
     says), or if it has stashes. "On a remote" is checked live with `git ls-remote` against every
     fetch and push URL of every remote, cached per URL: a tip counts as pushed
     if a remote advertises it (branches, tags and pull-request heads all
     count) or if it is an ancestor of an advertised commit that exists
     locally. Nothing is fetched, so a remote that has moved past what the
     clone holds can make a pushed tip look unpushed; that errs towards
     copying.
   - A clone with no remote, or whose remote is a path inside a scanned or
     cleaned root, has every branch counted as unpushed.
   - If `ls-remote` fails or times out, the branches it could not clear are
     reported as **unverified**, the entry is marked unknown, and it is still
     copied. An unknown result is never treated as safe.
   - A worktree is at risk for uncommitted or untracked changes. Its commits
     are judged by where its git dir lives:
     - in a clone found by the scan: that clone's checks cover them;
     - elsewhere in a scanned or cleaned root (a submodule's
       `.git/modules/<name>`, a `--separate-git-dir`): it gets the clone checks
       too, and the entry holding the git dir is marked
       `git-dir-of-at-risk-checkout` so the commits are copied with it;
     - in a durable repository: commits that only a detached `HEAD` reaches
       (no branch, tag or remote-tracking ref) are reported as
       `detached-commits` with the commit id. The copy of the worktree does
       not contain them; they stay in the main repository until git
       garbage-collects them, so give them a branch there.
   - Clones and worktrees are also at risk for **ignored files** that are not
     regenerable (a `.temp/` of review batches, for example). Ignored names
     such as `build`, `dist`, `*.egg-info`, `*.pyc` and the exclusions below do
     not count.
3. **Judges other top-level entries** (folders and single files) by the rule
   of the cleaner for that root: an entry is at risk if it holds a non-empty
   file that the next one or two cleaner runs would delete, and its newest file
   changed within `--recent-days` (default 14). For `/private/tmp` that is
   tmp_cleaner at the next two midnights with all three timestamps four days
   old. For the per-user root (label `darwin-user-temp`) it is dirhelper at the
   next two 03:35 runs with three days, and because its comparator is
   unconfirmed the guard assumes the *oldest* of atime, mtime and ctime decides,
   which errs towards copying. Files inside a repository found in the entry are
   judged by the git rules instead.
4. **Excludes** regenerable content everywhere: `pytest-of-*`,
   `pytest-cache-files-*`, `__pycache__`, `.mypy_cache`, `.pytest_cache`,
   `.ruff_cache`, `node_modules`, `.venv`, uv cache directories and
   `node-compile-cache`, plus top-level `com.apple.*` service directories.
   `--exclude GLOB` adds a name glob for any depth, `--exclude-top GLOB` one for
   top-level names only. An exclusion that would match a name inside `.git`
   (`objects`, `logs`, `*.pack`, ...) is refused, because it would corrupt a
   copied clone. Excluded paths appear in the report only as counts.
5. **Copies** each at-risk entry with `rsync -aHx`, never `--delete`, into
   `<dest>/<root-label>/<path relative to the root>/` (labels `tmp` and
   `darwin-user-temp`). A later run updates the same copy, so files deleted at
   the source stay in the copy. After each copy of a clone, loose refs that the
   source no longer has as loose files (it packed them, or deleted the branch)
   are retired in the copy only: removed when the source's `packed-refs` holds
   the same value, otherwise moved to `refs/tmp-state-guard/stale/<time>/` so
   their commits stay reachable. Files that vanish while a live session is
   working (rsync exit 24, or openrsync's 23 with only "No such file or
   directory" errors) do not fail the copy. An entry inside one already copied
   in the same run is reported as `covered`. Caps apply per entry (5 GB of content), per run
   (30 GB to transfer) and as a free-space floor on the backup disk (50 GB);
   an entry that would break one is skipped and reported, never copied in part.
   The guard refuses to run if the destination or state directory lies inside
   a scanned or cleaned root.
6. **Reports** a JSON document and a short summary, and with `--notify` posts a
   local macOS notification titled "XMechBay tmp-state guard" when the exit
   code is not 0. Nothing is sent to any external service.

It never modifies, touches, chmods, pushes, fetches into, stashes or deletes
anything in a source. Git runs with optional locks disabled, so `git status`
does not rewrite the index, and with every configured clean or process filter
switched off, because a filter such as git-lfs's would write into `.git`. The
only network use is `git ls-remote`, run outside the repository with prompts
disabled and SSH in batch mode. One side effect of reading cannot be avoided:
copying a file can update its access time, which postpones the cleaner for
that file. Modification and change times, content and permissions are untouched.

## Run it by hand

```sh
python3 scripts/tmp_state_guard.py --dry-run          # what would be copied
python3 scripts/tmp_state_guard.py --dry-run --json   # the full report
python3 scripts/tmp_state_guard.py                    # copy now
```

`--dry-run` still runs `git ls-remote`, but copies nothing. Without
`--state-dir` the report goes only to stdout. The guard needs Python 3.9 or
later and only the standard library; macOS `/usr/bin/python3` is enough.

| Exit | Status | Meaning |
|---:|---|---|
| 0 | `ok` | Nothing at risk, or every at-risk entry already has an up-to-date copy |
| 10 | `rescued` | Something new was copied (in a dry run: would be copied) |
| 20 | `unrescued` | An at-risk entry was skipped by a cap or its copy failed |
| 30 | `incomplete` | Part of the observation failed: an unreadable root or directory, a git error, or an unverified remote |
| 2 | - | Refused: destination or state in a cleaned root, overlapping roots, an insecure state directory, or another run holds the lock |

When several apply, the higher code wins (30, then 20, then 10).

A directory that macOS privacy or sandbox protection hides from the user
(`EPERM`, for example `TemporaryItems` and some service directories in the
per-user temporary directory) is listed under `unreadable` and counted as
`protected_dirs`, but does not make a run incomplete: nothing running as the
user, an agent included, can read or write it. A directory that vanishes
during the scan is ignored. Any other read error makes the run incomplete.

## Install the nightly job

Preview first; the installer changes nothing without `--install`:

```sh
python3 scripts/install_tmp_state_guard.py
python3 scripts/install_tmp_state_guard.py --install --notify
```

This installs the per-user LaunchAgent `org.xmechbay.tmp-state-guard` with a
daily `StartCalendarInterval` at 21:30 local time. `--interval-hours N` adds a
run every N hours. The job runs `/usr/bin/python3` with `PATH` limited to the
system directories, so the same plan works on Apple Silicon and Intel Macs.
`--dest`, `--exclude` and `--exclude-top` are passed through to the guard.

The installer copies the guard to an immutable directory named by its content
hash under `~/Library/Application Support/XMechBay/tmp-state-guard/releases/`,
keeps state in `.../tmp-state-guard/state/` (a private `0700` directory), backs
up an existing plist as `<plist>.bak-tmp-state-guard-<timestamp>`, and loads the
job. Loading runs it once; the installer waits (default 600 s, `--wait`) for that
run's observation to appear in `latest.json` and rolls the change back if none
does. A login runs it again, which catches up after a night the Mac was off.
Install separately on each Mac; a job on one Mac does not see another.

## Inspect the reports

With `--state-dir`, which the LaunchAgent always sets:

- `latest.json` is the current report. It is written twice per run: once with
  `"phase": "observed"` before copying starts, then with `"phase": "complete"`.
  A report stuck at `observed` means the run did not finish.
- `summary.txt` is the short human summary of the last complete run.
- `history.json` keeps compact samples of recent runs (default 90).
- `launchd-stderr.log` holds a traceback if the guard ever crashed.

```sh
S=~/Library/Application\ Support/XMechBay/tmp-state-guard/state
cat "$S/summary.txt"
python3 -m json.tool "$S/latest.json" | less
```

Each record in `entries` gives the source path, its kind (`clone`, `worktree`,
`folder`, `file`), the reasons it is at risk (`unpushed-branches`,
`unverified-branches`, `uncommitted-changes`, `stashes`, `ignored-files`,
`detached-commits`, `git-dir-of-at-risk-checkout`, `files-due`, `git-error`),
sizes, git details (unpushed branch names, remote
lookups with credentials redacted) and the rescue outcome (`rescued`,
`up-to-date`, `would-copy`, `covered`, `skipped` with a reason, or `failed`).
`unreadable` names each top-level entry the scan could not read completely.
Reject a report older than a day rather than assume the job is still running.

## Restore from a copy

Copies live at `~/agent-state-backups/auto/<root-label>/<path>/`, for example
`~/agent-state-backups/auto/tmp/review-session/` for `/private/tmp/review-session`.
Restore into a durable location, never back into a temporary directory.

- **Clone:** the copy is a complete repository. Inspect it in place
  (`git -C <copy> status`, `git -C <copy> log --branches --not --remotes`),
  then push the branches you need or move the copy somewhere permanent. Tips
  the source later moved or deleted are kept as
  `git -C <copy> for-each-ref refs/tmp-state-guard/`.
- **Submodule:** its commits are in the superproject's copy, under
  `.git/modules/<name>`.
- **Detached commits of a worktree:** `latest.json` gives the commit id and the
  main repository; run `git -C <main repository> branch rescue/<name> <id>`
  before git garbage-collects it.
- **Worktree:** the copy's `.git` file still points at its main repository.
  If that repository still exists, copy the changed files into a fresh
  worktree, or move the copy to a durable path and run
  `git -C <main repository> worktree repair <new path>`.
- **Folder or file:** copy what you need, for example
  `rsync -a ~/agent-state-backups/auto/tmp/review-session/ ~/agent-work/review-session/`.

Nothing removes old copies. Review and delete them yourself when the work is
safe elsewhere.

## Uninstall

```sh
python3 scripts/install_tmp_state_guard.py --uninstall
```

This backs up and removes the plist and unloads the job. The state directory,
the release copies and every backup under `~/agent-state-backups/auto/` are kept.

## Limits

- It runs only while the user is logged in: a per-user LaunchAgent does not run
  at the login window, while logged out, or while the Mac is off. If the Mac
  sleeps through 21:30 and a cleaner run, launchd runs both on wake in no
  guaranteed order; the two-run window gives a day's margin for files, not for
  that race. dirhelper also runs at every boot, before any login, so a reboot
  can delete per-user temporary files earlier than predicted.
- dirhelper's comparator is assumed, not confirmed (see above).
- Files marked skip-worktree or assume-unchanged in a repository's index are not
  reported as changed.
- It cannot rescue anything created and deleted between two runs, and the
  copy is not an atomic snapshot of a directory that a live session is writing.
- An entry above the per-entry cap is never copied until the cap is raised;
  each night it is reported as skipped (exit 20).
- Repository discovery stops at `--max-depth`; a deeper repository is judged
  only as part of its top-level folder. A worktree whose main repository is also
  in a temporary directory relies on that repository being found and copied.
- Remotes needing credentials that only the repository's own configuration
  supplies, and remotes that are unreachable, give unverified results: the
  entry is copied, but the run reports exit 30.
- With filters off, git-lfs files whose stat data changed read as modified, so
  such a clone may be copied when it did not need to be.
- The non-git rule copies whatever qualifies, including caches an application
  keeps in the per-user temporary directory; add `--exclude-top` names for those.
- macOS 15 and later ship `openrsync` as `/usr/bin/rsync`; older releases ship
  rsync 2.6.9. The guard uses only options both support (`-aHx`, `--exclude`);
  rsync 2.6.9 has not been exercised.
- Stopping the job (logout, `launchctl bootout`) stops its git and rsync
  children with it; a copy interrupted that way is completed by the next run.
- Other locations are not watched: `/var/tmp`, caches under `~/Library`, and
  other users' files.
