#!/usr/bin/env python3
"""Interpret bounded agent screens and render compact per-host fleet tables.

Purpose: on-demand fleet session status feature; 2026-10-27.
Inputs: collected session dictionaries and normalized host reports.
Outputs: conservative screen evidence and Markdown; no probes or session input.
Python 3.9+ standard library. Screen claims are never promoted to validated work.
"""

import hashlib
import html
import json
import math
import re
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import PurePath


GOAL = re.compile(r"Pursuing goal|Goal paused|Goal stalled|Goal achieved|Goal hit usage limits")
MODEL = re.compile(r"(?:GPT-[\w.\-]+|gpt-[\w.\-]+|Gemini\s+[\w.]+(?:\s+[\w.\-]+)?|Claude\s+(?:Opus|Sonnet|Fable)[\w .\-]*|(?:Opus|Sonnet|Fable)\s*[\d.]*)")
GENERIC_MODEL = re.compile(r"^[A-Za-z][\w.\-]*(?:\s+[\w.\-]+){0,3}\s+(?:·\s*)?(?:low|medium|high|xhigh|ultra|max)(?:\s+fast)?(?=\s+·|\s+(?:Goal|Pursuing goal)\b|$)", re.I)
ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
COMPOSER = re.compile(r"^[>›❯»]\s*(.*)$")
DURATION = re.compile(r"\d+(?:\.\d+)?\s*(?:days?|hours?|minutes?|seconds?|[dhms])(?:\s*\d+(?:\.\d+)?\s*(?:days?|hours?|minutes?|seconds?|[dhms]))*", re.I)
PATH_TEXT = re.compile(r"(?<![\w:/])(?:~/|/|[A-Za-z]:\\)[^\s<>\[\]{}()'\";,]+")
EMAIL = re.compile(r"[\w.+\-]+@(?:[\w\-]+\.)+[A-Za-z]{2,}")
SECRET = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_\-]{20,})\b")
UUID = re.compile(r"\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b", re.I)
RULE = re.compile(r"^[\s─━═┄┈\-_|│┃┌┐└┘├┤┬┴┼╭╮╰╯]+$")
PLACEHOLDER = re.compile(r"(?:Ask Codex to do anything|Type a message|Ask anything|What would you like to do\?)", re.I)
SLASH_COMMAND = re.compile(r"/(?:quit|exit|resume|goal|btw|artifact|tasks|diff|model|help|context|status|continue)$")
METRIC = re.compile(r"\b\d+(?:/\d+)?(?:\s+(?:completed|merged|open|reviewed|validated|passing|failed))*\s+(?:records?|PRs?|batches|goals?|cycles?|tests?|reviews?|Mechs?|repositories)\b", re.I)
OPTIONAL_ABSENCE = {"tmux_executable_unavailable", "terminal_not_running"}
KNOWN_ERRORS = {"ssh_probe_unavailable", "ssh_probe_failed", "ssh_probe_timed_out", "snapshot_invalid",
                "snapshot_read_failed", "invalid_error_metadata", "process_inventory_empty",
                "process_rows_invalid", "process_inventory_partial", "session_inventory_truncated",
                "terminal_window_unreadable", "terminal_tab_unreadable", "terminal_screen_unreadable",
                "terminal_tabs_truncated", "terminal_probe_failed", "terminal_not_running",
                "tmux_executable_unavailable", "agent_identity_partial", "agent_discovery_partial", "pane_rows_invalid"}


def clean(value):
    text = ANSI.sub("", str(value or ""))
    return "".join(char for char in text if char in "\n\t" or
                   (ord(char) >= 32 and unicodedata.category(char) != "Cf"))


