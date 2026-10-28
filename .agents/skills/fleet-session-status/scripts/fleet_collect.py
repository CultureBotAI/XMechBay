#!/usr/bin/env python3
"""Collect a bounded, read-only Mac fleet session snapshot; Python 3.9+.

Purpose: on-demand fleet session table PR, 2026-10-27.
Inputs: local process/Terminal/tmux observations and optional private health JSON.
Output: private JSON payload; screens are for normalization, never publication.
No session input, credential inspection, environment dump, or agent model calls.
"""

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time


AGENTS = {"agy", "codex", "claude"}
RUNTIMES = {"node", "nodejs", "bash", "zsh", "sh"}
NODE_RUNTIMES = {"node", "nodejs"}
AGENT_TITLE = re.compile(r"(?:^|[^a-z0-9])(?:agy|antigravity|claude|codex)(?:$|[^a-z0-9])", re.IGNORECASE)
UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")
MAX_PROCESSES = 8192
MAX_SESSIONS = 256
MAX_PANES = 128
MAX_TABS = 128
MAX_RUNTIME_CANDIDATES = 128
MAX_SCREEN_BYTES = 16384
SCREEN_LINES = 80
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_HEALTH_BYTES = 1024 * 1024
TOTAL_SECONDS = 30

TERMINAL_SCRIPT = r'''function run(argv) {
  const wanted = new Set(JSON.parse(argv[0]));
  const app = Application("Terminal");
  const result = {tabs: [], errors: []};
  if (!app.running()) {
    result.errors.push("terminal_not_running");
    return JSON.stringify(result);
  }
  const windows = app.windows();
  outer: for (let wi = 0; wi < windows.length; wi++) {
    const w = windows[wi];
    let tabs;
    try { tabs = w.tabs(); } catch (e) {
      result.errors.push("terminal_window_unreadable"); continue;
    }
    for (let ti = 0; ti < tabs.length; ti++) {
      if (result.tabs.length >= 128) {
        result.errors.push("terminal_tabs_truncated"); break outer;
      }
      try {
        const t = tabs[ti];
        const tty = String(t.tty()).replace(/^\/dev\//, "");
        const selected = Boolean(t.selected());
        let title = "";
        try { title = String(t.customTitle() || ""); } catch (e) {}
        if (!title && selected) { try { title = String(w.name()); } catch (e) {} }
        let visible = null, minimized = null;
        try { visible = Boolean(w.visible()); } catch (e) {}
        try { minimized = Boolean(w.miniaturized()); } catch (e) {}
        let processNames = [], processesReadable = false;
        try {
          const allowed = new Set(["node", "nodejs", "bash", "zsh", "sh", "agy", "claude", "codex"]);
          processNames = Array.from(new Set(t.processes().map(String).filter(name => allowed.has(name))));
          processesReadable = true;
        } catch (e) {}
        const row = {window_id: Number(w.id()), tab: ti + 1, tty: tty,
          selected: selected, visible: visible, minimized: minimized,
          process_names: processNames, processes_readable: processesReadable,
          title: title.slice(0, 512), screen: "", errors: []};
        if (wanted.has(tty)) {
          try {
            row.screen = String(t.contents()).split(/\r\n?|\n/).slice(-80).join("\n");
            if (row.screen.length > 16384) {
              row.screen = row.screen.slice(-16384);
              row.errors.push("screen_bytes_truncated");
            }
          } catch (e) { row.errors.push("terminal_screen_unreadable"); }
        }
        result.tabs.push(row);
      } catch (e) { result.errors.push("terminal_tab_unreadable"); }
    }
  }
  return JSON.stringify(result);
}'''


class ProbeFailure(Exception):
    """Fixed error code; never includes raw arguments, output, or private paths."""


