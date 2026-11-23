#!/usr/bin/env python3
"""Serve or export a local 127.0.0.1 HTML symbol dashboard for Mac fleet sessions.

Purpose: MechBay local HTML dashboard across M1, M5, and Intel Macs; 2026-11-14.
Inputs: private host configuration (.local/fleet-status.json) or offline --snapshot.
Outputs: localhost-only HTTP UI with read-only Update endpoint, or 0600 static HTML.
No session input, model calls, credential access, external CDNs, or LAN exposure.
Python 3.9+ standard library only.
"""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlparse

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fleet_status import (
    HOST_KEY,
    ROOT,
    collect_host,
    discover_tailscale_status,
    ensure_private_dir,
    is_tailscale_ip,
    load_config,
    normalize_host,
    read_json,
    selected_keys,
)
from fleet_view import (
    CANONICAL_MECHS,
    GENERIC_TMUX_WINDOWS,
    STATE_DISPLAY,
    _error_codes,
    _normalize_overrides,
    classify_session_role,
    duration,
    host_symbol,
    is_m1_host,
    mech_color_key,
    private_text,
    session_symbols,
    target_label,
)

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
CSP_POLICY = (
    "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
    "img-src data:; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)


def validate_bind_host(host, allow_tailscale=False, local_tailscale_ips=()):
    value = str(host or "").strip()
    if value in LOOPBACK_HOSTS:
        return value
    if allow_tailscale and is_tailscale_ip(value) and value in set(local_tailscale_ips or ()):
        return value
    if allow_tailscale:
        raise ValueError("dashboard_bind_must_be_loopback_or_tailscale")
    raise ValueError("dashboard_bind_must_be_loopback")


def _is_loopback_client(addr_str):
    val = str(addr_str or "").strip().lower()
    return val in {"127.0.0.1", "::1", "::ffff:127.0.0.1"}


def _scrub_surrogates(value):
    if isinstance(value, str):
        return value.encode("utf-8", errors="replace").decode("utf-8")
    if isinstance(value, list):
        return [_scrub_surrogates(item) for item in value]
    if isinstance(value, dict):
        return {_scrub_surrogates(k): _scrub_surrogates(v) for k, v in value.items()}
    return value


def safe_json_for_html(value):
    return (
        json.dumps(_scrub_surrogates(value), separators=(",", ":"))
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )


def atomic_private_html(path, content):
    expanded = Path(path).expanduser()
    if expanded.is_symlink():
        raise ValueError("output_path_is_symlink")
    parent = ensure_private_dir(expanded.parent)
    destination = parent / expanded.name
    if destination.is_symlink():
        raise ValueError("output_path_is_symlink")
    descriptor, temporary = tempfile.mkstemp(
        prefix=".fleet-dashboard-", suffix=".html", dir=str(parent)
    )
    fd_open = True
    try:
        os.fchmod(descriptor, 0o600)
        stream = os.fdopen(descriptor, "w", encoding="utf-8")
        fd_open = False
        with stream:
            stream.write(content)
        if destination.is_symlink():
            raise ValueError("output_path_is_symlink")
        os.replace(temporary, destination)
    finally:
        if fd_open:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if os.path.lexists(temporary):
            os.unlink(temporary)
    return destination


def _inventory_state(coverage):
    if not isinstance(coverage, dict):
        return "unknown"
    proc = coverage.get("processes", "unknown")
    disc = coverage.get("agent_discovery", "unknown")
    if proc == "unknown":
        return "unknown"
    if proc == "complete" and disc in {"complete", "not_needed"}:
        return "complete"
    if "partial" in {proc, disc}:
        return "partial"
    return "unknown"


def _resolve_row_role(raw_row, role_overrides):
    has_precomputed = (
        raw_row.get("role") in {"mechbay", "claw", "mech", "other"}
        and bool(raw_row.get("project_label"))
    )
    if "cwd" in raw_row or not has_precomputed:
        info = classify_session_role(raw_row, overrides=role_overrides)
        return info["role"], info["project_label"], info["stream_label"]

    norm_overrides = _normalize_overrides(role_overrides)
    if norm_overrides:
        target = raw_row.get("target") or {}
        probes = [
            ("project", raw_row.get("project_label")),
            ("repo", raw_row.get("repo_label")),
            ("stream", raw_row.get("stream_label")),
        ]
        window = str(target.get("window") or "").strip()
        if window and window.lower() not in GENERIC_TMUX_WINDOWS and not window.isdigit():
            probes.append(("window", window))
        tmux_primary = str(target.get("tmux_session") or "").strip()
        if tmux_primary:
            probes.append(("tmux", tmux_primary))
        for member in target.get("tmux_sessions") or ():
            member_text = str(member or "").strip()
            if member_text.lower().startswith("xmb-view-"):
                probes.append(("tmux", member_text[9:]))
            elif member_text:
                probes.append(("tmux", member_text))
        for source_kind, probe in probes:
            key = str(probe or "").strip().lower()
            if key and key in norm_overrides:
                ov_role, ov_proj = norm_overrides[key]
                cur_stream = raw_row.get("stream_label")
                if source_kind == "stream" and cur_stream and ov_proj.lower() == str(cur_stream).strip().lower():
                    cur_stream = None
                return ov_role, ov_proj, cur_stream
    return raw_row["role"], raw_row["project_label"], raw_row.get("stream_label")


HUMAN_PROMPT_STATES = {"APPROVAL", "QUESTION", "SIGN-IN", "TRUST-PROMPT", "UPDATE-PROMPT"}


def _parse_duration_seconds(text):
    if not isinstance(text, str) or not text.strip():
        return -1
    total = 0.0
    matched = False
    for num_str, unit in re.findall(r"(\d+(?:\.\d+)?)\s*(days?|hours?|minutes?|seconds?|[dhms])\b", text, re.I):
        matched = True
        val = float(num_str)
        u = unit[0].lower()
        mult = 86400 if u == "d" else (3600 if u == "h" else (60 if u == "m" else 1))
        total += val * mult
    return int(total) if matched and math.isfinite(total) else -1


def _default_keystroke_hint(status, flag_set):
    if "subagent-approval" in flag_set:
        return "ctrl+k approve"
    if "approval-review" in flag_set:
        return "Approve in TUI · esc interrupts"
    defaults = {
        "APPROVAL": "1 / y / Enter approve · 2 / n / esc deny",
        "QUESTION": "1–N or ↑/↓ + Enter",
        "SIGN-IN": "Enter / complete browser sign-in",
        "TRUST-PROMPT": "1 / Enter trust · esc cancel",
        "UPDATE-PROMPT": "Enter update or skip",
    }
    return defaults.get(status, "Inspect terminal to respond")


def _human_block_metadata(raw_row, status, raw_flags, is_stale_cached):
    empty = {
        "kind": None,
        "action_label": None,
        "keystroke_hint": None,
        "agy_work_fixable": False,
        "agy_work_status": "not_applicable",
        "agy_work_hint": None,
        "is_pasted_prompt": False,
    }
    if is_stale_cached:
        return empty
    flag_set = set(raw_flags or ())
    agent = str(raw_row.get("agent") or "?")
    skip_perm = raw_row.get("skip_permissions")
    if status in HUMAN_PROMPT_STATES or "subagent-approval" in flag_set:
        if "subagent-approval" in flag_set:
            label = "Approve subagent command"
        elif "approval-review" in flag_set:
            label = "Approve pending request"
        elif status == "APPROVAL":
            label = "Approve command / tool"
        elif status == "QUESTION":
            label = "Answer agent question"
        elif status == "SIGN-IN":
            label = "Complete CLI sign-in"
        elif status == "TRUST-PROMPT":
            label = "Confirm workspace trust"
        elif status == "UPDATE-PROMPT":
            label = "Dismiss CLI update prompt"
        else:
            label = "Respond to prompt"
        raw_ks = raw_row.get("keystroke_hint")
        ks_hint = private_text(raw_ks, 64) if isinstance(raw_ks, str) and raw_ks.strip() else _default_keystroke_hint(status, flag_set)
        agy_fixable = False
        agy_status = "not_applicable"
        agy_hint = None
        if agent == "agy" and status == "APPROVAL":
            if raw_row.get("agy_work_fixable") is True or "agy-work-fixable" in flag_set or skip_perm is False:
                agy_fixable = True
                agy_status = "fixable"
                agy_hint = "Plain agy without skip-permissions — relaunch with agy-work -c to bypass routine approvals"
            elif raw_row.get("agy_work_status") == "already_enabled" or "agy-work-active" in flag_set or skip_perm is True:
                agy_fixable = False
                agy_status = "already_enabled"
                agy_hint = "Already running agy-work (--dangerously-skip-permissions); stopped on a standing Ask rule"
            else:
                agy_fixable = False
                agy_status = "unverified"
        return {
            "kind": "keystroke",
            "action_label": label,
            "keystroke_hint": ks_hint,
            "agy_work_fixable": agy_fixable,
            "agy_work_status": agy_status,
            "agy_work_hint": agy_hint,
            "is_pasted_prompt": False,
        }
    if "agent-message" in flag_set or bool(raw_row.get("agent_message")):
        return {
            "kind": "agent_message",
            "action_label": "Agent message needs reply",
            "keystroke_hint": "Type reply + Enter",
            "agy_work_fixable": False,
            "agy_work_status": "not_applicable",
            "agy_work_hint": None,
            "is_pasted_prompt": False,
        }
    if "draft" in flag_set or "pasted-prompt" in flag_set:
        is_pasted = bool("pasted-prompt" in flag_set or raw_row.get("is_pasted_prompt"))
        return {
            "kind": "pasted_prompt",
            "action_label": "Pasted prompt — NOT running" if is_pasted else "Unsent prompt — NOT running",
            "keystroke_hint": "Press Enter to run · clear to discard",
            "agy_work_fixable": False,
            "agy_work_status": "not_applicable",
            "agy_work_hint": None,
            "is_pasted_prompt": is_pasted,
        }
    return empty


_TTY_NAME_RE = re.compile(r"^tty[A-Za-z0-9]{1,12}$")
_TMUX_SESSION_NAME_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
_TMUX_PANE_ID_RE = re.compile(r"^%\d{1,8}$")


def _normalize_tty_name(value):
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if cleaned.startswith("/dev/"):
        cleaned = cleaned[5:]
    if _TTY_NAME_RE.fullmatch(cleaned):
        return cleaned
    return None


def _enrich_session(
    raw_row,
    host_key,
    host_label,
    host_stale,
    role_overrides,
    cached_age_text=None,
    host_source="ssh",
    mode="live",
    host_route=None,
):
    role, raw_project, raw_stream = _resolve_row_role(raw_row, role_overrides)
    project_label = private_text(raw_project, 48)
    stream_label = private_text(raw_stream, 48) if raw_stream else None

    is_stale_cached = bool(raw_row.get("stale_cached") or cached_age_text is not None)
    stale = bool(host_stale or is_stale_cached)
    symbols = session_symbols(raw_row, host_key=host_key, host_label=host_label, host_stale=stale)

    target = raw_row.get("target") or {}
    if target.get("transport") == "tmux" or target.get("pane"):
        loc_short = str(target.get("pane") or "pane?")
    elif target.get("window_id") is not None:
        loc_short = "T" + str(target["window_id"]) + "/" + str(target.get("tab", 1))
    else:
        loc_short = "P" + str(raw_row.get("pid") or "?")

    raw_win_id = target.get("window_id")
    window_id = raw_win_id if isinstance(raw_win_id, int) and not isinstance(raw_win_id, bool) and raw_win_id >= 1 else None
    raw_tab = target.get("tab")
    tab_index = raw_tab if isinstance(raw_tab, int) and not isinstance(raw_tab, bool) and raw_tab >= 1 else None
    transport_str = str(target.get("transport") or "process")
    norm_tty = _normalize_tty_name(raw_row.get("tty"))
    raw_pane = target.get("pane")
    tmux_pane = raw_pane.strip() if isinstance(raw_pane, str) and _TMUX_PANE_ID_RE.fullmatch(raw_pane.strip()) else None

    raw_lookup_sessions = []
    raw_primary_tmux = target.get("tmux_session")
    if isinstance(raw_primary_tmux, str) and _TMUX_SESSION_NAME_RE.fullmatch(raw_primary_tmux):
        raw_lookup_sessions.append(raw_primary_tmux)
    for raw_item in (target.get("tmux_sessions") or [])[:64]:
        if isinstance(raw_item, str) and _TMUX_SESSION_NAME_RE.fullmatch(raw_item) and raw_item not in raw_lookup_sessions:
            raw_lookup_sessions.append(raw_item)

    sid = raw_row.get("session_id")
    launch_short = sid[:8] if isinstance(sid, str) and len(sid) >= 8 else None

    timer = raw_row.get("timer")
    timer_text = ""
    prompt_wait_sec = -1
    if isinstance(timer, dict) and timer.get("text"):
        t_kind = timer.get("kind")
        prefix = "◎ " if t_kind == "goal" else ("⏳ " if t_kind == "prompt" else "▶ ")
        timer_text = prefix + private_text(timer["text"], 24)
        if t_kind == "prompt":
            prompt_wait_sec = _parse_duration_seconds(str(timer.get("text") or ""))

    delta_raw = raw_row.get("delta") or {}
    d_sec = delta_raw.get("seconds") if delta_raw.get("kind") == "unchanged" else None
    unchanged_sec = (
        int(d_sec)
        if isinstance(d_sec, (int, float)) and not isinstance(d_sec, bool) and math.isfinite(d_sec) and d_sec >= 0
        else -1
    )
    wait_seconds = max(prompt_wait_sec, unchanged_sec)

    age_sec = raw_row.get("age_seconds")
    up_num = age_sec if isinstance(age_sec, (int, float)) and not isinstance(age_sec, bool) and math.isfinite(age_sec) and age_sec >= 0 else -1

    listeners = raw_row.get("listeners") or {}
    l_count = listeners.get("count")
    port_num = l_count if isinstance(l_count, int) and not isinstance(l_count, bool) and l_count >= 0 else -1

    ctx = raw_row.get("context")
    ctx_pct = ctx.get("remaining_percent") if isinstance(ctx, dict) else None
    ctx_num = float(ctx_pct) if isinstance(ctx_pct, (int, float)) and not isinstance(ctx_pct, bool) and math.isfinite(ctx_pct) else 999.0

    memberships = [private_text(item, 48) for item in (target.get("tmux_sessions") or [])[:8] if isinstance(item, str)]
    raw_flags_list = [item for item in (raw_row.get("flags") or [])[:16] if isinstance(item, str)]
    flags = [private_text(item, 32) for item in raw_flags_list]
    errors = _error_codes(raw_row.get("errors", []))
    status_str = str(raw_row.get("status") or "UNKNOWN")
    hb_meta = _human_block_metadata(raw_row, status_str, raw_flags_list, is_stale_cached)
    composer_text = (
        private_text(raw_row["composer_text"], 120)
        if not is_stale_cached and isinstance(raw_row.get("composer_text"), str) and raw_row["composer_text"].strip()
        else None
    )
    agent_message = (
        private_text(raw_row["agent_message"], 120)
        if not is_stale_cached and isinstance(raw_row.get("agent_message"), str) and raw_row["agent_message"].strip()
        else None
    )

    is_m1_local = (
        mode == "live"
        and host_source == "local"
        and not stale
        and is_m1_host(host_key, host_label)
        and symbols["mac"].get("glyph") == "M1"
    )
    can_focus_terminal = bool(
        is_m1_local
        and (
            (transport_str == "terminal" and norm_tty is not None)
            or (transport_str == "tmux" and bool(raw_lookup_sessions) and (tmux_pane is not None or norm_tty is not None))
        )
    )

    return {
        "row_id": host_key + ":" + str(raw_row.get("pid") or loc_short) + ":" + str(raw_row.get("started_at") or ""),
        "host_key": host_key,
        "host_label": private_text(host_label, 64),
        "host_source": host_source,
        "host_route": host_route or host_source,
        "stale_cached": is_stale_cached,
        "cached_age_text": cached_age_text,
        "role": role,
        "project_label": project_label,
        "stream_label": stream_label,
        "repo_label": private_text(raw_row.get("repo_label") or project_label, 48),
        "color_key": mech_color_key(project_label, role),
        "target_short": private_text(loc_short, 24),
        "target_full": private_text(target_label(raw_row), 48),
        "transport": transport_str,
        "window_id": window_id,
        "tab_index": tab_index,
        "can_focus_terminal": can_focus_terminal,
        "tmux_pane": tmux_pane,
        "tmux_session": private_text(target.get("tmux_session") or "", 40) if target.get("tmux_session") else None,
        "tmux_window": private_text(target.get("window") or "", 40) if target.get("window") else None,
        "tmux_sessions": memberships,
        "tmux_lookup_sessions": raw_lookup_sessions,
        "pid": raw_row.get("pid") if isinstance(raw_row.get("pid"), int) else None,
        "tty": private_text(raw_row.get("tty") or "", 24),
        "launch_short": launch_short,
        "session_id": sid if isinstance(sid, str) else None,
        "agent": str(raw_row.get("agent") or "?"),
        "model": private_text(raw_row.get("model") or "", 64) if raw_row.get("model") else None,
        "model_compact": private_text(raw_row.get("model_compact") or "", 36) if raw_row.get("model_compact") else None,
        "status": status_str,
        "human_block_kind": hb_meta["kind"],
        "human_action_label": hb_meta["action_label"],
        "keystroke_hint": hb_meta["keystroke_hint"],
        "agy_work_fixable": hb_meta["agy_work_fixable"],
        "agy_work_status": hb_meta["agy_work_status"],
        "agy_work_hint": hb_meta["agy_work_hint"],
        "is_pasted_prompt": hb_meta["is_pasted_prompt"],
        "composer_text": composer_text,
        "agent_message": agent_message,
        "wait_seconds": wait_seconds,
        "up_text": duration(age_sec),
        "up_seconds": up_num,
        "timer_text": timer_text,
        "activity": private_text(raw_row["activity"], 120) if raw_row.get("activity") else None,
        "flags": flags,
        "errors": errors,
        "port_count": port_num,
        "ctx_sort": ctx_num,
        "symbols": symbols,
    }


def build_dashboard_payload(reports, duration_ms=0, refresh_seq=1, mode="live", role_overrides=None):
    now = datetime.now(timezone.utc)
    host_cards = []
    sessions = []
    attention = []
    human_input = []

    tone_counts = Counter()
    live_role_counts = Counter()
    all_role_counts = Counter()
    changed_now = 0
    live_sessions_count = 0
    cached_sessions_count = 0
    human_keystroke_count = 0
    human_agy_work_count = 0
    human_message_count = 0
    human_pasted_count = 0
    mechs_present = set()
    mechs_active = set()

    canonical_set = set(CANONICAL_MECHS.values())
    human_states = HUMAN_PROMPT_STATES

    for report in reports:
        key = str(report.get("key") or "host")
        label = str(report.get("label") or key)
        reachable = bool(report.get("reachable", False))
        stale_snap = bool(report.get("snapshot_stale", False))
        source = str(report.get("source") or "ssh")
        route = str(report.get("route") or source)
        if route not in {"local", "ssh", "tailscale", "snapshot"}:
            route = source
        errors = _error_codes(report.get("errors", []))
        mac_sym = host_symbol(key, label, stale=(not reachable or stale_snap))

        health = report.get("health") if isinstance(report.get("health"), dict) else {}
        system = report.get("system") if isinstance(report.get("system"), dict) else {}
        coverage = report.get("coverage") if isinstance(report.get("coverage"), dict) else {}

        last_meta = None
        host_raw_sessions = []
        cached_age_text = None
        if reachable:
            host_raw_sessions = list(report.get("sessions") or [])
            proc_err = [code for code in errors if code.startswith("process_") and code != "process_start_unparsed"]
            if proc_err or (coverage and coverage.get("processes") not in {"complete", None}):
                attention.append({
                    "severity": "critical",
                    "kind": "host-probe-error",
                    "host_key": key,
                    "mac": mac_sym,
                    "title": label + " process probe incomplete",
                    "detail": "Process inventory incomplete (" + (", ".join((proc_err or errors)[:3]) or str(coverage.get("processes"))) + ")",
                    "row_id": None,
                })
        else:
            lr = report.get("last_reachable")
            if isinstance(lr, dict):
                age_text = duration(lr.get("age_seconds"))
                cached_age_text = age_text
                lr_sessions = lr.get("sessions") if isinstance(lr.get("sessions"), list) else []
                last_meta = {
                    "observed_at": lr.get("observed_at"),
                    "age_seconds": lr.get("age_seconds"),
                    "age_text": age_text,
                    "cached_count": len(lr_sessions),
                }
                host_raw_sessions = lr_sessions
                if not system.get("arch") and isinstance(lr.get("system"), dict):
                    system = lr["system"]
                if health.get("status") in {None, "unknown"} and isinstance(lr.get("health"), dict):
                    health = lr["health"]
            attention.append({
                "severity": "critical",
                "kind": "host-unreachable",
                "host_key": key,
                "mac": mac_sym,
                "title": label + " is UNREACHABLE",
                "detail": (
                    ("Showing " + str(last_meta["cached_count"]) + " cached sessions from " + last_meta["age_text"] + " ago")
                    if last_meta else
                    ("Collection unavailable" + (" (" + ", ".join(errors[:3]) + ")" if errors else ""))
                ),
                "row_id": None,
            })

        host_state_dots = Counter()
        host_roles = Counter()
        host_human_keystrokes = 0
        host_human_agy_work = 0
        host_human_messages = 0
        host_human_pasted = 0
        for raw_row in host_raw_sessions:
            if not isinstance(raw_row, dict):
                continue
            enriched = _enrich_session(
                raw_row,
                key,
                label,
                (not reachable or stale_snap),
                role_overrides,
                cached_age_text=cached_age_text,
                host_source=source,
                mode=mode,
                host_route=route,
            )
            sessions.append(enriched)
            all_role_counts[enriched["role"]] += 1
            host_roles[enriched["role"]] += 1
            if not enriched["stale_cached"] and enriched["human_block_kind"] == "pasted_prompt":
                dot = "🟠"
            elif not enriched["stale_cached"] and enriched["human_block_kind"] == "agent_message":
                dot = "🟣"
            else:
                dot = STATE_DISPLAY.get(enriched["status"], STATE_DISPLAY["UNKNOWN"]).split()[0]
            host_state_dots[dot] += 1

            if enriched["stale_cached"]:
                cached_sessions_count += 1
                continue

            if enriched["project_label"] in canonical_set:
                mechs_present.add(enriched["project_label"])
                if enriched["status"] in {"Pursuing goal", "Working"}:
                    mechs_active.add(enriched["project_label"])

            live_sessions_count += 1
            live_role_counts[enriched["role"]] += 1
            hb_kind = enriched["human_block_kind"]
            st_tone = enriched["symbols"]["state"]["tone"]
            if hb_kind == "keystroke":
                tone_counts["yellow"] += 1
            elif hb_kind in {"agent_message", "pasted_prompt"}:
                if st_tone == "red":
                    tone_counts["red"] += 1
            else:
                tone_counts[st_tone] += 1
            if (raw_row.get("delta") or {}).get("kind") == "changed":
                changed_now += 1
            if hb_kind in {"keystroke", "agent_message", "pasted_prompt"}:
                if hb_kind == "keystroke":
                    host_human_keystrokes += 1
                    human_keystroke_count += 1
                    if enriched["agy_work_fixable"]:
                        host_human_agy_work += 1
                        human_agy_work_count += 1
                elif hb_kind == "agent_message":
                    host_human_messages += 1
                    human_message_count += 1
                else:
                    host_human_pasted += 1
                    human_pasted_count += 1
                human_input.append({
                    "row_id": enriched["row_id"],
                    "kind": hb_kind,
                    "status": enriched["status"],
                    "action_label": enriched["human_action_label"],
                    "keystroke_hint": enriched["keystroke_hint"],
                    "agy_work_fixable": enriched["agy_work_fixable"],
                    "agy_work_status": enriched["agy_work_status"],
                    "agy_work_hint": enriched["agy_work_hint"],
                    "is_pasted_prompt": enriched["is_pasted_prompt"],
                    "composer_text": enriched["composer_text"],
                    "agent_message": enriched["agent_message"],
                    "host_key": key,
                    "host_label": enriched["host_label"],
                    "mac": mac_sym,
                    "role": enriched["role"],
                    "project_label": enriched["project_label"],
                    "stream_label": enriched["stream_label"],
                    "color_key": enriched["color_key"],
                    "target_short": enriched["target_short"],
                    "target_full": enriched["target_full"],
                    "can_focus_terminal": enriched["can_focus_terminal"],
                    "tmux_session": enriched["tmux_session"],
                    "tmux_window": enriched["tmux_window"],
                    "cli": enriched["symbols"]["cli"],
                    "state": enriched["symbols"]["state"],
                    "guard": enriched["symbols"]["guard"],
                    "delta": enriched["symbols"]["delta"],
                    "view": enriched["symbols"]["view"],
                    "timer_text": enriched["timer_text"],
                    "up_text": enriched["up_text"],
                    "wait_seconds": enriched["wait_seconds"],
                    "activity": enriched["activity"],
                })

            if enriched["status"] in human_states:
                attention.append({
                    "severity": "attention",
                    "kind": "human-input",
                    "host_key": key,
                    "mac": mac_sym,
                    "title": enriched["project_label"] + " · " + enriched["target_short"],
                    "detail": enriched["symbols"]["state"]["dot"] + " " + enriched["status"] + (
                        " — ~" + enriched["activity"] if enriched["activity"] else ""
                    ),
                    "row_id": enriched["row_id"],
                })
            elif enriched["status"] in {"Goal stalled", "Goal hit usage limits"} or enriched["port_count"] >= 1000:
                reason = (
                    str(enriched["port_count"]) + " TCP listeners!"
                    if enriched["port_count"] >= 1000
                    else enriched["status"]
                )
                attention.append({
                    "severity": "critical",
                    "kind": "session-alert",
                    "host_key": key,
                    "mac": mac_sym,
                    "title": enriched["project_label"] + " · " + enriched["target_short"],
                    "detail": "🔴 " + reason,
                    "row_id": enriched["row_id"],
                })

        ratio = health.get("occupancy_ratio")
        occ_pct = round(100.0 * float(ratio), 1) if isinstance(ratio, (int, float)) and not isinstance(ratio, bool) and 0 <= ratio <= 1 else None
        h_status = str(health.get("status") or "unknown")
        p_status = str(health.get("process_status") or "unknown")
        if reachable and (h_status in {"warning", "critical"} or p_status in {"warning", "critical"}):
            sev = "critical" if "critical" in {h_status, p_status} else "warning"
            attention.append({
                "severity": sev,
                "kind": "port-pressure",
                "host_key": key,
                "mac": mac_sym,
                "title": label + " port/listener watch",
                "detail": "TCP " + (str(occ_pct) + "%" if occ_pct is not None else "?") + " (" + h_status + "), process maintenance: " + p_status,
                "row_id": None,
            })

        dot_order = ("🟢", "🟡", "🟣", "🟠", "🔴", "🔵", "⚪", "⚫")
        dot_summary = [{"dot": d, "count": host_state_dots[d]} for d in dot_order if host_state_dots[d]]

        inv_state = _inventory_state(coverage) if reachable else "unreachable"
        screens_read = coverage.get("screens_read") if isinstance(coverage.get("screens_read"), int) else None
        sessions_seen = coverage.get("sessions_seen") if isinstance(coverage.get("sessions_seen"), int) else len(host_raw_sessions)

        raw_cpus = system.get("cpu_count")
        cpu_count = raw_cpus if isinstance(raw_cpus, int) and not isinstance(raw_cpus, bool) and 1 <= raw_cpus <= 1024 else None
        raw_load = system.get("loadavg_1m")
        load_1m = (
            round(float(raw_load), 2)
            if isinstance(raw_load, (int, float)) and not isinstance(raw_load, bool) and math.isfinite(raw_load) and raw_load >= 0
            else None
        )
        host_human_total = host_human_keystrokes + host_human_messages + host_human_pasted
        host_cards.append({
            "key": key,
            "label": private_text(label, 64),
            "source": source,
            "route": route,
            "reachable": reachable,
            "snapshot_stale": stale_snap,
            "observed_at": report.get("observed_at"),
            "mac": mac_sym,
            "inventory_state": inv_state,
            "session_count": len(host_raw_sessions) if reachable else None,
            "screens_text": (str(screens_read) + "/" + str(sessions_seen)) if reachable and screens_read is not None else "—",
            "dot_summary": dot_summary,
            "human_keystroke_count": host_human_keystrokes,
            "human_agy_work_count": host_human_agy_work,
            "human_message_count": host_human_messages,
            "human_pasted_count": host_human_pasted,
            "human_prompt_count": host_human_keystrokes,
            "human_draft_count": host_human_pasted,
            "human_total_count": host_human_total,
            "role_counts": {r: host_roles.get(r, 0) for r in ("mechbay", "claw", "mech", "other")},
            "health": {
                "status": h_status,
                "occupancy_pct": occ_pct,
                "age_text": duration(health.get("age_seconds")),
                "process_status": p_status,
            },
            "system": {
                "arch": system.get("arch"),
                "cpu_count": cpu_count,
                "loadavg_1m": load_1m,
                "disk_free_gib": system.get("disk_free_gib"),
                "disk_total_gib": system.get("disk_total_gib"),
            },
            "errors": errors,
            "cache_error": report.get("cache_error"),
            "last_reachable": last_meta,
        })

    kind_order = {"keystroke": 0, "agent_message": 1, "pasted_prompt": 2}
    sorted_human_input = sorted(
        human_input,
        key=lambda item: (
            kind_order.get(item["kind"], 3),
            item["state"]["rank"],
            -item["wait_seconds"],
            item["mac"]["rank"],
            item["project_label"].lower(),
            item["target_short"],
        ),
    )[:64]

    human_total_count = human_keystroke_count + human_message_count + human_pasted_count

    return {
        "schema_version": 1,
        "generated_at": now.isoformat(),
        "mode": mode,
        "refresh_seq": refresh_seq,
        "duration_ms": duration_ms,
        "canonical_mechs": sorted(CANONICAL_MECHS.values()),
        "hosts": host_cards,
        "sessions": sessions,
        "stats": {
            "hosts_total": len(host_cards),
            "hosts_reachable": sum(1 for h in host_cards if h["reachable"]),
            "hosts_unreachable": sum(1 for h in host_cards if not h["reachable"]),
            "live_sessions": live_sessions_count,
            "cached_sessions": cached_sessions_count,
            "human_keystroke_count": human_keystroke_count,
            "human_agy_work_count": human_agy_work_count,
            "human_message_count": human_message_count,
            "human_pasted_count": human_pasted_count,
            "human_prompt_count": human_keystroke_count,
            "human_draft_count": human_pasted_count,
            "human_total_count": human_total_count,
            "human_input": sorted_human_input,
            "role_counts_live": {r: live_role_counts.get(r, 0) for r in ("mechbay", "claw", "mech", "other")},
            "role_counts_all": {r: all_role_counts.get(r, 0) for r in ("mechbay", "claw", "mech", "other")},
            "tone_counts": {
                "active": tone_counts.get("green", 0),
                "input": tone_counts.get("yellow", 0),
                "paused": tone_counts.get("orange", 0),
                "alert": tone_counts.get("red", 0),
                "done": tone_counts.get("blue", 0),
                "idle": tone_counts.get("white", 0),
                "unknown": tone_counts.get("slate", 0),
            },
            "changed_now": changed_now,
            "mechs_total": len(CANONICAL_MECHS),
            "mechs_present_count": len(mechs_present),
            "mechs_active_count": len(mechs_active),
            "attention": sorted(
                attention,
                key=lambda item: 0 if item.get("severity") == "critical" else 1,
            )[:24],
        },
    }


def collect_dashboard_reports(config_path=None, snapshot_path=None, requested_hosts=(), all_hosts=True, use_cache=True):
    if snapshot_path is not None:
        envelope = read_json(snapshot_path)
        if not isinstance(envelope, dict) or envelope.get("schema_version") != 1 or not isinstance(envelope.get("hosts"), dict):
            raise ValueError("offline_envelope_invalid")
        hosts = envelope["hosts"]
        if not hosts or len(hosts) > 16 or any(not HOST_KEY.fullmatch(key) for key in hosts):
            raise ValueError("offline_hosts_invalid")
        keys = selected_keys(requested_hosts, hosts, all_hosts=all_hosts or not requested_hosts)
        reports = []
        for key in keys:
            item = hosts[key]
            if not isinstance(item, dict) or not isinstance(item.get("label", key), str):
                raise ValueError("offline_host_invalid")
            reports.append(normalize_host(key, item.get("label", key), item.get("snapshot"), source="snapshot"))
        return reports, {"mode": "snapshot", "role_overrides": None}

    config = load_config(config_path)
    keys = selected_keys(
        requested_hosts, config["hosts"], config.get("aliases"),
        all_hosts=all_hosts and not requested_hosts or not requested_hosts,
    )
    state_dir = None if not use_cache else Path(config["resolved_state_dir"])
    overrides = config.get("role_overrides")
    aliases = config.get("aliases")
    need_ts = any(
        config["hosts"][k].get("transport", "ssh") == "ssh"
        and config["hosts"][k].get("tailscale") is not False
        for k in keys
    )
    ts_status = discover_tailscale_status() if need_ts else None
    with ThreadPoolExecutor(max_workers=min(4, len(keys))) as executor:
        futures = [
            executor.submit(
                collect_host,
                key,
                config["hosts"][key],
                config.get("max_health_age_seconds", 120),
                state_dir,
                overrides,
                aliases,
                ts_status,
            )
            for key in keys
        ]
        reports = [future.result() for future in futures]
    return reports, {"mode": "live", "role_overrides": overrides}


DASHBOARD_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>XMechBay · Local Fleet Session Dashboard</title>
<style>
  :root {
    --bg: #f4f6f8;
    --card: #ffffff;
    --surface-2: #edf1f5;
    --surface-hover: #f0f4f9;
    --ink: #131b24;
    --muted: #526273;
    --line: #d5dde6;
    --line-strong: #b6c3d1;
    --accent: #1d4ed8;
    --accent-ink: #ffffff;
    --shadow: 0 1px 3px rgba(15, 23, 42, 0.06), 0 4px 12px rgba(15, 23, 42, 0.04);
    --radius: 11px;

    /* Mech & role identity colors (CultureBotAI OKLCH suite) */
    --mech-habitatmech: #156f4a;
    --mech-communitymech: #008bf3;
    --mech-traitmech: #cc3867;
    --mech-cellstructuremech: #662cfc;
    --mech-proteintraitsmech: #109c98;
    --mech-antibioticmech: #8f4e21;
    --mech-naturalproductmech: #3559aa;
    --mech-taxonmech: #82307d;
    --mech-mediaingredientmech: #9a61cb;
    --mech-culturemech: #829c00;
    --mech-pathwaymech: #ac8200;
    --mech-dufmech: #566a7c;
    --mech-cofactormech: #c85a00;
    --mech-assaymech: #09697a;
    --mech-aimech: #5662a8;
    --mech-cmmmech: #b54b3f;
    --mech-edgemech: #2d6a8e;
    --mech-scaleupmech: #4d7c2b;
    --mech-xmechbay: #6d28d9;
    --mech-claw: #0f766e;
    --mech-generic-mech: #2563eb;
    --mech-other: #64748b;

    /* Semantic symbol pill palettes (light) */
    --tone-green-bg: #e3f5ea; --tone-green-fg: #0d5c32; --tone-green-bd: #a9dfc0;
    --tone-yellow-bg: #fff4ce; --tone-yellow-fg: #734c00; --tone-yellow-bd: #f0cf70;
    --tone-orange-bg: #fde8d4; --tone-orange-fg: #853a08; --tone-orange-bd: #f3bc8c;
    --tone-red-bg: #fde4e6; --tone-red-fg: #8e1924; --tone-red-bd: #f3aeb4;
    --tone-blue-bg: #e4eefc; --tone-blue-fg: #184999; --tone-blue-bd: #adc8f2;
    --tone-purple-bg: #efe7fc; --tone-purple-fg: #53229e; --tone-purple-bd: #cbb5f4;
    --tone-teal-bg: #e0f3f2; --tone-teal-fg: #0e5856; --tone-teal-bd: #9fd6d3;
    --tone-white-bg: #f1f4f7; --tone-white-fg: #3a4959; --tone-white-bd: #cfd8e2;
    --tone-slate-bg: #e6ebf0; --tone-slate-fg: #445364; --tone-slate-bd: #c3cdd8;
  }

  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --bg: #0d1218;
      --card: #141c25;
      --surface-2: #1b2531;
      --surface-hover: #1e2a38;
      --ink: #e8eef5;
      --muted: #93a4b7;
      --line: #273444;
      --line-strong: #394b61;
      --accent: #3b82f6;
      --accent-ink: #ffffff;
      --shadow: 0 2px 8px rgba(0, 0, 0, 0.35);

      --mech-habitatmech: #068d5d;
      --mech-communitymech: #0f95ff;
      --mech-traitmech: #ec5d85;
      --mech-cellstructuremech: #7251ff;
      --mech-proteintraitsmech: #00b9b5;
      --mech-antibioticmech: #e89e71;
      --mech-naturalproductmech: #9abbff;
      --mech-taxonmech: #b22bac;
      --mech-mediaingredientmech: #a97cd2;
      --mech-culturemech: #94af31;
      --mech-pathwaymech: #d8b63f;
      --mech-dufmech: #98a7b5;
      --mech-cofactormech: #ff9233;
      --mech-assaymech: #4dd4e8;
      --mech-aimech: #aab7ff;
      --mech-cmmmech: #f08c79;
      --mech-edgemech: #62b6e5;
      --mech-scaleupmech: #8dc663;
      --mech-xmechbay: #a78bfa;
      --mech-claw: #2dd4bf;
      --mech-generic-mech: #60a5fa;
      --mech-other: #94a3b8;

      --tone-green-bg: #113222; --tone-green-fg: #7de8ad; --tone-green-bd: #1f5c3e;
      --tone-yellow-bg: #362908; --tone-yellow-fg: #fcd875; --tone-yellow-bd: #695013;
      --tone-orange-bg: #39200c; --tone-orange-fg: #fdb883; --tone-orange-bd: #6e3e18;
      --tone-red-bg: #3b1419; --tone-red-fg: #fca5ad; --tone-red-bd: #70262f;
      --tone-blue-bg: #122747; --tone-blue-fg: #93c5fd; --tone-blue-bd: #234982;
      --tone-purple-bg: #261844; --tone-purple-fg: #d2b8ff; --tone-purple-bd: #482e7d;
      --tone-teal-bg: #102f30; --tone-teal-fg: #7be3df; --tone-teal-bd: #1e595b;
      --tone-white-bg: #1e2733; --tone-white-fg: #cbd5e1; --tone-white-bd: #334155;
      --tone-slate-bg: #1a222c; --tone-slate-fg: #94a3b8; --tone-slate-bd: #2d3b4d;
    }
  }

  :root[data-theme="dark"] {
    --bg: #0d1218;
    --card: #141c25;
    --surface-2: #1b2531;
    --surface-hover: #1e2a38;
    --ink: #e8eef5;
    --muted: #93a4b7;
    --line: #273444;
    --line-strong: #394b61;
    --accent: #3b82f6;
    --accent-ink: #ffffff;
    --shadow: 0 2px 8px rgba(0, 0, 0, 0.35);

    --mech-habitatmech: #068d5d;
    --mech-communitymech: #0f95ff;
    --mech-traitmech: #ec5d85;
    --mech-cellstructuremech: #7251ff;
    --mech-proteintraitsmech: #00b9b5;
    --mech-antibioticmech: #e89e71;
    --mech-naturalproductmech: #9abbff;
    --mech-taxonmech: #b22bac;
    --mech-mediaingredientmech: #a97cd2;
    --mech-culturemech: #94af31;
    --mech-pathwaymech: #d8b63f;
    --mech-dufmech: #98a7b5;
    --mech-cofactormech: #ff9233;
    --mech-assaymech: #4dd4e8;
    --mech-aimech: #aab7ff;
    --mech-cmmmech: #f08c79;
    --mech-edgemech: #62b6e5;
    --mech-scaleupmech: #8dc663;
    --mech-xmechbay: #a78bfa;
    --mech-claw: #2dd4bf;
    --mech-generic-mech: #60a5fa;
    --mech-other: #94a3b8;

    --tone-green-bg: #113222; --tone-green-fg: #7de8ad; --tone-green-bd: #1f5c3e;
    --tone-yellow-bg: #362908; --tone-yellow-fg: #fcd875; --tone-yellow-bd: #695013;
    --tone-orange-bg: #39200c; --tone-orange-fg: #fdb883; --tone-orange-bd: #6e3e18;
    --tone-red-bg: #3b1419; --tone-red-fg: #fca5ad; --tone-red-bd: #70262f;
    --tone-blue-bg: #122747; --tone-blue-fg: #93c5fd; --tone-blue-bd: #234982;
    --tone-purple-bg: #261844; --tone-purple-fg: #d2b8ff; --tone-purple-bd: #482e7d;
    --tone-teal-bg: #102f30; --tone-teal-fg: #7be3df; --tone-teal-bd: #1e595b;
    --tone-white-bg: #1e2733; --tone-white-fg: #cbd5e1; --tone-white-bd: #334155;
    --tone-slate-bg: #1a222c; --tone-slate-fg: #94a3b8; --tone-slate-bd: #2d3b4d;
  }

  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--ink);
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", system-ui, sans-serif;
    font-size: 13.5px;
    line-height: 1.4;
    font-variant-numeric: tabular-nums;
  }
  code, .mono {
    font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
    font-size: 12px;
  }

  .shell {
    max-width: 1580px;
    margin: 0 auto;
    padding: 14px 18px 40px;
  }

  /* Top header & Update controls */
  .topbar {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 12px 16px;
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: var(--radius);
    box-shadow: var(--shadow);
    margin-bottom: 12px;
  }
  .brand {
    display: flex;
    flex-direction: column;
    gap: 3px;
  }
  .brand-row {
    display: flex;
    align-items: center;
    gap: 9px;
    flex-wrap: wrap;
  }
  .brand h1 {
    margin: 0;
    font-size: 17px;
    font-weight: 760;
    letter-spacing: -0.015em;
  }
  .mode-pill {
    font-size: 11px;
    font-weight: 650;
    padding: 2px 8px;
    border-radius: 999px;
    background: var(--surface-2);
    color: var(--muted);
    border: 1px solid var(--line);
  }
  .stamp-line {
    font-size: 12px;
    color: var(--muted);
  }

  .actions {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
  }
  .btn {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    font: inherit;
    font-size: 12.5px;
    font-weight: 650;
    padding: 7px 13px;
    border-radius: 8px;
    border: 1px solid var(--line-strong);
    background: var(--card);
    color: var(--ink);
    cursor: pointer;
    text-decoration: none;
    transition: background 0.14s, border-color 0.14s, opacity 0.14s;
  }
  .btn:hover:not(:disabled) {
    background: var(--surface-2);
    border-color: var(--accent);
  }
  .btn:focus-visible, select:focus-visible, input:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
  }
  .btn-primary {
    background: var(--accent);
    color: var(--accent-ink);
    border-color: var(--accent);
  }
  .btn-primary:hover:not(:disabled) {
    background: var(--accent);
    filter: brightness(1.08);
  }
  .btn:disabled {
    opacity: 0.55;
    cursor: not-allowed;
  }
  .spinner {
    width: 12px;
    height: 12px;
    border: 2px solid currentColor;
    border-right-color: transparent;
    border-radius: 50%;
    display: none;
    animation: spin 0.7s linear infinite;
  }
  .is-loading .spinner { display: inline-block; }
  @keyframes spin { to { transform: rotate(360deg); } }

  .autorefresh-box {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 4px 9px;
    border-radius: 8px;
    border: 1px solid var(--line);
    background: var(--surface-2);
    font-size: 12px;
    color: var(--muted);
  }
  .autorefresh-box select {
    font: inherit;
    font-size: 12px;
    font-weight: 600;
    color: var(--ink);
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 6px;
    padding: 2px 6px;
  }
  .countdown-tag {
    font-size: 11px;
    font-weight: 650;
    color: var(--accent);
    min-width: 28px;
  }

  .error-toast {
    display: none;
    align-items: center;
    justify-content: space-between;
    gap: 8px 12px;
    flex-wrap: wrap;
    margin-bottom: 10px;
    padding: 9px 13px;
    border-radius: 8px;
    background: var(--tone-red-bg);
    color: var(--tone-red-fg);
    border: 1px solid var(--tone-red-bd);
    font-size: 12.5px;
    font-weight: 600;
  }
  .error-toast.is-visible {
    display: flex;
  }
  .toast-reload-btn {
    padding: 4px 10px;
    font-size: 12px;
    flex: none;
  }

  /* Needs Human Input top spotlight */
  .human-input-box {
    background: var(--card);
    border: 2px solid var(--tone-yellow-bd);
    border-left: 6px solid #eab308;
    border-radius: var(--radius);
    padding: 11px 14px;
    margin-bottom: 11px;
    box-shadow: var(--shadow);
  }
  .human-input-box.has-messages-only {
    border-color: var(--tone-purple-bd);
    border-left-color: #7c3aed;
  }
  .human-input-box.has-drafts-only {
    border-color: var(--tone-orange-bd);
    border-left-color: #ea580c;
  }
  .human-input-box.is-clear {
    border-width: 1px;
    border-color: var(--tone-green-bd);
    border-left: 4px solid #16a34a;
    padding: 7px 13px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    flex-wrap: wrap;
    font-size: 12.5px;
    color: var(--muted);
  }
  .human-input-box.is-clear b {
    color: var(--tone-green-fg);
  }
  .human-input-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px 12px;
    flex-wrap: wrap;
    margin-bottom: 8px;
  }
  .human-input-title-wrap {
    display: flex;
    align-items: center;
    gap: 9px;
    flex-wrap: wrap;
  }
  .human-input-badge {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    font-size: 12px;
    font-weight: 780;
    letter-spacing: 0.03em;
    text-transform: uppercase;
    padding: 3.5px 10px;
    border-radius: 999px;
    background: var(--tone-yellow-bg);
    color: var(--tone-yellow-fg);
    border: 1px solid var(--tone-yellow-bd);
  }
  .human-input-badge.is-message-only {
    background: var(--tone-purple-bg);
    color: var(--tone-purple-fg);
    border-color: var(--tone-purple-bd);
  }
  .human-input-badge.is-draft-only {
    background: var(--tone-orange-bg);
    color: var(--tone-orange-fg);
    border-color: var(--tone-orange-bd);
  }
  .human-input-sub {
    font-size: 12px;
    color: var(--muted);
    font-weight: 560;
  }
  .human-input-filter-btn {
    padding: 5px 11px;
    font-size: 12px;
  }
  .human-input-cats {
    display: flex;
    align-items: center;
    gap: 6px;
    flex-wrap: wrap;
    margin-bottom: 9px;
    padding-bottom: 8px;
    border-bottom: 1px solid var(--line);
  }
  .hic-cat-chip {
    font: inherit;
    font-size: 11.5px;
    font-weight: 680;
    padding: 3.5px 9px;
    border-radius: 999px;
    border: 1px solid var(--line-strong);
    background: var(--surface-2);
    color: var(--ink);
    cursor: pointer;
    transition: border-color 0.12s, background 0.12s;
  }
  .hic-cat-chip:hover {
    border-color: var(--accent);
  }
  .hic-cat-chip[aria-pressed="true"] {
    background: var(--ink);
    color: var(--card);
    border-color: var(--ink);
  }
  .hic-cat-chip.is-agy-chip {
    border-color: var(--tone-teal-bd);
    background: var(--tone-teal-bg);
    color: var(--tone-teal-fg);
  }
  .hic-cat-chip.is-agy-chip[aria-pressed="true"] {
    background: var(--tone-teal-fg);
    color: var(--card);
    border-color: var(--tone-teal-fg);
  }
  .human-input-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
    gap: 8px;
  }
  .human-input-card {
    font: inherit;
    text-align: left;
    width: 100%;
    background: var(--surface-2);
    color: var(--ink);
    border: 1px solid var(--tone-yellow-bd);
    border-left: 4px solid #eab308;
    border-radius: 9px;
    padding: 8px 11px;
    cursor: pointer;
    display: flex;
    flex-direction: column;
    gap: 5px;
    transition: background 0.12s, border-color 0.12s, transform 0.08s;
  }
  .human-input-card.is-agent-message {
    border-color: var(--tone-purple-bd);
    border-left-color: #7c3aed;
  }
  .human-input-card.is-pasted,
  .human-input-card.is-draft {
    border-color: var(--tone-orange-bd);
    border-left-color: #ea580c;
  }
  .human-input-card:hover {
    background: var(--surface-hover);
    border-color: var(--accent);
  }
  .human-input-card:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
  }
  .hic-top {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 6px;
    flex-wrap: wrap;
  }
  .hic-top-left {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    flex-wrap: wrap;
  }
  .hic-action {
    font-size: 11.5px;
    font-weight: 760;
    padding: 2px 7px;
    border-radius: 6px;
    background: var(--tone-yellow-bg);
    color: var(--tone-yellow-fg);
    border: 1px solid var(--tone-yellow-bd);
  }
  .human-input-card.is-agent-message .hic-action {
    background: var(--tone-purple-bg);
    color: var(--tone-purple-fg);
    border-color: var(--tone-purple-bd);
  }
  .human-input-card.is-pasted .hic-action,
  .human-input-card.is-draft .hic-action {
    background: var(--tone-orange-bg);
    color: var(--tone-orange-fg);
    border-color: var(--tone-orange-bd);
  }
  .hic-agy-badge {
    font-size: 11px;
    font-weight: 760;
    padding: 2px 7px;
    border-radius: 6px;
    background: var(--tone-teal-bg);
    color: var(--tone-teal-fg);
    border: 1px solid var(--tone-teal-bd);
    white-space: nowrap;
  }
  .hic-agy-active-badge {
    font-size: 10.5px;
    font-weight: 680;
    padding: 2px 6px;
    border-radius: 6px;
    background: var(--tone-slate-bg);
    color: var(--tone-slate-fg);
    border: 1px solid var(--tone-slate-bd);
    white-space: nowrap;
  }
  .hic-key-row {
    display: flex;
    align-items: center;
    gap: 5px;
    flex-wrap: wrap;
  }
  .hic-keys {
    font-family: ui-monospace, "SF Mono", Menlo, monospace;
    font-size: 11px;
    font-weight: 700;
    padding: 1.5px 6.5px;
    border-radius: 5px;
    background: var(--card);
    color: var(--ink);
    border: 1px solid var(--line-strong);
  }
  .hic-agy-cmd {
    font-family: ui-monospace, "SF Mono", Menlo, monospace;
    font-size: 10.5px;
    font-weight: 750;
    padding: 1.5px 6px;
    border-radius: 5px;
    background: var(--tone-teal-bg);
    color: var(--tone-teal-fg);
    border: 1px solid var(--tone-teal-bd);
  }
  .hic-wait {
    font-size: 11.5px;
    font-weight: 680;
    color: var(--muted);
    white-space: nowrap;
  }
  .hic-where {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 6px;
    flex-wrap: wrap;
  }
  .hic-proj {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    min-width: 0;
    font-size: 13px;
  }
  .hic-proj strong {
    font-weight: 760;
  }
  .hic-role {
    font-size: 10.5px;
    font-weight: 650;
    text-transform: uppercase;
    letter-spacing: 0.03em;
    color: var(--muted);
    padding: 1px 5px;
    border-radius: 4px;
    background: var(--card);
    border: 1px solid var(--line);
  }
  .hic-badges {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    flex-wrap: wrap;
  }
  .hic-loc {
    font-family: ui-monospace, "SF Mono", Menlo, monospace;
    font-size: 11.5px;
    font-weight: 750;
    padding: 2px 6px;
    border-radius: 5px;
    background: var(--card);
    color: var(--ink);
    border: 1px solid var(--line-strong);
  }
  .hic-act {
    font-size: 12px;
    color: var(--muted);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .hic-act.has-act {
    color: var(--ink);
  }

  /* Host telemetry cards */
  .host-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(310px, 1fr));
    gap: 10px;
    margin-bottom: 10px;
  }
  .host-card {
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: var(--radius);
    padding: 11px 14px;
    box-shadow: var(--shadow);
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .host-card.is-unreachable {
    border-color: var(--tone-red-bd);
  }
  .host-card.has-human-input {
    border-color: var(--tone-yellow-bd);
  }
  .host-card.has-agent-message {
    border-color: var(--tone-purple-bd);
  }
  .host-card.has-drafts-only {
    border-color: var(--tone-orange-bd);
  }
  .host-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
  }
  .host-head-pills {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    flex-wrap: wrap;
  }
  .host-input-pill {
    font: inherit;
    cursor: pointer;
  }
  .host-input-pill:hover {
    border-color: var(--accent);
  }
  .host-title {
    display: flex;
    align-items: center;
    gap: 7px;
    font-weight: 720;
    font-size: 14px;
  }
  .host-main-row {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 8px;
    flex-wrap: wrap;
  }
  .host-count {
    font-size: 18px;
    font-weight: 780;
    letter-spacing: -0.02em;
  }
  .host-dots {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 12px;
    font-weight: 650;
  }
  .host-roles-line {
    font-size: 11.5px;
    color: var(--muted);
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
  }
  .host-metrics {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 6px;
    padding-top: 7px;
    border-top: 1px solid var(--line);
  }
  .metric-cell {
    display: flex;
    flex-direction: column;
    gap: 1px;
  }
  .metric-cell .k {
    font-size: 10.5px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--muted);
  }
  .metric-cell .v {
    font-size: 12px;
    font-weight: 650;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .load-bar-track {
    width: 100%;
    height: 6px;
    margin-top: 3px;
    border-radius: 999px;
    background: var(--surface-2);
    border: 1px solid var(--line);
    overflow: hidden;
  }
  .load-bar-fill {
    height: 100%;
    border-radius: 999px;
    transition: width 0.2s ease;
  }
  .load-bar-fill.load-green  { background: #16a34a; }
  .load-bar-fill.load-yellow { background: #eab308; }
  .load-bar-fill.load-orange { background: #ea580c; }
  .load-bar-fill.load-red    { background: #dc2626; }

  /* Fleet rollup strip */
  .rollup-strip {
    display: grid;
    grid-template-columns: repeat(6, minmax(0, 1fr));
    gap: 8px;
    margin-bottom: 10px;
  }
  @media (max-width: 1020px) { .rollup-strip { grid-template-columns: repeat(3, minmax(0, 1fr)); } }
  @media (max-width: 580px) { .rollup-strip { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
  .rollup-tile {
    font: inherit;
    color: var(--ink);
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 9px;
    padding: 8px 11px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    text-align: left;
  }
  .rollup-tile.is-clickable {
    cursor: pointer;
    transition: border-color 0.14s, background 0.14s;
  }
  .rollup-tile.is-clickable:hover {
    border-color: var(--accent);
    background: var(--surface-hover);
  }
  .rollup-tile.has-human-input {
    border-color: var(--tone-yellow-bd);
    border-left: 4px solid #eab308;
    background: var(--tone-yellow-bg);
  }
  .rollup-tile.has-human-input .lbl,
  .rollup-tile.has-human-input .val {
    color: var(--tone-yellow-fg);
  }
  .rollup-tile.has-agent-message {
    border-color: var(--tone-purple-bd);
    border-left: 4px solid #7c3aed;
    background: var(--tone-purple-bg);
  }
  .rollup-tile.has-agent-message .lbl,
  .rollup-tile.has-agent-message .val {
    color: var(--tone-purple-fg);
  }
  .rollup-tile.has-drafts-only {
    border-color: var(--tone-orange-bd);
    border-left: 4px solid #ea580c;
    background: var(--tone-orange-bg);
  }
  .rollup-tile.has-drafts-only .lbl,
  .rollup-tile.has-drafts-only .val {
    color: var(--tone-orange-fg);
  }
  .rollup-tile .lbl {
    font-size: 11.5px;
    color: var(--muted);
    font-weight: 600;
  }
  .rollup-tile .val {
    font-size: 16px;
    font-weight: 780;
  }

  /* System & session alerts callout */
  .attention-box {
    background: var(--card);
    border: 1px solid var(--tone-red-bd);
    border-left: 4px solid #dc2626;
    border-radius: var(--radius);
    padding: 9px 13px;
    margin-bottom: 12px;
  }
  .attention-head {
    font-size: 12px;
    font-weight: 740;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--muted);
    margin-bottom: 6px;
  }
  .attention-list {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }
  .attention-chip {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    font-size: 12px;
    padding: 4px 9px;
    border-radius: 7px;
    background: var(--surface-2);
    border: 1px solid var(--line);
    color: var(--ink);
    cursor: pointer;
  }
  .attention-chip:hover { border-color: var(--accent); }

  /* Controls & filter bar */
  .toolbar {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 9px 14px;
    padding: 9px 13px;
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: var(--radius);
    margin-bottom: 14px;
  }
  .tool-group {
    display: flex;
    align-items: center;
    gap: 5px;
    flex-wrap: wrap;
  }
  .tool-label {
    font-size: 11px;
    font-weight: 720;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--muted);
    margin-right: 3px;
  }
  .chip {
    font: inherit;
    font-size: 12px;
    font-weight: 620;
    padding: 4px 10px;
    border-radius: 999px;
    border: 1px solid var(--line);
    background: var(--surface-2);
    color: var(--ink);
    cursor: pointer;
  }
  .chip:hover { border-color: var(--accent); }
  .chip[aria-pressed="true"], .chip[aria-selected="true"] {
    background: var(--ink);
    color: var(--card);
    border-color: var(--ink);
  }
  .search-input {
    font: inherit;
    font-size: 12.5px;
    padding: 5px 10px;
    border-radius: 7px;
    border: 1px solid var(--line-strong);
    background: var(--bg);
    color: var(--ink);
    min-width: 210px;
  }
  .cached-toggle {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    font-size: 12px;
    color: var(--muted);
    cursor: pointer;
  }

  /* Section blocks & three symbol tables */
  .table-section {
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: var(--radius);
    box-shadow: var(--shadow);
    margin-bottom: 14px;
    overflow: hidden;
  }
  .section-bar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    padding: 10px 14px;
    background: var(--surface-2);
    border-bottom: 1px solid var(--line);
  }
  .section-title {
    display: flex;
    align-items: center;
    gap: 9px;
    margin: 0;
    font-size: 14.5px;
    font-weight: 760;
  }
  .role-spine {
    width: 10px;
    height: 10px;
    border-radius: 50%;
    flex: none;
  }
  .section-badges {
    display: flex;
    align-items: center;
    gap: 7px;
    font-size: 12px;
    color: var(--muted);
    font-weight: 650;
  }

  .table-wrap {
    width: 100%;
    overflow-x: auto;
  }
  table.symbol-table {
    width: 100%;
    min-width: 1060px;
    border-collapse: collapse;
    table-layout: fixed;
  }
  table.symbol-table col.col-session { width: 19.5%; }
  table.symbol-table col.col-mac     { width: 6.2%; }
  table.symbol-table col.col-cli     { width: 7.6%; }
  table.symbol-table col.col-state   { width: 9.6%; }
  table.symbol-table col.col-guard   { width: 7.6%; }
  table.symbol-table col.col-delta   { width: 7.4%; }
  table.symbol-table col.col-ctx     { width: 6.2%; }
  table.symbol-table col.col-ports   { width: 6.6%; }
  table.symbol-table col.col-view    { width: 6.5%; }
  table.symbol-table col.col-up      { width: 8.3%; }
  table.symbol-table col.col-act     { width: 14.5%; }

  table.symbol-table thead th {
    position: sticky;
    top: 0;
    z-index: 2;
    background: var(--card);
    border-bottom: 2px solid var(--line-strong);
    padding: 7px 6px;
    text-align: left;
    font-size: 11px;
    font-weight: 740;
    text-transform: uppercase;
    letter-spacing: 0.045em;
    color: var(--muted);
    user-select: none;
    cursor: pointer;
    white-space: nowrap;
  }
  table.symbol-table thead th.sym-col { text-align: center; }
  table.symbol-table thead th:hover { color: var(--ink); }

  tr.session-row {
    border-bottom: 1px solid var(--line);
    cursor: pointer;
    transition: background 0.1s;
  }
  tr.session-row:hover { background: var(--surface-hover); }
  tr.session-row.is-stale-cached { opacity: 0.62; }
  tr.session-row.is-human-prompt {
    background: rgba(234, 179, 8, 0.10);
  }
  tr.session-row.is-human-prompt:hover {
    background: rgba(234, 179, 8, 0.17);
  }
  tr.session-row.is-human-prompt td:first-child {
    box-shadow: inset 4px 0 0 #eab308;
  }
  tr.session-row.is-human-message {
    background: rgba(124, 58, 237, 0.08);
  }
  tr.session-row.is-human-message:hover {
    background: rgba(124, 58, 237, 0.14);
  }
  tr.session-row.is-human-message td:first-child {
    box-shadow: inset 4px 0 0 #7c3aed;
  }
  tr.session-row.is-human-draft {
    background: rgba(234, 88, 12, 0.08);
  }
  tr.session-row.is-human-draft:hover {
    background: rgba(234, 88, 12, 0.14);
  }
  tr.session-row.is-human-draft td:first-child {
    box-shadow: inset 4px 0 0 #ea580c;
  }
  @keyframes rowFlash {
    0% { outline: 2px solid #eab308; outline-offset: -2px; }
    100% { outline: 2px solid transparent; outline-offset: -2px; }
  }
  tr.session-row.is-flash-target {
    animation: rowFlash 1.8s ease-out 1;
  }
  tr.session-row td {
    padding: 6.5px 6px;
    vertical-align: middle;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  tr.session-row td.sym-cell { text-align: center; }

  .sess-cell {
    display: flex;
    align-items: center;
    gap: 7px;
    min-width: 0;
  }
  .mech-dot {
    width: 9px;
    height: 9px;
    border-radius: 50%;
    flex: none;
  }
  .sess-names {
    display: flex;
    align-items: baseline;
    gap: 5px;
    min-width: 0;
    overflow: hidden;
  }
  .proj-name {
    font-weight: 700;
    color: var(--ink);
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .stream-tag {
    font-size: 11.5px;
    color: var(--muted);
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .target-tag {
    margin-left: auto;
    flex: none;
    font-family: ui-monospace, "SF Mono", Menlo, monospace;
    font-size: 11px;
    padding: 1px 5px;
    border-radius: 4px;
    background: var(--surface-2);
    color: var(--muted);
    border: 1px solid var(--line);
  }
  .target-tag.is-terminal-link,
  .hic-loc.is-terminal-link {
    color: var(--tone-purple-fg);
    border-color: var(--tone-purple-bd);
    background: var(--tone-purple-bg);
    font-weight: 720;
  }
  tr.session-row.is-m1-focusable:hover .target-tag.is-terminal-link,
  .human-input-card.is-m1-focusable:hover .hic-loc.is-terminal-link {
    border-color: var(--accent);
  }
  .row-expand-btn {
    flex: none;
    font: inherit;
    font-size: 11px;
    line-height: 1;
    padding: 2px 5px;
    border-radius: 4px;
    border: 1px solid var(--line);
    background: var(--card);
    color: var(--muted);
    cursor: pointer;
  }
  .row-expand-btn:hover {
    color: var(--ink);
    border-color: var(--accent);
    background: var(--surface-2);
  }
  .detail-focus-btn {
    margin-top: 3px;
    font-size: 11.5px;
    padding: 3px 9px;
  }

  /* Colored symbol pills */
  .sym-pill {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 3.5px;
    padding: 2.5px 6.5px;
    border-radius: 999px;
    font-size: 11.5px;
    font-weight: 680;
    line-height: 1.2;
    border: 1px solid transparent;
    max-width: 100%;
    white-space: nowrap;
  }
  .sym-dot { font-size: 10.5px; line-height: 1; }
  .sym-glyph { font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 11px; font-weight: 750; }
  .sym-txt { font-size: 11px; }

  .tone-green  { background: var(--tone-green-bg);  color: var(--tone-green-fg);  border-color: var(--tone-green-bd); }
  .tone-yellow { background: var(--tone-yellow-bg); color: var(--tone-yellow-fg); border-color: var(--tone-yellow-bd); }
  .tone-orange { background: var(--tone-orange-bg); color: var(--tone-orange-fg); border-color: var(--tone-orange-bd); }
  .tone-red    { background: var(--tone-red-bg);    color: var(--tone-red-fg);    border-color: var(--tone-red-bd); }
  .tone-blue   { background: var(--tone-blue-bg);   color: var(--tone-blue-fg);   border-color: var(--tone-blue-bd); }
  .tone-purple { background: var(--tone-purple-bg); color: var(--tone-purple-fg); border-color: var(--tone-purple-bd); }
  .tone-teal   { background: var(--tone-teal-bg);   color: var(--tone-teal-fg);   border-color: var(--tone-teal-bd); }
  .tone-white  { background: var(--tone-white-bg);  color: var(--tone-white-fg);  border-color: var(--tone-white-bd); }
  .tone-slate  { background: var(--tone-slate-bg);  color: var(--tone-slate-fg);  border-color: var(--tone-slate-bd); }

  .up-cell {
    font-size: 11.5px;
    color: var(--ink);
  }
  .up-cell .tmr {
    color: var(--muted);
    font-size: 11px;
  }
  .act-cell {
    font-size: 12px;
    color: var(--muted);
  }
  .act-cell.has-act { color: var(--ink); }

  tr.detail-row {
    background: var(--surface-2);
    border-bottom: 1px solid var(--line-strong);
  }
  tr.detail-row td {
    padding: 9px 14px;
    white-space: normal;
  }
  .detail-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    gap: 8px 16px;
    font-size: 12px;
  }
  .detail-item b {
    display: block;
    font-size: 10.5px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--muted);
    margin-bottom: 2px;
  }

  .empty-row td {
    padding: 16px;
    text-align: center;
    color: var(--muted);
    font-style: italic;
  }

  /* Collapsed Other Sessions drawer */
  details.other-drawer {
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: var(--radius);
    box-shadow: var(--shadow);
    margin-bottom: 16px;
    overflow: hidden;
  }
  details.other-drawer > summary {
    list-style: none;
    cursor: pointer;
    padding: 10px 14px;
    background: var(--surface-2);
    display: flex;
    align-items: center;
    justify-content: space-between;
    font-weight: 720;
    font-size: 13.5px;
  }
  details.other-drawer > summary::-webkit-details-marker { display: none; }
  details.other-drawer[open] > summary { border-bottom: 1px solid var(--line); }

  /* Compact Matrix view */
  table.matrix-table {
    width: 100%;
    border-collapse: collapse;
  }
  table.matrix-table th, table.matrix-table td {
    padding: 7px 10px;
    border-bottom: 1px solid var(--line);
    text-align: left;
    vertical-align: middle;
  }
  table.matrix-table thead th {
    background: var(--surface-2);
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--muted);
  }
  .matrix-pills {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
  }
  .matrix-pill-btn {
    padding: 0;
    margin: 0;
    border: none;
    background: transparent;
    font: inherit;
    cursor: pointer;
    border-radius: 999px;
  }
  .matrix-pill-btn:hover .sym-pill {
    border-color: var(--accent);
  }
  .matrix-pill-btn:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
  }

  /* Legend footer */
  .legend {
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: var(--radius);
    padding: 11px 15px;
    font-size: 12px;
    color: var(--muted);
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .legend-row {
    display: flex;
    flex-wrap: wrap;
    gap: 6px 14px;
  }
  .legend-row b { color: var(--ink); }
</style>
</head>
<body>
<div class="shell">
  <header class="topbar">
    <div class="brand">
      <div class="brand-row">
        <h1>XMechBay · Fleet Session Dashboard</h1>
        <span id="mode-badge" class="mode-pill">Read-only · 127.0.0.1</span>
      </div>
      <div id="stamp-line" class="stamp-line">Loading snapshot…</div>
    </div>
    <div class="actions">
      <button id="btn-update" type="button" class="btn btn-primary" title="Probe configured Macs now (shortcut: r)">
        <span class="spinner" aria-hidden="true"></span>
        <span id="btn-update-label">↻ Update</span>
      </button>
      <div class="autorefresh-box" title="Opt-in periodic read-only refresh (off by default)">
        <label for="select-autorefresh">Auto-refresh</label>
        <select id="select-autorefresh">
          <option value="0" selected>Off</option>
          <option value="60">60s</option>
          <option value="300">5m</option>
          <option value="900">15m</option>
        </select>
        <span id="autorefresh-countdown" class="countdown-tag" aria-live="polite"></span>
      </div>
      <a id="link-export" class="btn" href="/export.html" download="mechbay-dashboard-snapshot.html">Export HTML</a>
      <button id="btn-theme" type="button" class="btn" title="Cycle color theme (Auto / Light / Dark)">Theme: Auto</button>
    </div>
  </header>

  <div id="error-toast" class="error-toast" role="alert"></div>

  <section id="human-input-banner" class="human-input-box is-clear" aria-label="Sessions needing human input"></section>
  <section id="host-cards" class="host-grid" aria-label="Mac host status cards"></section>
  <section id="fleet-rollup" class="rollup-strip" aria-label="Fleet summary counts"></section>
  <section id="attention-banner" class="attention-box" style="display:none" aria-label="System and session alerts"></section>

  <section class="toolbar" aria-label="View mode and filters">
    <div class="tool-group" role="tablist" aria-label="Grouping mode">
      <span class="tool-label">View</span>
      <button type="button" class="chip" data-view="role" aria-selected="true">3 Tables: MechBay · Claw · Mechs</button>
      <button type="button" class="chip" data-view="host" aria-selected="false">3 Tables: By Mac</button>
      <button type="button" class="chip" data-view="matrix" aria-selected="false">Project × Mac Matrix</button>
    </div>
    <div class="tool-group" id="state-filters" aria-label="Symbol filter">
      <span class="tool-label">Filter</span>
      <button type="button" class="chip" data-filter="all" aria-pressed="true">All</button>
      <button type="button" class="chip" data-filter="input" aria-pressed="false" title="Show sessions waiting on human prompts or unsent composer drafts (shortcut: i)">🟡 Needs Input</button>
      <button type="button" class="chip" data-filter="active" aria-pressed="false">🟢 Active</button>
      <button type="button" class="chip" data-filter="alert" aria-pressed="false">🔴 Alerts</button>
      <button type="button" class="chip" data-filter="changed" aria-pressed="false">🟢 ◆ Δnow</button>
      <button type="button" class="chip" data-filter="idle" aria-pressed="false">⚪ Idle/Done</button>
      <button type="button" class="chip" data-filter="unknown" aria-pressed="false">⚫ Blind/Unk</button>
    </div>
    <div class="tool-group" id="mac-filters" aria-label="Mac filter"></div>
    <div class="tool-group">
      <input id="filter-search" class="search-input" type="search" placeholder="Filter repo, stream, CLI, ~activity (/)" aria-label="Filter sessions">
      <label id="cached-toggle-wrap" class="cached-toggle" style="display:none">
        <input id="toggle-cached" type="checkbox" checked>
        <span>Include cached unreachable rows</span>
      </label>
    </div>
  </section>

  <main id="tables-container"></main>

  <footer class="legend">
    <div class="legend-row">
      <span><b>State:</b> 🟢 ◎ goal · 🟢 ▶ work · 🟡 ! approve · 🟡 ? question · 🟡 🔑/🛡/↑ sign-in/trust/update · 🟠 ⏸ paused · 🔴 ✖ stalled · 🔴 ⊘ quota · 🔵 ✓ achieved · ⚪ ○ idle · ⚫ ? unknown</span>
    </div>
    <div class="legend-row">
      <span><b>Guard:</b> 🟢 ● clear · 🟡 ⚡ agy-work fixable · 🟡 ▲ keystroke prompt · 🟣 💬 agent reply needed · 🟠 📋 pasted not running · 🟠 ✎ unsent draft · 🔴 ⚡ retry/error · ⚫ ◌ blind</span>
      <span><b>Δ:</b> 🟢 ◆ Δnow · 🔵 ◈ &lt;15m · ⚪ ◇ 15m–2h · 🟠 ◌ &ge;2h · ⚫ · first/Δ?</span>
      <span><b>Ctx:</b> 🟢 &ge;50% · 🟡 20–49% · 🔴 &lt;20%</span>
      <span><b>Ports:</b> 🟢 &lt;100 · 🟡 100–499 · 🟠 500–999 · 🔴 &ge;1000!</span>
      <span><b>View:</b> 🟢▣ win · 🟢⊞ view · 🔵▫ tab · 🟡▾ min · ⚪⊟ tmux · ⚫◌ proc</span>
      <span><b>Keys:</b> <code>i</code> Needs Input · <code>/</code> search · <code>r</code> Update</span>
    </div>
    <div class="legend-row">
      <span><b>Observation contract:</b> <code>~</code> marks screen-reported text, never verified repository completion. <code>launch#</code> is shortened argv evidence; reconfirm current session ID before control. Clicking <b>Update</b> runs read-only probes only; clicking an <b>M1</b> session row switches local <code>Terminal.app</code> to that window and tab (zero input sent; non-M1 rows toggle details only).</span>
    </div>
  </footer>
</div>

<script id="initial-payload" type="application/json">__INITIAL_PAYLOAD_JSON__</script>
<script>
(function() {
  "use strict";

  const STATIC_EXPORT = __STATIC_EXPORT_BOOL__;
  const CSRF_TOKEN = "__CSRF_TOKEN__";

  let payload = JSON.parse(document.getElementById("initial-payload").textContent);
  let viewMode = "role";
  let stateFilter = "all";
  let inputSubFilter = "all";
  let macFilter = "all";
  let searchQuery = "";
  let includeCached = true;
  let sortCol = "default";
  let sortAsc = true;
  let otherDrawerOpen = false;
  let otherDrawerUserSet = false;
  let flashRowId = null;
  const expandedRows = new Set();

  let refreshInFlight = false;
  let focusInFlight = false;
  let pendingPostReloadFocusRowId = null;
  let autoIntervalSec = 0;
  let nextAutoAt = 0;
  let themeMode = "auto";

  const CSRF_RELOAD_AT_KEY = "xmechbay_csrf_reload_at";
  const CSRF_RECOVERY_KEY = "xmechbay_csrf_recovery";
  const CSRF_RELOAD_COOLDOWN_MS = 30000;
  const CSRF_RECOVERY_MAX_AGE_MS = 60000;

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    if (attrs) {
      for (const [k, v] of Object.entries(attrs)) {
        if (v === null || v === undefined || v === false) continue;
        if (k === "className") node.className = v;
        else if (k === "text") node.textContent = String(v);
        else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2).toLowerCase(), v);
        else node.setAttribute(k, v === true ? "" : String(v));
      }
    }
    if (children) {
      for (const child of Array.isArray(children) ? children : [children]) {
        if (child === null || child === undefined || child === false) continue;
        node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
      }
    }
    return node;
  }

  function mechColorVar(colorKey) {
    const safe = String(colorKey || "other").replace(/[^a-z0-9-]/g, "");
    return "var(--mech-" + (safe || "other") + ", var(--mech-other))";
  }

  function renderSymPill(sym, showGlyph) {
    const s = sym || {tone: "slate", dot: "⚫", glyph: "?", short: "?", detail: ""};
    const kids = [el("span", {className: "sym-dot", text: s.dot})];
    if (showGlyph && s.glyph && s.glyph !== s.short) {
      kids.push(el("span", {className: "sym-glyph", text: s.glyph}));
    }
    kids.push(el("span", {className: "sym-txt", text: s.short}));
    return el("span", {
      className: "sym-pill tone-" + s.tone,
      title: s.detail || s.short,
      "aria-label": s.detail || s.short
    }, kids);
  }

  function relAge(isoText) {
    if (!isoText) return "";
    const ms = Date.parse(isoText);
    if (Number.isNaN(ms)) return "";
    const sec = Math.max(0, Math.floor((Date.now() - ms) / 1000));
    if (sec < 5) return "just now";
    if (sec < 60) return sec + "s ago";
    const min = Math.floor(sec / 60);
    if (min < 60) return min + "m" + (sec % 60 ? (sec % 60) + "s" : "") + " ago";
    const hr = Math.floor(min / 60);
    return hr + "h" + (min % 60 ? (min % 60) + "m" : "") + " ago";
  }

  function formatUtc(isoText) {
    if (!isoText) return "unknown";
    const d = new Date(isoText);
    if (Number.isNaN(d.getTime())) return String(isoText);
    return d.toISOString().replace("T", " ").slice(0, 19) + " UTC";
  }

  function humanInputCounts(forHostKey) {
    const scopeHost = (typeof forHostKey === "string" && forHostKey) ? forHostKey : "all";
    if (scopeHost !== "all") {
      const hostObj = (payload.hosts || []).find(function(h) { return h.key === scopeHost; });
      if (hostObj) {
        const hk = typeof hostObj.human_keystroke_count === "number"
          ? hostObj.human_keystroke_count
          : (typeof hostObj.human_prompt_count === "number" ? hostObj.human_prompt_count : 0);
        const ha = typeof hostObj.human_agy_work_count === "number" ? hostObj.human_agy_work_count : 0;
        const hm = typeof hostObj.human_message_count === "number" ? hostObj.human_message_count : 0;
        const hp = typeof hostObj.human_pasted_count === "number"
          ? hostObj.human_pasted_count
          : (typeof hostObj.human_draft_count === "number" ? hostObj.human_draft_count : 0);
        const ht = typeof hostObj.human_total_count === "number" ? hostObj.human_total_count : (hk + hm + hp);
        return {
          keystrokes: hk,
          prompts: hk,
          agyWork: ha,
          messages: hm,
          pasted: hp,
          drafts: hp,
          total: ht,
          hostLabel: hostObj.label || scopeHost
        };
      }
    }
    const s = payload.stats || {};
    const tc = s.tone_counts || {};
    const keystrokes = typeof s.human_keystroke_count === "number"
      ? s.human_keystroke_count
      : (typeof s.human_prompt_count === "number" ? s.human_prompt_count : (tc.input || 0));
    const agyWork = typeof s.human_agy_work_count === "number" ? s.human_agy_work_count : 0;
    const messages = typeof s.human_message_count === "number" ? s.human_message_count : 0;
    const pasted = typeof s.human_pasted_count === "number"
      ? s.human_pasted_count
      : (typeof s.human_draft_count === "number" ? s.human_draft_count : 0);
    const total = typeof s.human_total_count === "number"
      ? s.human_total_count
      : (keystrokes + messages + pasted);
    return {
      keystrokes: keystrokes,
      prompts: keystrokes,
      agyWork: agyWork,
      messages: messages,
      pasted: pasted,
      drafts: pasted,
      total: total,
      hostLabel: null
    };
  }

  function setStateFilter(nextFilter, nextSubFilter) {
    stateFilter = nextFilter;
    inputSubFilter = (nextFilter === "input" && nextSubFilter) ? nextSubFilter : "all";
    for (const b of document.querySelectorAll("[data-filter]")) {
      b.setAttribute("aria-pressed", String(b.getAttribute("data-filter") === stateFilter));
    }
    renderHumanInputBanner();
    renderMain();
  }

  function setInputSubFilter(sub) {
    if (stateFilter === "input" && inputSubFilter === sub && sub !== "all") {
      setStateFilter("input", "all");
    } else if (sub === "all" && stateFilter !== "input") {
      setStateFilter("input", "all");
    } else if (sub === "all" && stateFilter === "input" && inputSubFilter === "all") {
      setStateFilter("all");
    } else {
      setStateFilter("input", sub);
    }
  }

  function isLoopbackPageHost() {
    if (!window.location || !window.location.hostname) return true;
    const h = String(window.location.hostname).trim().toLowerCase();
    return h === "127.0.0.1" || h === "localhost" || h === "::1" || h === "[::1]";
  }

  function isRowM1Focusable(row) {
    if (!row || STATIC_EXPORT || window.location.protocol === "file:") return false;
    if (!isLoopbackPageHost()) return false;
    if (!payload || payload.mode !== "live") return false;
    if (row.stale_cached || !row.can_focus_terminal) return false;
    const glyph = row.symbols && row.symbols.mac ? row.symbols.mac.glyph : (row.mac ? row.mac.glyph : "");
    return glyph === "M1";
  }

  function jumpToSessionRow(rowId) {
    if (!rowId) return;
    const sess = (payload.sessions || []).find(r => r.row_id === rowId);
    if (viewMode === "matrix") {
      viewMode = "role";
      for (const b of document.querySelectorAll("[data-view]")) {
        b.setAttribute("aria-selected", String(b.getAttribute("data-view") === "role"));
      }
    }
    if (sess && sess.role === "other") {
      otherDrawerOpen = true;
      otherDrawerUserSet = true;
    }
    if (sess && sess.stale_cached && !includeCached) {
      includeCached = true;
      const cb = document.getElementById("toggle-cached");
      if (cb) cb.checked = true;
    }
    if (sess && !matchesFilters(sess)) {
      macFilter = "all";
      stateFilter = "all";
      inputSubFilter = "all";
      searchQuery = "";
      const searchInput = document.getElementById("filter-search");
      if (searchInput) searchInput.value = "";
      for (const b of document.querySelectorAll("[data-filter]")) {
        b.setAttribute("aria-pressed", String(b.getAttribute("data-filter") === "all"));
      }
      renderMacFilterChips();
    }
    expandedRows.add(rowId);
    flashRowId = rowId;
    renderHumanInputBanner();
    renderMain();
    const target = document.getElementById("row-" + rowId);
    if (target && typeof target.scrollIntoView === "function") {
      target.scrollIntoView({behavior: "smooth", block: "center"});
    }
    if (sess && isRowM1Focusable(sess)) {
      focusTerminalSession(sess);
    }
  }

  function updateHeader() {
    const counts = humanInputCounts("all");
    const scopedCounts = macFilter === "all" ? counts : humanInputCounts(macFilter);
    if (counts.keystrokes > 0) {
      document.title = "🟡 (" + counts.total + " need input) XMechBay · Fleet Dashboard";
    } else if (counts.messages > 0) {
      document.title = "🟣 (" + counts.total + " need reply) XMechBay · Fleet Dashboard";
    } else if (counts.pasted > 0) {
      document.title = "🟠 (" + counts.pasted + " pasted not running) XMechBay · Fleet Dashboard";
    } else {
      document.title = "XMechBay · Local Fleet Session Dashboard";
    }

    for (const b of document.querySelectorAll("[data-filter]")) {
      if (b.getAttribute("data-filter") === "input") {
        b.textContent = scopedCounts.total > 0 ? ("🟡 Needs Input (" + scopedCounts.total + ")") : "🟡 Needs Input";
      }
    }

    const modeBadge = document.getElementById("mode-badge");
    const btnUpdate = document.getElementById("btn-update");
    const linkExport = document.getElementById("link-export");
    const autoSelect = document.getElementById("select-autorefresh");
    if (STATIC_EXPORT || window.location.protocol === "file:") {
      modeBadge.textContent = "Static HTML Snapshot · Offline";
      btnUpdate.disabled = true;
      btnUpdate.title = "Run scripts/fleet-dashboard.sh to start localhost Update server";
      autoSelect.disabled = true;
      linkExport.style.display = "none";
    } else if (payload.mode === "snapshot") {
      modeBadge.textContent = isLoopbackPageHost() ? "Synthetic Fixture · 127.0.0.1" : "Synthetic Fixture · Tailscale";
    } else {
      const tsHostCount = (payload.hosts || []).filter(function(h) { return h.reachable && h.route === "tailscale"; }).length;
      if (!isLoopbackPageHost()) {
        modeBadge.textContent = "Read-only · Tailscale";
      } else if (tsHostCount > 0) {
        modeBadge.textContent = "Read-only · 127.0.0.1 + Tailscale (" + tsHostCount + ")";
      } else {
        modeBadge.textContent = "Read-only · 127.0.0.1";
      }
    }

    const stamp = document.getElementById("stamp-line");
    const rel = relAge(payload.generated_at);
    stamp.textContent = "Observed " + formatUtc(payload.generated_at) +
      (rel ? " (" + rel + ")" : "") +
      " · Refresh #" + (payload.refresh_seq || 1) +
      " · " + (payload.duration_ms || 0) + " ms";
  }

  function matchesSpotlightItemSubFilter(item) {
    if (stateFilter !== "input" || inputSubFilter === "all") return true;
    const kind = item.kind === "prompt" ? "keystroke" : (item.kind === "draft" ? "pasted_prompt" : item.kind);
    if (inputSubFilter === "keystroke") return kind === "keystroke";
    if (inputSubFilter === "agy_work") return kind === "keystroke" && Boolean(item.agy_work_fixable);
    if (inputSubFilter === "agent_message") return kind === "agent_message";
    if (inputSubFilter === "pasted_prompt") return kind === "pasted_prompt";
    return true;
  }

  function renderHumanInputBanner() {
    const box = document.getElementById("human-input-banner");
    if (!box) return;
    const s = payload.stats || {};
    const allItems = Array.isArray(s.human_input) ? s.human_input : [];
    const items = macFilter === "all"
      ? allItems
      : allItems.filter(function(item) { return item.host_key === macFilter; });
    const counts = humanInputCounts(macFilter);
    const fleetCounts = macFilter === "all" ? counts : humanInputCounts("all");

    if (!items.length && counts.total === 0) {
      box.className = "human-input-box is-clear";
      if (macFilter !== "all") {
        const hostName = counts.hostLabel || macFilter;
        const otherNote = fleetCounts.total > 0
          ? (" (" + fleetCounts.total + " waiting on other Macs)")
          : "";
        box.replaceChildren(
          el("span", null, [
            el("b", {text: "🟢 0 sessions waiting on human input on " + hostName}),
            el("span", {text: " — no keystroke prompts, agent messages, or pasted prompts on this Mac" + otherNote})
          ]),
          el("button", {
            type: "button",
            className: "btn human-input-filter-btn",
            text: "Show All Macs",
            onClick: function() {
              macFilter = "all";
              renderMacFilterChips();
              updateHeader();
              renderHumanInputBanner();
              renderMain();
            }
          })
        );
        return;
      }
      const liveTotal = typeof s.live_sessions === "number" ? s.live_sessions : 0;
      box.replaceChildren(
        el("span", null, [
          el("b", {text: "🟢 0 sessions waiting on human input"}),
          el("span", {text: " — no keystroke prompts, agent messages, or pasted prompts sitting idle across " + liveTotal + (liveTotal === 1 ? " live session" : " live sessions")})
        ]),
        stateFilter === "input" ? el("button", {
          type: "button",
          className: "btn human-input-filter-btn",
          text: "Show all sessions",
          onClick: function() { setStateFilter("all"); }
        }) : null
      );
      return;
    }

    const messagesOnly = counts.keystrokes === 0 && counts.messages > 0;
    const draftsOnly = counts.keystrokes === 0 && counts.messages === 0 && counts.pasted > 0;
    box.className = "human-input-box" +
      (messagesOnly ? " has-messages-only" : (draftsOnly ? " has-drafts-only" : ""));

    const breakdownParts = [];
    if (counts.hostLabel) {
      breakdownParts.push("on " + counts.hostLabel);
    }
    if (counts.keystrokes > 0) {
      let ksPart = counts.keystrokes + (counts.keystrokes === 1 ? " keystroke prompt" : " keystroke prompts");
      ksPart += " (" + counts.agyWork + "/" + counts.keystrokes + " fixable by agy-work)";
      breakdownParts.push(ksPart);
    }
    if (counts.messages > 0) {
      breakdownParts.push(counts.messages + (counts.messages === 1 ? " agent message awaiting reply" : " agent messages awaiting reply"));
    }
    if (counts.pasted > 0) {
      breakdownParts.push(counts.pasted + (counts.pasted === 1 ? " pasted/unsent prompt not running" : " pasted/unsent prompts not running"));
    }
    if (counts.total > items.length) breakdownParts.push("showing top " + items.length + " of " + counts.total);
    const subText = breakdownParts.join(" · ") + " — click a card to jump to session (key: i)";

    const badgeCls = "human-input-badge" + (messagesOnly ? " is-message-only" : (draftsOnly ? " is-draft-only" : ""));
    const badgeTxt = (messagesOnly
      ? "🟣 Agent Messages Need Reply · "
      : (draftsOnly ? "🟠 Pasted Prompts Not Running · " : "🟡 Needs Human Input · ")) + counts.total;

    const filterActive = stateFilter === "input";
    const head = el("div", {className: "human-input-head"}, [
      el("div", {className: "human-input-title-wrap"}, [
        el("span", {className: badgeCls, text: badgeTxt}),
        el("span", {className: "human-input-sub", text: subText})
      ]),
      el("button", {
        type: "button",
        className: "btn human-input-filter-btn" + (filterActive ? " btn-primary" : ""),
        "aria-pressed": String(filterActive),
        text: filterActive ? "Showing Needs Input ✓ (Show All)" : ("Filter tables to Needs Input (" + counts.total + ")"),
        onClick: function() { setStateFilter(filterActive ? "all" : "input"); }
      })
    ]);

    const catDefs = [
      {sub: "all", label: "All (" + counts.total + ")", cls: "", title: "Show all human-blocked sessions"},
      {sub: "keystroke", label: "🟡 ⌨ Keystrokes / Questions (" + counts.keystrokes + ")", cls: "", title: "Interactive TUI approvals, menus, and questions requiring specific keystrokes"},
      {sub: "agy_work", label: "⚡ Fixable by agy-work (" + counts.agyWork + "/" + counts.keystrokes + ")", cls: " is-agy-chip", title: "Subset of AGY command approvals running without --dangerously-skip-permissions (relaunch with agy-work -c)"},
      {sub: "agent_message", label: "🟣 💬 Agent Messages (" + counts.messages + ")", cls: "", title: "Coding agent finished turn with a plain-text question or confirmation request at the end of its response"},
      {sub: "pasted_prompt", label: "🟠 📋 Pasted Not Running (" + counts.pasted + ")", cls: "", title: "Pasted or typed prompt sitting in the composer without executing (press Enter to run)"}
    ];
    const catsBar = el("div", {className: "human-input-cats", role: "group", "aria-label": "Human blocker categories"});
    for (const cd of catDefs) {
      const activeSub = filterActive && inputSubFilter === cd.sub;
      catsBar.appendChild(el("button", {
        type: "button",
        className: "hic-cat-chip" + cd.cls,
        "data-input-sub": cd.sub,
        "aria-pressed": String(activeSub),
        title: cd.title,
        text: cd.label,
        onClick: function() { setInputSubFilter(cd.sub); }
      }));
    }

    const visibleItems = items.filter(matchesSpotlightItemSubFilter);
    const grid = el("div", {className: "human-input-grid"});
    if (!visibleItems.length) {
      grid.appendChild(el("div", {
        className: "human-input-sub",
        text: "0 sessions in the selected sub-category — click another category pill above to view all " + counts.total + "."
      }));
    }
    for (const item of visibleItems) {
      const normKind = item.kind === "prompt" ? "keystroke" : (item.kind === "draft" ? "pasted_prompt" : item.kind);
      const isPasted = normKind === "pasted_prompt";
      const isMessage = normKind === "agent_message";
      const actionPrefix = isPasted
        ? (item.is_pasted_prompt ? "🟠 📋 " : "🟠 ✎ ")
        : (isMessage ? "🟣 💬 " : "🟡 ⌨ ");
      const waitParts = [];
      if (item.timer_text) waitParts.push(item.timer_text);
      if (item.delta && item.delta.short && item.delta.short !== "first" && item.delta.short !== "Δ?") {
        waitParts.push(item.delta.short);
      } else if (item.up_text && item.up_text !== "?") {
        waitParts.push("up " + item.up_text);
      }
      const canFocusItem = isRowM1Focusable(item);
      const locBase = item.tmux_session
        ? (item.target_short + " · " + item.tmux_session)
        : item.target_short;
      const locText = canFocusItem ? (locBase + " ↗") : locBase;

      let actStr;
      let hasAct = false;
      if (isPasted) {
        if (item.composer_text) {
          actStr = "❯ " + item.composer_text;
          hasAct = true;
        } else if (item.activity) {
          actStr = "~" + item.activity;
          hasAct = true;
        } else {
          actStr = "Prompt sitting in composer — NOT running (press Enter)";
        }
      } else if (isMessage) {
        const msg = item.agent_message || item.activity;
        actStr = msg ? ("💬 " + msg) : "Plain-text question at end of agent response";
        hasAct = Boolean(msg);
      } else {
        actStr = item.activity ? ("~" + item.activity) : "Interactive prompt open — inspect terminal to respond";
        hasAct = Boolean(item.activity);
      }

      const projKids = [
        el("span", {className: "mech-dot", style: "background:" + mechColorVar(item.color_key)}),
        el("strong", {text: item.project_label})
      ];
      if (item.stream_label) {
        projKids.push(el("span", {className: "stream-tag", text: "· " + item.stream_label}));
      }
      projKids.push(el("span", {className: "hic-role", text: item.role}));

      const topLeftKids = [
        el("span", {className: "hic-action", text: actionPrefix + (item.action_label || item.status)})
      ];
      if (item.agy_work_fixable) {
        topLeftKids.push(el("span", {
          className: "hic-agy-badge",
          title: item.agy_work_hint || "Relaunch with agy-work -c (--dangerously-skip-permissions) to bypass routine approvals",
          text: "⚡ Fixable via agy-work"
        }));
      } else if (item.agy_work_status === "already_enabled") {
        topLeftKids.push(el("span", {
          className: "hic-agy-active-badge",
          title: item.agy_work_hint || "Already running with --dangerously-skip-permissions; stopped on a standing Ask rule",
          text: "🛡 agy-work on (Ask gate)"
        }));
      }

      const ksHint = item.keystroke_hint || (isPasted
        ? "Press Enter to run · clear to discard"
        : (isMessage ? "Type reply + Enter" : "Inspect terminal to respond"));
      const keyRowKids = [
        el("span", {className: "hic-keys", text: "⌨ " + ksHint})
      ];
      if (item.agy_work_fixable) {
        keyRowKids.push(el("span", {className: "hic-agy-cmd", text: "agy-work -c"}));
      }

      let cardCls = "human-input-card";
      if (canFocusItem) cardCls += " is-m1-focusable";
      if (isPasted) cardCls += " is-pasted is-draft";
      else if (isMessage) cardCls += " is-agent-message";
      else cardCls += " is-keystroke is-prompt" + (item.agy_work_fixable ? " is-agy-work-fixable" : "");

      const cardTitle = canFocusItem
        ? ("Switch to M1 Terminal (" + item.target_full + ") · " + item.project_label)
        : ("Jump to " + item.project_label + " (" + item.host_label + " · " + item.target_full + ")");

      grid.appendChild(el("button", {
        type: "button",
        className: cardCls,
        title: cardTitle,
        onClick: function() { jumpToSessionRow(item.row_id); }
      }, [
        el("div", {className: "hic-top"}, [
          el("div", {className: "hic-top-left"}, topLeftKids),
          el("span", {className: "hic-wait", text: waitParts.join(" · ")})
        ]),
        el("div", {className: "hic-where"}, [
          el("div", {className: "hic-proj"}, projKids),
          el("div", {className: "hic-badges"}, [
            renderSymPill(item.mac, false),
            el("span", {className: "hic-loc" + (canFocusItem ? " is-terminal-link" : ""), text: locText}),
            renderSymPill(item.view, true),
            renderSymPill(item.cli, false)
          ])
        ]),
        el("div", {className: "hic-key-row"}, keyRowKids),
        el("div", {className: "hic-act" + (hasAct ? " has-act" : ""), text: actStr})
      ]));
    }
    box.replaceChildren(head, catsBar, grid);
  }

  function renderHostCards() {
    const box = document.getElementById("host-cards");
    box.replaceChildren();
    for (const h of payload.hosts || []) {
      const statusTone = !h.reachable ? "red" : (h.source === "snapshot" ? "blue" : "green");
      const statusLabel = !h.reachable ? "🔴 UNREACHABLE" : (h.source === "snapshot" ? "🔵 SNAPSHOT" : "🟢 LIVE");
      const statusPill = el("span", {className: "sym-pill tone-" + statusTone, text: statusLabel});
      let routePill = null;
      if (h.reachable && h.route === "tailscale") {
        routePill = el("span", {
          className: "sym-pill tone-teal",
          title: "Connected remotely over Tailscale SSH",
          text: "🔗 Tailscale"
        });
      } else if (!h.reachable && Array.isArray(h.errors) && h.errors.includes("tailscale_peer_offline")) {
        routePill = el("span", {
          className: "sym-pill tone-slate",
          title: "Tailscale reports this peer is currently offline",
          text: "◌ Tailscale offline"
        });
      }
      const hostHumanTotal = typeof h.human_total_count === "number" ? h.human_total_count : 0;
      const hostKeystrokes = typeof h.human_keystroke_count === "number"
        ? h.human_keystroke_count
        : (typeof h.human_prompt_count === "number" ? h.human_prompt_count : 0);
      const hostAgyWork = typeof h.human_agy_work_count === "number" ? h.human_agy_work_count : 0;
      const hostMessages = typeof h.human_message_count === "number" ? h.human_message_count : 0;
      const hostPasted = typeof h.human_pasted_count === "number"
        ? h.human_pasted_count
        : (typeof h.human_draft_count === "number" ? h.human_draft_count : 0);
      let hostInputBtn = null;
      if (h.reachable && hostHumanTotal > 0) {
        let pillTone = "orange";
        let pillText = "🟠 " + hostHumanTotal + " pasted";
        if (hostKeystrokes > 0) {
          pillTone = "yellow";
          pillText = "🟡 " + hostHumanTotal + " input" + (hostAgyWork > 0 ? (" · " + hostAgyWork + "⚡") : "");
        } else if (hostMessages > 0) {
          pillTone = "purple";
          pillText = "🟣 " + hostHumanTotal + " reply";
        }
        const tipParts = [];
        if (hostKeystrokes > 0) tipParts.push(hostKeystrokes + " keystroke prompt(s)" + (hostAgyWork > 0 ? " (" + hostAgyWork + " fixable by agy-work)" : ""));
        if (hostMessages > 0) tipParts.push(hostMessages + " agent message(s)");
        if (hostPasted > 0) tipParts.push(hostPasted + " pasted/unsent prompt(s) not running");
        hostInputBtn = el("button", {
          type: "button",
          className: "sym-pill tone-" + pillTone + " host-input-pill",
          title: "Filter tables to " + h.label + " (" + tipParts.join(" · ") + ")",
          text: pillText,
          onClick: function() {
            if (macFilter === h.key && stateFilter === "input") {
              macFilter = "all";
              renderMacFilterChips();
              updateHeader();
              setStateFilter("all");
            } else {
              macFilter = h.key;
              renderMacFilterChips();
              updateHeader();
              setStateFilter("input");
            }
          }
        });
      }

      let countText = "UNREACHABLE";
      if (h.reachable) {
        if (h.inventory_state === "complete") countText = h.session_count + " sessions";
        else if (h.inventory_state === "partial") countText = h.session_count + " observed (partial)";
        else countText = h.session_count + " observed (inv ?)";
      } else if (h.last_reachable) {
        countText = h.last_reachable.cached_count + " cached (" + h.last_reachable.age_text + " ago)";
      }

      const dotsWrap = el("div", {className: "host-dots"});
      for (const item of h.dot_summary || []) {
        dotsWrap.appendChild(el("span", {text: item.dot + item.count}));
      }

      const rc = h.role_counts || {};
      const rolesLine = el("div", {className: "host-roles-line"}, [
        el("span", {text: "MechBay: " + (rc.mechbay || 0)}),
        el("span", {text: "Claw: " + (rc.claw || 0)}),
        el("span", {text: "Mechs: " + (rc.mech || 0)}),
        el("span", {text: "Other: " + (rc.other || 0)})
      ]);

      const hl = h.health || {};
      const tcpText = (hl.occupancy_pct !== null && hl.occupancy_pct !== undefined ? hl.occupancy_pct + "% " : "? ") + (hl.status || "unknown");
      const sys = h.system || {};
      const diskText = (sys.disk_free_gib !== null && sys.disk_free_gib !== undefined) ? sys.disk_free_gib + " GiB" : "—";
      const hasRawLoad = typeof sys.loadavg_1m === "number" && Number.isFinite(sys.loadavg_1m) && sys.loadavg_1m >= 0;
      const hasLoad = Boolean(h.reachable) && hasRawLoad;
      const cpus = (typeof sys.cpu_count === "number" && Number.isInteger(sys.cpu_count) && sys.cpu_count > 0) ? sys.cpu_count : null;
      const loadText = hasLoad
        ? (sys.loadavg_1m + (cpus ? "/" + cpus + "c" : "") + (sys.arch ? " · " + sys.arch : ""))
        : (sys.arch || "—");
      let loadBarNode = null;
      let loadTitle = !h.reachable
        ? ("Host unreachable" + (hasRawLoad ? (" (cached 1m load was " + sys.loadavg_1m + (h.last_reachable && h.last_reachable.age_text ? ", " + h.last_reachable.age_text + " ago" : "") + ")") : ""))
        : "1m load average unavailable";
      if (hasLoad) {
        const denom = cpus || 10;
        const ratio = sys.loadavg_1m / denom;
        const pct = sys.loadavg_1m === 0 ? 0 : Math.min(100, Math.max(5, Math.round(ratio * 100)));
        const loadTone = ratio < 0.60 ? "load-green" : (ratio < 0.85 ? "load-yellow" : (ratio < 1.10 ? "load-orange" : "load-red"));
        loadTitle = "1m load average: " + sys.loadavg_1m +
          (cpus ? (" across " + cpus + " logical cores (" + Math.round(ratio * 100) + "%)") : " (scaled against 10-core reference; core count not reported)") +
          (sys.arch ? " · " + sys.arch : "");
        loadBarNode = el("div", {
          className: "load-bar-track",
          role: "meter",
          "aria-label": loadTitle,
          "aria-valuemin": "0",
          "aria-valuemax": String(denom),
          "aria-valuenow": String(sys.loadavg_1m)
        }, [
          el("div", {className: "load-bar-fill " + loadTone, style: "width:" + pct + "%"})
        ]);
      }

      const metrics = el("div", {className: "host-metrics"}, [
        el("div", {className: "metric-cell"}, [el("span", {className: "k", text: "TCP Ports"}), el("span", {className: "v", text: tcpText, title: "Proc maint: " + (hl.process_status || "unknown") + " · age " + (hl.age_text || "?")})]),
        el("div", {className: "metric-cell"}, [el("span", {className: "k", text: "Screens"}), el("span", {className: "v", text: h.screens_text || "—"})]),
        el("div", {className: "metric-cell"}, [el("span", {className: "k", text: "Disk Free"}), el("span", {className: "v", text: diskText})]),
        el("div", {className: "metric-cell", title: loadTitle}, [el("span", {className: "k", text: "Load 1m"}), el("span", {className: "v", text: loadText}), loadBarNode])
      ]);

      let cardCls = "host-card";
      if (!h.reachable) cardCls += " is-unreachable";
      else if (hostKeystrokes > 0) cardCls += " has-human-input";
      else if (hostMessages > 0) cardCls += " has-agent-message";
      else if (hostPasted > 0) cardCls += " has-drafts-only";
      const card = el("article", {className: cardCls}, [
        el("div", {className: "host-head"}, [
          el("div", {className: "host-title"}, [renderSymPill(h.mac, false), el("span", {text: h.label})]),
          el("div", {className: "host-head-pills"}, [hostInputBtn, routePill, statusPill])
        ]),
        el("div", {className: "host-main-row"}, [el("div", {className: "host-count", text: countText}), dotsWrap]),
        rolesLine,
        metrics
      ]);
      box.appendChild(card);
    }
  }

  function renderRollupAndAttention() {
    const s = payload.stats || {};
    const tc = s.tone_counts || {};
    const counts = humanInputCounts();
    const rollup = document.getElementById("fleet-rollup");
    const inputTileCls = "rollup-tile is-clickable" +
      (counts.keystrokes > 0
        ? " has-human-input"
        : (counts.messages > 0 ? " has-agent-message" : (counts.pasted > 0 ? " has-drafts-only" : "")));
    const inputTileLbl = counts.keystrokes > 0
      ? "🟡 Needs Human Input"
      : (counts.messages > 0 ? "🟣 Agent Replies" : (counts.pasted > 0 ? "🟠 Pasted Not Running" : "🟡 Needs Human Input"));
    const inputTileTitle = counts.keystrokes + " keystroke prompt(s) (" + counts.agyWork + " agy-work fixable) · " +
      counts.messages + " agent message(s) · " + counts.pasted + " pasted/unsent prompt(s) — click to toggle Needs Input filter";
    rollup.replaceChildren(
      el("button", {
        type: "button",
        className: inputTileCls,
        title: inputTileTitle,
        onClick: function() { setStateFilter(stateFilter === "input" ? "all" : "input"); }
      }, [el("span", {className: "lbl", text: inputTileLbl}), el("span", {className: "val", text: counts.total})]),
      el("div", {className: "rollup-tile"}, [el("span", {className: "lbl", text: "🟢 Active"}), el("span", {className: "val", text: tc.active || 0})]),
      el("div", {className: "rollup-tile"}, [el("span", {className: "lbl", text: "🔴 Alert / 🟠 Pause"}), el("span", {className: "val", text: (tc.alert || 0) + " / " + (tc.paused || 0)})]),
      el("div", {className: "rollup-tile"}, [el("span", {className: "lbl", text: "🟢 ◆ Changed"}), el("span", {className: "val", text: s.changed_now || 0})]),
      el("div", {className: "rollup-tile"}, [el("span", {className: "lbl", text: "Mechs Observed"}), el("span", {className: "val", text: (s.mechs_present_count || 0) + "/" + (s.mechs_total || 18)})]),
      el("div", {className: "rollup-tile"}, [el("span", {className: "lbl", text: "🔵 Done · ⚪ Idle"}), el("span", {className: "val", text: (tc.done || 0) + " · " + (tc.idle || 0)})])
    );

    const toggleWrap = document.getElementById("cached-toggle-wrap");
    toggleWrap.style.display = (s.cached_sessions || 0) > 0 ? "inline-flex" : "none";

    const banner = document.getElementById("attention-banner");
    const hasDedicatedInputBanner = Boolean(document.getElementById("human-input-banner"));
    const rawAttention = s.attention || [];
    const items = hasDedicatedInputBanner
      ? rawAttention.filter(item => item.kind !== "human-input")
      : rawAttention;
    if (!items.length) {
      banner.style.display = "none";
      banner.replaceChildren();
      return;
    }
    banner.style.display = "block";
    const list = el("div", {className: "attention-list"});
    for (const item of items) {
      list.appendChild(el("button", {
        type: "button",
        className: "attention-chip",
        onClick: function() {
          if (item.row_id) {
            jumpToSessionRow(item.row_id);
          }
        }
      }, [
        renderSymPill(item.mac, false),
        el("strong", {text: item.title + ":"}),
        el("span", {text: item.detail})
      ]));
    }
    banner.replaceChildren(
      el("div", {className: "attention-head", text: (hasDedicatedInputBanner ? "System & Session Alerts (" : "Attention (") + items.length + ")"}),
      list
    );
  }

  function renderMacFilterChips() {
    const wrap = document.getElementById("mac-filters");
    wrap.replaceChildren(el("span", {className: "tool-label", text: "Mac"}));
    const allBtn = el("button", {
      type: "button",
      className: "chip",
      "aria-pressed": String(macFilter === "all"),
      text: "All Macs",
      onClick: function() { macFilter = "all"; renderMacFilterChips(); updateHeader(); renderHumanInputBanner(); renderMain(); }
    });
    wrap.appendChild(allBtn);
    for (const h of payload.hosts || []) {
      const label = (h.mac ? h.mac.dot + " " + h.mac.glyph : h.key);
      wrap.appendChild(el("button", {
        type: "button",
        className: "chip",
        "aria-pressed": String(macFilter === h.key),
        text: label,
        onClick: function() { macFilter = h.key; renderMacFilterChips(); updateHeader(); renderHumanInputBanner(); renderMain(); }
      }));
    }
  }

  function normalizedRowHumanKind(row) {
    if (row.stale_cached) return null;
    const k = row.human_block_kind;
    if (k === "keystroke" || k === "prompt") return "keystroke";
    if (k === "agent_message") return "agent_message";
    if (k === "pasted_prompt" || k === "draft") return "pasted_prompt";
    if (row.symbols.state.tone === "yellow") return "keystroke";
    if (row.symbols.guard.short === "reply") return "agent_message";
    if (row.symbols.guard.short === "draft" || row.symbols.guard.short === "pasted") return "pasted_prompt";
    return null;
  }

  function isRowHumanBlocked(row) {
    return normalizedRowHumanKind(row) !== null;
  }

  function matchesRowInputSubFilter(row) {
    const k = normalizedRowHumanKind(row);
    if (!k) return false;
    if (inputSubFilter === "all") return true;
    if (inputSubFilter === "keystroke") return k === "keystroke";
    if (inputSubFilter === "agy_work") return k === "keystroke" && Boolean(row.agy_work_fixable || row.symbols.guard.short === "agy-wk");
    if (inputSubFilter === "agent_message") return k === "agent_message";
    if (inputSubFilter === "pasted_prompt") return k === "pasted_prompt";
    return true;
  }

  function matchesFilters(row) {
    if (row.stale_cached && !includeCached) return false;
    if (macFilter !== "all" && row.host_key !== macFilter) return false;
    const stTone = row.symbols.state.tone;
    const rowHumanBlocked = isRowHumanBlocked(row);
    if (stateFilter === "input" && !matchesRowInputSubFilter(row)) return false;
    if (stateFilter === "active" && stTone !== "green") return false;
    if (stateFilter === "alert" && stTone !== "red" && stTone !== "orange" && row.symbols.ports.tone !== "red") return false;
    if (stateFilter === "changed" && row.symbols.delta.short !== "Δnow") return false;
    if (stateFilter === "idle" && (rowHumanBlocked || (stTone !== "white" && stTone !== "blue"))) return false;
    if (stateFilter === "unknown" && (rowHumanBlocked || (stTone !== "slate" && row.symbols.guard.short !== "blind"))) return false;
    if (searchQuery) {
      const hay = [
        row.project_label, row.stream_label || "", row.repo_label,
        row.target_short, row.agent, row.model_compact || "",
        row.status, row.human_action_label || "", row.keystroke_hint || "",
        row.agy_work_fixable ? "agy-work fixable" : "", row.agy_work_hint || "",
        row.composer_text || "", row.agent_message || "",
        row.activity || "", row.host_label
      ].join(" ").toLowerCase();
      if (!hay.includes(searchQuery)) return false;
    }
    return true;
  }

  function defaultUrgencyTier(row) {
    const k = normalizedRowHumanKind(row);
    if (k === "keystroke") return 0;
    if (k === "agent_message") return 1;
    if (k === "pasted_prompt") return 2;
    if (row.symbols.state.rank <= 25) return 3;
    return 4;
  }

  function sortSessions(rows) {
    const copy = rows.slice();
    const dir = sortAsc ? 1 : -1;
    copy.sort(function(a, b) {
      if (sortCol === "project") {
        return dir * (a.project_label.localeCompare(b.project_label) || (a.stream_label || "").localeCompare(b.stream_label || ""));
      }
      if (sortCol === "mac") return dir * ((a.symbols.mac.rank - b.symbols.mac.rank) || a.project_label.localeCompare(b.project_label));
      if (sortCol === "cli") return dir * ((a.symbols.cli.rank - b.symbols.cli.rank) || a.project_label.localeCompare(b.project_label));
      if (sortCol === "state") return dir * ((a.symbols.state.rank - b.symbols.state.rank) || a.project_label.localeCompare(b.project_label));
      if (sortCol === "guard") return dir * ((a.symbols.guard.rank - b.symbols.guard.rank) || a.project_label.localeCompare(b.project_label));
      if (sortCol === "delta") return dir * ((a.symbols.delta.rank - b.symbols.delta.rank) || a.project_label.localeCompare(b.project_label));
      if (sortCol === "ctx") return dir * ((a.ctx_sort - b.ctx_sort) || a.project_label.localeCompare(b.project_label));
      if (sortCol === "ports") return dir * (((b.port_count) - (a.port_count)) || a.project_label.localeCompare(b.project_label));
      if (sortCol === "view") return dir * ((a.symbols.view.rank - b.symbols.view.rank) || a.project_label.localeCompare(b.project_label));
      if (sortCol === "up") return dir * (((b.up_seconds) - (a.up_seconds)) || a.project_label.localeCompare(b.project_label));
      // Default: human prompts first, unsent drafts second, critical alerts third, then project/Mac/target
      const urgA = defaultUrgencyTier(a);
      const urgB = defaultUrgencyTier(b);
      if (urgA !== urgB) return urgA - urgB;
      return (
        a.project_label.localeCompare(b.project_label) ||
        (a.stream_label || "").localeCompare(b.stream_label || "") ||
        (a.symbols.mac.rank - b.symbols.mac.rank) ||
        a.target_short.localeCompare(b.target_short)
      );
    });
    return copy;
  }

  function buildSymbolTable(rows, emptyMessage) {
    const cols = [
      ["col-session", "project", "Session · Target", false],
      ["col-mac",     "mac",     "Mac",     true],
      ["col-cli",     "cli",     "CLI",     true],
      ["col-state",   "state",   "State",   true],
      ["col-guard",   "guard",   "Guard",   true],
      ["col-delta",   "delta",   "Δ",       true],
      ["col-ctx",     "ctx",     "Ctx",     true],
      ["col-ports",   "ports",   "Ports",   true],
      ["col-view",    "view",    "View",    true],
      ["col-up",      "up",      "Up · Timer", false],
      ["col-act",     "default", "~Activity",  false]
    ];

    const colgroup = el("colgroup", null, cols.map(c => el("col", {className: c[0]})));
    const headRow = el("tr");
    for (const [, key, label, isSym] of cols) {
      const indicator = sortCol === key ? (sortAsc ? " ▲" : " ▼") : "";
      headRow.appendChild(el("th", {
        className: isSym ? "sym-col" : "",
        text: label + indicator,
        onClick: function() {
          if (sortCol === key) sortAsc = !sortAsc;
          else { sortCol = key; sortAsc = true; }
          renderMain();
        }
      }));
    }

    const tbody = el("tbody");
    const sorted = sortSessions(rows);
    if (!sorted.length) {
      tbody.appendChild(el("tr", {className: "empty-row"}, [
        el("td", {colspan: "11", text: emptyMessage || "No matching sessions in this table."})
      ]));
    } else {
      for (const row of sorted) {
        const isOpen = expandedRows.has(row.row_id);
        const canFocusM1 = isRowM1Focusable(row);
        const toggleRow = function() {
          if (expandedRows.has(row.row_id)) expandedRows.delete(row.row_id);
          else expandedRows.add(row.row_id);
          flashRowId = null;
          renderMain();
        };

        const nameKids = [el("span", {className: "proj-name", text: row.project_label})];
        if (row.stream_label) {
          nameKids.push(el("span", {className: "stream-tag", text: "· " + row.stream_label}));
        }
        const sessTitle = canFocusM1
          ? ("Click row to switch to M1 Terminal (" + row.target_full + ") · click ▸ for details")
          : (row.project_label + (row.stream_label ? " · " + row.stream_label : "") + " (" + row.target_full + ")");
        const sessCellKids = [
          el("span", {className: "mech-dot", style: "background:" + mechColorVar(row.color_key)}),
          el("div", {className: "sess-names"}, nameKids),
          el("span", {
            className: "target-tag" + (canFocusM1 ? " is-terminal-link" : ""),
            text: canFocusM1 ? (row.target_short + " ↗") : row.target_short
          })
        ];
        if (canFocusM1) {
          sessCellKids.push(el("button", {
            id: "expand-" + row.row_id,
            type: "button",
            className: "row-expand-btn",
            "aria-label": (isOpen ? "Collapse" : "Expand") + " details for " + row.project_label,
            "aria-expanded": String(isOpen),
            title: (isOpen ? "Hide" : "Show") + " session details without switching apps (Space)",
            text: isOpen ? "▾" : "▸",
            onClick: function(ev) {
              if (ev) {
                ev._detailToggleHandled = true;
                if (typeof ev.stopPropagation === "function") ev.stopPropagation();
              }
              toggleRow();
            }
          }));
        }
        const sessCell = el("div", {className: "sess-cell", title: sessTitle}, sessCellKids);

        const upKids = [el("span", {text: row.up_text})];
        if (row.timer_text) upKids.push(el("span", {className: "tmr", text: " · " + row.timer_text}));
        if (row.stale_cached && row.cached_age_text) {
          upKids.push(el("span", {className: "tmr", text: " (cached " + row.cached_age_text + ")"}));
        }

        const rowHumanKind = normalizedRowHumanKind(row);
        let actText = row.activity ? ("~" + row.activity) : "—";
        let hasAct = Boolean(row.activity);
        if (rowHumanKind === "pasted_prompt" && row.composer_text) {
          actText = "❯ " + row.composer_text;
          hasAct = true;
        } else if (rowHumanKind === "agent_message" && row.agent_message) {
          actText = "💬 " + row.agent_message;
          hasAct = true;
        }
        let rowCls = "session-row";
        if (canFocusM1) rowCls += " is-m1-focusable";
        if (row.stale_cached) rowCls += " is-stale-cached";
        else if (rowHumanKind === "keystroke") rowCls += " is-human-prompt";
        else if (rowHumanKind === "agent_message") rowCls += " is-human-message";
        else if (rowHumanKind === "pasted_prompt") rowCls += " is-human-draft";
        if (flashRowId === row.row_id) rowCls += " is-flash-target";

        const tr = el("tr", {
          id: "row-" + row.row_id,
          className: rowCls,
          tabindex: "0",
          "aria-expanded": String(isOpen),
          title: canFocusM1 ? sessTitle : null,
          onClick: function(ev) {
            if (ev && ev._detailToggleHandled) return;
            if (ev && ev.target && typeof ev.target.closest === "function" && ev.target.closest(".row-expand-btn")) return;
            if (typeof window.getSelection === "function") {
              const sel = window.getSelection();
              if (
                sel &&
                !sel.isCollapsed &&
                String(sel).length > 0 &&
                ((sel.anchorNode && typeof tr.contains === "function" && tr.contains(sel.anchorNode)) ||
                 (sel.focusNode && typeof tr.contains === "function" && tr.contains(sel.focusNode)))
              ) {
                return;
              }
            }
            if (canFocusM1) {
              focusTerminalSession(row);
            } else {
              toggleRow();
            }
          },
          onKeydown: function(ev) {
            if (ev && ev.target && ev.target.tagName === "BUTTON") return;
            if (canFocusM1 && ev.key === "Enter") {
              ev.preventDefault();
              focusTerminalSession(row);
            } else if (ev.key === "Enter" || ev.key === " ") {
              ev.preventDefault();
              toggleRow();
            }
          }
        }, [
          el("td", null, sessCell),
          el("td", {className: "sym-cell"}, renderSymPill(row.symbols.mac, false)),
          el("td", {className: "sym-cell"}, renderSymPill(row.symbols.cli, false)),
          el("td", {className: "sym-cell"}, renderSymPill(row.symbols.state, true)),
          el("td", {className: "sym-cell"}, renderSymPill(row.symbols.guard, true)),
          el("td", {className: "sym-cell"}, renderSymPill(row.symbols.delta, true)),
          el("td", {className: "sym-cell"}, renderSymPill(row.symbols.ctx, false)),
          el("td", {className: "sym-cell"}, renderSymPill(row.symbols.ports, false)),
          el("td", {className: "sym-cell"}, renderSymPill(row.symbols.view, true)),
          el("td", {className: "up-cell", title: "Process age: " + row.up_text + (row.timer_text ? " · UI timer: " + row.timer_text : "")}, upKids),
          el("td", {className: "act-cell" + (hasAct ? " has-act" : ""), title: actText, text: actText})
        ]);
        tbody.appendChild(tr);

        if (isOpen) {
          const tmuxInfo = row.tmux_sessions && row.tmux_sessions.length
            ? row.tmux_sessions.join(", ") + (row.tmux_window ? " (window: " + row.tmux_window + ")" : "")
            : (row.transport + " · " + row.target_short);
          const idInfo = row.session_id
            ? (row.session_id + " (argv launch ID only — reconfirm current ID before control)")
            : "Not in argv (unverified)";
          const flagInfo = [].concat(row.flags || [], row.errors || []).join(", ") || "none";
          const detailItems = [];
          if (row.human_action_label) {
            const prefix = rowHumanKind === "pasted_prompt" ? "🟠 " : (rowHumanKind === "agent_message" ? "🟣 " : "🟡 ");
            detailItems.push(el("div", {className: "detail-item"}, [
              el("b", {text: "Human Action Needed"}),
              el("span", {text: prefix + row.human_action_label + " (" + row.status + ")"})
            ]));
          }
          if (row.keystroke_hint) {
            detailItems.push(el("div", {className: "detail-item"}, [
              el("b", {text: "Required Keystrokes"}),
              el("span", {className: "mono", text: "⌨ " + row.keystroke_hint})
            ]));
          }
          if (row.agy_work_fixable || row.agy_work_hint) {
            detailItems.push(el("div", {className: "detail-item"}, [
              el("b", {text: "agy-work Permission Status"}),
              el("span", {text: (row.agy_work_fixable ? "⚡ Fixable via agy-work — " : "🛡 ") + (row.agy_work_hint || "Relaunch with agy-work -c")})
            ]));
          }
          if (row.composer_text) {
            detailItems.push(el("div", {className: "detail-item"}, [
              el("b", {text: "Unsent Composer Prompt (Not Running)"}),
              el("span", {className: "mono", text: "❯ " + row.composer_text})
            ]));
          }
          if (row.agent_message) {
            detailItems.push(el("div", {className: "detail-item"}, [
              el("b", {text: "Agent End-of-Response Message"}),
              el("span", {text: "💬 " + row.agent_message})
            ]));
          }
          const rawActText = row.activity ? ("~" + row.activity) : "—";
          if (canFocusM1) {
            detailItems.push(el("div", {className: "detail-item"}, [
              el("b", {text: "M1 Terminal Navigation"}),
              el("button", {
                type: "button",
                className: "btn btn-primary detail-focus-btn",
                text: "↗ Switch to Terminal (" + row.target_short + ")",
                onClick: function() { focusTerminalSession(row); }
              })
            ]));
          }
          detailItems.push(
            el("div", {className: "detail-item"}, [el("b", {text: "Host & Target"}), el("span", {text: row.host_label + " · " + tmuxInfo + " · PID " + (row.pid || "?") + " · " + (row.tty || "?")})]),
            el("div", {className: "detail-item"}, [el("b", {text: "CLI & Model"}), el("span", {text: row.agent + " / " + (row.model || row.model_compact || "model not on screen")})]),
            el("div", {className: "detail-item"}, [el("b", {text: "Screen-Reported Activity (~)"}), el("span", {text: rawActText})]),
            el("div", {className: "detail-item"}, [el("b", {text: "Launch ID Evidence"}), el("span", {className: "mono", text: idInfo})]),
            el("div", {className: "detail-item"}, [el("b", {text: "Flags & Collection Codes"}), el("span", {className: "mono", text: flagInfo})])
          );
          tbody.appendChild(el("tr", {className: "detail-row"}, [
            el("td", {colspan: "11"}, [
              el("div", {className: "detail-grid"}, detailItems)
            ])
          ]));
        }
      }
    }

    return el("div", {className: "table-wrap"}, [
      el("table", {className: "symbol-table"}, [colgroup, el("thead", null, headRow), tbody])
    ]);
  }

  function sectionDotsSummary(rows) {
    const counts = {};
    for (const r of rows) {
      const k = normalizedRowHumanKind(r);
      const dot = k === "pasted_prompt" ? "🟠" : (k === "agent_message" ? "🟣" : r.symbols.state.dot);
      counts[dot] = (counts[dot] || 0) + 1;
    }
    return ["🟢", "🟡", "🟣", "🟠", "🔴", "🔵", "⚪", "⚫"]
      .filter(d => counts[d])
      .map(d => d + counts[d])
      .join("  ");
  }

  function buildSectionCard(title, colorKey, rows, emptyMessage) {
    const dots = sectionDotsSummary(rows);
    return el("section", {className: "table-section"}, [
      el("div", {className: "section-bar"}, [
        el("h2", {className: "section-title"}, [
          el("span", {className: "role-spine", style: "background:" + mechColorVar(colorKey)}),
          el("span", {text: title})
        ]),
        el("div", {className: "section-badges"}, [
          dots ? el("span", {text: dots}) : null,
          el("span", {text: rows.length + (rows.length === 1 ? " session" : " sessions")})
        ])
      ]),
      buildSymbolTable(rows, emptyMessage)
    ]);
  }

  function renderRoleView(container, filtered) {
    const mechbayRows = filtered.filter(r => r.role === "mechbay");
    const clawRows = filtered.filter(r => r.role === "claw");
    const mechRows = filtered.filter(r => r.role === "mech");
    const otherRows = filtered.filter(r => r.role === "other");
    const otherHumanCount = otherRows.filter(isRowHumanBlocked).length;
    const otherHasKeystroke = otherRows.some(r => normalizedRowHumanKind(r) === "keystroke");
    const otherHasMessage = otherRows.some(r => normalizedRowHumanKind(r) === "agent_message");

    container.appendChild(buildSectionCard("1 · MechBay Sessions", "xmechbay", mechbayRows, "No MechBay sessions observed matching current filter."));
    container.appendChild(buildSectionCard("2 · Claw Sessions", "claw", clawRows, "No Claw sessions observed matching current filter."));
    container.appendChild(buildSectionCard("3 · Mech Sessions", "culturemech", mechRows, "No *Mech sessions observed matching current filter."));

    const drawer = el("details", {className: "other-drawer", id: "other-sessions-drawer"});
    const autoOpenForHuman = !otherDrawerUserSet && otherHumanCount > 0;
    if (otherDrawerOpen || autoOpenForHuman || ((stateFilter !== "all" || searchQuery) && otherRows.length > 0)) {
      drawer.setAttribute("open", "");
    }
    drawer.addEventListener("toggle", function() {
      if (stateFilter === "all" && !searchQuery) {
        otherDrawerOpen = Boolean(drawer.open);
        otherDrawerUserSet = true;
      }
    });
    const otherDots = sectionDotsSummary(otherRows);
    const summaryBadges = [];
    if (otherHumanCount > 0) {
      const drTone = otherHasKeystroke ? "yellow" : (otherHasMessage ? "purple" : "orange");
      const drDot = otherHasKeystroke ? "🟡 " : (otherHasMessage ? "🟣 " : "🟠 ");
      summaryBadges.push(el("span", {
        className: "sym-pill tone-" + drTone,
        text: drDot + otherHumanCount + " need input"
      }));
    }
    summaryBadges.push(el("span", {text: otherDots || "collapsed by default"}));
    drawer.appendChild(el("summary", null, [
      el("span", {text: "▸ Other Observed Sessions (" + otherRows.length + ") — companion & non-Mech workspaces"}),
      el("span", {className: "section-badges"}, summaryBadges)
    ]));
    drawer.appendChild(buildSymbolTable(otherRows, "No companion or unmatched sessions observed."));
    container.appendChild(drawer);
  }

  function renderHostView(container, filtered) {
    for (const h of payload.hosts || []) {
      const hostRows = filtered.filter(r => r.host_key === h.key);
      const emptyMsg = !h.reachable && !h.last_reachable
        ? (h.label + " is UNREACHABLE — current session count and state are unknown.")
        : ("No matching sessions on " + h.label + ".");
      const title = (h.mac ? h.mac.dot + " " + h.mac.glyph + " · " : "") + h.label + (!h.reachable ? " (UNREACHABLE)" : "");
      container.appendChild(buildSectionCard(title, "generic-mech", hostRows, emptyMsg));
    }
  }

  function renderMatrixView(container, filtered) {
    const hosts = payload.hosts || [];
    const orderedProjects = [
      {name: "XMechBay", role: "mechbay", color: "xmechbay"},
      {name: "culturebotai-claw", role: "claw", color: "claw"}
    ];
    const seen = new Set(["XMechBay", "culturebotai-claw"]);
    for (const m of payload.canonical_mechs || []) {
      orderedProjects.push({name: m, role: "mech", color: m.toLowerCase()});
      seen.add(m);
    }
    for (const r of filtered) {
      if (r.role !== "other" && !seen.has(r.project_label)) {
        orderedProjects.push({name: r.project_label, role: r.role, color: r.color_key});
        seen.add(r.project_label);
      }
    }

    const headCells = [el("th", {text: "Project"}), el("th", {text: "Role"})];
    for (const h of hosts) {
      headCells.push(el("th", {text: (h.mac ? h.mac.dot + " " + h.mac.glyph : h.key) + " · " + h.label}));
    }
    headCells.push(el("th", {text: "Total"}));

    const tbody = el("tbody");
    for (const proj of orderedProjects) {
      const projRows = filtered.filter(r => r.project_label === proj.name);
      if (!projRows.length && (stateFilter !== "all" || searchQuery)) continue;
      const cells = [
        el("td", null, el("div", {className: "sess-cell"}, [
          el("span", {className: "mech-dot", style: "background:" + mechColorVar(proj.color)}),
          el("strong", {text: proj.name})
        ])),
        el("td", {text: proj.role})
      ];
      for (const h of hosts) {
        const cellRows = projRows.filter(r => r.host_key === h.key);
        if (!cellRows.length) {
          cells.push(el("td", {style: "color:var(--muted)", text: "—"}));
        } else {
          const wrap = el("div", {className: "matrix-pills"});
          for (const r of cellRows) {
            const rk = normalizedRowHumanKind(r);
            const useGuard = rk === "pasted_prompt" || rk === "agent_message" || (rk === "keystroke" && r.agy_work_fixable);
            const sym = useGuard ? r.symbols.guard : r.symbols.state;
            const pill = renderSymPill(sym, true);
            const pillCanFocusM1 = isRowM1Focusable(r);
            const pillTitle = r.project_label + (r.stream_label ? " · " + r.stream_label : "") +
              " (" + r.target_short + "): " + (sym.detail || "") +
              (pillCanFocusM1 ? " · Click to switch to M1 Terminal ↗" : "");
            wrap.appendChild(el("button", {
              type: "button",
              className: "matrix-pill-btn",
              title: pillTitle,
              onClick: function() { jumpToSessionRow(r.row_id); }
            }, pill));
          }
          cells.push(el("td", null, wrap));
        }
      }
      cells.push(el("td", {style: "font-weight:700", text: String(projRows.length)}));
      tbody.appendChild(el("tr", null, cells));
    }

    const otherRows = filtered.filter(r => r.role === "other");
    if (otherRows.length > 0) {
      const otherCells = [
        el("td", null, el("div", {className: "sess-cell"}, [
          el("span", {className: "mech-dot", style: "background:var(--mech-other)"}),
          el("strong", {text: "Other Observed Sessions"})
        ])),
        el("td", {text: "other"})
      ];
      for (const h of hosts) {
        const cellRows = otherRows.filter(r => r.host_key === h.key);
        if (!cellRows.length) {
          otherCells.push(el("td", {style: "color:var(--muted)", text: "—"}));
        } else {
          otherCells.push(el("td", {style: "color:var(--muted);font-family:var(--mono);font-size:12px", text: cellRows.length + " (" + sectionDotsSummary(cellRows) + ")"}));
        }
      }
      otherCells.push(el("td", {style: "font-weight:700", text: String(otherRows.length)}));
      tbody.appendChild(el("tr", null, otherCells));
    }

    container.appendChild(el("section", {className: "table-section"}, [
      el("div", {className: "section-bar"}, [
        el("h2", {className: "section-title", text: "Project × Mac Symbol Matrix"}),
        el("div", {className: "section-badges", text: orderedProjects.length + " fleet repositories"})
      ]),
      el("div", {className: "table-wrap"}, [
        el("table", {className: "matrix-table"}, [el("thead", null, el("tr", null, headCells)), tbody])
      ])
    ]));
  }

  function renderMain() {
    const container = document.getElementById("tables-container");
    const prevActive = document.activeElement;
    const prevId = (
      prevActive &&
      prevActive.id &&
      typeof container.contains === "function" &&
      container.contains(prevActive)
    ) ? prevActive.id : null;
    container.replaceChildren();
    const filtered = (payload.sessions || []).filter(matchesFilters);
    if (viewMode === "role") renderRoleView(container, filtered);
    else if (viewMode === "host") renderHostView(container, filtered);
    else renderMatrixView(container, filtered);
    if (prevId) {
      const restored = document.getElementById(prevId);
      if (restored && typeof restored.focus === "function") {
        try {
          restored.focus({preventScroll: true});
        } catch (_) {
          try { restored.focus(); } catch (__) {}
        }
      }
    }
  }

  function renderAll() {
    updateHeader();
    renderHumanInputBanner();
    renderHostCards();
    renderRollupAndAttention();
    renderMacFilterChips();
    renderMain();
  }

  function hideErrorToast() {
    const toast = document.getElementById("error-toast");
    toast.classList.remove("is-visible");
    toast.replaceChildren();
  }

  function captureUiState() {
    const searchInput = document.getElementById("filter-search");
    return {
      viewMode: viewMode,
      stateFilter: stateFilter,
      inputSubFilter: inputSubFilter,
      macFilter: macFilter,
      searchInputVal: searchInput ? String(searchInput.value || "").slice(0, 200) : "",
      includeCached: includeCached,
      sortCol: sortCol,
      sortAsc: sortAsc,
      otherDrawerOpen: otherDrawerOpen,
      otherDrawerUserSet: otherDrawerUserSet,
      expandedRowIds: Array.from(expandedRows).slice(0, 50),
      autoIntervalSec: autoIntervalSec,
      themeMode: themeMode
    };
  }

  function persistRecoverySnapshot(pendingRefresh) {
    if (STATIC_EXPORT || window.location.protocol === "file:") return false;
    try {
      window.sessionStorage.setItem(CSRF_RECOVERY_KEY, JSON.stringify({
        at: Date.now(),
        pendingRefresh: Boolean(pendingRefresh),
        ui: captureUiState()
      }));
      return true;
    } catch (_) {
      return false;
    }
  }

  function showErrorToast(message, offerReload) {
    const toast = document.getElementById("error-toast");
    const children = [el("span", {text: message})];
    if (offerReload && !STATIC_EXPORT && window.location.protocol !== "file:") {
      children.push(el("button", {
        type: "button",
        className: "btn toast-reload-btn",
        text: "Reload page",
        onClick: function() {
          persistRecoverySnapshot(false);
          window.location.reload();
        }
      }));
    }
    toast.replaceChildren.apply(toast, children);
    toast.classList.add("is-visible");
  }

  function attemptCsrfRestartReload(opts) {
    if (STATIC_EXPORT || window.location.protocol === "file:") return false;
    const pendingRefresh = opts && typeof opts.pendingRefresh === "boolean" ? opts.pendingRefresh : true;
    const pendingFocusRowId = opts && typeof opts.pendingFocusRowId === "string" ? opts.pendingFocusRowId.slice(0, 256) : null;
    try {
      const now = Date.now();
      const prevAt = parseInt(window.sessionStorage.getItem(CSRF_RELOAD_AT_KEY) || "0", 10);
      if (!Number.isNaN(prevAt) && prevAt > 0 && now >= prevAt && (now - prevAt) < CSRF_RELOAD_COOLDOWN_MS) {
        return false;
      }
      window.sessionStorage.setItem(CSRF_RELOAD_AT_KEY, String(now));
      window.sessionStorage.setItem(CSRF_RECOVERY_KEY, JSON.stringify({
        at: now,
        pendingRefresh: pendingRefresh,
        pendingFocusRowId: pendingFocusRowId,
        ui: captureUiState()
      }));
    } catch (_) {
      return false;
    }
    window.location.reload();
    return true;
  }

  function restoreCsrfRecoveryState() {
    if (STATIC_EXPORT || window.location.protocol === "file:") return false;
    let rec = null;
    try {
      const raw = window.sessionStorage.getItem(CSRF_RECOVERY_KEY);
      if (!raw) return false;
      window.sessionStorage.removeItem(CSRF_RECOVERY_KEY);
      rec = JSON.parse(raw);
    } catch (_) {
      return false;
    }
    if (!rec || typeof rec !== "object" || typeof rec.at !== "number") return false;
    const ageMs = Date.now() - rec.at;
    if (ageMs < 0 || ageMs > CSRF_RECOVERY_MAX_AGE_MS) return false;

    const ui = rec.ui && typeof rec.ui === "object" ? rec.ui : {};
    const allowedViews = new Set(["role", "host", "matrix"]);
    if (typeof ui.viewMode === "string" && allowedViews.has(ui.viewMode)) {
      viewMode = ui.viewMode;
      for (const b of document.querySelectorAll("[data-view]")) {
        b.setAttribute("aria-selected", String(b.getAttribute("data-view") === viewMode));
      }
    }
    const allowedFilters = new Set(["all", "input", "active", "alert", "changed", "idle", "unknown"]);
    if (typeof ui.stateFilter === "string" && allowedFilters.has(ui.stateFilter)) {
      stateFilter = ui.stateFilter;
      for (const b of document.querySelectorAll("[data-filter]")) {
        b.setAttribute("aria-pressed", String(b.getAttribute("data-filter") === stateFilter));
      }
    }
    const allowedSubFilters = new Set(["all", "keystroke", "agy_work", "agent_message", "pasted_prompt"]);
    if (stateFilter === "input" && typeof ui.inputSubFilter === "string" && allowedSubFilters.has(ui.inputSubFilter)) {
      inputSubFilter = ui.inputSubFilter;
    }
    const validHostKeys = new Set((payload.hosts || []).map(function(h) { return h.key; }));
    if (typeof ui.macFilter === "string" && (ui.macFilter === "all" || validHostKeys.has(ui.macFilter))) {
      macFilter = ui.macFilter;
    }
    if (typeof ui.searchInputVal === "string") {
      const cleanSearch = ui.searchInputVal.slice(0, 200);
      const searchInput = document.getElementById("filter-search");
      if (searchInput) searchInput.value = cleanSearch;
      searchQuery = cleanSearch.trim().toLowerCase();
    }
    if (typeof ui.includeCached === "boolean") {
      includeCached = ui.includeCached;
      const cb = document.getElementById("toggle-cached");
      if (cb) cb.checked = includeCached;
    }
    const allowedSortCols = new Set(["default", "project", "mac", "cli", "state", "guard", "delta", "ctx", "ports", "view", "up"]);
    if (typeof ui.sortCol === "string" && allowedSortCols.has(ui.sortCol)) {
      sortCol = ui.sortCol;
    }
    if (typeof ui.sortAsc === "boolean") sortAsc = ui.sortAsc;
    if (typeof ui.otherDrawerOpen === "boolean") otherDrawerOpen = ui.otherDrawerOpen;
    if (typeof ui.otherDrawerUserSet === "boolean") otherDrawerUserSet = ui.otherDrawerUserSet;
    if (Array.isArray(ui.expandedRowIds)) {
      const validRowIds = new Set((payload.sessions || []).map(function(s) { return s.row_id; }));
      for (const rowId of ui.expandedRowIds.slice(0, 50)) {
        if (typeof rowId === "string" && validRowIds.has(rowId)) {
          expandedRows.add(rowId);
        }
      }
    }
    const allowedAuto = new Set([0, 60, 300, 900]);
    if (typeof ui.autoIntervalSec === "number" && allowedAuto.has(ui.autoIntervalSec)) {
      autoIntervalSec = ui.autoIntervalSec;
      const autoSelect = document.getElementById("select-autorefresh");
      if (autoSelect) autoSelect.value = String(autoIntervalSec);
      nextAutoAt = autoIntervalSec > 0 ? Date.now() + autoIntervalSec * 1000 : 0;
      document.getElementById("autorefresh-countdown").textContent = autoIntervalSec > 0 ? (autoIntervalSec + "s") : "";
    }
    const allowedThemes = new Set(["auto", "light", "dark"]);
    if (typeof ui.themeMode === "string" && allowedThemes.has(ui.themeMode)) {
      themeMode = ui.themeMode;
      if (themeMode === "auto") document.documentElement.removeAttribute("data-theme");
      else document.documentElement.setAttribute("data-theme", themeMode);
      document.getElementById("btn-theme").textContent = "Theme: " + themeMode.charAt(0).toUpperCase() + themeMode.slice(1);
    }

    if (typeof rec.pendingFocusRowId === "string" && rec.pendingFocusRowId.length <= 256) {
      pendingPostReloadFocusRowId = rec.pendingFocusRowId;
    }

    if (!rec.pendingRefresh) return false;
    const genMs = payload && payload.generated_at ? Date.parse(payload.generated_at) : NaN;
    const alreadyFreshFromReload = !Number.isNaN(genMs) && genMs >= (rec.at - 5);
    return !alreadyFreshFromReload;
  }

  function describeTerminalFocusError(status, errCode, row) {
    const label = (row && row.project_label ? row.project_label : "session") +
      (row && row.target_short ? (" (" + row.target_short + ")") : "");
    if (errCode === "m1_local_sessions_only") {
      return "Terminal window switching is only available for live local M1 sessions.";
    }
    if (errCode === "terminal_not_running") {
      return "Terminal.app is not running on M1 (dashboard will not cold-launch GUI apps).";
    }
    if (errCode === "terminal_tab_not_found") {
      return label + " tab is no longer open in Terminal.app — click Update to refresh.";
    }
    if (errCode === "session_not_found") {
      return label + " is no longer in the server snapshot — click Update to refresh.";
    }
    if (errCode === "tmux_session_not_attached_in_terminal") {
      return label + " is running in tmux with no attached M1 Terminal.app tab.";
    }
    if (errCode === "tmux_window_not_visible_in_terminal") {
      const winRef = row && row.tmux_window ? (" (" + row.tmux_window + ")") : "";
      return label + " is in another tmux window" + winRef + " — attached M1 Terminal tab(s) are viewing a different window.";
    }
    if (errCode === "terminal_automation_not_authorized") {
      return "macOS Automation permission denied — allow Terminal control in System Settings → Privacy & Security → Automation.";
    }
    if (errCode === "terminal_focus_timed_out") {
      return "Terminal.app timed out responding to focus request (check for modal dialogs or Automation permission prompt).";
    }
    if (errCode === "terminal_window_not_raised" || errCode === "terminal_tab_not_selected") {
      return "Terminal.app did not raise " + label + " to the front window — try clicking again.";
    }
    if (errCode === "session_has_no_terminal_window") {
      return label + " is a headless background process with no Terminal.app window or tab.";
    }
    if (errCode === "local_macos_gui_required" || errCode === "terminal_focus_unavailable_in_snapshot_mode") {
      return "Local macOS Terminal switching is unavailable in this server mode.";
    }
    return "HTTP " + status + (errCode ? " (" + errCode + ")" : "");
  }

  async function focusTerminalSession(row) {
    if (!isRowM1Focusable(row) || focusInFlight) return false;
    focusInFlight = true;
    let reloadingPage = false;
    hideErrorToast();
    try {
      const resp = await fetch("/api/focus-terminal", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-MechBay-Token": CSRF_TOKEN,
          "Accept": "application/json"
        },
        body: JSON.stringify({row_id: row.row_id})
      });
      if (!resp.ok) {
        let errCode = "";
        try {
          const errBody = await resp.json();
          if (errBody && typeof errBody.error === "string") {
            errCode = errBody.error.replace(/[^A-Za-z0-9_.-]/g, "").slice(0, 64);
          }
        } catch (_) {}
        if (resp.status === 403 && errCode === "invalid_csrf_token") {
          if (attemptCsrfRestartReload({pendingRefresh: false, pendingFocusRowId: row.row_id})) {
            reloadingPage = true;
            setTimeout(function() {
              focusInFlight = false;
              showErrorToast(
                "Terminal switch failed: Dashboard server restarted or token expired (HTTP 403: invalid_csrf_token) — reload this page to reconnect.",
                true
              );
            }, 4000);
            return false;
          }
          const csrfErr = new Error(
            "Dashboard server restarted or token expired (HTTP 403: invalid_csrf_token) — reload this page to reconnect."
          );
          csrfErr.offerReload = true;
          throw csrfErr;
        }
        const httpErr = new Error(describeTerminalFocusError(resp.status, errCode, row));
        httpErr.offerReload = false;
        throw httpErr;
      }
      await resp.json();
      flashRowId = row.row_id;
      renderMain();
      return true;
    } catch (err) {
      const msg = "Terminal switch failed: " + (err && err.message ? err.message : String(err));
      showErrorToast(msg, Boolean(err && err.offerReload));
      return false;
    } finally {
      if (!reloadingPage) {
        focusInFlight = false;
      }
    }
  }

  async function triggerRefresh() {
    if (STATIC_EXPORT || window.location.protocol === "file:" || refreshInFlight) return;
    refreshInFlight = true;
    let reloadingPage = false;
    const btn = document.getElementById("btn-update");
    const lbl = document.getElementById("btn-update-label");
    btn.classList.add("is-loading");
    btn.disabled = true;
    lbl.textContent = "Updating…";
    hideErrorToast();
    try {
      const resp = await fetch("/api/refresh", {
        method: "POST",
        headers: {"X-MechBay-Token": CSRF_TOKEN, "Accept": "application/json"}
      });
      if (!resp.ok) {
        let errCode = "";
        try {
          const errBody = await resp.json();
          if (errBody && typeof errBody.error === "string") {
            errCode = errBody.error.replace(/[^A-Za-z0-9_.-]/g, "").slice(0, 64);
          }
        } catch (_) {}
        if (resp.status === 403 && errCode === "invalid_csrf_token") {
          if (attemptCsrfRestartReload()) {
            reloadingPage = true;
            lbl.textContent = "Reloading…";
            setTimeout(function() {
              refreshInFlight = false;
              btn.classList.remove("is-loading");
              btn.disabled = false;
              lbl.textContent = "↻ Update";
              showErrorToast(
                "Update failed: Dashboard server restarted or token expired (HTTP 403: invalid_csrf_token) — reload this page to reconnect.",
                true
              );
            }, 4000);
            return;
          }
          const csrfErr = new Error(
            "Dashboard server restarted or token expired (HTTP 403: invalid_csrf_token) — reload this page to reconnect."
          );
          csrfErr.offerReload = true;
          throw csrfErr;
        }
        const httpErr = new Error(
          "Refresh returned HTTP " + resp.status + (errCode ? " (" + errCode + ")" : "")
        );
        httpErr.offerReload = (resp.status === 403);
        throw httpErr;
      }
      payload = await resp.json();
      renderAll();
    } catch (err) {
      const msg = "Update failed: " + (err && err.message ? err.message : String(err));
      showErrorToast(msg, Boolean(err && err.offerReload));
    } finally {
      if (!reloadingPage) {
        refreshInFlight = false;
        btn.classList.remove("is-loading");
        btn.disabled = false;
        lbl.textContent = "↻ Update";
        if (autoIntervalSec > 0) {
          nextAutoAt = Date.now() + autoIntervalSec * 1000;
        }
      }
    }
  }

  // Event bindings
  document.getElementById("btn-update").addEventListener("click", triggerRefresh);

  document.getElementById("select-autorefresh").addEventListener("change", function(ev) {
    autoIntervalSec = parseInt(ev.target.value, 10) || 0;
    nextAutoAt = autoIntervalSec > 0 ? Date.now() + autoIntervalSec * 1000 : 0;
    document.getElementById("autorefresh-countdown").textContent = autoIntervalSec > 0 ? (autoIntervalSec + "s") : "";
  });

  document.getElementById("btn-theme").addEventListener("click", function(ev) {
    const order = ["auto", "light", "dark"];
    themeMode = order[(order.indexOf(themeMode) + 1) % order.length];
    if (themeMode === "auto") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", themeMode);
    ev.currentTarget.textContent = "Theme: " + themeMode.charAt(0).toUpperCase() + themeMode.slice(1);
  });

  for (const btn of document.querySelectorAll("[data-view]")) {
    btn.addEventListener("click", function() {
      viewMode = btn.getAttribute("data-view");
      for (const b of document.querySelectorAll("[data-view]")) {
        b.setAttribute("aria-selected", String(b === btn));
      }
      renderMain();
    });
  }

  for (const btn of document.querySelectorAll("[data-filter]")) {
    btn.addEventListener("click", function() {
      setStateFilter(btn.getAttribute("data-filter"));
    });
  }

  document.getElementById("filter-search").addEventListener("input", function(ev) {
    searchQuery = String(ev.target.value || "").trim().toLowerCase();
    renderMain();
  });

  document.getElementById("toggle-cached").addEventListener("change", function(ev) {
    includeCached = Boolean(ev.target.checked);
    renderMain();
  });

  document.addEventListener("keydown", function(ev) {
    const active = document.activeElement;
    const inInput = active && (active.tagName === "INPUT" || active.tagName === "SELECT" || active.tagName === "TEXTAREA");
    if (ev.key === "/" && !inInput) {
      ev.preventDefault();
      document.getElementById("filter-search").focus();
    } else if (ev.key === "Escape" && active === document.getElementById("filter-search")) {
      active.value = "";
      searchQuery = "";
      active.blur();
      renderMain();
    } else if ((ev.key === "i" || ev.key === "I") && !inInput && !ev.metaKey && !ev.ctrlKey && !ev.altKey) {
      ev.preventDefault();
      setStateFilter(stateFilter === "input" ? "all" : "input");
    } else if ((ev.key === "r" || ev.key === "R") && !inInput && !ev.metaKey && !ev.ctrlKey && !ev.altKey) {
      ev.preventDefault();
      triggerRefresh();
    }
  });

  setInterval(function() {
    updateHeader();
    const cd = document.getElementById("autorefresh-countdown");
    if (autoIntervalSec > 0 && nextAutoAt > 0 && !STATIC_EXPORT) {
      const rem = Math.max(0, Math.ceil((nextAutoAt - Date.now()) / 1000));
      cd.textContent = rem + "s";
      if (rem <= 0 && !refreshInFlight) {
        triggerRefresh();
      }
    } else {
      cd.textContent = "";
    }
  }, 1000);

  const shouldRefreshAfterReload = restoreCsrfRecoveryState();
  renderAll();
  if (pendingPostReloadFocusRowId) {
    const recoveredFocusId = pendingPostReloadFocusRowId;
    pendingPostReloadFocusRowId = null;
    const recoveredRow = (payload.sessions || []).find(function(s) { return s.row_id === recoveredFocusId; });
    if (recoveredRow && isRowM1Focusable(recoveredRow)) {
      focusTerminalSession(recoveredRow);
    } else if (!recoveredRow) {
      showErrorToast("Terminal switch failed: " + describeTerminalFocusError(404, "session_not_found", null), false);
    }
  }
  if (shouldRefreshAfterReload) {
    triggerRefresh();
  }
})();
</script>
</body>
</html>
"""


_TEMPLATE_TOKENS = re.compile(r"__(?:INITIAL_PAYLOAD_JSON|STATIC_EXPORT_BOOL|CSRF_TOKEN)__")


def render_dashboard_html(payload, csrf_token="", static_export=False):
    if not re.fullmatch(r"[A-Za-z0-9_-]{0,96}", csrf_token or ""):
        raise ValueError("invalid_csrf_token")
    replacements = {
        "__INITIAL_PAYLOAD_JSON__": safe_json_for_html(payload),
        "__STATIC_EXPORT_BOOL__": "true" if static_export else "false",
        "__CSRF_TOKEN__": "" if static_export else (csrf_token or ""),
    }
    return _TEMPLATE_TOKENS.sub(lambda match: replacements[match.group(0)], DASHBOARD_TEMPLATE)


class _RefreshFlight:
    __slots__ = ("done", "payload", "error")

    def __init__(self):
        self.done = False
        self.payload = None
        self.error = None


class DashboardState:
    """Thread-safe state with single-flight coalescing for concurrent Update clicks."""

    def __init__(self, collect_fn, snapshot_html_path=None, wait_timeout=270):
        self._collect_fn = collect_fn
        self.snapshot_html_path = Path(snapshot_html_path) if snapshot_html_path else None
        self.wait_timeout = wait_timeout
        self.csrf_token = secrets.token_urlsafe(24)
        self.focus_lock = threading.Lock()
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._inflight = None
        self._seq = 0
        self._payload = None

    def get_payload(self):
        with self._lock:
            if self._payload is not None:
                return self._payload
        return self.refresh()

    def refresh(self):
        with self._cond:
            if self._inflight is not None:
                flight = self._inflight
                finished = self._cond.wait_for(lambda: flight.done, timeout=self.wait_timeout)
                if not finished:
                    raise RuntimeError("dashboard_refresh_timed_out")
                if flight.error is not None:
                    raise flight.error
                return flight.payload
            flight = _RefreshFlight()
            self._inflight = flight
            next_seq = self._seq + 1

        started = time.monotonic()
        try:
            reports, meta = self._collect_fn()
            elapsed = round((time.monotonic() - started) * 1000)
            payload = build_dashboard_payload(
                reports,
                duration_ms=elapsed,
                refresh_seq=next_seq,
                mode=meta.get("mode", "live"),
                role_overrides=meta.get("role_overrides"),
            )
            if self.snapshot_html_path is not None and meta.get("mode") == "live":
                try:
                    html_doc = render_dashboard_html(payload, csrf_token="", static_export=True)
                    atomic_private_html(self.snapshot_html_path, html_doc)
                except (OSError, ValueError):
                    pass
            with self._cond:
                self._seq = next_seq
                self._payload = payload
                flight.payload = payload
                flight.done = True
                self._inflight = None
                self._cond.notify_all()
            return payload
        except BaseException as exc:
            with self._cond:
                flight.error = exc if isinstance(exc, Exception) else RuntimeError("dashboard_refresh_aborted")
                flight.done = True
                self._inflight = None
                self._cond.notify_all()
            raise


class LoopbackHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        server_address,
        handler_cls,
        state,
        allow_tailscale=False,
        local_tailscale_ips=(),
        extra_host_names=(),
    ):
        host = validate_bind_host(
            server_address[0],
            allow_tailscale=allow_tailscale,
            local_tailscale_ips=local_tailscale_ips,
        )
        if ":" in host:
            self.address_family = socket.AF_INET6
        self.state = state
        self.extra_host_names = tuple(extra_host_names or ())
        super().__init__((host, server_address[1]), handler_cls)


def _format_host_for_header(raw_host):
    val = str(raw_host or "").strip().rstrip(".").lower()
    if not val:
        return None
    if ":" in val and not val.startswith("["):
        return "[" + val + "]"
    return val


def _allowed_host_headers(port, extra_hosts=()):
    allowed = {
        "127.0.0.1:" + str(port),
        "localhost:" + str(port),
        "[::1]:" + str(port),
    } | ({"127.0.0.1", "localhost", "[::1]"} if port == 80 else set())
    for raw in extra_hosts or ():
        fmt = _format_host_for_header(raw)
        if not fmt:
            continue
        allowed.add(fmt + ":" + str(port))
        allowed.add(fmt)
    return allowed


def _allowed_origins(port, extra_hosts=()):
    allowed = {
        "http://127.0.0.1:" + str(port),
        "http://localhost:" + str(port),
        "http://[::1]:" + str(port),
    } | ({"http://127.0.0.1", "http://localhost", "http://[::1]"} if port == 80 else set())
    for raw in extra_hosts or ():
        fmt = _format_host_for_header(raw)
        if not fmt:
            continue
        allowed.add("http://" + fmt + ":" + str(port))
        allowed.add("https://" + fmt + ":" + str(port))
        allowed.add("http://" + fmt)
        allowed.add("https://" + fmt)
    return allowed


TERMINAL_FOCUS_JXA = """function run(argv) {
  if (!argv || argv.length < 1) {
    return JSON.stringify({ok: false, error: "missing_focus_args"});
  }
  var req = JSON.parse(argv[0]);
  var wantedTtys = Array.isArray(req.ttys) ? req.ttys : [];
  if (!wantedTtys.length) {
    return JSON.stringify({ok: false, error: "missing_focus_tty"});
  }
  var wantedWinId = typeof req.window_id === "number" ? req.window_id : null;
  var wantedTabIdx = typeof req.tab_index === "number" ? req.tab_index : null;
  var app = Application("Terminal");
  if (!app.running()) {
    return JSON.stringify({ok: false, error: "terminal_not_running"});
  }
  var windows = app.windows();
  var bestWin = null;
  var bestTab = null;
  var bestWinId = null;
  var bestTabIdx = null;
  var bestTty = null;
  var bestScore = -1;

  for (var wi = 0; wi < windows.length; wi++) {
    var w = windows[wi];
    var wid = null;
    var tabs = null;
    try {
      wid = Number(w.id());
      tabs = w.tabs();
    } catch (_) {
      continue;
    }
    if (!tabs || !tabs.length) continue;

    for (var ti = 0; ti < tabs.length; ti++) {
      var t = tabs[ti];
      var rawTty = "";
      try {
        rawTty = String(t.tty() || "");
      } catch (_) {
        continue;
      }
      var normTty = rawTty.indexOf("/dev/") === 0 ? rawTty.slice(5) : rawTty;
      var ttyPos = wantedTtys.indexOf(normTty);
      if (ttyPos === -1) continue;
      var tabOneBased = ti + 1;
      var score = 100 - Math.min(ttyPos, 50);
      if (wantedWinId !== null && wid === wantedWinId) score += 200;
      if (wantedTabIdx !== null && tabOneBased === wantedTabIdx) score += 50;
      if (score > bestScore) {
        bestScore = score;
        bestWin = w;
        bestTab = t;
        bestWinId = wid;
        bestTabIdx = tabOneBased;
        bestTty = normTty;
      }
    }
  }

  if (!bestWin || !bestTab) {
    return JSON.stringify({ok: false, error: "terminal_tab_not_found"});
  }

  try {
    if (bestWin.miniaturized()) {
      bestWin.miniaturized = false;
    }
  } catch (_) {}
  bestTab.selected = true;
  try { bestWin.index = 1; } catch (_) {}
  try { bestWin.frontmost = true; } catch (_) {}
  app.activate();
  try { bestTab.selected = true; } catch (_) {}
  try { bestWin.index = 1; } catch (_) {}
  try { bestWin.frontmost = true; } catch (_) {}

  var tabSelected = false;
  try {
    tabSelected = Boolean(bestTab.selected());
  } catch (_) {}
  var frontWinId = null;
  try {
    var afterWins = app.windows();
    if (afterWins.length > 0) frontWinId = Number(afterWins[0].id());
  } catch (_) {}

  if (!tabSelected) {
    return JSON.stringify({ok: false, error: "terminal_tab_not_selected"});
  }

  return JSON.stringify({
    ok: true,
    window_id: bestWinId,
    tab_index: bestTabIdx,
    tty: bestTty,
    front_window_id: frontWinId
  });
}
"""


def _terminal_app_is_running():
    proc = subprocess.run(
        ["/bin/ps", "-axo", "comm="],
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )
    if proc.returncode != 0:
        return False
    return bool(re.search(r"/Terminal\.app/Contents/MacOS/Terminal$", proc.stdout, re.M))


def _find_local_tmux_bin():
    for candidate in ("/opt/homebrew/bin/tmux", "/usr/local/bin/tmux", "/usr/bin/tmux"):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return shutil.which("tmux")


def _resolve_local_tmux_client_ttys(session_row):
    wanted = []
    for candidate_name in (
        list(session_row.get("tmux_lookup_sessions") or [])
        + [session_row.get("tmux_session")]
        + list(session_row.get("tmux_sessions") or [])
    ):
        if (
            isinstance(candidate_name, str)
            and _TMUX_SESSION_NAME_RE.fullmatch(candidate_name)
            and candidate_name not in wanted
        ):
            wanted.append(candidate_name)
    if not wanted:
        return "not_attached", []

    tmux_bin = _find_local_tmux_bin()
    if not tmux_bin:
        return "not_attached", []

    raw_pane = session_row.get("tmux_pane")
    target_pane = (
        raw_pane.strip()
        if isinstance(raw_pane, str) and _TMUX_PANE_ID_RE.fullmatch(raw_pane.strip())
        else None
    )
    row_pane_tty = _normalize_tty_name(session_row.get("tty"))

    try:
        proc_panes = subprocess.run(
            [tmux_bin, "list-panes", "-a", "-F", "#{pane_id}\t#{pane_tty}\t#{window_id}\t#{session_name}"],
            text=True,
            capture_output=True,
            timeout=4,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return "timed_out", []
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return "not_attached", []
    if proc_panes.returncode != 0:
        return "not_attached", []

    target_window_ids = set()
    wanted_set = set(wanted)
    pane_matched = False
    for line in proc_panes.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        p_id = parts[0].strip()
        p_tty = _normalize_tty_name(parts[1])
        w_id = parts[2].strip()
        s_name = parts[3].strip()
        if target_pane is not None:
            if p_id != target_pane:
                continue
            if row_pane_tty and p_tty and p_tty != row_pane_tty:
                continue
        elif row_pane_tty is not None:
            if p_tty != row_pane_tty:
                continue
            target_pane = p_id
        else:
            continue
        pane_matched = True
        if w_id:
            target_window_ids.add(w_id)
        if _TMUX_SESSION_NAME_RE.fullmatch(s_name):
            if s_name not in wanted_set:
                wanted.append(s_name)
            wanted_set.add(s_name)

    if (target_pane is not None or row_pane_tty is not None) and not pane_matched:
        return "pane_not_found", []

    rank_idx = {name: idx for idx, name in enumerate(wanted)}
    try:
        proc_clients = subprocess.run(
            [tmux_bin, "list-clients", "-F", "#{client_tty}\t#{session_name}\t#{window_id}\t#{pane_id}"],
            text=True,
            capture_output=True,
            timeout=4,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return "timed_out", []
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return "not_attached", []
    if proc_clients.returncode != 0:
        return "not_attached", []

    hits = []
    other_window_attached = 0
    for line in proc_clients.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        tty_name = _normalize_tty_name(parts[0])
        sess_name = parts[1].strip()
        c_win_id = parts[2].strip() if len(parts) >= 3 else ""
        c_pane_id = parts[3].strip() if len(parts) >= 4 else ""
        if not tty_name:
            continue
        viewer_pref = 0 if sess_name.startswith("xmb-view-") else 1
        sess_pos = rank_idx.get(sess_name, 999)
        if target_window_ids:
            if target_pane and c_pane_id == target_pane:
                hits.append(((0, viewer_pref, sess_pos), tty_name))
            elif c_win_id and c_win_id in target_window_ids:
                hits.append(((1, viewer_pref, sess_pos), tty_name))
            elif sess_name in wanted_set:
                other_window_attached += 1
        elif sess_name in wanted_set:
            hits.append(((1, viewer_pref, sess_pos), tty_name))

    if hits:
        hits.sort(key=lambda item: item[0])
        ordered = []
        for _, tty_name in hits:
            if tty_name not in ordered:
                ordered.append(tty_name)
        return "ok", ordered
    if other_window_attached > 0:
        return "window_not_visible", []
    return "not_attached", []


def focus_local_terminal_session(session_row, mode="live"):
    """Switch local M1 Terminal.app to session_row's window and tab without sending input."""
    if not isinstance(session_row, dict):
        return False, 400, {"error": "invalid_session_row"}
    mac_glyph = ((session_row.get("symbols") or {}).get("mac") or {}).get("glyph")
    if (
        mode != "live"
        or session_row.get("host_source") != "local"
        or bool(session_row.get("stale_cached"))
        or not is_m1_host(session_row.get("host_key"), session_row.get("host_label"))
        or mac_glyph != "M1"
    ):
        return False, 403, {"error": "m1_local_sessions_only"}
    if not session_row.get("can_focus_terminal"):
        return False, 409, {"error": "session_has_no_terminal_window"}
    if sys.platform != "darwin" or os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        return False, 409, {"error": "local_macos_gui_required"}
    try:
        running = _terminal_app_is_running()
    except subprocess.TimeoutExpired:
        return False, 504, {"error": "terminal_focus_timed_out"}
    if not running:
        return False, 409, {"error": "terminal_not_running"}

    transport = str(session_row.get("transport") or "process")
    if transport == "tmux":
        tmux_state, ttys = _resolve_local_tmux_client_ttys(session_row)
        if tmux_state == "timed_out":
            return False, 504, {"error": "terminal_focus_timed_out"}
        if tmux_state == "pane_not_found":
            return False, 404, {"error": "terminal_tab_not_found"}
        if tmux_state == "window_not_visible":
            return False, 409, {"error": "tmux_window_not_visible_in_terminal"}
        if not ttys:
            return False, 409, {"error": "tmux_session_not_attached_in_terminal"}
        window_id = None
        tab_index = None
    elif transport == "terminal":
        norm_tty = _normalize_tty_name(session_row.get("tty"))
        if not norm_tty:
            return False, 409, {"error": "session_has_no_terminal_window"}
        ttys = [norm_tty]
        raw_wid = session_row.get("window_id")
        window_id = raw_wid if isinstance(raw_wid, int) and not isinstance(raw_wid, bool) and raw_wid >= 1 else None
        raw_tab = session_row.get("tab_index")
        tab_index = raw_tab if isinstance(raw_tab, int) and not isinstance(raw_tab, bool) and raw_tab >= 1 else None
    else:
        return False, 409, {"error": "session_has_no_terminal_window"}

    req_arg = json.dumps({"ttys": ttys, "window_id": window_id, "tab_index": tab_index})
    try:
        proc = subprocess.run(
            ["/usr/bin/osascript", "-l", "JavaScript", "-e", TERMINAL_FOCUS_JXA, req_arg],
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, 504, {"error": "terminal_focus_timed_out"}
    if proc.returncode != 0:
        stderr_low = (proc.stderr or "").lower()
        if "-1743" in stderr_low or "not authorized to send apple events" in stderr_low:
            return False, 403, {"error": "terminal_automation_not_authorized"}
        return False, 502, {"error": "terminal_focus_osascript_failed"}
    try:
        res = json.loads(proc.stdout.strip() or "{}")
    except ValueError:
        return False, 502, {"error": "terminal_focus_invalid_response"}
    if not isinstance(res, dict) or not res.get("ok"):
        err_code = str(res.get("error") if isinstance(res, dict) else "" or "terminal_focus_failed")
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", err_code):
            err_code = "terminal_focus_failed"
        status_code = 404 if err_code == "terminal_tab_not_found" else (409 if err_code == "terminal_not_running" else 502)
        return False, status_code, {"error": err_code}

    front_wid = res.get("front_window_id")
    actual_wid = res.get("window_id")
    if front_wid is not None and actual_wid is not None and front_wid != actual_wid:
        return False, 502, {"error": "terminal_window_not_raised"}

    return True, 200, {
        "ok": True,
        "row_id": session_row.get("row_id"),
        "host_key": session_row.get("host_key"),
        "project_label": session_row.get("project_label"),
        "window_id": actual_wid,
        "tab_index": res.get("tab_index"),
        "tty": res.get("tty"),
        "front_window_id": front_wid,
    }


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "XMechBayDashboard/1.0"
    sys_version = ""
    timeout = 10

    def log_message(self, format, *args):
        return  # Silence per-request stdout/stderr chatter by default.

    def _send_bytes(self, status_code, content_type, body):
        try:
            self.send_response(status_code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Security-Policy", CSP_POLICY)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            self.close_connection = True
            return

    def _send_json(self, status_code, value):
        data = (json.dumps(value) + "\n").encode("utf-8")
        self._send_bytes(status_code, "application/json; charset=utf-8", data)

    def _check_host_header(self):
        port = self.server.server_address[1]
        extra = getattr(self.server, "extra_host_names", ())
        host_hdr = (self.headers.get("Host") or "").strip().lower()
        return host_hdr in _allowed_host_headers(port, extra_hosts=extra)

    def _read_post_body(self, max_bytes=4096):
        if self.headers.get("Transfer-Encoding") is not None:
            self.close_connection = True
            return False, 413, "unsupported_transfer_encoding", b""
        raw_len = (self.headers.get("Content-Length") or "0").strip()
        if not raw_len.isdigit():
            self.close_connection = True
            return False, 400, "invalid_content_length", b""
        length = int(raw_len)
        if length > max_bytes:
            self.close_connection = True
            return False, 413, "payload_too_large", b""
        body = b""
        if length > 0:
            try:
                body = self.rfile.read(length)
            except socket.timeout:
                self.close_connection = True
                return False, 408, "request_timeout", b""
            if len(body) < length:
                self.close_connection = True
                return False, 400, "incomplete_request_body", b""
        return True, 200, "", body

    def _drain_post_body(self, max_bytes=4096):
        ok, status_code, err_code, _ = self._read_post_body(max_bytes=max_bytes)
        return ok, status_code, err_code

    def do_GET(self):
        if not self._check_host_header():
            self._send_json(403, {"error": "forbidden_host_header"})
            return
        parsed = urlparse(self.path)
        route = parsed.path
        if route == "/favicon.ico":
            self._send_bytes(204, "image/x-icon", b"")
            return
        if route not in {"/", "/index.html", "/api/status", "/export.html"}:
            self._send_json(404, {"error": "not_found"})
            return
        try:
            payload = self.server.state.get_payload()
            if route == "/api/status":
                status_code, content_type, body = 200, "application/json; charset=utf-8", (json.dumps(payload) + "\n").encode("utf-8")
            elif route == "/export.html":
                doc = render_dashboard_html(payload, csrf_token="", static_export=True)
                status_code, content_type, body = 200, "text/html; charset=utf-8", doc.encode("utf-8")
            else:
                doc = render_dashboard_html(payload, csrf_token=self.server.state.csrf_token, static_export=False)
                status_code, content_type, body = 200, "text/html; charset=utf-8", doc.encode("utf-8")
        except (OSError, ValueError, RuntimeError, TypeError):
            self._send_json(500, {"error": "dashboard_collection_failed"})
            return
        self._send_bytes(status_code, content_type, body)

    def do_POST(self):
        if not self._check_host_header():
            self._send_json(403, {"error": "forbidden_host_header"})
            return
        parsed = urlparse(self.path)
        route = parsed.path
        if route not in {"/api/refresh", "/api/focus-terminal"}:
            self._send_json(404, {"error": "not_found"})
            return
        port = self.server.server_address[1]
        extra = getattr(self.server, "extra_host_names", ())
        origin = self.headers.get("Origin")
        if origin is not None and origin.strip().lower() not in _allowed_origins(port, extra_hosts=extra):
            self._send_json(403, {"error": "forbidden_origin"})
            return
        supplied = self.headers.get("X-MechBay-Token") or ""
        if not supplied or not secrets.compare_digest(supplied, self.server.state.csrf_token):
            self._send_json(403, {"error": "invalid_csrf_token"})
            return
        ok, err_status, err_code, raw_body = self._read_post_body()
        if not ok:
            self._send_json(err_status, {"error": err_code})
            return
        if route == "/api/refresh":
            try:
                payload = self.server.state.refresh()
            except (OSError, ValueError, RuntimeError, TypeError):
                self._send_json(500, {"error": "dashboard_refresh_failed"})
                return
            self._send_json(200, payload)
            return

        if not _is_loopback_client(self.client_address[0]):
            self._send_json(403, {"error": "loopback_client_required"})
            return

        try:
            req_data = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self._send_json(400, {"error": "invalid_json_body"})
            return
        if not isinstance(req_data, dict):
            self._send_json(400, {"error": "invalid_json_body"})
            return
        row_id = req_data.get("row_id")
        if (
            not isinstance(row_id, str)
            or not 1 <= len(row_id) <= 256
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in row_id)
        ):
            self._send_json(400, {"error": "invalid_row_id"})
            return
        try:
            payload = self.server.state.get_payload()
        except (OSError, ValueError, RuntimeError, TypeError):
            self._send_json(500, {"error": "dashboard_collection_failed"})
            return
        if payload.get("mode") != "live":
            self._send_json(409, {"error": "terminal_focus_unavailable_in_snapshot_mode"})
            return
        target_row = next(
            (row for row in (payload.get("sessions") or []) if row.get("row_id") == row_id),
            None,
        )
        if target_row is None:
            self._send_json(404, {"error": "session_not_found"})
            return
        try:
            with self.server.state.focus_lock:
                _, focus_status, focus_body = focus_local_terminal_session(
                    target_row, mode=payload.get("mode", "live")
                )
        except (OSError, subprocess.SubprocessError, ValueError, RuntimeError):
            self._send_json(500, {"error": "terminal_focus_failed"})
            return
        self._send_json(focus_status, focus_body)


def create_server(
    state,
    host="127.0.0.1",
    port=8765,
    allow_tailscale=False,
    local_tailscale_ips=(),
    extra_host_names=(),
):
    validated = validate_bind_host(
        host,
        allow_tailscale=allow_tailscale,
        local_tailscale_ips=local_tailscale_ips,
    )
    return LoopbackHTTPServer(
        (validated, port),
        DashboardHandler,
        state,
        allow_tailscale=allow_tailscale,
        local_tailscale_ips=local_tailscale_ips,
        extra_host_names=extra_host_names,
    )


def resolve_local_tailscale_bind(ts_status=None):
    status = ts_status if isinstance(ts_status, dict) else discover_tailscale_status()
    self_node = (status or {}).get("self")
    if not isinstance(self_node, dict) or self_node.get("online") is False:
        raise ValueError("tailscale_unavailable")
    ips = [ip for ip in (self_node.get("ips") or []) if is_tailscale_ip(ip)]
    if not ips:
        raise ValueError("tailscale_unavailable")
    extra_names = list(ips)
    for field in ("dns_name", "short_name"):
        val = self_node.get(field)
        if isinstance(val, str) and val and val not in extra_names:
            extra_names.append(val)
    return {
        "ips": ips,
        "ipv4": self_node.get("ipv4") or ips[0],
        "dns_name": self_node.get("dns_name"),
        "short_name": self_node.get("short_name"),
        "extra_host_names": extra_names,
    }


def open_browser_local(url):
    if sys.platform != "darwin" or os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        raise RuntimeError("Browser launch requires a local macOS session")
    subprocess.run(["/usr/bin/open", url], check=True, timeout=15)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("hosts", nargs="*", help="configured host keys (defaults to all configured hosts)")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(os.environ.get("XMECHBAY_FLEET_CONFIG", str(ROOT / ".local/fleet-status.json"))),
    )
    parser.add_argument("--all", action="store_true", help="include all configured hosts (default when none specified)")
    parser.add_argument("--snapshot", type=Path, help="offline multi-host fixture; no SSH or caches")
    parser.add_argument("--no-cache", action="store_true", help="do not read/write comparison or fallback caches")
    parser.add_argument("--host", default="127.0.0.1", help="loopback bind address (or local Tailscale IP with --tailscale)")
    parser.add_argument("--tailscale", action="store_true", help="also serve over this Mac's Tailscale interface (no LAN wildcard)")
    parser.add_argument("--port", type=int, default=8765, help="TCP port on loopback/Tailscale (0 chooses an ephemeral port)")
    parser.add_argument("--export-html", type=Path, help="write one private static HTML snapshot (0600) and exit")
    parser.add_argument(
        "--report-dir",
        type=Path,
        default=ROOT / ".local/fleet-reports",
        help="private directory for latest static HTML snapshot during live refreshes",
    )
    parser.add_argument("--open", action="store_true", help="open http://127.0.0.1:<port>/ in local macOS browser")
    args = parser.parse_args(argv)

    if args.all and args.hosts:
        parser.error("choose specific host keys or --all")
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")

    try:
        ts_bind = resolve_local_tailscale_bind() if args.tailscale else None
        local_ts_ips = ts_bind["ips"] if ts_bind else ()
        extra_host_names = ts_bind["extra_host_names"] if ts_bind else ()
        bind_host = validate_bind_host(
            args.host,
            allow_tailscale=bool(args.tailscale),
            local_tailscale_ips=local_ts_ips,
        )

        def collect_once():
            return collect_dashboard_reports(
                config_path=args.config,
                snapshot_path=args.snapshot,
                requested_hosts=args.hosts,
                all_hosts=args.all or not args.hosts,
                use_cache=not args.no_cache,
            )

        if args.export_html is not None:
            started = time.monotonic()
            reports, meta = collect_once()
            elapsed = round((time.monotonic() - started) * 1000)
            payload = build_dashboard_payload(
                reports,
                duration_ms=elapsed,
                refresh_seq=1,
                mode=meta.get("mode", "live"),
                role_overrides=meta.get("role_overrides"),
            )
            doc = render_dashboard_html(payload, csrf_token="", static_export=True)
            out_path = atomic_private_html(args.export_html, doc)
            print(str(out_path))
            return 0 if all(r.get("reachable") for r in reports) else 2

        latest_html = None if (args.no_cache or args.snapshot is not None) else (args.report_dir / "dashboard-latest.html")
        state = DashboardState(collect_once, snapshot_html_path=latest_html)
        state.get_payload()  # Initial collection before opening browser.
        server = create_server(
            state,
            host=bind_host,
            port=args.port,
            allow_tailscale=bool(args.tailscale),
            local_tailscale_ips=local_ts_ips,
            extra_host_names=extra_host_names,
        )
        secondary_server = None
        try:
            bound_port = server.server_address[1]
            if ts_bind and bind_host in LOOPBACK_HOSTS and ts_bind.get("ipv4"):
                secondary_server = create_server(
                    state,
                    host=ts_bind["ipv4"],
                    port=bound_port,
                    allow_tailscale=True,
                    local_tailscale_ips=local_ts_ips,
                    extra_host_names=extra_host_names,
                )
                sec_thread = threading.Thread(target=secondary_server.serve_forever, daemon=True)
                sec_thread.start()
            if bind_host == "::1":
                url_host = "[::1]"
            elif bind_host in LOOPBACK_HOSTS:
                url_host = "127.0.0.1"
            elif ":" in bind_host:
                url_host = "[" + bind_host + "]"
            else:
                url_host = bind_host
            url = "http://" + url_host + ":" + str(bound_port) + "/"
            ts_note = ""
            if ts_bind:
                ts_host_disp = ts_bind.get("dns_name") or ts_bind.get("ipv4")
                ts_note = " and Tailscale http://" + str(ts_host_disp) + ":" + str(bound_port) + "/"
            print("MechBay dashboard listening on " + url + ts_note + " (Ctrl+C to stop)", flush=True)
            if args.open:
                try:
                    open_browser_local(url)
                except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                    print("Browser launch skipped: " + str(error), file=sys.stderr)
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            if secondary_server is not None:
                secondary_server.shutdown()
                secondary_server.server_close()
            server.server_close()
        return 0
    except (OSError, ValueError, TypeError, RuntimeError) as error:
        code = str(error)
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
            code = "dashboard_configuration_or_bind_failed"
        print(json.dumps({"error": code, "hint": "Use --help; configure .local/fleet-status.json or pass --snapshot."}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