def private_text(value, limit=92):
    text = clean(value)
    text = SECRET.sub("[secret]", text)
    text = EMAIL.sub("[account]", text)
    text = re.sub(r"https?://\S+", "[link]", text)
    text = PATH_TEXT.sub(lambda match: match.group() if SLASH_COMMAND.fullmatch(match.group()) else "[path]", text)
    text = UUID.sub("[id]", text)
    text = re.sub(r"\s+", " ", text).strip(" │┃")
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def cell(value):
    text = html.escape(clean(value).replace("\n", " "), quote=False)
    for character in ("\\", "|", "`", "[", "]", "*", "_"):
        text = text.replace(character, "\\" + character)
    return text


def _lines(screen):
    return [line.strip(" \t│┃") for line in clean(screen)[-50000:].splitlines()[-160:]
            if line.strip()]


def _is_footer(line, allow_generic=False):
    match = MODEL.search(line)
    return bool(allow_generic and GENERIC_MODEL.match(line) or match and ("·" in line or "for shortcuts" in line
                or line.startswith(("[WORK]", "[PERSONAL]"))
                or (match.start() == 0 and re.search(r"\b(?:low|medium|high|xhigh|ultra|max)\b", line, re.I))))


def _model(footer):
    match = MODEL.search(footer) or GENERIC_MODEL.match(footer)
    if not match:
        return None, None
    text = footer[match.start():]
    text = re.split(r"\s+·\s+(?!(?:low|medium|high|xhigh|ultra|max)\b)|\s+(?:Pursuing goal|Goal |\d+% context)", text, maxsplit=1)[0]
    text = re.sub(r"\s*·\s*", " ", text).strip()
    text = private_text(text, 60)
    short = re.sub(r"^(?:GPT-\d+(?:\.\d+)?-|gpt-\d+(?:\.\d+)?-|Gemini\s+\d+(?:\.\d+)?\s+|Claude\s+)", "", text)
    for full, abbreviation in (("ultra", "U"), ("xhigh", "XH"), ("high", "H"),
                               ("medium", "M"), ("low", "L"), ("max", "MAX")):
        short = re.sub(r"\b" + full + r"\b", abbreviation, short, flags=re.I)
    return text, short[:36]


def _composer(lines, footer_index):
    indices = [i for i, line in enumerate(lines) if COMPOSER.match(line)]
    if not indices:
        return None, False, False
    index = indices[-1]
    content = COMPOSER.match(lines[index]).group(1).strip(" │▏")
    placeholder = bool(PLACEHOLDER.fullmatch(content))
    end = footer_index if footer_index is not None and footer_index > index else len(lines)
    remainder = [line for line in lines[index + 1:end]
                 if not RULE.fullmatch(line) and not line.startswith(("? for shortcuts", "← for agents"))]
    empty = (not content or placeholder) and not remainder
    drafted = bool(content and not placeholder and not remainder)
    return index, empty, drafted


def _prompt(lines, composer_index, empty):
    tail = "\n".join(lines[-20:])
    # A subagent permission overlay can precede the main agent's empty composer.
    if re.search(r"ctrl\+k\s+(?:to\s+)?approve|(?:^|\n)(?:self|subagent) needs approval\b", tail, re.I):
        return "APPROVAL", ["subagent-approval"]
    if re.search(r"⇧← to answer|to navigate questions|to submit answer", tail, re.I):
        return "QUESTION", []
    # Earlier user/assistant turns are history when a later empty composer exists.
    scope = lines[composer_index + 1:] if composer_index is not None else lines[-20:]
    if not empty and composer_index is None:
        scope = lines[-20:]
    text = "\n".join(scope)
    for state, expression in (
        ("SIGN-IN", r"Sign in with (?:ChatGPT|your)|authentication required|Log in to continue"),
        ("TRUST-PROMPT", r"Trust this folder|Do you trust (?:the|this)|trust the (?:files|contents)"),
        ("UPDATE-PROMPT", r"Update available|Skip until next version|Update now"),
        ("APPROVAL", r"Requesting permission|Allow sandbox bypass|Confirm the command is safe|"
                     r"Would you like to (?:run|make the following edits|grant these permissions)|"
                     r"Run this command\?|Allow command|approval request"),
        ("QUESTION", r"Queued follow-up|How's the CLI experience|Switch to (?:gpt-|claude-|gemini-)|"
                     r"Type your answer|(?:^|\n)\s*(?:Question|Questions)\s*[:(]|"
                     r"(?:^|\n)\s*\?\s+(?:Which|.*question)|"
                     r"(?:^|\n)\s*(?:Select|Choose) (?:an? option|one of the following)"),
    ):
        if re.search(expression, text, re.I):
            return state, ["usage-limit"] if "Switch to " in text else []
    return None, []