class Runner:
    def __init__(self, seconds=TOTAL_SECONDS):
        self.deadline = time.monotonic() + seconds

    def run(self, command, timeout=6, limit=MAX_OUTPUT_BYTES):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ProbeFailure("collection_deadline_exceeded")
        try:
            with tempfile.TemporaryFile() as output:
                subprocess.run(command, stdout=output, stderr=subprocess.DEVNULL,
                               timeout=min(timeout, remaining), check=True)
                output.seek(0)
                data = output.read(limit + 1)
            if len(data) > limit:
                raise ProbeFailure("probe_output_truncated")
            return data.decode("utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            raise ProbeFailure("probe_timed_out")
        except (OSError, subprocess.SubprocessError):
            raise ProbeFailure("probe_failed")


def tty_name(value):
    return value[5:] if value.startswith("/dev/") else value


def age_seconds(value):
    try:
        days, clock = value.split("-", 1) if "-" in value else ("0", value)
        parts = [int(part) for part in clock.split(":")]
        if not days.isdigit() or len(parts) not in {2, 3} or any(part < 0 for part in parts):
            return None
        if len(parts) == 2:
            parts.insert(0, 0)
        return int(days) * 86400 + parts[0] * 3600 + parts[1] * 60 + parts[2]
    except ValueError:
        return None


def parse_processes(text):
    rows, errors = {}, []
    for line in text.splitlines():
        if not line.strip():
            continue
        fields = line.split(None, 9)
        if len(fields) != 10 or not fields[0].isdigit() or not fields[1].isdigit():
            errors.append("process_row_unparsed")
            continue
        if len(rows) >= MAX_PROCESSES:
            errors.append("process_inventory_truncated")
            break
        pid = int(fields[0])
        started_raw = " ".join(fields[4:9])
        start_errors = []
        try:
            started = datetime.strptime(started_raw, "%a %b %d %H:%M:%S %Y").astimezone(timezone.utc).isoformat()
        except ValueError:
            started = started_raw
            start_errors.append("process_start_unparsed")
        rows[pid] = {"pid": pid, "ppid": int(fields[1]), "tty": tty_name(fields[2]),
                     "age_seconds": age_seconds(fields[3]), "started_at": started, "errors": start_errors,
                     "executable": Path(fields[9]).name, "executable_path": fields[9]}
    return rows, sorted(set(errors))


def ancestors(pid, processes):
    visited = set()
    while pid in processes and pid not in visited:
        visited.add(pid)
        yield pid
        pid = processes[pid]["ppid"]


def parse_panes(text):
    panes, errors = [], []
    for line in text.splitlines():
        fields = line.split("\t", 8)
        if (len(fields) != 9 or not re.fullmatch(r"%\d+", fields[0]) or
                not fields[1].isdigit() or not re.fullmatch(r"@\d+", fields[4])):
            errors.append("tmux_row_unparsed")
            continue
        if len(panes) >= MAX_PANES:
            errors.append("tmux_panes_truncated")
            break
        panes.append({"pane": fields[0], "pid": int(fields[1]), "tty": tty_name(fields[2]),
                      "tmux_session": fields[3], "window": fields[5], "tmux_window_id": fields[4],
                      "pane_index": fields[6], "command": fields[7], "cwd": fields[8]})
    return panes, sorted(set(errors))


def session_id_evidence(agent, tokens):
    """Return (launch ID, understood grammar); launch ID is not restart proof."""
    value_flags = {"--model", "--effort", "--agent", "--project", "--add-dir", "--mode",
                   "--config", "-c", "--profile", "-p", "--cd", "-C", "-m",
                   "--ask-for-approval", "-a", "--enable", "--disable", "--local-provider",
                   "--permission-mode"}
    boolean_flags = {"--no-daemon", "--no-alt-screen", "--full-auto", "--search", "--oss",
                     "--dangerously-skip-permissions", "--dangerously-bypass-approvals-and-sandbox",
                     "--disable-slash-commands", "--remote-control", "--verbose"}
    if agent == "agy":
        boolean_flags.add("--sandbox")
    elif agent == "codex":
        value_flags.update({"--sandbox", "-s"})
    prompt_flags = {"--prompt", "--print", "--prompt-interactive", "-i", "--system-prompt", "--append-system-prompt"}
    if agent in {"agy", "claude"}:
        prompt_flags.add("-p")
    identity_changing = {"--fork-session", "--fork", "--session-id", "--new-session", "--new", "--continue", "--last"}
    if agent == "agy":
        identity_changing.add("-c")
    ids = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.split("=", 1)[0] in identity_changing:
            return None, False
        # ps renders argv as flat text, without preserving argument boundaries.
        # A prompt/config/model/path value can therefore resemble later options.
        # Never parse beyond any value-bearing option or retain an earlier ID.
        if token.split("=", 1)[0] in prompt_flags | value_flags:
            return None, False
        if agent == "codex" and token == "resume":
            # A single positional prompt "resume UUID" renders identically.
            return None, False
        id_flag = "--conversation" if agent == "agy" else "--resume" if agent == "claude" else None
        if id_flag and (token == id_flag or agent == "claude" and token == "-r"):
            # Split flag/value forms also lack evidence of argv boundaries.
            return None, False
        if id_flag and token.startswith(id_flag + "="):
            value = token.split("=", 1)[1]
            if UUID.fullmatch(value):
                ids.append(value.lower())
            else:
                return None, False
            index += 1
            continue
        if token in boolean_flags:
            index += 1
            continue
        # An unknown option can change identity even after a recognized ID.
        # Unknown positional syntax is also deliberately insufficient evidence.
        return None, False
    distinct = set(ids)
    return (next(iter(distinct)), True) if len(distinct) == 1 else (None, not distinct)


def explicit_session_id(agent, tokens):
    return session_id_evidence(agent, tokens)[0]


def argv_identity(text, executable):
    # ps text is not shell syntax: interpreting literal quotes or backslashes
    # would manufacture option tokens that were never present in argv.
    tokens = text.split()
    ambiguous = any(character in text for character in ("'", '"', "\\"))
    if not tokens:
        return {"agent": None, "session_id": None, "service": False, "argv_parsed": False}
    offset = 0
    wrapper = executable in RUNTIMES
    if wrapper:
        if len(tokens) < 2:
            return {"agent": None, "session_id": None, "service": False}
        offset = 1
    name = Path(tokens[offset]).name
    aliases = {"codex-work": "codex", "codex-personal": "codex",
               "claude-work": "claude", "claude-personal": "claude"}
    agent = aliases.get(name, name)
    if agent not in AGENTS:
        if wrapper and name == "cli.js" and "/claude-code/" in tokens[offset]:
            agent = "claude"
        elif wrapper and name == "codex.js" and "/@openai/codex/" in tokens[offset]:
            agent = "codex"
        elif executable in AGENTS:
            agent = executable
        else:
            return {"agent": None, "session_id": None, "service": False}
    rest = tokens[offset + 1:]
    service = bool(rest and rest[0] in {"app-server", "app-server-daemon", "remote-control", "mic-serve", "mcp"})
    session_id, inheritable = (None, False) if ambiguous else session_id_evidence(agent, rest)
    return {"agent": agent, "session_id": session_id, "service": service,
            "session_id_inheritable": inheritable}


def parse_arguments(text, processes, requested):
    result = {}
    for line in text.splitlines():
        fields = line.strip().split(None, 1)
        if len(fields) == 2 and fields[0].isdigit():
            pid = int(fields[0])
            if pid in requested and pid in processes:
                result[pid] = argv_identity(fields[1], processes[pid]["executable"])
    return result


def runtime_candidates(processes, panes, tabs):
    """Positive UI metadata narrows argv probes; titles never establish identity."""
    hinted_ttys = {tab["tty"] for tab in tabs
                   if AGENT_TITLE.search(tab.get("title", "")) or
                   set(tab.get("process_names", [])) & (AGENTS | NODE_RUNTIMES)}
    candidates = {pid for pid, row in processes.items()
                  if row["executable"] in RUNTIMES and row["tty"] in hinted_ttys}
    for pane in panes:
        if pane["command"] in AGENTS | NODE_RUNTIMES:
            for pid, row in processes.items():
                if (row["executable"] in RUNTIMES and row["tty"] == pane["tty"] and
                        pane["pid"] in ancestors(pid, processes)):
                    candidates.add(pid)
    return candidates


def candidate_pids(processes, panes, tabs=()):
    native = {pid for pid, row in processes.items() if row["executable"] in AGENTS}
    requested = set(native)
    for pid in native:
        for ancestor in ancestors(pid, processes):
            if processes[ancestor]["executable"] in RUNTIMES:
                requested.add(ancestor)
    # Read only a bounded subset of positively hinted Terminal/tmux runtimes.
    additional = sorted(runtime_candidates(processes, panes, tabs) - requested)
    requested.update(additional[:MAX_RUNTIME_CANDIDATES])
    return requested


def logical_agents(processes, identities):
    candidates = {}
    for pid, process in processes.items():
        identity = identities.get(pid, {})
        kind = identity.get("agent") or (process["executable"] if process["executable"] in AGENTS else None)
        if kind and not identity.get("service"):
            candidates[pid] = {**process, "agent": kind, "session_id": identity.get("session_id"), "errors": list(process.get("errors", []))}
    wrappers = set()
    for pid, row in candidates.items():
        if row["executable"] not in AGENTS:
            continue
        inherited = []
        trusted = identities.get(pid, {}).get("session_id_inheritable", True)
        for ancestor in list(ancestors(pid, processes))[1:]:
            # A native parent is another logical session, even if it has its own
            # launcher further up the tree. Its identity cannot belong to us.
            if processes[ancestor]["executable"] in AGENTS:
                break
            candidate = candidates.get(ancestor)
            if candidate and candidate["agent"] != row["agent"]:
                break
            if candidate and candidate["agent"] == row["agent"] and candidate["executable"] in RUNTIMES:
                wrappers.add(ancestor)
                trusted = trusted and identities.get(ancestor, {}).get("session_id_inheritable", True)
                if candidate.get("session_id"):
                    inherited.append(candidate["session_id"])
        ids = set(inherited + ([row["session_id"]] if row["session_id"] else []))
        if not trusted:
            row["session_id"] = None
        elif len(ids) == 1:
            row["session_id"] = next(iter(ids))
        elif len(ids) > 1:
            row["session_id"] = None
            row["errors"].append("session_id_ambiguous")
    return [row for pid, row in sorted(candidates.items()) if pid not in wrappers]


def parse_cwds(text):
    result, current = {}, None
    for line in text.splitlines():
        if line.startswith("p") and line[1:].isdigit():
            current = int(line[1:])
        elif line.startswith("n") and current is not None:
            result[current] = line[1:]
    return result


def pane_for_process(process, panes, processes):
    lineage = list(ancestors(process["pid"], processes))
    matches = [pane for pane in panes if pane["pid"] in lineage]
    return min(matches, key=lambda pane: lineage.index(pane["pid"])) if matches else None


def screen_tail(text):
    value = "\n".join(text.splitlines()[-SCREEN_LINES:])
    encoded = value.encode("utf-8")
    return encoded[-MAX_SCREEN_BYTES:].decode("utf-8", errors="replace"), len(encoded) > MAX_SCREEN_BYTES


def terminal_tabs(runner, processes, wanted_ttys):
    running = any(row["executable_path"].endswith("/Terminal.app/Contents/MacOS/Terminal") for row in processes.values())
    if not running:
        return [], ["terminal_not_running"]
    try:
        raw = runner.run(["/usr/bin/osascript", "-l", "JavaScript", "-e", TERMINAL_SCRIPT,
                          json.dumps(sorted(wanted_ttys))], timeout=10, limit=3 * 1024 * 1024)
        result = json.loads(raw)
        if not isinstance(result, dict) or not isinstance(result.get("tabs"), list):
            raise ValueError()
        tabs = result["tabs"][:MAX_TABS]
        errors = result.get("errors", [])
        allowed = {"terminal_not_running", "terminal_window_unreadable", "terminal_tabs_truncated", "terminal_tab_unreadable"}
        if not isinstance(errors, list) or any(error not in allowed for error in errors):
            raise ValueError()
        for tab in tabs:
            if (not isinstance(tab, dict) or type(tab.get("window_id")) is not int or
                    type(tab.get("tab")) is not int or not isinstance(tab.get("tty"), str) or
                    not isinstance(tab.get("screen"), str) or not isinstance(tab.get("title"), str)):
                raise ValueError()
            names = tab.get("process_names", [])
            if not isinstance(names, list) or any(not isinstance(name, str) for name in names):
                raise ValueError()
            tab["process_names"] = sorted(set(names) & (RUNTIMES | AGENTS))
            tab["screen"], truncated = screen_tail(tab["screen"])
            if truncated:
                tab.setdefault("errors", []).append("screen_bytes_truncated")
        if len(result["tabs"]) > MAX_TABS:
            errors.append("terminal_tabs_truncated")
        return tabs, sorted(set(errors))
    except (ProbeFailure, ValueError, TypeError, KeyError):
        return [], ["terminal_probe_failed"]


def read_health(path):
    try:
        if path.is_symlink() or not path.is_file():
            return {"report": None, "error": "health_report_missing_or_unsupported"}
        with path.open("rb") as stream:
            data = stream.read(MAX_HEALTH_BYTES + 1)
        if len(data) > MAX_HEALTH_BYTES:
            return {"report": None, "error": "health_report_oversized"}
        report = json.loads(data)
        if (not isinstance(report, dict) or report.get("schema_version") != 1 or
                report.get("status") not in {"ok", "warning", "critical", "unknown"} or
                not isinstance(report.get("host"), dict) or not isinstance(report.get("observed_at"), str)):
            raise ValueError()
        observed = datetime.fromisoformat(report["observed_at"].replace("Z", "+00:00"))
        if observed.tzinfo is None:
            raise ValueError()
        # Transport only the monitor schema fields needed by the renderer.
        fields = ("schema_version", "observed_at", "status", "host", "process_status", "attribution",
                  "offenders", "largest_processes", "errors")
        return {"report": {key: report[key] for key in fields if key in report}, "error": None}
    except (OSError, ValueError, TypeError, UnicodeDecodeError):
        return {"report": None, "error": "health_report_invalid"}


def collect(health_report=None, runner=None):
    start = time.monotonic()
    runner = runner or Runner()
    result = {"schema_version": 1, "observed_at": datetime.now(timezone.utc).isoformat(),
              "duration_ms": 0, "sessions": [], "health": {"report": None, "error": None},
              "errors": [], "coverage": {"processes": "unknown", "terminal": "unknown",
                                          "tmux": "unknown", "cwd": "unknown", "identity": "not_needed",
                                          "agent_discovery": "unknown",
                                          "screen_tail_lines": SCREEN_LINES,
                                          "argv": "exact agents, runtime ancestors, and bounded Terminal/tmux metadata candidates"}}
    try:
        processes, errors = parse_processes(runner.run(["/usr/bin/env", "LC_ALL=C", "/bin/ps", "-axo",
                                                        "pid=,ppid=,tty=,etime=,lstart=,comm="]))
        if not processes:
            errors.append("process_inventory_empty")
        result["errors"].extend(errors)
        result["coverage"]["processes"] = "unknown" if not processes else "partial" if errors else "complete"
    except ProbeFailure as error:
        processes = {}
        result["errors"].append("process_" + str(error))
    tmux = next((path for path in ("/opt/homebrew/bin/tmux", "/usr/local/bin/tmux", "/usr/bin/tmux")
                 if Path(path).is_file()), None) or shutil.which("tmux")
    panes = []
    if tmux:
        fmt = "#{pane_id}\t#{pane_pid}\t#{pane_tty}\t#{session_name}\t#{window_id}\t#{window_name}\t#{pane_index}\t#{pane_current_command}\t#{pane_current_path}"
        try:
            panes, errors = parse_panes(runner.run([tmux, "list-panes", "-a", "-F", fmt]))
            result["errors"].extend(errors)
            result["coverage"]["tmux"] = "partial" if errors else "complete"
        except ProbeFailure as error:
            result["errors"].append("tmux_" + str(error))
    else:
        result["coverage"]["tmux"] = "unavailable"
        result["errors"].append("tmux_executable_unavailable")
    # Metadata first: a standalone Node CLI need not have a native child or a
    # tmux pane. Only positively hinted tab runtimes receive an argv probe.
    tabs, terminal_errors = terminal_tabs(runner, processes, set())
    requested = candidate_pids(processes, panes, tabs)
    if runtime_candidates(processes, panes, tabs) - requested:
        result["errors"].append("runtime_candidates_truncated")
    identities = {}
    if requested:
        try:
            text = runner.run(["/bin/ps", "-p", ",".join(map(str, sorted(requested))), "-o", "pid=,args="], limit=1024 * 1024)
            identities = parse_arguments(text, processes, requested)
            parsed = {pid for pid, identity in identities.items() if identity.get("argv_parsed", True)}
            result["coverage"]["identity"] = "complete" if requested <= parsed else "partial"
            if result["coverage"]["identity"] == "partial":
                result["errors"].append("agent_identity_partial")
        except ProbeFailure as error:
            result["errors"].append("agent_identity_" + str(error))
            result["coverage"]["identity"] = "unknown"
    parsed_identities = {pid for pid, identity in identities.items() if identity.get("argv_parsed", True)}
    unresolved_runtimes = {pid for pid, row in processes.items()
                           if row["executable"] in NODE_RUNTIMES and pid not in parsed_identities}
    # Node processes without positive UI metadata are deliberately not inspected
    # globally. Their presence prevents a claim of complete agent discovery.
    discovery_incomplete = (bool(unresolved_runtimes) or bool(requested - parsed_identities) or
                            result["coverage"]["processes"] != "complete" or
                            "runtime_candidates_truncated" in result["errors"] or
                            bool(set(terminal_errors) - {"terminal_not_running"}))
    result["coverage"]["agent_discovery"] = "partial" if discovery_incomplete else "complete"
    if discovery_incomplete:
        result["errors"].append("agent_discovery_partial")
    agents = logical_agents(processes, identities)
    if len(agents) > MAX_SESSIONS:
        result["errors"].append("session_inventory_truncated")
        result["coverage"]["processes"] = "partial"
        agents = agents[:MAX_SESSIONS]
    cwds = {}
    if agents:
        try:
            text = runner.run(["/usr/sbin/lsof", "-a", "-p", ",".join(str(row["pid"]) for row in agents), "-d", "cwd", "-Fn"])
            cwds = parse_cwds(text)
            result["coverage"]["cwd"] = "complete" if all(row["pid"] in cwds for row in agents) else "partial"
        except ProbeFailure as error:
            result["errors"].append("cwd_" + str(error))
    else:
        result["coverage"]["cwd"] = "not_needed"
    attached = {row["pid"]: pane_for_process(row, panes, processes) for row in agents}
    wanted = {row["tty"] for row in agents if attached[row["pid"]] is None and row["tty"] not in {"??", "?", "-"}}
    if wanted:
        observed_tabs, errors = terminal_tabs(runner, processes, wanted)
        terminal_errors.extend(errors)
        if observed_tabs:
            tabs = observed_tabs
        else:
            for tab in tabs:
                if tab["tty"] in wanted:
                    tab.setdefault("errors", []).append("terminal_screen_unreadable")
    terminal_errors = sorted(set(terminal_errors))
    result["errors"].extend(terminal_errors)
    result["coverage"]["terminal"] = "complete" if not terminal_errors else "unavailable" if terminal_errors == ["terminal_not_running"] else "partial"
    pane_counts = Counter(pane["pane"] for pane in attached.values() if pane)
    tty_counts = Counter(row["tty"] for row in agents if attached[row["pid"]] is None)
    pane_screens = {}
    for pane in panes:
        if pane["pane"] not in pane_counts or pane_counts[pane["pane"]] > 1:
            continue
        try:
            pane_screens[pane["pane"]] = runner.run([tmux, "capture-pane", "-p", "-t", pane["pane"], "-S", "-80"], timeout=2, limit=MAX_SCREEN_BYTES * 4)
        except ProbeFailure as error:
            pane_screens[pane["pane"]] = None
            result["errors"].append("screen_" + str(error))
    for agent in agents:
        row = {key: agent[key] for key in ("agent", "pid", "ppid", "tty", "started_at", "age_seconds", "session_id")}
        row.update({"session_id_source": "argv" if row["session_id"] else None,
                    "cwd": cwds.get(row["pid"], ""), "screen": "", "title": "",
                    "target": {"transport": "process"}, "errors": list(agent["errors"])})
        if row["pid"] not in identities:
            row["errors"].append("argv_identity_unavailable")
        pane = attached[row["pid"]]
        if pane:
            row["target"] = {key: pane[key] for key in ("pane", "tmux_session", "window", "tmux_window_id", "pane_index")}
            row["target"]["transport"] = "tmux"
            if not row["cwd"]:
                row["cwd"] = pane["cwd"]
                row["errors"].append("cwd_from_pane")
            if pane_counts[pane["pane"]] > 1:
                row["errors"].append("screen_shared_between_agents")
            elif pane_screens.get(pane["pane"]) is None:
                row["errors"].append("screen_unavailable")
            else:
                row["screen"], truncated = screen_tail(pane_screens[pane["pane"]])
                if truncated:
                    row["errors"].append("screen_bytes_truncated")
        else:
            matches = [tab for tab in tabs if tab["tty"] == row["tty"]]
            if len(matches) == 1:
                tab = matches[0]
                row["target"] = {key: tab.get(key) for key in ("window_id", "tab", "selected", "visible", "minimized")}
                row["target"]["transport"] = "terminal"
                row["title"] = tab["title"]
                if tty_counts[row["tty"]] == 1:
                    row["screen"] = tab["screen"]
                    row["errors"].extend(tab.get("errors", []))
                else:
                    row["errors"].append("screen_shared_between_agents")
            else:
                row["errors"].append("screen_unavailable" if not matches else "terminal_target_ambiguous")
        if not row["cwd"]:
            row["errors"].append("cwd_unavailable")
        result["sessions"].append(row)
    # A known pane command without a matching OS agent remains explicit unknown
    # identity rather than disappearing from the inventory.
    for pane in panes:
        if pane["pane"] not in pane_counts and pane["command"] in AGENTS:
            if len(result["sessions"]) >= MAX_SESSIONS:
                result["errors"].append("session_inventory_truncated")
                result["coverage"]["processes"] = "partial"
                break
            result["sessions"].append({"agent": pane["command"], "pid": None, "ppid": None,
                "tty": pane["tty"], "started_at": None, "age_seconds": None, "cwd": pane["cwd"],
                "session_id": None, "session_id_source": None,
                "target": {"transport": "tmux", "pane": pane["pane"], "tmux_session": pane["tmux_session"],
                           "window": pane["window"], "tmux_window_id": pane["tmux_window_id"]},
                "screen": "", "title": "", "errors": ["agent_process_unverified"]})
    result["health"] = read_health(health_report or Path.home() / "Library" / "Application Support" / "XMechBay" / "port-pressure" / "state" / "latest.json")
    result["errors"] = sorted(set(result["errors"]))
    result["duration_ms"] = round((time.monotonic() - start) * 1000)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collect", action="store_true", help="collect one local snapshot")
    parser.add_argument("--health-report", type=Path, help="explicit private monitor report path")
    args = parser.parse_args(argv)
    print(json.dumps(collect(args.health_report)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
