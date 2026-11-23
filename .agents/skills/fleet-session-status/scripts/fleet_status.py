#!/usr/bin/env python3
"""Render separate on-demand host tables from read-only session observations.

Purpose: reusable fleet status skill and local HTML dashboard backend; 2026-11-14.
Inputs: private host configuration (local or SSH) or synthetic snapshots.
Outputs: Markdown/JSON, optional private Excel workbook, comparison and fallback caches.
No session input, model calls, credential access, or autonomous scheduling.
Python 3.9+; Excel export additionally uses XlsxWriter.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
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
TRANSPORTS = {"ssh", "local"}
ROLES = {"mechbay", "claw", "mech", "other"}
HOST_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,47}$")
UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")
TAILSCALE_IPV4_NET = ipaddress.IPv4Network(((100 << 24) | (64 << 16), 10))
TAILSCALE_IPV6_NET = ipaddress.IPv6Network((0xfd7a115ca1e0 << 80, 48))


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


def is_tailscale_ip(value):
    if not isinstance(value, str) or not value or "%" in value:
        return False
    try:
        addr = ipaddress.ip_address(value.strip())
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv4Address):
        return addr in TAILSCALE_IPV4_NET
    return addr in TAILSCALE_IPV6_NET


def is_tailscale_hostname(value):
    if not isinstance(value, str) or not value:
        return False
    cleaned = value.strip().rstrip(".").lower()
    return is_tailscale_ip(cleaned) or cleaned.endswith("." + "ts.net")


def ssh_target_valid(value):
    return bare_host(value) or is_tailscale_ip(value)


def find_tailscale_bin():
    override = os.environ.get("XMECHBAY_TAILSCALE_BIN")
    if override is not None:
        return override if os.path.isfile(override) and os.access(override, os.X_OK) else None
    for candidate in (
        shutil.which("tailscale"),
        "/usr/local/bin/tailscale",
        "/opt/homebrew/bin/tailscale",
        "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
    ):
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _parse_tailscale_node(node):
    if not isinstance(node, dict):
        return None
    raw_dns = str(node.get("DNSName") or "").strip().rstrip(".").lower()
    dns_name = raw_dns if bare_host(raw_dns) else None
    short_name = dns_name.split(".")[0] if dns_name and bare_host(dns_name.split(".")[0]) else None
    host_raw = str(node.get("HostName") or "").strip()
    host_slug = re.sub(r"[^a-z0-9-]+", "-", host_raw.lower()).strip("-") or None
    ips = []
    ipv4 = None
    for item in (node.get("TailscaleIPs") or [])[:8]:
        if isinstance(item, str) and is_tailscale_ip(item.strip()):
            ip_str = item.strip()
            ips.append(ip_str)
            if ipv4 is None and "." in ip_str:
                ipv4 = ip_str
    online = node.get("Online") if type(node.get("Online")) is bool else None
    targets = []
    for cand in [ipv4, dns_name, short_name, *ips]:
        if cand and ssh_target_valid(cand) and cand not in targets:
            targets.append(cand)
    return {
        "dns_name": dns_name,
        "short_name": short_name,
        "host_slug": host_slug,
        "ips": ips,
        "ipv4": ipv4,
        "online": online,
        "targets": targets,
    }


def parse_tailscale_status_json(raw_data):
    if not isinstance(raw_data, dict):
        return None
    backend = str(raw_data.get("BackendState") or "")
    if backend and backend in {"Stopped", "NeedsLogin", "NoState"}:
        return None
    self_node = _parse_tailscale_node(raw_data.get("Self"))
    peers = []
    raw_peers = raw_data.get("Peer")
    if isinstance(raw_peers, dict):
        for peer_val in list(raw_peers.values())[:256]:
            parsed = _parse_tailscale_node(peer_val)
            if parsed and (parsed["targets"] or parsed["short_name"] or parsed["host_slug"]):
                peers.append(parsed)
    return {"self": self_node, "peers": peers}


def discover_tailscale_status(timeout=3.0):
    ts_bin = find_tailscale_bin()
    if not ts_bin:
        return None
    try:
        proc = subprocess.run(
            [ts_bin, "status", "--json"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0 or not proc.stdout or len(proc.stdout) > MAX_BYTES:
        return None
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        return None
    return parse_tailscale_status_json(data)


def match_tailscale_peer(key, host_cfg, aliases=None, ts_status=None):
    if (not isinstance(ts_status, dict)
            or not isinstance(host_cfg, dict)
            or host_cfg.get("transport", "ssh") != "ssh"
            or host_cfg.get("tailscale") is False):
        return None
    peers = ts_status.get("peers") or []
    explicit_ts = host_cfg.get("tailscale_host")
    if isinstance(explicit_ts, str) and explicit_ts.strip():
        needle = explicit_ts.strip().rstrip(".").lower()
        explicit_hits = [
            peer for peer in peers
            if needle in {peer.get("short_name"), peer.get("dns_name"), *(ip.lower() for ip in peer.get("ips", ()))}
        ]
        if len(explicit_hits) == 1:
            return explicit_hits[0]
        if len(explicit_hits) > 1:
            return None
    lookup_names = {str(key).strip().lower()}
    for alias_name, target_key in (aliases or {}).items():
        if target_key == key and isinstance(alias_name, str):
            lookup_names.add(alias_name.strip().lower())
    primary = str(host_cfg.get("host") or "").strip().rstrip(".").lower()
    if primary:
        stem = primary.split(".")[0]
        if stem:
            lookup_names.add(stem)
    name_hits = [
        peer for peer in peers
        if peer.get("short_name") in lookup_names or peer.get("dns_name") in lookup_names
    ]
    if len(name_hits) == 1:
        return name_hits[0]
    if len(name_hits) > 1:
        return None
    slug_hits = [peer for peer in peers if peer.get("host_slug") in lookup_names]
    if len(slug_hits) == 1:
        return slug_hits[0]
    return None


def enrich_host_with_tailscale(key, host_cfg, aliases=None, ts_status=None):
    if not isinstance(host_cfg, dict) or host_cfg.get("transport", "ssh") != "ssh":
        return host_cfg
    peer = match_tailscale_peer(key, host_cfg, aliases=aliases, ts_status=ts_status)
    if peer is None:
        return host_cfg
    enriched = dict(host_cfg)
    enriched["_tailscale_peer_online"] = peer.get("online")
    if peer.get("online") is not False and peer.get("targets"):
        enriched["_auto_tailscale_targets"] = list(peer["targets"])
    return enriched


def resolve_ssh_candidates(host_cfg):
    primary = host_cfg.get("host")
    explicit_alias = host_cfg.get("host_key_alias")
    explicit_alias = explicit_alias if bare_host(explicit_alias) else None
    peer_online = host_cfg.get("_tailscale_peer_online")
    candidates = []
    seen = set()

    def _add(target, route_kind):
        if not ssh_target_valid(target) or target in seen:
            return
        seen.add(target)
        alias = explicit_alias
        if alias is None and primary and target != primary and bare_host(primary):
            alias = primary
        candidates.append((target, route_kind, alias))

    if primary:
        _add(primary, "tailscale" if is_tailscale_hostname(primary) else "ssh")
    if peer_online is not False:
        for auto_target in (host_cfg.get("_auto_tailscale_targets") or []):
            _add(auto_target, "tailscale")
        if host_cfg.get("tailscale_host"):
            _add(host_cfg["tailscale_host"], "tailscale")
    for fallback in (host_cfg.get("fallback_hosts") or []):
        _add(fallback, "tailscale" if is_tailscale_hostname(fallback) else "ssh")
    return candidates, peer_online


def run_ssh_command(host_cfg, remote_cmd_str, input_bytes=None, timeout=60):
    candidates, _ = resolve_ssh_candidates(host_cfg)
    if not candidates:
        raise RuntimeError("ssh_probe_unavailable")
    has_fallbacks = len(candidates) > 1
    saw_timeout = False
    saw_unverified_key = False
    for idx, (target, route_kind, alias) in enumerate(candidates):
        conn_timeout = "4" if (idx == 0 and has_fallbacks) else ("8" if has_fallbacks else "10")
        ssh_opts = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=" + conn_timeout]
        if alias:
            ssh_opts.extend(["-o", "HostKeyAlias=" + alias])
        try:
            result = subprocess.run(
                ["ssh", *ssh_opts, "--", target, remote_cmd_str],
                input=input_bytes,
                capture_output=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            saw_timeout = True
            continue
        except OSError:
            raise RuntimeError("ssh_probe_unavailable")
        if result.returncode == 0:
            return result.stdout, route_kind, target, alias
        if result.returncode != 255:
            raise RuntimeError("ssh_probe_failed")
        stderr_txt = (result.stderr or b"").decode("utf-8", errors="replace")
        if "REMOTE HOST IDENTIFICATION HAS CHANGED" in stderr_txt or "Host key verification failed" in stderr_txt:
            saw_unverified_key = True
    if saw_timeout:
        raise RuntimeError("ssh_probe_timed_out")
    if saw_unverified_key:
        raise RuntimeError("ssh_host_key_unverified")
    raise RuntimeError("ssh_probe_failed")


def _validate_role_overrides(overrides):
    if not isinstance(overrides, dict) or len(overrides) > 128:
        raise ValueError("configuration_role_overrides_invalid")
    for key, spec in overrides.items():
        if not isinstance(key, str) or not 0 < len(key) <= 80 or any(ord(c) < 32 for c in key):
            raise ValueError("configuration_role_overrides_invalid")
        if isinstance(spec, str):
            if spec not in ROLES:
                raise ValueError("configuration_role_overrides_invalid")
        elif isinstance(spec, dict):
            if spec.get("role") not in ROLES:
                raise ValueError("configuration_role_overrides_invalid")
            project = spec.get("project")
            if project is not None and (not isinstance(project, str) or not 0 < len(project) <= 80
                                        or any(ord(c) < 32 for c in project)):
                raise ValueError("configuration_role_overrides_invalid")
        else:
            raise ValueError("configuration_role_overrides_invalid")


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
        transport = host.get("transport", "ssh")
        if transport not in TRANSPORTS:
            raise ValueError("configuration_transport_invalid")
        ssh_target = host.get("host")
        if transport == "ssh":
            if not ssh_target_valid(ssh_target):
                raise ValueError("configuration_ssh_host_invalid")
        elif ssh_target is not None and not ssh_target_valid(ssh_target):
            raise ValueError("configuration_ssh_host_invalid")
        if "tailscale" in host and type(host["tailscale"]) is not bool:
            raise ValueError("configuration_tailscale_invalid")
        if host.get("tailscale_host") is not None and not ssh_target_valid(host["tailscale_host"]):
            raise ValueError("configuration_ssh_host_invalid")
        if host.get("host_key_alias") is not None and not bare_host(host["host_key_alias"]):
            raise ValueError("configuration_ssh_host_invalid")
        fallbacks = host.get("fallback_hosts")
        if fallbacks is not None:
            if (not isinstance(fallbacks, list) or len(fallbacks) > 8
                    or any(not ssh_target_valid(item) for item in fallbacks)):
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
    if "role_overrides" in config:
        _validate_role_overrides(config["role_overrides"])
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


def _normalize_system(raw):
    empty = {"arch": None, "cpu_count": None, "loadavg_1m": None, "disk_free_gib": None, "disk_total_gib": None}
    if not isinstance(raw, dict):
        return empty
    arch = raw.get("arch")
    out = dict(empty)
    if isinstance(arch, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", arch):
        out["arch"] = arch
    cpus = raw.get("cpu_count")
    if type(cpus) is int and 1 <= cpus <= 1024:
        out["cpu_count"] = cpus
    for field in ("loadavg_1m", "disk_free_gib", "disk_total_gib"):
        val = raw.get(field)
        if type(val) in {int, float} and math.isfinite(val) and val >= 0:
            out[field] = round(float(val), 2 if field == "loadavg_1m" else 1)
    return out


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
                    or row.get("started_at") is not None or row.get("skip_permissions") is not None):
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
        skip_perm = row.get("skip_permissions")
        if skip_perm is not None and type(skip_perm) is not bool:
            raise ValueError("snapshot_identity_invalid")
        target = row.get("target")
        if not isinstance(target, dict) or target.get("transport") not in {"tmux", "terminal", "process"}:
            raise ValueError("snapshot_target_invalid")
        memberships = target.get("tmux_sessions", [])
        if not isinstance(memberships, list) or any(not isinstance(name, str) for name in memberships):
            raise ValueError("snapshot_target_invalid")
    if not isinstance(snapshot.get("coverage", {}), dict):
        raise ValueError("snapshot_coverage_invalid")
    if "system" in snapshot and snapshot["system"] is not None and not isinstance(snapshot["system"], dict):
        raise ValueError("snapshot_system_invalid")


def safe_codes(values):
    if not isinstance(values, list):
        return ["invalid_error_metadata"]
    return [code for code in values[:32] if isinstance(code, str)
            and re.fullmatch(r"[a-z][a-z0-9_:-]{0,95}", code)]


def fetch_snapshot(host):
    transport = host.get("transport", "ssh")
    python = host.get("python", "/usr/bin/python3")
    route_used = "local" if transport == "local" else "ssh"
    if transport == "local":
        command = [python, "-B", str(SCRIPT_DIR / "fleet_collect.py"), "--collect"]
        if host.get("health_report"):
            command.extend(["--health-report", host["health_report"]])
        try:
            result = subprocess.run(command, capture_output=True, timeout=45)
        except subprocess.TimeoutExpired:
            raise RuntimeError("local_probe_timed_out")
        except OSError:
            raise RuntimeError("local_probe_unavailable")
        if result.returncode:
            raise RuntimeError("local_probe_failed")
        raw_stdout = result.stdout
    else:
        command = [python, "-B", "-", "--collect"]
        if host.get("health_report"):
            command.extend(["--health-report", host["health_report"]])
        raw_stdout, route_used, _, _ = run_ssh_command(
            host,
            shlex.join(command),
            input_bytes=(SCRIPT_DIR / "fleet_collect.py").read_bytes(),
            timeout=60,
        )
    if len(raw_stdout) > MAX_BYTES:
        raise ValueError("snapshot_size_limit")
    try:
        snapshot = json.loads(raw_stdout)
    except ValueError:
        raise ValueError("snapshot_json_invalid")
    validate_snapshot(snapshot)
    snapshot["_route"] = route_used
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


def normalize_host(key, label, snapshot, max_health_age=120, source="ssh", now=None, role_overrides=None, route=None):
    validate_snapshot(snapshot)
    now = now or datetime.now(timezone.utc)
    observed = timestamp(snapshot["observed_at"])
    age = (now - observed).total_seconds()
    stale = age < -30 or age > 120
    health, owners, health_time = health_summary(snapshot.get("health"), now, max_health_age)
    system = _normalize_system(snapshot.get("system"))
    rows = []
    for session in snapshot["sessions"]:
        row = analyze_session(session, role_overrides=role_overrides)
        for field in ("agent", "pid", "tty", "started_at", "age_seconds", "session_id", "session_id_source", "skip_permissions"):
            row[field] = session.get(field)
        row["session_id_current_verified"] = False
        # Targets contain only transport selectors, never titles, screens or argv.
        row["target"] = {field: session["target"][field] for field in
                         ("transport", "pane", "tmux_session", "tmux_sessions", "window", "window_id", "tab", "selected", "minimized")
                         if field in session["target"]}
        started = timestamp(session.get("started_at"))
        row["listeners"] = {"count": None, "status": "unknown", "stale": health["status"] == "stale"}
        if health_time and started and started <= health_time:
            row["listeners"] = owners.get((session["pid"], session["agent"]), row["listeners"])
        row["delta"] = {"kind": "unavailable", "seconds": None}
        row["errors"] = safe_codes(session.get("errors", []))
        rows.append(row)
    coverage = {key: value for key, value in snapshot.get("coverage", {}).items()
                if isinstance(value, (int, bool)) or isinstance(value, str) and re.fullmatch(r"[a-z0-9_ -]{1,80}", value)}
    coverage.update({"screens_read": sum(bool(row.get("screen")) for row in snapshot["sessions"]),
                     "sessions_seen": len(rows),
                     "process_only": sum(row["target"]["transport"] == "process" for row in rows)})
    resolved_route = route or snapshot.get("_route") or source
    if resolved_route not in {"local", "ssh", "tailscale", "snapshot"}:
        resolved_route = source
    return {"key": key, "label": label, "source": source, "route": resolved_route, "reachable": True,
            "observed_at": snapshot["observed_at"], "queried_at": now.isoformat(),
            "snapshot_stale": stale, "sessions": rows, "health": health, "system": system,
            "coverage": coverage, "errors": safe_codes(snapshot.get("errors", []))}


def ensure_private_dir(directory):
    target = Path(directory).expanduser()
    if not target.is_absolute():
        target = Path.cwd() / target
    current = Path(target.anchor)
    uid = os.getuid()
    for part in target.parts[1:]:
        if part in {"", "."}:
            continue
        if part == "..":
            current = current.parent
            continue
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            try:
                os.mkdir(current, 0o700)
                os.chmod(current, 0o700)
            except FileExistsError:
                pass
            info = current.lstat()
        if stat.S_ISLNK(info.st_mode):
            if info.st_uid not in {uid, 0}:
                raise ValueError("directory_symlink_untrusted")
            try:
                resolved = current.resolve(strict=True)
            except OSError as error:
                raise ValueError("directory_path_invalid") from error
            res_info = resolved.lstat()
            if not stat.S_ISDIR(res_info.st_mode):
                raise ValueError("directory_path_invalid")
            current = resolved
        elif not stat.S_ISDIR(info.st_mode):
            raise ValueError("directory_path_invalid")
    return current


def atomic_json(path, value):
    descriptor, temporary = tempfile.mkstemp(prefix=".fleet-status-", dir=str(path.parent))
    fd_open = True
    try:
        os.fchmod(descriptor, 0o600)
        stream = os.fdopen(descriptor, "w")
        fd_open = False
        with stream:
            json.dump(value, stream)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if fd_open:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if os.path.lexists(temporary):
            os.unlink(temporary)


@contextmanager
def cache_lock(directory, key):
    directory = ensure_private_dir(directory)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError("cache_directory_not_private")
    descriptor = os.open(directory / (key + ".lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(descriptor)


def _row_screen_unreadable(row):
    if "screen-unavailable" in (row.get("flags") or ()):
        return True
    return bool(set(row.get("errors") or ()) & {
        "screen_unavailable",
        "terminal_screen_unreadable",
        "screen_shared_between_agents",
        "terminal_target_ambiguous",
    })


def compare_cache(report, directory, host_identity):
    if not report.get("reachable") or report.get("snapshot_stale"):
        for row in report.get("sessions", ()):
            row["delta"] = {"kind": "unavailable", "seconds": None}
        return
    coverage = report.get("coverage") if isinstance(report.get("coverage"), dict) else {}
    proc_errors = any(
        code.startswith("process_") and code != "process_start_unparsed"
        for code in (report.get("errors") or [])
    )
    if coverage.get("processes") != "complete" or proc_errors:
        report["cache_error"] = "comparison_incomplete_inventory"
        for row in report["sessions"]:
            row["delta"] = {"kind": "unavailable", "seconds": None}
        return
    discovery_complete = coverage.get("agent_discovery", "complete") in {"complete", "not_needed"}
    key = report["key"]
    parser_identity = hashlib.sha256((SCRIPT_DIR / "fleet_view.py").read_bytes()).hexdigest()
    identity = hashlib.sha256((host_identity + "\0" + parser_identity).encode()).hexdigest()
    try:
        now = timestamp(report.get("queried_at"))
        if now is None:
            raise ValueError("invalid_queried_at")
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
                    row["delta"] = {"kind": "unavailable", "seconds": None}
                    continue
                stable = str(row["pid"]) + ":" + row["started_at"]
                old = previous.get(stable, {})
                old = old if isinstance(old, dict) else {}
                changed = timestamp(old.get("changed_at"))
                last_seen = timestamp(old.get("seen_at"))
                if _row_screen_unreadable(row):
                    row["delta"] = {"kind": "unavailable", "seconds": None}
                    if old.get("fingerprint") and changed and last_seen and changed <= last_seen <= now:
                        current[stable] = {
                            "fingerprint": old["fingerprint"],
                            "changed_at": old["changed_at"],
                            "seen_at": now.isoformat(),
                        }
                    else:
                        current[stable] = {
                            "fingerprint": None,
                            "changed_at": None,
                            "seen_at": now.isoformat(),
                        }
                    continue
                fingerprint = row["evidence_fingerprint"]
                if old.get("fingerprint") and changed and last_seen and changed <= last_seen <= now:
                    same = old.get("fingerprint") == fingerprint
                    row["delta"] = {"kind": "unchanged" if same else "changed",
                                    "seconds": (now - changed).total_seconds() if same else 0,
                                    "sample_gap_seconds": (now - last_seen).total_seconds()}
                    change_time = changed if same else now
                else:
                    row["delta"] = {"kind": "first", "seconds": None}
                    change_time = now
                current[stable] = {"fingerprint": fingerprint, "changed_at": change_time.isoformat(), "seen_at": now.isoformat()}
            if discovery_complete:
                report["changes"] = {"appeared": len(set(current) - set(previous)),
                                     "departed": len(set(previous) - set(current)), "previous_sample": bool(previous)}
                atomic_json(path, {"schema_version": 1, "host_identity": identity, "sessions": current})
            else:
                report["cache_error"] = "comparison_incomplete_inventory"
                if current:
                    if len(current) >= 256:
                        merged = dict(list(current.items())[:256])
                    else:
                        epoch_min = datetime.min.replace(tzinfo=timezone.utc)
                        def _prev_rank(item):
                            entry = item[1] if isinstance(item[1], dict) else {}
                            seen = timestamp(entry.get("seen_at"))
                            if seen is not None and seen <= now:
                                return (1, seen)
                            return (0, seen or epoch_min)
                        carry_budget = 256 - len(current)
                        stale_candidates = [
                            (k, v) for k, v in previous.items() if k not in current
                        ]
                        if len(stale_candidates) > carry_budget:
                            stale_candidates = sorted(
                                stale_candidates, key=_prev_rank, reverse=True
                            )[:carry_budget]
                        merged = dict(stale_candidates)
                        merged.update(current)
                    atomic_json(path, {"schema_version": 1, "host_identity": identity, "sessions": merged})
    except (OSError, ValueError):
        report["cache_error"] = "comparison_cache_unavailable"
        for row in report["sessions"]:
            row["delta"] = {"kind": "unavailable", "seconds": None}


def save_last_reachable(report, directory):
    """Persist normalized reachable host snapshot for explicit stale fallback on roam."""
    if not report.get("reachable") or report.get("snapshot_stale"):
        return
    coverage = report.get("coverage") if isinstance(report.get("coverage"), dict) else {}
    if coverage.get("processes") != "complete":
        return
    if any(code.startswith("process_") and code != "process_start_unparsed" for code in (report.get("errors") or [])):
        return
    key = report["key"]
    try:
        with cache_lock(directory, key + "-last"):
            path = directory / (key + ".last.json")
            if path.is_symlink():
                raise ValueError("cache_file_invalid")
            payload = {
                "schema_version": 1,
                "key": key,
                "label": report.get("label", key),
                "route": report.get("route", report.get("source", "ssh")),
                "observed_at": report.get("observed_at"),
                "health": report.get("health", {"status": "unknown"}),
                "system": report.get("system"),
                "coverage": report.get("coverage", {}),
                "sessions": report.get("sessions", [])[:256],
            }
            atomic_json(path, payload)
    except (OSError, ValueError):
        pass


def load_last_reachable(key, directory, now=None):
    now = now or datetime.now(timezone.utc)
    try:
        with cache_lock(directory, key + "-last"):
            path = directory / (key + ".last.json")
            if path.is_symlink() or not path.is_file():
                return None
            stored = read_json(path)
        if (not isinstance(stored, dict) or stored.get("schema_version") != 1
                or stored.get("key") != key or not isinstance(stored.get("sessions"), list)):
            return None
        observed = timestamp(stored.get("observed_at"))
        if observed is None:
            return None
        age = max(0.0, (now - observed).total_seconds())
        cached_sessions = []
        for item in stored["sessions"][:256]:
            if isinstance(item, dict):
                copy = dict(item)
                copy["stale_cached"] = True
                copy["delta"] = {"kind": "stale_cached", "seconds": None}
                copy["listeners"] = {"count": None, "status": "stale", "stale": True}
                cached_sessions.append(copy)
        stored_health = stored.get("health") if isinstance(stored.get("health"), dict) else {"status": "unknown"}
        cached_health = dict(stored_health)
        if cached_health.get("status") not in {"unknown", None}:
            cached_health["status"] = "stale"
        if cached_health.get("process_status") not in {"unknown", None}:
            cached_health["process_status"] = "stale"
        cached_health["age_seconds"] = age
        stored_route = stored.get("route")
        if stored_route not in {"local", "ssh", "tailscale", "snapshot"}:
            stored_route = "ssh"
        return {
            "observed_at": stored["observed_at"],
            "age_seconds": age,
            "route": stored_route,
            "health": cached_health,
            "system": _normalize_system(stored.get("system")),
            "coverage": stored.get("coverage") if isinstance(stored.get("coverage"), dict) else {},
            "sessions": cached_sessions,
        }
    except (OSError, ValueError):
        return None


def unavailable(key, label, code, source="ssh", last_reachable=None, extra_codes=()):
    codes = [code]
    for extra in extra_codes:
        if extra and extra not in codes:
            codes.append(extra)
    result = {"key": key, "label": label, "source": source, "route": source, "reachable": False,
              "observed_at": datetime.now(timezone.utc).isoformat(), "sessions": [],
              "errors": codes, "coverage": {}, "health": {"status": "unknown"},
              "system": _normalize_system(None)}
    if last_reachable is not None:
        result["last_reachable"] = last_reachable
    return result


def collect_host(key, host, max_age, state_dir=None, role_overrides=None, aliases=None, ts_status=None):
    label = host.get("label", key)
    transport = host.get("transport", "ssh")
    identity = host.get("host") or ("local:" + key)
    effective_host = enrich_host_with_tailscale(key, host, aliases=aliases, ts_status=ts_status)
    try:
        report = normalize_host(
            key, label, fetch_snapshot(effective_host), max_age,
            source=transport, role_overrides=role_overrides,
        )
        if state_dir:
            compare_cache(report, state_dir, identity)
            save_last_reachable(report, state_dir)
        return report
    except (OSError, ValueError, RuntimeError, TypeError, KeyError, AttributeError) as error:
        code = str(error)
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
            code = "host_observation_failed"
        extra = ("tailscale_peer_offline",) if effective_host.get("_tailscale_peer_online") is False else ()
        cached = load_last_reachable(key, state_dir) if state_dir else None
        return unavailable(key, label, code, source=transport, last_reachable=cached, extra_codes=extra)


def open_excel(workbook, zoom=150):
    """Open only on the local Mac and verify this workbook's display zoom."""
    if sys.platform != "darwin" or os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        raise RuntimeError("Excel opening requires a local macOS GUI session")
    path = Path(workbook).resolve()
    result = subprocess.run(["/usr/bin/osascript", str(SCRIPT_DIR / "open_fleet_excel.applescript"),
                             str(path), str(zoom)], check=True, capture_output=True, text=True, timeout=45)
    if result.stdout.strip() != str(path) + "\n" + str(zoom):
        raise RuntimeError("Excel workbook identity or zoom could not be verified")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("hosts", nargs="*", help="configured host keys; one table per key")
    parser.add_argument("--config", type=Path, default=Path(os.environ.get("XMECHBAY_FLEET_CONFIG", str(ROOT / ".local/fleet-status.json"))))
    parser.add_argument("--all", action="store_true", help="collect all configured hosts concurrently")
    parser.add_argument("--list", action="store_true", help="list configured keys without contacting hosts")
    parser.add_argument("--snapshot", type=Path, help="offline multi-host fixture, no SSH or comparison cache")
    parser.add_argument("--json", action="store_true", help="normalized report including exact known IDs, without raw screens or paths")
    parser.add_argument("--no-cache", action="store_true", help="do not read or write comparison state")
    excel = parser.add_mutually_exclusive_group()
    excel.add_argument("--excel", dest="excel", action="store_true", default=None,
                       help="export and open in local Microsoft Excel at 150%% (default for live tables on macOS)")
    excel.add_argument("--no-excel", dest="excel", action="store_false", help="terminal output only")
    parser.add_argument("--excel-dir", type=Path, default=ROOT / ".local/fleet-reports",
                        help="private directory for timestamped Excel snapshots")
    args = parser.parse_args(argv)
    if args.excel and (args.json or args.list):
        parser.error("--excel cannot be combined with --json or --list")
    wants_excel = (args.excel if args.excel is not None else
                   sys.platform == "darwin" and not args.json and not args.snapshot
                   and not os.environ.get("SSH_CONNECTION") and not os.environ.get("SSH_TTY"))
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
            overrides = config.get("role_overrides")
            aliases = config.get("aliases")
            need_ts = any(
                config["hosts"][k].get("transport", "ssh") == "ssh"
                and config["hosts"][k].get("tailscale") is not False
                for k in keys
            )
            ts_status = discover_tailscale_status() if need_ts else None
            with ThreadPoolExecutor(max_workers=min(4, len(keys))) as executor:
                futures = [executor.submit(collect_host, key, config["hosts"][key],
                            config.get("max_health_age_seconds", 120), state_dir, overrides,
                            aliases, ts_status) for key in keys]
                reports = [future.result() for future in futures]
        if args.json:
            print(json.dumps({"schema_version": 1, "hosts": reports}, indent=2))
        else:
            print("\n\n".join(render_host(report) for report in reports))
        report_code = 0 if all(report["reachable"] for report in reports) else 2
        if wants_excel:
            try:
                from fleet_excel import export_reports
                workbook = export_reports(reports, args.excel_dir, zoom=150)
                print("Excel snapshot: " + str(workbook), file=sys.stderr)
                open_excel(workbook, zoom=150)
                print("Opened in Microsoft Excel at 150%.", file=sys.stderr)
            except (OSError, ValueError, RuntimeError, ImportError, subprocess.SubprocessError) as error:
                print("Excel export/open failed: " + str(error) + ". Use --no-excel for terminal output only.", file=sys.stderr)
                return report_code or 1
        return report_code
    except (OSError, ValueError, TypeError, RuntimeError) as error:
        code = str(error)
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
            code = "configuration_or_snapshot_unreadable"
        print(json.dumps({"error": code, "hint": "Use --help; configure hosts privately or select --snapshot."}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