def _activity(lines, composer_index, footer_index, status, current_input=True):
    if status == "APPROVAL" and re.search(r"ctrl\+k\s+approve|needs approval", "\n".join(lines[-20:]), re.I):
        return "subagent awaiting command approval"
    end = composer_index if composer_index is not None and current_input else (footer_index if footer_index is not None else len(lines))
    candidates = lines[:end]
    previous = [i for i, line in enumerate(candidates) if COMPOSER.match(line)]
    if previous:
        candidates = candidates[previous[-1] + 1:]
    # /goal detail panels repeat the objective and usage, rather than reporting
    # fresh work. Preserve the assistant's preceding result instead.
    for i, line in enumerate(candidates[:-2]):
        if line == "Goal" and candidates[i + 1].startswith("Status:") and candidates[i + 2].startswith("Objective:"):
            candidates = candidates[:i]
            break
    paragraphs = []
    for line in candidates[-40:]:
        bullet = bool(re.match(r"^[•●○⏺✻⡿⣯⣻⢿⣟]", line))
        line = re.sub(r"^[•●○⏺✻⡿⣯⣻⢿⣟└├┬╰]+\s*", "", line)
        if (RULE.fullmatch(line) or _is_footer(line) or COMPOSER.match(line)
                or re.match(r"(?:Error ID:|Resume with|agy --conversation|\? for shortcuts|"
                            r"ctrl\+|alt\+|esc to |Verbosity|summaries\.|┃|\[\d+\]:|Worked for\b|"
                            r"\d+ artifacts?\s*·|\d+ more lines\b|Tip:|Commands:|Usage:|"
                            r"← for agents|Keyboard shortcuts:)", line, re.I)
                or re.match(r"\+?\s*\d+ lines?\s*\([^)]*to expand\)", line)
                or re.match(r"(?:```|[\{\[]|[\}\]]+[,.]?\s*$|[\"'][\w.\-]+[\"']\s*:)", line)
                or UUID.fullmatch(line.strip(".,\"'"))
                or re.fullmatch(r"\S+\.(?:json|jsonl|yaml|yml|md|csv|tsv)[,.]?", line, re.I)
                or re.search(r"\b(?:GCP Project|Signed in as|account:)\b", line, re.I)
                or re.fullmatch(r"(?:Pursuing goal|Goal [\w ]+|Working)\s*\([^)]*\)", line)):
            continue
        thought = re.search(r"Thought for [^(]+\(([^)]+)\)", line)
        if thought:
            line = thought.group(1)
        line = re.sub(r"\s*\(ctrl\+o to expand\)\s*", "", line)
        line = line.replace("│", " ")
        if not bullet and paragraphs and not re.search(r"[.!?;]$", paragraphs[-1]):
            paragraphs[-1] = (paragraphs[-1] + " " + line)[:400]
        else:
            paragraphs.append(line)
    candidates = []
    for index, paragraph in enumerate(paragraphs[-8:]):
        text = private_text(paragraph, 400)
        if (re.fullmatch(r"(?:true|false|null|json|yaml|md|[0-9a-f]{12,}|\[(?:path|id)\])[,.]?", text, re.I)
                or len(text.split()) < 3 and not METRIC.search(text)
                and not re.match(r"(?:Report|Completed|Finished|Reviewed|Validated|Saved|Waiting|Running)\b", text, re.I)
                and not re.fullmatch(r"[A-Z][A-Z0-9_ -]{2,}", text)):
            continue
        metrics = METRIC.findall(text)
        score = 4 if metrics else 1
        if re.match(r"(?:⚠\s*)?(?:Waiting|Stopping|Blocked|Error|Connection failed|There was a network issue)\b", text, re.I):
            score = 6
        if len(metrics) >= 2:
            text = " · ".join(metrics)
        candidates.append((score, index, private_text(text, 80)))
    return max(candidates)[2] if candidates else None


