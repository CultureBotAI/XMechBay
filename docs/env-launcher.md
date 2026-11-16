# Install an explicit environment launcher

`scripts/adhoc/install_env_launcher.py` installs one small shell entrypoint for
an already reviewed local environment loader. Any client's research subprocess
can call that entrypoint without restarting the client or changing its global
environment. The loader remains responsible for parsing credentials and running
the child command; this installer never executes it or reads credential files.

Supply absolute paths through operator-chosen variables. The destination's
parent directory must already exist, belong to the installing user, and have no
group/world write bits. Symlinks anywhere in that parent chain are refused.

```bash
python3 scripts/adhoc/install_env_launcher.py \
  --destination "$LAUNCHER_PATH" \
  --interpreter "$PYTHON_PATH" --runtime "$REVIEWED_RUNTIME_PATH" \
  --tdm-env "$TDM_ENV_PATH" \
  --cborg-shell "$CBORG_ENV_PATH" \
  --backup-tag env-launcher-20990101
```

This prints a JSON plan and changes nothing. The tag is an example: choose a
new tag ending in the actual `YYYYMMDD` when installing. Repeat `--cborg-shell`
to pass additional credential-source paths in the specified order. Paths are
arguments, never shell source. The credential files need not exist at install
time; the runtime decides what a missing source means.

After reviewing the plan, repeat the command with `--apply`. The interpreter
must resolve to an executable regular file; its resolved path is pinned in the
launcher, so package-manager symlinks are supported without depending on later
retargeting. The runtime must be an existing regular file whose final component
is not a symlink. Choose a pinned reviewed deployment path; the installer does
not certify its code or detect later edits to that runtime.

The installed mode is `0700`. Replacing a launcher first creates an exclusive
adjacent `<destination>.bak-<tag>` containing the previous bytes and permission
bits. Existing backups, symlink/nonregular/hardlinked destinations, foreign-owned
destinations, files larger than 1 MiB, and collisions with the supplied input
paths are refused. Identical bytes with mode `0700` are a no-op. Installation
uses a temporary file in the same pinned directory and atomically publishes it,
then checks the persisted bytes and mode. A new destination is published without
overwriting a file that appeared concurrently. Existing destinations are
rechecked before replacement; coordinate installation with other writers rather
than treating this as a lock against another process running as the same user.
If verification fails, inspect the retained backup and current destination before
retrying; a nonzero exit is not a claim that nothing was written.

Call the installed entrypoint with the runtime's usual arguments, including its
`--` child-command separator:

```bash
"$LAUNCHER_PATH" -- "$RESEARCH_COMMAND" --help
```

The launcher preserves each supplied argument, including empty strings and
additional `--` arguments, and returns the runtime's exit status. A successful
installation proves only the wrapper's bytes and mode; verify a bounded
authorized child command before adopting it more widely. No PATH, shell profile,
client configuration, running session, or credential value is modified.

To roll back an update, inspect both files to ensure no later change would be
lost, then restore the recorded adjacent backup and its prior permission bits.
For a first installation, stop calling the entrypoint and retire only that file
through the operator's normal deletion workflow. The source runtime and
credential files are unaffected.
