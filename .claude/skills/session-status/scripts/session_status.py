#!/usr/bin/env python3
"""Read-only agent process/tmux inventory; explicit host or offline snapshot required.

Inputs: SSH host or JSON snapshot; output: JSON metadata, never raw screen text.
Public infrastructure extraction, 2026-10-25. Requires Python 3.10 or later.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

AGENTS = {"claude", "codex", "agy"}
PROBE_ERRORS = {
    "process_probe_failed", "tmux_unavailable_or_no_server",
    "tmux_executable_unavailable", "tmux_server_unavailable_or_inaccessible",
    "tmux_probe_timed_out", "tmux_probe_failed",
}


def validate_snapshot(snapshot: Any) -> None:
    """Reject malformed or arbitrary error data without including its contents."""
    if not isinstance(snapshot, dict):
        raise ValueError("snapshot must be an object")
    for collection in ("processes", "panes"):
        if not isinstance(snapshot.get(collection), list):
            raise ValueError("snapshot requires process and pane arrays")
        for row in snapshot[collection]:
            if not isinstance(row, dict):
                raise ValueError("snapshot rows must be objects")
            if type(row.get("pid")) is not int or row["pid"] <= 0:
                raise ValueError("snapshot PID must be a positive integer")
            for field in ("tty", "executable"):
                if not isinstance(row.get(field), str) or not row[field]:
                    raise ValueError("snapshot identity fields must be nonempty strings")
            if collection == "processes":
                if type(row.get("ppid")) is not int or row["ppid"] < 0:
                    raise ValueError("snapshot parent PID must be a nonnegative integer")
            else:
                if not isinstance(row.get("pane"), str) or not re.fullmatch(r"%\d+", row["pane"]):
                    raise ValueError("snapshot pane ID is invalid")
                if not isinstance(row.get("session"), str) or not row["session"]:
                    raise ValueError("snapshot session must be a nonempty string")
                for field in ("cwd", "screen"):
                    if field in row and not isinstance(row[field], str):
                        raise ValueError("snapshot optional fields must be strings")
    errors = snapshot.get("errors", [])
    if not isinstance(errors, list):
        raise ValueError("snapshot errors must be an array")
    for error in errors:
        if not isinstance(error, str) or (error not in PROBE_ERRORS and
                                         not re.fullmatch(r"screen_probe_failed:%\d+", error)):
            raise ValueError("snapshot contains an unrecognized probe error")


def host_name(value: str) -> str:
    """Accept a bare DNS name, IPv4 address, or simple SSH alias, never options."""
    if len(value) > 253 or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", value):
        raise argparse.ArgumentTypeError("use a bare host name or SSH alias, without a user, port, or options")
    if any(not label or len(label) > 63 for label in value.split(".")):
        raise argparse.ArgumentTypeError("invalid host name")
    return value


def parse_processes(text: str) -> list[dict[str, Any]]:
    """Parse PID, parent PID, tty, executable; command arguments are never queried."""
    rows = []
    for line in text.splitlines():
        parts = line.split(None, 3)
        if len(parts) != 4 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        rows.append({"pid": int(parts[0]), "ppid": int(parts[1]), "tty": parts[2],
                     "executable": Path(parts[3]).name})
    return rows


def parse_panes(text: str, include_paths: bool = False) -> list[dict[str, Any]]:
    rows = []
    for line in text.splitlines():
        parts = line.split("\t", 5)
        if len(parts) < 5 or not parts[1].isdigit() or not re.fullmatch(r"%\d+", parts[0]):
            continue
        pane = {"pane": parts[0], "pid": int(parts[1]), "tty": parts[2],
                "session": parts[3], "executable": parts[4]}
        if include_paths and len(parts) == 6:
            pane["cwd"] = parts[5]
        rows.append(pane)
    return rows


def belongs_to_pane(pid: int, pane_pid: int, processes: dict[int, dict[str, Any]]) -> bool:
    seen: set[int] = set()
    while pid and pid not in seen:
        if pid == pane_pid:
            return True
        seen.add(pid)
        pid = processes.get(pid, {}).get("ppid", 0)
    return False


def agent_rows(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Associate agents with tmux by process ancestry; never infer work from a PID."""
    processes = {row["pid"]: row for row in snapshot["processes"]}
    rows = []
    matched_panes = set()
    for process in processes.values():
        if process["executable"] not in AGENTS:
            continue
        row = {"agent": process["executable"], "pid": process["pid"], "tty": process["tty"]}
        for pane in snapshot["panes"]:
            if belongs_to_pane(process["pid"], pane["pid"], processes):
                row["pane_info"] = pane
                matched_panes.add(pane["pane"])
                break
        rows.append(row)
    # A pane command can identify an agent whose OS executable name is a runtime.
    for pane in snapshot["panes"]:
        if pane["pane"] not in matched_panes and pane["executable"] in AGENTS:
            rows.append({"agent": pane["executable"], "pid": None,
                         "tty": pane["tty"], "pane_info": pane})
    return sorted(rows, key=lambda row: (row["tty"], row["pid"] or 0, row["agent"]))