def _timer(footer, status, working_status):
    is_goal = status in {"Pursuing goal", "Goal paused", "Goal stalled", "Goal achieved", "Goal hit usage limits"}
    text = footer if is_goal else working_status if status == "Working" else ""
    expression = GOAL.pattern if is_goal else r"Working|Running|Reconnecting"
    match = re.search(r"(?:" + expression + r")\s*\(([^)]*)\)", text, re.I)
    if not match:
        return None
    duration = DURATION.search(match.group(1))
    return {"kind": "goal" if is_goal else "turn", "text": duration.group(0).strip()} if duration else None


def _context(lines):
    for line in reversed(lines[-6:]):
        match = re.search(r"(\d+(?:\.\d+)?)%\s+context\s+(?:left|remaining)|"
                          r"context\s+(?:left|remaining)\s*:?\s*(\d+(?:\.\d+)?)%", line, re.I)
        if match:
            value = float(next(item for item in match.groups() if item is not None))
            if 0 <= value <= 100:
                return {"remaining_percent": int(value) if value.is_integer() else value, "source": "screen"}
    return None


def _goal_footer(lines, footer_index, composer_index):
    """Read goal state only from the current model footer or its wrapped status."""
    if footer_index is None or composer_index is not None and footer_index < composer_index:
        return ""
    footer = lines[footer_index]
    if GOAL.search(footer):
        return footer
    following = lines[footer_index + 1:footer_index + 4]
    dedicated = re.compile(r"(?:" + GOAL.pattern + r")(?:\s*\([^)]*\))?\s*$")
    for line in following:
        if dedicated.fullmatch(line):
            return line
    # Some narrow layouts break the status itself at the footer boundary.
    if following and re.search(r"\b(?:Goal|Pursuing)$", footer):
        suffix = re.search(r"\b(?:Goal|Pursuing)$", footer).group()
        wrapped = suffix + " " + following[0]
        if dedicated.fullmatch(wrapped):
            return wrapped
    return ""


def _working_status(lines, footer_index, composer_index):
    """Accept dedicated current UI status, never a phrase inside prior prose."""
    end = min(len(lines), footer_index + 4) if footer_index is not None else len(lines)
    start = composer_index + 1 if composer_index is not None else max(0, end - 6)
    candidates = lines[start:end]
    # Codex draws its interrupt/status line immediately above a blank composer.
    if composer_index is not None:
        preceding = [line for line in lines[max(0, composer_index - 3):composer_index]
                     if not RULE.fullmatch(line)]
        if preceding and re.search(r"\besc to interrupt\b", preceding[-1], re.I):
            candidates = [preceding[-1]] + candidates
    for line in reversed(candidates):
        text = re.sub(r"^[•●○⏺✻⡿⣯⣻⢿⣟]+\s*", "", line)
        if re.fullmatch(r"(?:Working|Running|Reconnecting)\s*\([^)]*\)(?:\s*[·•]\s*esc to interrupt)?", text, re.I):
            return text
        if re.fullmatch(r"\d+\s+task\(s\)\s+running", text, re.I):
            return text
    return ""


