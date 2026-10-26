#!/usr/bin/env python3
"""Record offline evidence for a multi-instance coordination design review.

Purpose: probe historical-prompt classification and terminal transport deadlines.
Inputs: --repo-root (default current directory); synthetic screens and prompt text.
Output: compact JSON observations; no live transport, real screens, or private paths.
Serves the federation research branch, 2026-10-26. Temporary fixtures are removed.
"""

import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch


def load_module(root, relative, name):
    spec = importlib.util.spec_from_file_location(name, root / relative)
    if spec is None or spec.loader is None:
        raise ValueError("source module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    try:
        sys.dont_write_bytecode = True
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path("."))
    args = parser.parse_args()
    control = load_module(args.repo_root,
                          ".agents/skills/mac-orchestration/scripts/mac_term_control.py",
                          "coordination_probe_control")
    status = load_module(args.repo_root,
                         ".agents/skills/session-status/scripts/session_status.py",
                         "coordination_probe_status")
    screen = ("Earlier transcript: Requesting permission for a completed action\n"
              "Action approved and completed\n›\nGPT-example high · Pursuing goal\n")
    snapshot = {"processes": [], "panes": [
        {"pane": "%1", "pid": 100, "tty": "pts/0", "session": "demo",
         "executable": "codex", "screen": screen}], "errors": []}
    observed = status.report(snapshot, classifier=control.status_state)

    transport = control.Transport("worker.example.invalid")
    success = subprocess.CompletedProcess([], 0, "synthetic output", "")
    with patch.object(control.subprocess, "run", return_value=success) as mocked:
        transport.run(["synthetic-probe"])
        run_deadline = mocked.call_args.kwargs.get("timeout")
    with tempfile.TemporaryDirectory(prefix="xmechbay-offline-probe-") as directory:
        source = Path(directory) / "goal.txt"
        source.write_text("/goal Inspect the synthetic sample project.\n", encoding="utf-8")
        with patch.object(transport, "run", side_effect=["/tmp/xmechbay-mac-term.ABCD1234\n", ""]):
            with patch.object(control.subprocess, "run", return_value=success) as mocked:
                with transport.prompt_file(source):
                    scp_deadline = mocked.call_args.kwargs.get("timeout")

    print(json.dumps({
        "screen_probe": {
            "fixture": "historical approval text followed by a current goal footer",
            "latest_footer_state": "Pursuing goal",
            "reported_state": observed["sessions"][0]["status"],
            "raw_screen_in_report": "screen" in observed["sessions"][0],
        },
        "transport_probe": {
            "run_execution_timeout_seconds": run_deadline,
            "scp_execution_timeout_seconds": scp_deadline,
            "all_transport_calls_mocked": True,
        },
    }, indent=2))


if __name__ == "__main__":
    main()
