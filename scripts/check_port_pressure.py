#!/usr/bin/env python3
"""Observe macOS TCP port pressure without controlling agent sessions.

Purpose: early warning of accumulated listeners; port-pressure PR, 2026-10-27.
Inputs: bounded local netstat/sysctl probes, or a synthetic offline snapshot.
Outputs: JSON health report, optional private bounded state and local notification.
Python 3.9+; standard library only. No network requests or session-control input.
"""

import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time


EXIT_CODES = {"ok": 0, "warning": 10, "critical": 20, "unknown": 30}
RANK = {"ok": 0, "warning": 1, "critical": 2, "unknown": 3}
MAX_INPUT_BYTES = 8 * 1024 * 1024
MAX_REPORT_BYTES = 1024 * 1024
MAX_STATE_BYTES = 4 * 1024 * 1024
PROBE_TIMEOUT = 10
TCP_STATES = {"CLOSED", "LISTEN", "SYN_SENT", "SYN_RECEIVED", "ESTABLISHED",
              "CLOSE_WAIT", "FIN_WAIT_1", "CLOSING", "LAST_ACK", "FIN_WAIT_2", "TIME_WAIT"}
OWNER = re.compile(r"([A-Za-z0-9_.+\-]{1,64}):([0-9]{1,10})$")
NOTIFICATION_SCRIPT = '''on run argv
display notification (item 1 of argv) with title "XMechBay port pressure"
end run
'''


class ProbeError(Exception):
    """A fixed diagnostic code, never raw command output or private paths."""


def bounded_read(path, limit):
    with Path(path).open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ProbeError("input_size_limit_exceeded")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise ProbeError("input_encoding_invalid")


def run_probe(command):
    # Temporary files contain probe data, not executable helpers, and disappear
    # at context exit. Timeout bounds runtime; only bounded output is read.
    try:
        with tempfile.TemporaryFile() as output:
            subprocess.run(command, stdout=output, stderr=subprocess.DEVNULL,
                           timeout=PROBE_TIMEOUT, check=True)
            output.seek(0)
            data = output.read(MAX_INPUT_BYTES + 1)
        if len(data) > MAX_INPUT_BYTES:
            raise ProbeError("probe_output_limit_exceeded")
        return data.decode("utf-8")
    except subprocess.TimeoutExpired:
        raise ProbeError("probe_timed_out")
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError):
        raise ProbeError("probe_failed")


def collect():
    limits = run_probe(["/usr/sbin/sysctl", "-n", "net.inet.ip.portrange.first",
                        "net.inet.ip.portrange.last"]).split()
    if len(limits) != 2 or not all(part.isdigit() for part in limits):
        raise ProbeError("port_range_invalid")
    first, last = map(int, limits)
    if not 1 <= first <= last <= 65535:
        raise ProbeError("port_range_invalid")
    return run_probe(["/usr/sbin/netstat", "-anv", "-p", "tcp"]), first, last


def threshold_level(value, warning, critical):
    if value >= critical:
        return "critical"
    if value >= warning:
        return "warning"
    return "ok"


def empty_report(now=None):
    return {"schema_version": 1,
            "observed_at": datetime.fromtimestamp(time.time() if now is None else now,
                                                   timezone.utc).isoformat(),
            "status": "unknown", "exit_code": EXIT_CODES["unknown"],
            "host": {"status": "unknown", "observed_status": "unknown"},
            "process_status": "unknown",
            "attribution": {"status": "unknown", "unattributed_rows": 0},
            "offenders": [], "largest_processes": [], "errors": [],
            "notification": {"status": "disabled"},
            "interpretation": "Distinct TCP port occupancy is an early-warning heuristic, not an exact free-bind count. Agent activity and restart safety are not inferred."}


