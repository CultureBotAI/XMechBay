#!/usr/bin/env python3
"""Run only the tmp-state guard's filesystem scan (no git, no network, no copies).

Purpose: check how the guard classifies unreadable directories on a real Mac
without a second full run (tmp-state-guard PR, 2026-11-08). Listing and lstat
only: file contents are never opened, so access times do not change.
Inputs: the guard's default roots, or guard options such as --root.
Outputs: one line per top-level entry with unreadable or protected directories,
then totals: entries, repositories found (not inspected), folder-rule entries at
risk, unreadable and protected directory counts.
"""

import importlib.util
from pathlib import Path
import sys
import time

spec = importlib.util.spec_from_file_location(
    "tmp_state_guard", Path(__file__).resolve().parents[1] / "tmp_state_guard.py")
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)

settings = guard.settings_from_args(guard.parse_args(["--dry-run"] + sys.argv[1:]))
now = time.time()
scanner = guard.Scanner(settings, now, guard.file_times)
tops = []
for label, root in settings.roots:
    tops.extend(scanner.scan_root(label, root))
recent = now - settings.recent_days * guard.DAY
repos = folder_risk = unreadable = protected = 0
for top in tops:
    for node in top.subtree():
        repos += node.kind in ("clone", "worktree")
    folder_risk += top.kind in ("folder", "file") and bool(top.due_two) and top.newest_change >= recent
    if top.total("walk_errors") or top.total("protected"):
        print("%s\tunreadable=%d\tprotected=%d" % (top.rel, top.total("walk_errors"), top.total("protected")))
    unreadable += top.total("walk_errors")
    protected += top.total("protected")
print("entries=%d repositories_found=%d folder_rule_at_risk=%d unreadable=%d protected=%d errors=%s"
      % (len(tops), repos, folder_risk, unreadable, protected, settings.errors))