def collect(include_paths: bool = False, screens: bool = False) -> dict[str, Any]:
    """Execute read-only probes. No Terminal GUI, authentication files, or args."""
    errors = []
    try:
        ps = subprocess.run(["ps", "-eo", "pid=,ppid=,tty=,comm="], text=True,
                            capture_output=True, timeout=15, check=True)
        processes = parse_processes(ps.stdout)
    except (OSError, subprocess.SubprocessError):
        processes = []
        errors.append("process_probe_failed")
    fmt = "#{pane_id}\t#{pane_pid}\t#{pane_tty}\t#{session_name}\t#{pane_current_command}"
    if include_paths:
        fmt += "\t#{pane_current_path}"
    try:
        result = subprocess.run(["tmux", "list-panes", "-a", "-F", fmt], text=True,
                                capture_output=True, timeout=15, check=True)
        panes = parse_panes(result.stdout, include_paths)
    except FileNotFoundError:
        panes = []
        errors.append("tmux_executable_unavailable")
    except subprocess.TimeoutExpired:
        panes = []
        errors.append("tmux_probe_timed_out")
    except subprocess.CalledProcessError:
        panes = []
        errors.append("tmux_server_unavailable_or_inaccessible")
    except (OSError, subprocess.SubprocessError):
        panes = []
        errors.append("tmux_probe_failed")
    snapshot = {"processes": processes, "panes": panes, "errors": errors}
    if screens:
        active = {row["pane_info"]["pane"] for row in agent_rows(snapshot) if "pane_info" in row}
        for pane in panes:
            if pane["pane"] not in active:
                continue
            try:
                result = subprocess.run(["tmux", "capture-pane", "-p", "-t", pane["pane"], "-S", "-40"],
                                        text=True, capture_output=True, timeout=15, check=True)
                pane["screen"] = result.stdout
            except (OSError, subprocess.SubprocessError):
                errors.append("screen_probe_failed:" + pane["pane"])
    return snapshot


def load_classifier():
    script = Path(__file__).resolve().parents[2] / "mac-orchestration/scripts/mac_term_control.py"
    spec = importlib.util.spec_from_file_location("xmechbay_screen_classifier", script)
    if spec is None or spec.loader is None:
        raise RuntimeError("screen classifier unavailable")
    module = importlib.util.module_from_spec(spec)
    # Read-only inspection must not leave canonical-only caches in skill mirrors.
    previous_bytecode_setting = sys.dont_write_bytecode
    try:
        sys.dont_write_bytecode = True
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous_bytecode_setting
    return module.status_state


def report(snapshot: dict[str, Any], include_paths: bool = False, classifier=None) -> dict[str, Any]:
    validate_snapshot(snapshot)
    result = {"sessions": [], "errors": list(snapshot.get("errors", [])),
              "coverage": "Exact executable names and tmux pane commands only; process presence does not establish activity."}
    for source in agent_rows(snapshot):
        row = {key: value for key, value in source.items() if key != "pane_info"}
        row["status"] = "UNKNOWN"
        if "pane_info" in source:
            pane = source["pane_info"]
            row.update({"pane": pane["pane"], "tmux_session": pane["session"]})
            if include_paths and "cwd" in pane:
                row["cwd"] = pane["cwd"]
            if classifier and pane.get("screen"):
                row["status"] = classifier(pane["screen"]) or "UNKNOWN"
        result["sessions"].append(row)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--host", type=host_name, help="explicit SSH destination; configure user/port in SSH config")
    source.add_argument("--snapshot", type=Path, help="offline JSON snapshot, no connection")
    source.add_argument("--collect", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--include-paths", action="store_true", help="include runtime working directories")
    parser.add_argument("--screens", action="store_true", help="classify tmux screens; screen contents are omitted from the final report")
    args = parser.parse_args()
    try:
        if args.collect:
            print(json.dumps(collect(args.include_paths, args.screens)))
            return 0
        if args.snapshot:
            snapshot = json.loads(args.snapshot.read_text())
        else:
            remote = "python3 - --collect"
            if args.include_paths:
                remote += " --include-paths"
            if args.screens:
                remote += " --screens"
            result = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "--", args.host, remote],
                                    input=Path(__file__).read_text(), text=True, capture_output=True, timeout=90)
            if result.returncode:
                raise RuntimeError("SSH probe failed; verify the host, SSH configuration, and Python 3 availability")
            snapshot = json.loads(result.stdout)
        classifier = load_classifier() if args.screens else None
        print(json.dumps(report(snapshot, args.include_paths, classifier), indent=2))
        return 0 if "process_probe_failed" not in snapshot.get("errors", []) else 2
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError) as exc:
        # Avoid echoing remote stderr, snapshot contents, or input paths.
        print(json.dumps({"error": "status probe could not complete", "error_type": type(exc).__name__}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
