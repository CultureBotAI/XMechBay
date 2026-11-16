#!/usr/bin/env python3
"""Read-only resource probe for one Mac in the fleet (fleet-ops skill).

Purpose: report the host signals that decide whether agent work is safe and
durable there: free space on the data volume, swap, load, the size of the two
temporary roots macOS cleans (and their largest entries), Codex session logs,
the tmp-state guard's last report, Time Machine local snapshots (which hold the
blocks of deleted files), and Claude Code transcript retention. Optional
`--watch PATH` arguments add the size of named directories (for example a
campaign's `.temp`).

Inputs: none besides `--watch`, `--top` and `--du-timeout`. Output: one JSON
document on stdout. It never writes, deletes, chmods or sends anything; every
`du` runs with a timeout and a timeout is reported, never treated as zero.
Runs on the macOS system Python 3.9 so it can be piped over SSH:

    ssh HOST /usr/bin/python3 - --watch '~/work/.temp' < host_resources_probe.py
"""
import argparse
import json
import os
import re
import subprocess
import time
from typing import Dict, List, Optional

HOME = os.path.expanduser("~")


def run(cmd: List[str], timeout: int = 60) -> Optional[str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout if proc.returncode == 0 else None


def du_kib(path: str, timeout: int) -> Dict[str, object]:
    path = os.path.expanduser(path)
    if not os.path.lexists(path):
        return {"path": path, "exists": False}
    try:
        proc = subprocess.run(["du", "-skx", path], capture_output=True, text=True, timeout=timeout)
        kib = int(proc.stdout.split()[0])
        return {"path": path, "exists": True, "gib": round(kib / 2 ** 20, 2)}
    except subprocess.TimeoutExpired:
        return {"path": path, "exists": True, "gib": None, "error": "timeout"}
    except (OSError, ValueError, IndexError):
        return {"path": path, "exists": True, "gib": None, "error": "unreadable"}


def top_children(path: str, count: int, timeout: int) -> Dict[str, object]:
    """Largest direct children of a directory (du -d 1), largest first."""
    path = os.path.expanduser(path)
    try:
        proc = subprocess.run(["du", "-kx", "-d", "1", path], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"path": path, "error": "timeout", "children": []}
    except OSError:
        return {"path": path, "error": "unreadable", "children": []}
    rows, total = [], None
    for line in proc.stdout.splitlines():
        kib, _, child = line.partition("\t")
        try:
            value = int(kib)
        except ValueError:
            continue
        if child.rstrip("/") == path.rstrip("/"):
            total = value
        else:
            rows.append((value, os.path.basename(child)))
    rows.sort(reverse=True)
    return {"path": path, "gib": round(total / 2 ** 20, 2) if total is not None else None,
            "children": [{"name": n, "gib": round(k / 2 ** 20, 2)} for k, n in rows[:count]]}


def parse_df(text: Optional[str]) -> Dict[str, Optional[float]]:
    """Parse `df -k` output for one filesystem."""
    if not text:
        return {"free_gib": None, "total_gib": None, "used_pct": None}
    fields = text.strip().splitlines()[-1].split()
    try:
        total, used, avail = int(fields[1]), int(fields[2]), int(fields[3])
    except (IndexError, ValueError):
        return {"free_gib": None, "total_gib": None, "used_pct": None}
    return {"free_gib": round(avail / 2 ** 20, 1), "total_gib": round(total / 2 ** 20, 1),
            "used_pct": round(100.0 * used / max(total, 1), 1)}


def parse_swap(text: Optional[str]) -> Dict[str, Optional[float]]:
    """Parse `sysctl -n vm.swapusage` (for example `total = 2048.00M  used = 512.00M  free = ...`)."""
    values = {}
    for key, number, unit in re.findall(r"(total|used|free) = ([0-9.]+)([MG])", text or ""):
        values[key] = float(number) / (1024 if unit == "M" else 1)
    return {"total_gib": round(values["total"], 2) if "total" in values else None,
            "used_gib": round(values["used"], 2) if "used" in values else None}


def guard_state() -> Dict[str, object]:
    base = os.path.join(HOME, "Library/Application Support/XMechBay/tmp-state-guard")
    plist = os.path.join(HOME, "Library/LaunchAgents/org.xmechbay.tmp-state-guard.plist")
    state = {"installed": os.path.exists(plist)}
    try:
        with open(os.path.join(base, "state/latest.json")) as handle:
            report = json.load(handle)
    except (OSError, ValueError):
        return state
    entries = report.get("entries") or []
    skipped: Dict[str, int] = {}
    failed = 0
    for entry in entries:
        rescue = entry.get("rescue") or {}
        if rescue.get("status") == "skipped":
            reason = str(rescue.get("reason"))
            skipped[reason] = skipped.get(reason, 0) + 1
        elif rescue.get("status") == "failed":
            failed += 1
    state.update({
        "phase": report.get("phase"), "status": report.get("status"), "exit_code": report.get("exit_code"),
        "finished_at": report.get("finished_at") or report.get("observed_at"),
        "at_risk": len(entries), "skipped": skipped, "failed": failed,
        "unknown": sum(1 for e in entries if e.get("unknown")),
        "top_level_excludes": (report.get("settings") or {}).get("top_level_excludes"),
    })
    return state


def claude_retention() -> Dict[str, object]:
    """cleanupPeriodDays per Claude Code config directory that exists ("absent" when it has no settings.json)."""
    out = {}
    for name in (".claude", ".claude-work"):
        if not os.path.isdir(os.path.join(HOME, name)):
            continue
        path = os.path.join(HOME, name, "settings.json")
        try:
            with open(path) as handle:
                out[name] = json.load(handle).get("cleanupPeriodDays")
        except (OSError, ValueError):
            out[name] = "absent"
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--watch", action="append", default=[], help="extra directory to size (repeatable)")
    parser.add_argument("--top", type=int, default=5, help="largest entries to list per temporary root")
    parser.add_argument("--du-timeout", type=int, default=240, help="seconds allowed per du call")
    args = parser.parse_args(argv)
    user_temp = (run(["getconf", "DARWIN_USER_TEMP_DIR"]) or "").strip().rstrip("/")
    report = {
        "schema_version": 1,
        "observed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "hostname": os.uname().nodename,
        "disk": parse_df(run(["df", "-k", "/System/Volumes/Data"]) or run(["df", "-k", HOME])),
        "swap": parse_swap(run(["sysctl", "-n", "vm.swapusage"])),
        "memory_gib": None,
        "loadavg": list(os.getloadavg()),
        "cpus": os.cpu_count(),
        "tmp_roots": [top_children(p, args.top, args.du_timeout)
                      for p in ("/private/tmp", os.path.realpath(user_temp) if user_temp else None) if p],
        "codex_sessions": du_kib("~/.codex/sessions", args.du_timeout),
        "watched": [du_kib(p, args.du_timeout) for p in args.watch],
        "guard": guard_state(),
        "local_snapshots": len(re.findall(r"com\.apple\.TimeMachine", run(["tmutil", "listlocalsnapshots", "/"]) or "")),
        "claude_cleanup_period_days": claude_retention(),
    }
    mem = run(["sysctl", "-n", "hw.memsize"])
    if mem and mem.strip().isdigit():
        report["memory_gib"] = round(int(mem) / 2 ** 30, 1)
    print(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