def analyze(text, first, last, host_warning=0.5, host_critical=0.75,
            process_warning=500, process_critical=1000, now=None):
    report = empty_report(now)
    if not 1 <= first <= last <= 65535:
        report["errors"].append("port_range_invalid")
        return report
    if len(text.encode("utf-8")) > MAX_INPUT_BYTES:
        report["errors"].append("input_size_limit_exceeded")
        return report
    ports = set()
    listeners = Counter()
    socket_rows = Counter()
    tcp_rows = malformed = unattributed = 0
    for line in text.splitlines():
        fields = line.split()
        if not fields or fields[0] not in {"tcp4", "tcp6", "tcp46"}:
            continue
        tcp_rows += 1
        if len(fields) < 6 or fields[5] not in TCP_STATES:
            malformed += 1
            continue
        endpoint = fields[3].rsplit(".", 1)
        if (len(endpoint) != 2 or not endpoint[0] or
                (endpoint[1] != "*" and not endpoint[1].isdigit())):
            malformed += 1
            continue
        if endpoint[1] != "*":
            if len(endpoint[1]) > 5:
                malformed += 1
                continue
            port = int(endpoint[1])
            if not 0 <= port <= 65535:
                malformed += 1
                continue
            # Host pressure remains measurable even when owner attribution fails.
            if first <= port <= last:
                ports.add(port)
        owner = OWNER.fullmatch(fields[10]) if len(fields) > 10 else None
        if not owner or int(owner[2]) <= 0:
            unattributed += 1
            continue
        identity = (owner[1], int(owner[2]))
        socket_rows[identity] += 1
        if fields[5] == "LISTEN":
            listeners[identity] += 1
    size = last - first + 1
    ratio = len(ports) / size
    observed = threshold_level(ratio, host_warning, host_critical)
    measurement_ok = tcp_rows > 0 and malformed == 0
    report["host"] = {"first": first, "last": last, "range_size": size,
                      "distinct_local_ports": len(ports), "occupancy_ratio": ratio,
                      "tcp_rows": tcp_rows, "unparsed_rows": malformed,
                      "observed_status": observed if tcp_rows else "unknown",
                      "status": observed if measurement_ok else "unknown"}
    report["attribution"] = {"status": ("unknown" if not socket_rows else
                                         "partial" if unattributed or malformed else "complete"),
                             "unattributed_rows": unattributed}
    rows = [{"program": program, "pid": pid, "listen_sockets": listeners[(program, pid)],
             "tcp_rows": count,
             "status": threshold_level(listeners[(program, pid)], process_warning, process_critical)}
            for (program, pid), count in socket_rows.items()]
    rows.sort(key=lambda row: (-row["listen_sockets"], -row["tcp_rows"], row["program"], row["pid"]))
    report["largest_processes"] = rows[:8]
    report["offenders"] = [row for row in rows if row["status"] != "ok"]
    report["process_status"] = max([row["status"] for row in rows], key=RANK.get) if rows else "unknown"
    if not tcp_rows:
        report["errors"].append("no_tcp_measurements")
    if malformed:
        report["errors"].append("tcp_measurement_partial")
    report["status"] = (max([observed] + [row["status"] for row in rows], key=RANK.get)
                        if measurement_ok else "unknown")
    report["exit_code"] = EXIT_CODES[report["status"]]
    # Fail visibly if pathological input would produce an oversized report;
    # never silently present a truncated offender inventory as complete.
    if len(json.dumps(report).encode("utf-8")) > MAX_REPORT_BYTES:
        report["offender_count"] = len(report["offenders"])
        report["offenders"] = []
        report["largest_processes"] = []
        report["errors"].append("process_detail_limit_exceeded")
        report["status"] = "unknown"
        report["exit_code"] = EXIT_CODES["unknown"]
    return report


def load_json(path, default):
    if not path.exists():
        return default
    if path.is_symlink() or not path.is_file():
        raise ProbeError("state_file_invalid")
    try:
        return json.loads(bounded_read(path, MAX_STATE_BYTES))
    except (OSError, ValueError):
        raise ProbeError("state_file_unreadable")