def analyze_session(session):
    """Interpret screen evidence only; PID/title/age never establish activity."""
    lines = _lines(session.get("screen", ""))
    composers = [i for i, line in enumerate(lines) if COMPOSER.match(line)]
    latest_composer = composers[-1] if composers else None
    footer_indices = [i for i, line in enumerate(lines)
                      if _is_footer(line, latest_composer is not None and i > latest_composer)]
    footer_index = footer_indices[-1] if footer_indices else None
    footer = lines[footer_index] if footer_index is not None else ""
    model, compact = _model(footer)
    index, empty, drafted = _composer(lines, footer_index)
    status, flags = _prompt(lines, index, empty)
    recent = "\n".join(lines[-6:])
    goal_footer = _goal_footer(lines, footer_index, index)
    working_status = _working_status(lines, footer_index, index)
    goal = GOAL.findall(goal_footer)
    task_present = bool(re.search(r"\b[1-9]\d*\s+task\(s\)", recent))
    background_task = bool(task_present and re.search(r"▸\s*\S", str(session.get("title", ""))))
    if status is None:
        if goal:
            status = goal[-1]
        elif background_task or working_status:
            status = "Working"
        elif empty and footer and not task_present and not re.search(r"\bGoal\b", footer):
            status = "idle"
        else:
            status = "UNKNOWN"
    if drafted and status == "UNKNOWN":
        flags.append("draft")
    if background_task:
        flags.append("background-task")
    elif task_present:
        flags.append("task-present")
    if not lines:
        flags.append("screen-unavailable")
    if re.search(r"Reconnecting|waiting for network|retrying in \d", recent, re.I) and status != "idle":
        flags.append("network-retry")
    activity = _activity(lines, index, footer_index, status, empty or drafted)
    if activity and re.search(r"network issue|Connection failed|could not connect|hit.*usage limit", activity, re.I):
        flags.append("last-error")
    cwd = str(session.get("cwd") or "").rstrip("/")
    home_only = re.fullmatch(r"/(?:Users|home)/[^/]+", cwd)
    repo = "(home)" if home_only else (PurePath(cwd).name if cwd else "unknown")
    context = _context(lines)
    stable_activity = DURATION.sub("<elapsed>", activity or "") if re.match(r"(?:Thinking|Working|Running|Reconnecting|Thought for)\b", activity or "", re.I) else activity
    evidence = {"status": status, "model": model, "activity": stable_activity,
                "context": context, "flags": sorted(flags)}
    fingerprint = hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()
    return {"status": status, "model": model, "model_compact": compact,
            "timer": _timer(goal_footer or footer, status, working_status), "activity": activity,
            "activity_source": "screen" if activity else None, "context": context,
            "evidence_fingerprint": fingerprint, "flags": flags,
            "repo_label": private_text(repo or "unknown", 40)}


