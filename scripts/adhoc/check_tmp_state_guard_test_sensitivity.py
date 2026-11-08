#!/usr/bin/env python3
"""Mutation check: the guard's "sources unchanged" test must fail when the guard writes.

Purpose: evidence for the tmp-state-guard PR (2026-11-08) that
tests/test_tmp_state_guard.py::test_sources_are_byte_and_stat_identical_after_a_rescue
detects source modification, not just that it passes.
Inputs: none (synthetic repositories under a temporary directory, as in the test).
Outputs: one line per mutation, "caught" or "MISSED"; exit 1 if any is missed.
Mutations: (1) git may take optional locks, so `git status` can rewrite the index;
(2) rsync is given --remove-source-files. Run from the checkout root.
"""

import importlib.util
import io
import os
from pathlib import Path
import sys
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("test_tmp_state_guard", ROOT / "tests" / "test_tmp_state_guard.py")
tests = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tests)
GUARD = tests.GUARD
TEST = "test_sources_are_byte_and_stat_identical_after_a_rescue"
original_env = GUARD.git_environment
original_run = GUARD.run_command


def env_with_locks():
    env = original_env()
    env.pop("GIT_OPTIONAL_LOCKS")
    return env


def rsync_removing_sources(command, **kwargs):
    if command and os.path.basename(command[0]) == "rsync":
        command = [command[0], "--remove-source-files"] + command[1:]
    return original_run(command, **kwargs)


def outcome(patcher):
    with patcher:
        result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(
            unittest.defaultTestLoader.loadTestsFromName("RescueLimitTests." + TEST, tests))
    return "caught" if (result.failures or result.errors) else "MISSED"


baseline = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(
    unittest.defaultTestLoader.loadTestsFromName("RescueLimitTests." + TEST, tests))
print("baseline (unmutated):", "passes" if baseline.wasSuccessful() else "FAILS")
results = {
    "git optional locks allowed": outcome(mock.patch.object(GUARD, "git_environment", env_with_locks)),
    "rsync --remove-source-files": outcome(mock.patch.object(GUARD, "run_command", rsync_removing_sources)),
}
for name, verdict in results.items():
    print(name + ":", verdict)
sys.exit(0 if baseline.wasSuccessful() and all(v == "caught" for v in results.values()) else 1)