def atomic_json(path, value):
    descriptor, temporary = tempfile.mkstemp(prefix=".port-pressure-", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def locked_state(directory):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or
            stat.S_IMODE(info.st_mode) & 0o077):
        raise ProbeError("state_directory_not_private")
    descriptor = os.open(directory / "run.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ProbeError("probe_already_running")
        yield
    finally:
        os.close(descriptor)


def persist_report(directory, report, history_limit):
    history = load_json(directory / "history.json", [])
    if not isinstance(history, list) or any(not isinstance(row, dict) for row in history):
        raise ProbeError("history_invalid")
    sample = {"observed_at": report["observed_at"], "status": report["status"],
              "host": report["host"], "attribution": report["attribution"],
              "largest_processes": report["largest_processes"], "errors": report["errors"]}
    history = history[-(history_limit - 1):] if history_limit > 1 else []
    history.append(sample)
    atomic_json(directory / "latest.json", report)
    atomic_json(directory / "history.json", history)
    atomic_json(directory / "alert.json", {"active": report["status"] != "ok", "report": report})


def notification_evidence(report):
    return {"host": report["host"].get("observed_status", report["host"]["status"]),
            "process": report["process_status"],
            "measurement": "unknown" if report["status"] == "unknown" else "confirmed"}


def evidence_escalated(previous, current):
    # Unknown is missing evidence, not a severity above a confirmed critical.
    risk = {"unknown": -1, "ok": 0, "warning": 1, "critical": 2}
    if any(risk[current[key]] > max(0, risk[previous[key]]) for key in ("host", "process")):
        return True
    if previous["measurement"] != current["measurement"]:
        return (current["measurement"] == "unknown" or
                any(risk[current[key]] > 0 for key in ("host", "process")))
    return False


def notify(directory, report, cooldown, now=None):
    now = time.time() if now is None else now
    previous = load_json(directory / "notification.json", {})
    if not isinstance(previous, dict):
        raise ProbeError("notification_state_invalid")
    last = previous.get("last_attempt_epoch", 0)
    level = previous.get("status", "ok")
    if (type(last) not in {int, float} or not math.isfinite(last) or last < 0 or
            last > now + cooldown or not isinstance(level, str) or level not in RANK):
        raise ProbeError("notification_state_invalid")
    prior_evidence = previous.get("last_observed")
    if prior_evidence is not None:
        if (not isinstance(prior_evidence, dict) or
                set(prior_evidence) != {"host", "process", "measurement"} or
                any(not isinstance(prior_evidence[key], str) or prior_evidence[key] not in RANK
                    for key in ("host", "process")) or
                not isinstance(prior_evidence["measurement"], str) or
                prior_evidence["measurement"] not in {"unknown", "confirmed"}):
            raise ProbeError("notification_state_invalid")
    evidence = notification_evidence(report)
    state = {"schema_version": 2, "last_attempt_epoch": last,
             "status": report["status"], "last_observed": evidence}
    if report["status"] == "ok":
        # Remember recovery so a subsequent recurrence can alert immediately.
        atomic_json(directory / "notification.json", state)
        return {"status": "not_needed"}
    # Legacy state recorded only aggregate severity. It cannot prove that an
    # unchanged aggregate hides no new host risk; request one fresh alert.
    escalated = prior_evidence is None or evidence_escalated(prior_evidence, evidence)
    if now - last < cooldown and not escalated:
        atomic_json(directory / "notification.json", state)
        return {"status": "cooldown"}
    # Save before requesting a best-effort notification to prevent retry storms.
    state["last_attempt_epoch"] = now
    atomic_json(directory / "notification.json", state)
    message = ("Port health: %s; host pressure: %s; process maintenance: %s. Inspect the private health report."
               % (report["status"], report["host"]["status"], report["process_status"]))
    try:
        subprocess.run(["/usr/bin/osascript", "-e", NOTIFICATION_SCRIPT, message],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=PROBE_TIMEOUT, check=True)
        return {"status": "requested"}
    except (OSError, subprocess.SubprocessError):
        return {"status": "failed"}


def positive_int(value):
    result = int(value)
    if result <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return result


def parser_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, help="offline netstat text; requires --first and --last")
    parser.add_argument("--first", type=int)
    parser.add_argument("--last", type=int)
    parser.add_argument("--state-dir", type=Path, help="private directory for durable local state")
    parser.add_argument("--notify", action="store_true", help="request rate-limited local macOS notifications")
    parser.add_argument("--history-limit", type=positive_int, default=120)
    parser.add_argument("--notification-cooldown", type=positive_int, default=900)
    parser.add_argument("--host-warning", type=float, default=0.5)
    parser.add_argument("--host-critical", type=float, default=0.75)
    parser.add_argument("--process-warning", type=positive_int, default=500)
    parser.add_argument("--process-critical", type=positive_int, default=1000)
    args = parser.parse_args(argv)
    if args.snapshot:
        if args.first is None or args.last is None or not 1 <= args.first <= args.last <= 65535:
            parser.error("offline snapshots require a valid --first/--last port range")
    elif args.first is not None or args.last is not None:
        parser.error("local probes discover their range; --first/--last require --snapshot")
    if not 0 < args.host_warning < args.host_critical <= 1:
        parser.error("host thresholds must satisfy 0 < warning < critical <= 1")
    if args.process_warning >= args.process_critical:
        parser.error("process warning must be less than process critical")
    if args.history_limit > 120:
        parser.error("history-limit must not exceed 120")
    if args.notify and not args.state_dir:
        parser.error("--notify requires --state-dir for durable cooldown state")
    return args


def measure(args):
    try:
        if args.snapshot:
            text, first, last = bounded_read(args.snapshot, MAX_INPUT_BYTES), args.first, args.last
        else:
            text, first, last = collect()
        return analyze(text, first, last, args.host_warning, args.host_critical,
                       args.process_warning, args.process_critical)
    except (ProbeError, OSError) as error:
        report = empty_report()
        report["errors"].append(str(error) if isinstance(error, ProbeError) else "snapshot_unreadable")
        return report


def main(argv=None):
    args = parser_args(argv)
    report = None
    try:
        if args.state_dir:
            with locked_state(args.state_dir):
                report = measure(args)
                try:
                    persist_report(args.state_dir, report, args.history_limit)
                    if args.notify:
                        report["notification"] = notify(args.state_dir, report, args.notification_cooldown)
                        atomic_json(args.state_dir / "latest.json", report)
                        atomic_json(args.state_dir / "alert.json", {"active": report["status"] != "ok", "report": report})
                except (ProbeError, OSError) as error:
                    report["status"], report["exit_code"] = "unknown", EXIT_CODES["unknown"]
                    report["errors"].append(str(error) if isinstance(error, ProbeError) else "state_write_failed")
                    # Preserve a visible failure while still owning the lock.
                    # A genuinely unwritable directory may permit stdout only.
                    try:
                        atomic_json(args.state_dir / "latest.json", report)
                        atomic_json(args.state_dir / "alert.json", {"active": True, "report": report})
                    except OSError:
                        pass
        else:
            report = measure(args)
    except (ProbeError, OSError) as error:
        report = report or empty_report()
        report["status"], report["exit_code"] = "unknown", EXIT_CODES["unknown"]
        report["errors"].append(str(error) if isinstance(error, ProbeError) else "state_write_failed")
    print(json.dumps(report, indent=2))
    return report["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