def duration(seconds):
    if (not isinstance(seconds, (int, float)) or isinstance(seconds, bool)
            or not math.isfinite(seconds) or seconds < 0):
        return "?"
    seconds = int(seconds)
    if seconds < 60:
        return str(seconds) + "s"
    minutes = seconds // 60
    if minutes < 60:
        return str(minutes) + "m"
    hours = minutes // 60
    if hours < 48:
        return str(hours) + "h" + (str(minutes % 60) + "m" if minutes % 60 else "")
    return str(hours // 24) + "d" + (str(hours % 24) + "h" if hours % 24 else "")


def _target(row):
    target = row.get("target") or {}
    if target.get("transport") == "tmux" or target.get("pane"):
        label = str(target.get("pane") or "pane?")
    elif target.get("window_id") is not None:
        label = "T" + str(target["window_id"]) + "/" + str(target.get("tab", 1))
    else:
        label = "P" + str(row.get("pid") or "?")
    identity = row.get("session_id")
    if isinstance(identity, str) and UUID.fullmatch(identity):
        label += " launch#" + identity[:8]
    return label


STATE_DISPLAY = {"Pursuing goal": "🟢 goal", "Working": "🟢 work", "idle": "⚪ idle",
                 "Goal paused": "🟠 paused", "Goal stalled": "🔴 stalled", "Goal achieved": "🔵 achieved",
                 "Goal hit usage limits": "🔴 quota", "APPROVAL": "🟡 approve", "QUESTION": "🟡 question",
                 "SIGN-IN": "🟡 sign-in", "TRUST-PROMPT": "🟡 trust", "UPDATE-PROMPT": "🟡 update", "UNKNOWN": "⚫ unknown"}


def _state(row):
    text = STATE_DISPLAY.get(row.get("status"), STATE_DISPLAY["UNKNOWN"])
    timer = row.get("timer")
    if isinstance(timer, dict) and timer.get("text"):
        text += " · " + str(timer["text"])
    notes = {"network-retry": "retry", "draft": "draft", "last-error": "last error", "screen-unavailable": "no screen", "background-task": "task", "task-present": "task present"}
    for flag in row.get("flags", []):
        if flag in notes:
            text += " · " + notes[flag]
    return text


def _coverage(value, sessions):
    if isinstance(value, str):
        return private_text(value, 100)
    if isinstance(value, dict):
        read = value.get("screens_read")
        seen = value.get("sessions_seen", len(sessions))
        if isinstance(read, int):
            text = "screens " + str(read) + "/" + str(seen)
            parts = []
            for transport in ("terminal", "tmux"):
                state = value.get(transport)
                if state is not None:
                    parts.append(transport + " " + ("n/a" if state in {"unavailable", "not_needed"} else str(state)))
            return text + ("; " + ", ".join(parts) if parts else "")
    return "coverage partial" if any("screen-unavailable" in row.get("flags", []) for row in sessions) else "screens observed"


def _stamp(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (TypeError, ValueError):
        pass
    return private_text(value or "time unknown", 36)


def _error_codes(values):
    result = []
    for value in values if isinstance(values, list) else []:
        if not isinstance(value, str):
            result.append("collection_error")
            continue
        if value in OPTIONAL_ABSENCE:
            continue
        if (value in KNOWN_ERRORS or
                re.fullmatch(r"(?:process|tmux|cwd|screen|agent_identity)_(?:probe_failed|probe_timed_out|probe_unavailable|probe_size_limit|budget_exhausted)", value)):
            result.append(value)
        else:
            result.append("collection_error")
    return sorted(set(result))


def _state_counts(sessions):
    counts = Counter()
    for row in sessions:
        group = STATE_DISPLAY.get(row.get("status"), STATE_DISPLAY["UNKNOWN"]).split()[0]
        counts[group] += 1
    return " ".join(symbol + str(counts[symbol]) for symbol in ("🟢", "⚪", "🟡", "🟠", "🔴", "🔵", "⚫") if counts[symbol])


def render_host(report):
    """Render one host without collapsing duplicate repositories or sessions."""
    label = cell(report.get("label", "Host"))
    offline = report.get("source") == "snapshot"
    stale = bool(report.get("snapshot_stale"))
    provenance = "OFFLINE snapshot" if offline else "SSH observed"
    if stale:
        provenance += " · STALE, last observed"
    heading = "**" + label + " · " + cell(_stamp(report.get("observed_at"))) + " · " + provenance + "**"
    errors = _error_codes(report.get("errors", []))
    if not report.get("reachable", False):
        detail = " Codes: " + ", ".join("`" + code + "`" for code in errors[:5]) + "." if errors else ""
        return heading.replace("SSH observed", "UNREACHABLE") + "\n\nCollection unavailable; current session count and state are unknown." + detail + "\n"
    sessions = list(report.get("sessions") or [])
    coverage = report.get("coverage") or {}
    process_coverage = coverage.get("processes", "unknown") if isinstance(coverage, dict) else "unknown"
    discovery_coverage = coverage.get("agent_discovery", "unknown") if isinstance(coverage, dict) else "unknown"
    inventory_coverage = ("unknown" if process_coverage == "unknown" else
                          "complete" if process_coverage == discovery_coverage == "complete" else
                          "partial" if "partial" in {process_coverage, discovery_coverage} else "unknown")
    inventory = (str(len(sessions)) + " sessions" if inventory_coverage == "complete" else
                 str(len(sessions)) + " observed; inventory partial" if inventory_coverage == "partial" else
                 "count unknown (" + str(len(sessions)) + " observed)")
    summary = " · ".join(part for part in (inventory, _state_counts(sessions), _coverage(coverage, sessions)) if part)
    health = report.get("health") or {}
    ratio = health.get("occupancy_ratio")
    pressure = ("%.1f%%" % (100 * ratio) if isinstance(ratio, (int, float)) and not isinstance(ratio, bool) and 0 <= ratio <= 1 else "?")
    summary += " · TCP " + pressure + " " + str(health.get("status", "unknown"))
    summary += " · health age " + duration(health.get("age_seconds"))
    if health.get("process_status") in {"warning", "critical"}:
        summary += " · process maintenance " + health["process_status"]
    if errors:
        summary += " · collection partial (" + str(len(errors)) + " errors)"
    if report.get("cache_error"):
        summary += " · change history unavailable"
    output = [heading, "", cell(summary), "", "| Session · target | Agent / model | State · timer | Last reported / activity | Up · Δ | TCP L |",
              "|---|---|---|---|---|---:|"]
    sessions.sort(key=lambda row: (0 if "claw" in str(row.get("repo_label", "")).lower() else 1,
                                    str(row.get("repo_label", "")).lower(), _target(row), row.get("pid") or 0))
    for row in sessions:
        session = str(row.get("repo_label") or "unknown") + " · " + _target(row)
        agent = {"codex": "Cx", "agy": "Ag", "claude": "Cl"}.get(row.get("agent"), str(row.get("agent") or "?"))
        agent += " / " + str(row.get("model_compact") or "?")
        activity = "~" + row["activity"] if row.get("activity") else "—"
        context = row.get("context")
        if isinstance(context, dict) and "remaining_percent" in context:
            activity += " · ctx " + str(context["remaining_percent"]) + "% left"
        delta = row.get("delta") or {}
        change = {"first": "first", "changed": "Δnow", "unchanged": "=" + duration(delta.get("seconds"))}.get(delta.get("kind"), "Δ?")
        age = duration(row.get("age_seconds")) + " · " + change
        listeners = row.get("listeners") or {}
        count = listeners.get("count")
        sockets = str(count) if isinstance(count, int) and not isinstance(count, bool) and count >= 0 else "—"
        if listeners.get("stale"):
            sockets += " stale"
        if listeners.get("status") in {"warning", "critical"}:
            sockets += "!"
        output.append("| " + " | ".join(cell(value) for value in (session, agent, _state(row), activity, age, sockets)) + " |")
    if not sessions:
        missing = ("No agent sessions observed" if inventory_coverage == "complete" else
                   "Process inventory unavailable" if process_coverage == "unknown" else
                   "Agent discovery unavailable" if inventory_coverage == "unknown" else
                   "No sessions observed; inventory partial")
        output.append("| " + missing + " | — | — | Coverage above defines this observation | — | — |")
    attention_states = {"APPROVAL", "QUESTION", "SIGN-IN", "TRUST-PROMPT", "UPDATE-PROMPT", "UNKNOWN"}
    attention = [str(row.get("repo_label") or "unknown") + " " + _target(row) + ": " + _state(row)
                 for row in sessions if row.get("status") in attention_states]
    if attention:
        output.extend(["", ("Last observed attention: " if stale else "Attention: ") + cell("; ".join(attention[:5])) +
                       ("; +" + str(len(attention) - 5) + " more" if len(attention) > 5 else "") + "."])
    if errors:
        output.extend(["", "Collection codes: " + ", ".join("`" + code + "`" for code in errors[:5]) + ("; more in JSON" if len(errors) > 5 else "") + "."])
    output.extend(["", "🟢 active · ⚪ idle · 🟡 input needed · 🟠 paused · 🔴 stalled/quota · 🔵 achieved · ⚫ unknown.",
                   "", "Cx Codex · Ag AGY · Cl Claude; U ultra, XH extra-high, H high. ~ screen claim; Up process age; Δ observed change, not proven progress; L listeners. launch# is shortened argv evidence: reconfirm the current session ID before control. Full launch IDs are in JSON."])
    return "\n".join(output) + "\n"
