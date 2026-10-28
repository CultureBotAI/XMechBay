#!/usr/bin/env python3
"""Render separate on-demand host tables from read-only session observations.

Purpose: reusable fleet status skill; 2026-10-27.
Inputs: private host configuration or synthetic snapshots; bounded SSH probes.
Outputs: Markdown/JSON, optional private hashes for comparisons between calls.
No session input, model calls, credential access, or autonomous scheduling.
Python 3.9+ standard library.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fleet_view import analyze_session, render_host

ROOT = Path(__file__).resolve().parents[4]
SCRIPT_DIR = Path(__file__).resolve().parent
MAX_BYTES = 8 * 1024 * 1024
STATES = {"ok", "warning", "critical", "unknown"}
HOST_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,47}$")
UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")


def timestamp(value):
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except ValueError:
        return None


def read_json(path):
    with Path(path).open("rb") as stream:
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("input_size_limit")
    return json.loads(data)


def bare_host(value):
    return (isinstance(value, str) and 0 < len(value) <= 253
            and re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", value)
            and all(part and len(part) <= 63 for part in value.split(".")))


def load_config(path):
    config = read_json(path)
    if not isinstance(config, dict) or config.get("schema_version") != 1:
        raise ValueError("configuration_schema_invalid")
    hosts = config.get("hosts")
    if not isinstance(hosts, dict) or not 1 <= len(hosts) <= 16:
        raise ValueError("configuration_hosts_invalid")
    for key, host in hosts.items():
        if not isinstance(key, str) or not HOST_KEY.fullmatch(key) or not isinstance(host, dict):
            raise ValueError("configuration_host_invalid")
        if not bare_host(host.get("host")):
            raise ValueError("configuration_ssh_host_invalid")
        label = host.get("label", key)
        if not isinstance(label, str) or not 0 < len(label) <= 80 or any(ord(c) < 32 for c in label):
            raise ValueError("configuration_label_invalid")
        for field in ("python", "health_report"):
            value = host.get(field)
            if value is not None and (not isinstance(value, str) or not value or len(value) > 2048
                                      or any(ord(c) < 32 for c in value)):
                raise ValueError("configuration_path_invalid")
        python = host.get("python", "/usr/bin/python3")
        if not python.startswith("/"):
            raise ValueError("configuration_python_requires_absolute_path")
    aliases = config.get("aliases", {})
    if not isinstance(aliases, dict):
        raise ValueError("configuration_aliases_invalid")
    for key, target in aliases.items():
        if (not isinstance(key, str) or not HOST_KEY.fullmatch(key) or key in hosts
                or not isinstance(target, str) or target not in hosts):
            raise ValueError("configuration_alias_invalid")
    age = config.get("max_health_age_seconds", 120)
    if type(age) is not int or not 30 <= age <= 3600:
        raise ValueError("configuration_health_age_invalid")
    state = config.get("state_dir", "fleet-status-state")
    if not isinstance(state, str) or not state or any(ord(c) < 32 for c in state):
        raise ValueError("configuration_state_directory_invalid")
    config["resolved_state_dir"] = str((Path(path).resolve().parent / Path(state).expanduser()).absolute())
    return config


def selected_keys(requested, hosts, aliases=None, all_hosts=False):
    aliases = aliases or {}
    if all_hosts and requested:
        raise ValueError("choose_host_names_or_all")
    names = list(hosts) if all_hosts else [aliases.get(key, key) for key in requested]
    if not names:
        raise ValueError("select_a_host_or_all")
    if any(key not in hosts for key in names):
        raise ValueError("host_not_configured")
    return list(dict.fromkeys(names))


def validate_snapshot(snapshot):
    if not isinstance(snapshot, dict) or snapshot.get("schema_version") != 1:
        raise ValueError("snapshot_schema_invalid")
    if timestamp(snapshot.get("observed_at")) is None:
        raise ValueError("snapshot_time_invalid")
    rows = snapshot.get("sessions")
    if not isinstance(rows, list) or len(rows) > 256:
        raise ValueError("snapshot_sessions_invalid")
    seen = set()
    for row in rows:
        if (not isinstance(row, dict) or not isinstance(row.get("agent"), str)
                or row["agent"] not in {"agy", "codex", "claude"}):
            raise ValueError("snapshot_agent_invalid")
        if row.get("pid") is None:
            if ("agent_process_unverified" not in row.get("errors", []) or row.get("session_id") is not None
                    or row.get("started_at") is not None):
                raise ValueError("snapshot_process_invalid")
        else:
            if type(row.get("pid")) is not int or row["pid"] <= 0 or row["pid"] in seen:
                raise ValueError("snapshot_process_invalid")
            seen.add(row["pid"])
            if not isinstance(row.get("started_at"), str) or not row["started_at"]:
                raise ValueError("snapshot_identity_invalid")
        if not isinstance(row.get("tty"), str):
            raise ValueError("snapshot_identity_invalid")
        age = row.get("age_seconds")
        if age is not None and (type(age) not in {int, float} or not math.isfinite(age) or age < 0):
            raise ValueError("snapshot_age_invalid")
        for field in ("screen", "title", "cwd"):
            if row.get(field) is not None and not isinstance(row[field], str):
                raise ValueError("snapshot_text_invalid")
        if not isinstance(row.get("screen", ""), str) or len(row.get("screen", "")) > 32768:
            raise ValueError("snapshot_screen_limit")
        sid = row.get("session_id")
        if sid is not None and (not isinstance(sid, str) or not UUID.fullmatch(sid)
                                or row.get("session_id_source") != "argv"):
            raise ValueError("snapshot_session_id_unverified")
        target = row.get("target")
        if not isinstance(target, dict) or target.get("transport") not in {"tmux", "terminal", "process"}:
            raise ValueError("snapshot_target_invalid")
    if not isinstance(snapshot.get("coverage", {}), dict):
        raise ValueError("snapshot_coverage_invalid")


def safe_codes(values):
    if not isinstance(values, list):
        return ["invalid_error_metadata"]
    return [code for code in values[:32] if isinstance(code, str)
            and re.fullmatch(r"[a-z][a-z0-9_:-]{0,95}", code)]


def fetch_snapshot(host):
    command = [host.get("python", "/usr/bin/python3"), "-B", "-", "--collect"]
    if host.get("health_report"):
        command.extend(["--health-report", host["health_report"]])
    try:
        result = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "--", host["host"], shlex.join(command)],
            input=(SCRIPT_DIR / "fleet_collect.py").read_bytes(), capture_output=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("ssh_probe_timed_out")
    except OSError:
        raise RuntimeError("ssh_probe_unavailable")
    if result.returncode:
        raise RuntimeError("ssh_probe_failed")
    if len(result.stdout) > MAX_BYTES:
        raise ValueError("snapshot_size_limit")
    try:
        snapshot = json.loads(result.stdout)
    except ValueError:
        raise ValueError("snapshot_json_invalid")
    validate_snapshot(snapshot)
    return snapshot


def health_summary(raw, now, max_age):
    unknown = {"status": "unknown", "occupancy_ratio": None, "age_seconds": None,
               "process_status": "unknown", "attribution": "unknown"}
    if not isinstance(raw, dict) or raw.get("error"):
        return unknown, {}, None
    report = raw.get("report")
    if not isinstance(report, dict) or report.get("schema_version") != 1:
        return unknown, {}, None
    observed = timestamp(report.get("observed_at"))
    host = report.get("host")
    if (observed is None or not isinstance(host, dict) or not isinstance(host.get("status"), str)
            or host["status"] not in STATES):
        return unknown, {}, None
    age = (now - observed).total_seconds()
    if age < -5:
        return {**unknown, "reason": "future_health_timestamp"}, {}, observed
    ratio = host.get("occupancy_ratio")
    if type(ratio) not in {int, float} or not math.isfinite(ratio) or not 0 <= ratio <= 1:
        return unknown, {}, observed
    stale = age > max_age
    process_status = report.get("process_status", "unknown")
    attribution = report.get("attribution", {}).get("status", "unknown") if isinstance(report.get("attribution"), dict) else "unknown"
    health = {"status": "stale" if stale else host["status"], "occupancy_ratio": ratio,
              "age_seconds": max(0, age), "process_status": process_status if isinstance(process_status, str) and process_status in STATES else "unknown",
              "attribution": attribution if isinstance(attribution, str) and attribution in {"complete", "partial", "unknown"} else "unknown"}
    owners = {}
    if stale or host["status"] == "unknown":
        return health, owners, observed
    for key in ("largest_processes", "offenders"):
        values = report.get(key, [])
        if not isinstance(values, list) or len(values) > 2048:
            return unknown, {}, observed
        for row in values:
            if not isinstance(row, dict):
                continue
            pid, count, program = row.get("pid"), row.get("listen_sockets"), row.get("program")
            if (type(pid) is int and pid > 0 and type(count) is int and count >= 0
                    and isinstance(program, str) and program in {"codex", "agy", "claude"}):
                owners[(pid, program)] = {"count": count,
                    "status": row.get("status") if isinstance(row.get("status"), str) and row["status"] in STATES else "unknown", "stale": False}
    return health, owners, observed


def normalize_host(key, label, snapshot, max_health_age=120, source="ssh", now=None):
    validate_snapshot(snapshot)
    now = now or datetime.now(timezone.utc)
    observed = timestamp(snapshot["observed_at"])
    age = (now - observed).total_seconds()
    stale = age < -30 or age > 120
    health, owners, health_time = health_summary(snapshot.get("health"), now, max_health_age)
    rows = []
    for session in snapshot["sessions"]:
        row = analyze_session(session)
        for field in ("agent", "pid", "tty", "started_at", "age_seconds", "session_id", "session_id_source"):
            row[field] = session.get(field)
        row["session_id_current_verified"] = False
        # Targets contain only transport selectors, never titles, screens or argv.
        row["target"] = {field: session["target"][field] for field in
                         ("transport", "pane", "tmux_session", "window", "window_id", "tab", "selected", "minimized")
                         if field in session["target"]}
        started = timestamp(session.get("started_at"))
        row["listeners"] = {"count": None, "status": "unknown", "stale": health["status"] == "stale"}
        if health_time and started and started <= health_time:
            row["listeners"] = owners.get((session["pid"], session["agent"]), row["listeners"])
        row["delta"] = {"kind": "first", "seconds": None}
        row["errors"] = safe_codes(session.get("errors", []))
        rows.append(row)
    coverage = {key: value for key, value in snapshot.get("coverage", {}).items()
                if isinstance(value, (int, bool)) or isinstance(value, str) and re.fullmatch(r"[a-z0-9_ -]{1,80}", value)}
    coverage.update({"screens_read": sum(bool(row.get("screen")) for row in snapshot["sessions"]),
                     "sessions_seen": len(rows),
                     "process_only": sum(row["target"]["transport"] == "process" for row in rows)})
    return {"key": key, "label": label, "source": source, "reachable": True,
            "observed_at": snapshot["observed_at"], "queried_at": now.isoformat(),
            "snapshot_stale": stale, "sessions": rows, "health": health,
            "coverage": coverage, "errors": safe_codes(snapshot.get("errors", []))}


def atomic_json(path, value):
    descriptor, temporary = tempfile.mkstemp(prefix=".fleet-status-", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def cache_lock(directory, key):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError("cache_directory_not_private")
    descriptor = os.open(directory / (key + ".lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(descriptor)


def compare_cache(report, directory, host_identity):
    if not report["reachable"] or report.get("snapshot_stale"):
        return
    coverage = report.get("coverage", {})
    if (coverage.get("processes") != "complete"
            or coverage.get("agent_discovery", "complete") not in {"complete", "not_needed"}):
        report["cache_error"] = "comparison_incomplete_inventory"
        for row in report["sessions"]:
            row["delta"] = {"kind": "unavailable", "seconds": None}
        return
    key = report["key"]
    parser_identity = hashlib.sha256((SCRIPT_DIR / "fleet_view.py").read_bytes()).hexdigest()
    identity = hashlib.sha256((host_identity + "\0" + parser_identity).encode()).hexdigest()
    now = timestamp(report["queried_at"])
    try:
        with cache_lock(directory, key):
            path = directory / (key + ".json")
            previous = {}
            if path.is_symlink():
                raise ValueError("cache_file_invalid")
            if path.exists():
                stored = read_json(path)
                if (not isinstance(stored, dict) or stored.get("schema_version") != 1
                        or not isinstance(stored.get("sessions"), dict)):
                    raise ValueError("cache_schema_invalid")
                if stored.get("host_identity") == identity:
                    previous = stored["sessions"]
            current = {}
            for row in report["sessions"]:
                if row["pid"] is None or not row["started_at"]:
                    continue
                stable = str(row["pid"]) + ":" + row["started_at"]
                old = previous.get(stable, {})
                old = old if isinstance(old, dict) else {}
                changed = timestamp(old.get("changed_at"))
                last_seen = timestamp(old.get("seen_at"))
                fingerprint = row["evidence_fingerprint"]
                if changed and last_seen and changed <= last_seen <= now:
                    same = old.get("fingerprint") == fingerprint
                    row["delta"] = {"kind": "unchanged" if same else "changed",
                                    "seconds": (now - changed).total_seconds() if same else 0,
                                    "sample_gap_seconds": (now - last_seen).total_seconds()}
                    change_time = changed if same else now
                else:
                    change_time = now
                current[stable] = {"fingerprint": fingerprint, "changed_at": change_time.isoformat(), "seen_at": now.isoformat()}
            report["changes"] = {"appeared": len(set(current) - set(previous)),
                                 "departed": len(set(previous) - set(current)), "previous_sample": bool(previous)}
            atomic_json(path, {"schema_version": 1, "host_identity": identity, "sessions": current})
    except (OSError, ValueError):
        report["cache_error"] = "comparison_cache_unavailable"
        for row in report["sessions"]:
            row["delta"] = {"kind": "unavailable", "seconds": None}


def unavailable(key, label, code, source="ssh"):
    return {"key": key, "label": label, "source": source, "reachable": False,
            "observed_at": datetime.now(timezone.utc).isoformat(), "sessions": [],
            "errors": [code], "coverage": {}, "health": {"status": "unknown"}}


def collect_host(key, host, max_age, state_dir=None):
    label = host.get("label", key)
    try:
        report = normalize_host(key, label, fetch_snapshot(host), max_age)
        if state_dir:
            compare_cache(report, state_dir, host["host"])
        return report
    except (OSError, ValueError, RuntimeError, TypeError, KeyError, AttributeError) as error:
        code = str(error)
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
            code = "host_observation_failed"
        return unavailable(key, label, code)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("hosts", nargs="*", help="configured host keys; one table per key")
    parser.add_argument("--config", type=Path, default=Path(os.environ.get("XMECHBAY_FLEET_CONFIG", str(ROOT / ".local/fleet-status.json"))))
    parser.add_argument("--all", action="store_true", help="collect all configured hosts concurrently")
    parser.add_argument("--list", action="store_true", help="list configured keys without contacting hosts")
    parser.add_argument("--snapshot", type=Path, help="offline multi-host fixture, no SSH or comparison cache")
    parser.add_argument("--json", action="store_true", help="normalized report including exact known IDs, without raw screens or paths")
    parser.add_argument("--no-cache", action="store_true", help="do not read or write comparison state")
    args = parser.parse_args(argv)
    try:
        if args.snapshot:
            envelope = read_json(args.snapshot)
            if not isinstance(envelope, dict) or envelope.get("schema_version") != 1 or not isinstance(envelope.get("hosts"), dict):
                raise ValueError("offline_envelope_invalid")
            hosts = envelope["hosts"]
            if not hosts or len(hosts) > 16 or any(not HOST_KEY.fullmatch(key) for key in hosts):
                raise ValueError("offline_hosts_invalid")
            keys = selected_keys(args.hosts, hosts, all_hosts=args.all or not args.hosts)
            reports = []
            for key in keys:
                item = hosts[key]
                if not isinstance(item, dict) or not isinstance(item.get("label", key), str):
                    raise ValueError("offline_host_invalid")
                reports.append(normalize_host(key, item.get("label", key), item.get("snapshot"), source="snapshot"))
        else:
            config = load_config(args.config)
            if args.list:
                print("\n".join(config["hosts"]))
                return 0
            keys = selected_keys(args.hosts, config["hosts"], config.get("aliases"), args.all)
            state_dir = None if args.no_cache else Path(config["resolved_state_dir"])
            with ThreadPoolExecutor(max_workers=min(4, len(keys))) as executor:
                futures = [executor.submit(collect_host, key, config["hosts"][key],
                            config.get("max_health_age_seconds", 120), state_dir) for key in keys]
                reports = [future.result() for future in futures]
        if args.json:
            print(json.dumps({"schema_version": 1, "hosts": reports}, indent=2))
        else:
            print("\n\n".join(render_host(report) for report in reports))
        return 0 if all(report["reachable"] for report in reports) else 2
    except (OSError, ValueError, TypeError) as error:
        code = str(error)
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
            code = "configuration_or_snapshot_unreadable"
        print(json.dumps({"error": code, "hint": "Use --help; configure hosts privately or select --snapshot."}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
