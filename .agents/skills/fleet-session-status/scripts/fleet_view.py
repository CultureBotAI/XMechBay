#!/usr/bin/env python3
"""Interpret bounded agent screens, classify fleet roles, and render tables/symbols.

Purpose: on-demand fleet session status and local HTML symbol dashboard; 2026-11-14.
Inputs: collected session dictionaries and normalized host reports.
Outputs: conservative screen evidence, role classification, symbol badges, and Markdown.
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
MENU_CHOICE = re.compile(
    r"^[>›❯»]\s*\d+[.)]\s+(?:Yes|No|Allow|Deny|Approve|Reject|Cancel|Skip|Update|Always|Once|Trust|Sign in|Log in|Continue|Proceed|Exit|Quit)\b",
    re.I,
)
MENU_NAV_HINT = re.compile(
    r"(?:Enter to (?:select|confirm)|↑/↓\s*(?:to\s+)?navigate|Navigate\s*·|Space to (?:select|toggle)|Esc to cancel)",
    re.I,
)
DURATION = re.compile(r"\d+(?:\.\d+)?\s*(?:days?|hours?|minutes?|seconds?|[dhms])(?:\s*\d+(?:\.\d+)?\s*(?:days?|hours?|minutes?|seconds?|[dhms]))*", re.I)
PATH_TEXT = re.compile(r"(?<![\w:/↑←→↓])(?:~/|/(?![↓→\s])|[A-Za-z]:\\)[^\s<>\[\]{}()'\";,]+")
EMAIL = re.compile(r"[\w.+\-]+@(?:[\w\-]+\.)+[A-Za-z]{2,}")
SECRET = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_\-]{20,})\b")
UUID = re.compile(r"\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b", re.I)
RULE = re.compile(r"^[\s─━═┄┈\-_|│┃┌┐└┘├┤┬┴┼╭╮╰╯]+$")
BOX_TOP_RULE = re.compile(
    r"^(?:[\s─━═┄┈\-_|│┃┌┐└┘├┤┬┴┼╭╮╰╯]+|[─━═]{2,}\s*(?:History\s+\d+/\d+|Draft|Prompt)\s*[─━═]{2,})$",
    re.I,
)
SPINNER_PREFIX = r"[•●○⏺✻✽✶✢✳◐◓◑◒◯◌\u2800-\u28ff]"
AGY_TASK_STRIP = re.compile(r"^[•●○]\s*\[\d{2}:\d{2}:\d{2}\]\s+.+\s+running$", re.I)
AGY_BRAILLE_ACTIVE = re.compile(r"^[\u2800-\u28ff]\s+\S")
CLAUDE_DONE_LINE = re.compile(
    r"^(?:[✻✽✶✢✳•●○⏺]+\s*)?(?:Worked|Churned|Baked|Cogitated|Cooked|Brewed|Saut[eé]ed|Crunched)\s+for\b",
    re.I,
)
CLAUDE_GOAL_ACTIVE = re.compile(r"^[◎◉●•]?\s*/goal\s+active(?:\s*\(([^)]*)\))?\s*$", re.I)
CLAUDE_ACTIVE_SPINNER = re.compile(
    r"^[✻✽✶✢✳◐◓◑◒\u2800-\u28ff]\s+[A-Z][A-Za-z0-9 _/\-]{1,48}(?:…|\.\.\.)(?:\s*\([^)]*\))?$"
)
CLAUDE_ACTIVE_SUBAGENT = re.compile(
    r"^[◯◌◐◓◑◒\u2800-\u28ff]\s+\S.+?\b\d+(?:\.\d+)?\s*(?:days?|hours?|minutes?|seconds?|[dhms])\b.*[↓↑]\s*[\d.]+k?\s*tokens\b",
    re.I,
)
INTERRUPTIBLE_STATUS = re.compile(
    r"^(?:[•●○⏺✻✽✶✢✳◐◓◑◒\u2800-\u28ff]+\s*)?[A-Z][^\n(]{1,72}"
    r"\((?:[^)]*[·•]\s*)?esc to (?:interrupt|cancel)(?:\s*[·•][^)]*)?\)",
    re.I,
)
PLACEHOLDER = re.compile(r"(?:Ask Codex to do anything|Type a message|Ask anything|What would you like to do\?)", re.I)
PASTED_CHIP = re.compile(r"\[(?:Pasted (?:text|content|image)\b|\d+\s+lines\s+pasted\b|Image #\d+)", re.I)
INTERRUPTED_LINE = re.compile(
    r"^(?:[⎿└├╰•●○⏺✻]\s*)*Interrupted(?:\s+by\s+user|\s*[·•—-]\s*What should\b|$)",
    re.I,
)
COURTESY_SIGNOFF = re.compile(
    r"\b(?:let me know if (?:you (?:need|have|want) any(?:thing| other| further)?|there(?:'s| is) anything else)|"
    r"feel free to (?:ask|let me know|reach out)|happy to (?:help|assist|elaborate)|hope (?:this|that) helps)\b",
    re.I,
)
UI_TRAILER_LINE = re.compile(
    r"^(?:[•●○⏺✻✽✶✢✳└├┬╰※✔]+\s*)?(?:(?:Worked|Churned|Baked|Cogitated|Cooked|Brewed|Saut[eé]ed|Crunched)\s+for\b|"
    r"\d+\s+artifacts?\s*·|\d+\s+background terminals?\b|Commands:|Usage:|Tip:|\? for shortcuts|"
    r"← for agents|Error ID:|Resume with\b|agy --conversation|\+?\s*\d+ lines?\s*\([^)]*to expand\)|"
    r"recap:|↳\s*recap:|new task\?\s*(?:/clear|\[path\])\b|Update installed\b|"
    r"↓\s*Back to bottom\b|enter/esc latest\b|"
    r"context\s+(?:left|remaining)|\d+(?:\.\d+)?%\s+context)",
    re.I,
)
EXPLICIT_AGENT_ACTION = re.compile(
    r"(?:^|\b)Yes/No Question\s*:|"
    r"(?:^|(?<=[.!?]\s))Approve\s+(?:merging|the\b|this\b|these\b|opening\b|creating\b|running\b|pushing\b|deleting\b|closing\b|applying\b|PR\b|#\d+)|"
    r"\b(?:Ready for|Awaiting)\s+(?:your\s+)?(?:merge\s+|explicit\s+)?approval\b|"
    r"\bDo you (?:approve|authorize|want|prefer|wish)\b|"
    r"\bWould you (?:like|prefer|want)\b|"
    r"\b(?:Shall|Should|May|Can) (?:I|we) (?:proceed|merge|open|create|push|commit|delete|remove|run|apply|send|continue|start|update|close|relaunch|branch|build|fix|implement|file|clean|drop|revert|deploy)\b|"
    r"\bReply\s+[\"'“”`]?(?:yes|no|go|approve|confirm|proceed|continue|ok|1|2|a|b)\b(?:\s+to\s+\w+|\s+if\b|\s+or\b|\s*[.,;:)—-]|$)|"
    r"\bsay\s+[\"'“”`][A-Za-z0-9 _\-]{2,32}[\"'“”`](?:\s+and\b|\s+to\b|\s+if\b|\s*[.,;:])|"
    r"\bPlease (?:confirm|approve|reply|choose|select|specify|indicate|tell me|advise|decide|authorize|run|re-run|authenticate|sign in|provide|paste)\b|"
    r"\b(?:Waiting|Paused|Blocked|Stopping)\s+(?:for|on|until)\s+your\s+(?:approval|confirmation|go-ahead|input|reply|response|decision|answer|authorization|choice|direction|instructions?)\b|"
    r"\b(?:Let me know|Tell me)\s+(?:whether|which|what|how|when|before I|if\s+(?:you|we|I)\b)|"
    r"\bChoose\s+(?:option|between|one of)\b",
    re.I,
)
SLASH_COMMAND = re.compile(
    r"/(?:quit|exit|resume|goal|btw|artifact|tasks|diff|model|help|context|status|continue|clear|compact|clean|ps|permissions|review)$"
)
METRIC = re.compile(r"\b\d+(?:/\d+)?(?:\s+(?:completed|merged|open|reviewed|validated|passing|failed))*\s+(?:records?|PRs?|batches|goals?|cycles?|tests?|reviews?|Mechs?|repositories)\b", re.I)
OPTIONAL_ABSENCE = {"tmux_executable_unavailable", "terminal_not_running"}
KNOWN_ERRORS = {
    "ssh_probe_unavailable", "ssh_probe_failed", "ssh_probe_timed_out",
    "local_probe_unavailable", "local_probe_failed", "local_probe_timed_out",
    "snapshot_invalid", "snapshot_read_failed", "snapshot_size_limit",
    "snapshot_json_invalid", "snapshot_schema_invalid", "snapshot_time_invalid",
    "snapshot_sessions_invalid", "snapshot_agent_invalid", "snapshot_process_invalid",
    "snapshot_target_invalid", "snapshot_screen_invalid", "host_observation_failed",
    "invalid_error_metadata", "process_inventory_empty", "process_rows_invalid",
    "process_row_unparsed", "process_inventory_partial", "process_inventory_truncated",
    "process_start_unparsed", "session_inventory_truncated", "runtime_candidates_truncated",
    "terminal_window_unreadable", "terminal_tab_unreadable", "terminal_screen_unreadable",
    "terminal_tabs_truncated", "terminal_probe_failed", "terminal_not_running",
    "terminal_target_ambiguous", "tmux_executable_unavailable", "tmux_row_unparsed",
    "tmux_panes_truncated", "agent_identity_partial", "agent_discovery_partial",
    "agent_process_unverified", "argv_identity_unavailable", "session_id_ambiguous",
    "pane_rows_invalid", "screen_unavailable", "screen_shared_between_agents",
    "screen_bytes_truncated", "cwd_unavailable", "cwd_from_pane",
}

CANONICAL_MECHS = {
    "aimech": "AIMech",
    "antibioticmech": "AntibioticMech",
    "assaymech": "AssayMech",
    "cellstructuremech": "CellStructureMech",
    "cmmmech": "CMMMech",
    "cofactormech": "CofactorMech",
    "communitymech": "CommunityMech",
    "culturemech": "CultureMech",
    "dufmech": "DUFMech",
    "edgemech": "EdgeMech",
    "habitatmech": "HabitatMech",
    "mediaingredientmech": "MediaIngredientMech",
    "naturalproductmech": "NaturalProductMech",
    "pathwaymech": "PathwayMech",
    "proteintraitsmech": "ProteinTraitsMech",
    "scaleupmech": "ScaleupMech",
    "taxonmech": "TaxonMech",
    "traitmech": "TraitMech",
}

MECH_SLUG_PREFIXES = (
    ("mediaingredientmech-", "MediaIngredientMech"),
    ("naturalproductmech-", "NaturalProductMech"),
    ("cellstructuremech-", "CellStructureMech"),
    ("proteintraitsmech-", "ProteinTraitsMech"),
    ("antibioticmech-", "AntibioticMech"),
    ("communitymech-", "CommunityMech"),
    ("cofactormech-", "CofactorMech"),
    ("culturemech-", "CultureMech"),
    ("habitatmech-", "HabitatMech"),
    ("pathwaymech-", "PathwayMech"),
    ("scaleupmech-", "ScaleupMech"),
    ("edgemech-", "EdgeMech"),
    ("taxonmech-", "TaxonMech"),
    ("traitmech-", "TraitMech"),
    ("assaymech-", "AssayMech"),
    ("dufmech-", "DUFMech"),
    ("cmmmech-", "CMMMech"),
    ("aimech-", "AIMech"),
    ("commmech-", "CommunityMech"),
    ("mim-", "MediaIngredientMech"),
    ("ptm-", "ProteinTraitsMech"),
    ("csm-", "CellStructureMech"),
    ("abm-", "AntibioticMech"),
    ("npm-", "NaturalProductMech"),
    ("cfm-", "CofactorMech"),
    ("pwm-", "PathwayMech"),
    ("txm-", "TaxonMech"),
    ("duf-", "DUFMech"),
    ("cmm-", "CMMMech"),
    ("aim-", "AIMech"),
    ("asm-", "AssayMech"),
    ("sum-", "ScaleupMech"),
    ("cm-", "CultureMech"),
    ("hm-", "HabitatMech"),
    ("tm-", "TraitMech"),
    ("em-", "EdgeMech"),
)

NON_FLEET_REPOS = {
    "dismech": "dismech",
    "kg-microbe": "kg-microbe",
    "protstruct_review": "protstruct_review",
    "culturebotai.github.io": "CultureBotAI.github.io",
}

CONTAINER_DIRS = {
    "/", "users", "home", "documents", "vimss", "ontology", "mechs", "kg-hub", "agent-work",
    "repos", "repositories", "projects", "work", "srv", "opt", "var", "private",
    "tmp", ".local", "worktrees", "fleet-boss", "boss-workers",
}

GENERIC_STREAM_LEAVES = {".local", "worktree", "repo", "checkout", "main"}
REPO_INTERNAL_SUBDIRS = {
    "worktrees", "worktree", ".local", "branches", "src", "scripts", "tests", "docs", "data",
}
GENERIC_TMUX_WINDOWS = {"worker", "main", "bash", "zsh", "sh", "fish", "tmux", "fleet", "mechs"}
GENERIC_CAPITAL_MECH = re.compile(r"^([A-Z][A-Za-z0-9]*Mech)(?:$|[-_].+)")


def clean(value):
    text = ANSI.sub("", str(value or ""))
    return "".join(char for char in text if char in "\n\t" or
                   (ord(char) >= 32 and unicodedata.category(char) not in {"Cf", "Cs"}))


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


def _normalize_overrides(overrides):
    result = {}
    if not isinstance(overrides, dict):
        return result
    for raw_key, spec in overrides.items():
        if not isinstance(raw_key, str) or not raw_key.strip():
            continue
        key = raw_key.strip().lower()
        if isinstance(spec, str) and spec in {"mechbay", "claw", "mech", "other"}:
            result[key] = (spec, raw_key.strip())
        elif isinstance(spec, dict) and spec.get("role") in {"mechbay", "claw", "mech", "other"}:
            project = spec.get("project")
            result[key] = (spec["role"], project.strip() if isinstance(project, str) and project.strip() else raw_key.strip())
    return result


def _match_segment_role(segment, overrides):
    text = str(segment or "").strip()
    if not text:
        return None
    low = text.lower()
    if low in overrides:
        role, project = overrides[low]
        return role, project, None
    if low in {"xmechbay", "mechbay"}:
        return "mechbay", "XMechBay" if low == "xmechbay" else "MechBay", None
    if low.startswith(("xmechbay-", "xmechbay_", "mechbay-", "mechbay_")):
        return "mechbay", "XMechBay", text
    if low in {"culturebotai-claw", "claw"}:
        return "claw", "culturebotai-claw", None
    if low == "sample-claw":
        return "claw", "sample-claw", None
    if low.startswith(("culturebotai-claw-", "culturebotai-claw_", "claw-", "claw_")):
        return "claw", "culturebotai-claw", text
    if low in NON_FLEET_REPOS:
        return "other", NON_FLEET_REPOS[low], None
    for prefix, canonical in NON_FLEET_REPOS.items():
        if low.startswith((prefix + "-", prefix + "_")):
            return "other", canonical, text
    if low in CANONICAL_MECHS:
        return "mech", CANONICAL_MECHS[low], None
    for prefix, canonical in MECH_SLUG_PREFIXES:
        if low.startswith(prefix) and len(low) > len(prefix):
            return "mech", canonical, text
    match = GENERIC_CAPITAL_MECH.match(text)
    if match:
        base = match.group(1)
        if base.lower() not in {"xmechbay", "mechbay", "dismech"}:
            return "mech", base, (text if text != base else None)
    return None


def _home_hint_role(session, overrides):
    title = clean(session.get("title") or "")
    lines = _lines(session.get("screen", ""))
    footer = ""
    for line in reversed(lines[-6:]):
        if _is_footer(line, True):
            footer = line
            break
    haystack = title + "\n" + footer
    if not haystack.strip():
        return None
    matches = []
    if re.search(r"\b(?:XMechBay|MechBay)\b", haystack, re.I):
        matches.append(("mechbay", "XMechBay"))
    if re.search(r"\bculturebotai-claw\b", haystack, re.I):
        matches.append(("claw", "culturebotai-claw"))
    for key, canonical in CANONICAL_MECHS.items():
        if re.search(r"\b" + re.escape(canonical) + r"\b", haystack, re.I):
            matches.append(("mech", canonical))
    for key, (role, project) in overrides.items():
        if len(key) >= 3 and re.search(r"\b" + re.escape(key) + r"\b", haystack, re.I):
            matches.append((role, project))
    unique = list(dict.fromkeys(matches))
    if len(unique) == 1:
        role, project = unique[0]
        return {"role": role, "project_label": private_text(project, 48), "stream_label": "(home)"}
    return None


def _stream_from_parts(raw_parts, idx, seg_stream):
    deeper = [item for item in raw_parts[idx + 1:] if item.lower() not in GENERIC_STREAM_LEAVES]
    if seg_stream and deeper:
        return seg_stream + "/" + deeper[-1]
    if seg_stream:
        return seg_stream
    if deeper:
        return deeper[-1]
    return None


def _is_kg_microbe_umbrella(raw_parts, idx, norm_overrides):
    if idx + 1 >= len(raw_parts) or raw_parts[idx].lower() != "kg-microbe":
        return False
    if idx > 0 and raw_parts[idx - 1].lower() == "kg-microbe":
        return False
    if idx > 0 and raw_parts[idx - 1].lower() == "kg-hub":
        return True
    next_low = raw_parts[idx + 1].lower()
    if next_low in REPO_INTERNAL_SUBDIRS:
        return False
    if _match_segment_role(raw_parts[idx + 1], norm_overrides) is not None:
        return True
    if next_low in {"mechs", "repos", "repositories", "projects"} and idx + 2 < len(raw_parts):
        return _match_segment_role(raw_parts[idx + 2], norm_overrides) is not None
    return False


def classify_session_role(session, overrides=None):
    """Classify a raw or normalized session into mechbay | claw | mech | other."""
    norm_overrides = _normalize_overrides(overrides)
    cwd = clean(session.get("cwd") or "").rstrip("/")
    repo_label = clean(session.get("repo_label") or "").strip()
    home_only = bool(re.fullmatch(r"/(?:Users|home)/[^/]+", cwd)) or (not cwd and repo_label == "(home)")
    raw_parts = []
    if cwd and not home_only:
        home_prefix = re.match(r"^/(?:Users|home)/[^/]+(?:/|$)", cwd)
        rel_cwd = cwd[home_prefix.end():] if home_prefix else cwd.lstrip("/")
        raw_parts = [part for part in PurePath(rel_cwd).parts if part and part not in {"/", "."}]
    # Single shallowest-to-deepest pass: skip generic container directories and
    # KG-Microbe umbrella folders so enclosing repositories (including configured
    # role overrides) win before deeper branch/worktree names.
    for idx, part in enumerate(raw_parts):
        low = part.lower()
        if low in CONTAINER_DIRS:
            continue
        if _is_kg_microbe_umbrella(raw_parts, idx, norm_overrides):
            continue
        matched = _match_segment_role(part, norm_overrides)
        if matched is not None:
            role, project, seg_stream = matched
            stream = _stream_from_parts(raw_parts, idx, seg_stream)
            return {
                "role": role,
                "project_label": private_text(project, 48),
                "stream_label": private_text(stream, 48) if stream else None,
            }
    target = session.get("target") or {}
    candidates = []
    window = clean(target.get("window") or "").strip()
    if window and window.lower() not in GENERIC_TMUX_WINDOWS and not window.isdigit():
        candidates.append(window)
    tmux_primary = clean(target.get("tmux_session") or "").strip()
    if tmux_primary:
        candidates.append(tmux_primary)
    for member in target.get("tmux_sessions") or ():
        member_text = clean(member).strip()
        if member_text.lower().startswith("xmb-view-"):
            candidates.append(member_text[9:])
        elif member_text:
            candidates.append(member_text)
    for candidate in candidates:
        matched = _match_segment_role(candidate, norm_overrides)
        if matched is not None:
            role, project, seg_stream = matched
            stream = seg_stream
            if not stream and window and window.lower() not in GENERIC_TMUX_WINDOWS and window.lower() != project.lower():
                stream = window
            return {
                "role": role,
                "project_label": private_text(project, 48),
                "stream_label": private_text(stream, 48) if stream else None,
            }
    if repo_label and repo_label not in {"(home)", "unknown", "worktree"}:
        matched = _match_segment_role(repo_label, norm_overrides)
        if matched is not None:
            role, project, seg_stream = matched
            return {
                "role": role,
                "project_label": private_text(project, 48),
                "stream_label": private_text(seg_stream, 48) if seg_stream else None,
            }
    if home_only:
        hinted = _home_hint_role(session, norm_overrides)
        if hinted is not None:
            return hinted
        return {"role": "other", "project_label": "(home)", "stream_label": None}
    fallback_parts = [part for part in raw_parts if part.lower() not in CONTAINER_DIRS and part.lower() not in GENERIC_STREAM_LEAVES]
    fallback = fallback_parts[-1] if fallback_parts else (raw_parts[-1] if raw_parts else (repo_label or "unknown"))
    if fallback.lower() in norm_overrides:
        role, project = norm_overrides[fallback.lower()]
        return {"role": role, "project_label": private_text(project, 48), "stream_label": None}
    return {"role": "other", "project_label": private_text(fallback or "unknown", 48), "stream_label": None}


def mech_color_key(project_label, role):
    """Map canonical Mech or role name to CSS accent token suffix."""
    low = str(project_label or "").strip().lower()
    if low in CANONICAL_MECHS:
        return low
    if role == "mechbay":
        return "xmechbay"
    if role == "claw":
        return "claw"
    if role == "mech":
        return "generic-mech"
    return "other"


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


def _is_menu_choice(line, lines=None, idx=None):
    if MENU_CHOICE.match(line):
        return True
    if lines is None or idx is None or not COMPOSER.match(line):
        return False
    after = []
    for ln in lines[idx + 1:min(len(lines), idx + 18)]:
        if _is_footer(ln, True) or re.match(r"^[>›❯»]\s*(?:$|\D)", ln):
            break
        after.append(ln)
    before = lines[max(0, idx - 3):idx]
    if re.match(r"^[>›❯»]\s*\d+[.)]\s+\S", line):
        if any(MENU_NAV_HINT.search(ln) for ln in after):
            return True
        neighbors = before[-2:] + after[:6]
        if (any(re.match(r"^\d+[.)]\s+\S", nb) for nb in neighbors)
                and any(re.search(r"(?:^\?\s+|\?\s*$|\b(?:Select|Choose|Which|Question|Questions|Trust|Allow|Update available|Sign in)\b)", b, re.I) for b in before)):
            return True
    if re.match(r"^[❯›]\s+\S", line) and not PLACEHOLDER.search(line):
        for ln in after[:6]:
            if RULE.fullmatch(ln) or _is_footer(ln, True):
                break
            if MENU_NAV_HINT.search(ln):
                return True
    return False


def _is_composer_line(line, lines=None, idx=None):
    return bool(COMPOSER.match(line)) and not _is_menu_choice(line, lines, idx)


def _composer_details(lines, footer_index):
    indices = [i for i, line in enumerate(lines) if _is_composer_line(line, lines, i)]
    if not indices:
        return None, False, False, None, False
    index = indices[-1]
    content = COMPOSER.match(lines[index]).group(1).strip(" │▏")
    placeholder = bool(PLACEHOLDER.fullmatch(content))
    end = footer_index if footer_index is not None and footer_index > index else len(lines)
    raw_slice = lines[index + 1:end]
    remainder = [
        line for line in raw_slice
        if not RULE.fullmatch(line)
        and not line.startswith(("? for shortcuts", "← for agents", "enter/esc latest"))
        and not AGY_TASK_STRIP.match(line)
    ]
    empty = (not content or placeholder) and not remainder
    drafted = False
    box_lines = []
    if content and not placeholder:
        if not remainder:
            drafted = True
        elif footer_index is not None and footer_index > index:
            has_opening_rule = index > 0 and bool(BOX_TOP_RULE.fullmatch(lines[index - 1]))
            after_rule = []
            seen_rule = False
            for line in raw_slice:
                if line.startswith(("? for shortcuts", "← for agents", "enter/esc latest")) or AGY_TASK_STRIP.match(line):
                    continue
                if RULE.fullmatch(line):
                    seen_rule = True
                    continue
                if seen_rule:
                    after_rule.append(line)
                else:
                    box_lines.append(line)
            framed_box = has_opening_rule and seen_rule
            codex_box = (not seen_rule) and lines[index][:1] in {"›", "»"} and len(box_lines) <= 12
            if (box_lines and not after_rule and (framed_box or codex_box)
                    and not any(re.match(r"^" + SPINNER_PREFIX + r"|^[└├┬╰┃⎿※]", ln) for ln in box_lines)
                    and not any(re.match(r"(?:(?:Worked|Churned|Baked|Cogitated|Cooked|Brewed|Saut[eé]ed|Crunched)\s+for\b|Goal\b|Commands:|Usage:|Tip:)", ln, re.I) for ln in box_lines)):
                drafted = True
    composer_raw = None
    is_pasted = False
    if drafted:
        parts = [content] + [ln.strip(" │▏") for ln in box_lines if ln.strip(" │▏")]
        composer_raw = " ".join(parts).strip()
        is_pasted = bool(PASTED_CHIP.search(composer_raw) or box_lines or len(composer_raw) >= 80)
    return index, empty, drafted, composer_raw, is_pasted


def _composer(lines, footer_index):
    return _composer_details(lines, footer_index)[:3]


def _approval_status(lines, composer_index):
    """Detect dedicated active approval-wait status line immediately above composer."""
    end = composer_index if composer_index is not None else len(lines)
    window = [ln for ln in lines[max(0, end - 10):end] if not BOX_TOP_RULE.fullmatch(ln)]
    if not window:
        return ""
    idx = len(window) - 1
    while idx >= 0:
        ln = window[idx]
        if re.match(r"^└\s", ln) or re.match(r"^\+?\s*\d+\s+lines?\s*\([^)]*to expand\)", ln, re.I):
            idx -= 1
            continue
        # Skip wrapped continuation lines belonging to an immediately preceding `└` sub-block.
        if not re.match(r"^" + SPINNER_PREFIX + r"|^[>›❯»]", ln) and any(
            re.match(r"^└\s", prev) for prev in window[max(0, idx - 4):idx]
        ):
            idx -= 1
            continue
        text = re.sub(r"^" + SPINNER_PREFIX + r"+\s*", "", ln).strip()
        if re.match(r"Reviewing (?:\d+\s+)?approval requests?\s*\([^)]*esc to interrupt[^)]*\)", text, re.I):
            return text
        break
    return ""


def _prompt(lines, composer_index, empty):
    if composer_index is not None:
        before = lines[:composer_index]
        prev_composers = [i for i, ln in enumerate(before) if _is_composer_line(ln, before, i)]
        after_prev = before[prev_composers[-1] + 1:] if prev_composers else before
        preceding = [ln for ln in after_prev if not RULE.fullmatch(ln)][-8:]
        overlay_text = "\n".join(preceding + lines[composer_index + 1:])
    else:
        overlay_text = "\n".join(lines[-20:])
    # A subagent permission overlay can precede the main agent's empty composer.
    if re.search(r"(?:^|\n)\s*(?:ctrl\+k\s+(?:to\s+)?approve\b|(?:self|subagent) needs approval\b)", overlay_text, re.I):
        return "APPROVAL", ["subagent-approval"]
    if re.search(r"(?:^|\n)\s*(?:⇧← to answer\b|(?:←/→|↑/↓|Tab)\s+to navigate questions\b|Enter to submit answer\b)", overlay_text, re.I):
        return "QUESTION", []
    if _approval_status(lines, composer_index):
        return "APPROVAL", ["approval-review"]
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
                     r"Do you want to (?:proceed|make this edit|create|run|allow)\b|"
                     r"Allow (?:command|creation|deletion|modification|execution|access)\b|"
                     r"Run this command\?|Navigate\s*·\s*f\s+full diff|approval request|"
                     r"(?:^|\n)\s*[>›❯»]?\s*\d+[.)]\s+(?:Yes,\s*(?:allow|proceed|and don't ask)|No,\s*(?:deny|and tell))\b"),
        ("QUESTION", r"Queued follow-up|How's the CLI experience|Switch to (?:gpt-|claude-|gemini-)|"
                     r"Type your answer|Enter to select\s*·\s*↑/↓\s*(?:to\s+)?navigate|"
                     r"(?:^|\n)\s*\d+[.)]\s+(?:Type something\.|Chat about this)\b|"
                     r"(?:^|\n)\s*(?:Question|Questions)\s*[:(]|"
                     r"(?:^|\n)\s*\?\s+(?:Which|.*question)|"
                     r"(?:^|\n)\s*(?:Select|Choose) (?:an? option|one of the following)"),
    ):
        if re.search(expression, text, re.I):
            return state, ["usage-limit"] if "Switch to " in text else []
    return None, []


def _keystroke_hint(status, flags, lines, composer_index):
    """Derive concise required user keystrokes from the active TUI prompt."""
    flag_set = set(flags or ())
    if status not in {"APPROVAL", "QUESTION", "SIGN-IN", "TRUST-PROMPT", "UPDATE-PROMPT"} and "subagent-approval" not in flag_set:
        return None
    prompt_scope = "\n".join(lines[max(0, composer_index - 8):] if composer_index is not None else lines[-20:])
    active_scope = "\n".join(lines[composer_index + 1:] if composer_index is not None else lines[-20:])
    if "subagent-approval" in flag_set:
        return "ctrl+k approve · alt+j manage" if re.search(r"\balt\+j\b", prompt_scope, re.I) else "ctrl+k approve"
    if "approval-review" in flag_set:
        return "Approve in TUI · esc interrupts"
    if re.search(r"⇧← to answer", prompt_scope, re.I):
        return "⇧← focus question · ↑/↓ + Enter"
    if re.search(r"\[1\]\s+\w+.*\[0\]\s+Skip", active_scope, re.I):
        return "0 skip · 1–3 rate CLI"
    nums = [int(m) for m in re.findall(r"(?:^|\n)\s*[>›❯»]?\s*(\d+)[.)]\s+\S", active_scope) if 1 <= int(m) <= 9]
    if nums:
        max_n = max(nums)
        if status == "APPROVAL":
            if re.search(r"\bf\s+full diff\b", active_scope, re.I):
                return "1 or Enter allow · 2 deny · f diff · ↑/↓"
            if max_n == 4:
                return "1 allow once · 2–3 always-allow · 4/esc cancel"
            if max_n >= 3 or re.search(r"don't ask again", active_scope, re.I):
                return "1 allow once · 2 session-allow · 3/esc deny"
            return "1 or Enter allow · 2 deny · ↑/↓"
        if status == "QUESTION":
            return ("1–" + str(max_n) + " or ↑/↓ + Enter") if max_n >= 2 else "1 or Enter confirm"
        if status == "TRUST-PROMPT":
            return "1 or Enter trust · 2/esc cancel"
        if status == "UPDATE-PROMPT":
            return ("1–" + str(max_n) + " or ↑/↓ + Enter") if max_n >= 2 else "Enter confirm · esc skip"
        return ("1–" + str(max_n) + " or ↑/↓ + Enter") if max_n >= 2 else "1 or Enter confirm"
    if MENU_NAV_HINT.search(active_scope):
        if re.search(r"Space to (?:select|toggle)", active_scope, re.I):
            return "↑/↓ move · Space toggle · Enter submit"
        return "↑/↓ select · Enter confirm"
    if re.search(r"Type your answer", active_scope, re.I):
        return "Type answer + Enter"
    fallbacks = {
        "APPROVAL": "y / Enter approve · n / esc deny",
        "QUESTION": "↑/↓ + Enter or type answer",
        "SIGN-IN": "Enter / complete browser sign-in",
        "TRUST-PROMPT": "Enter trust folder · esc cancel",
        "UPDATE-PROMPT": "Enter update or skip",
    }
    return fallbacks.get(status, "Inspect terminal to respond")


def _extract_trailing_question_or_action(paragraph_text):
    text = private_text(paragraph_text, 360)
    if not text or COURTESY_SIGNOFF.search(text) or INTERRUPTED_LINE.search(text):
        return None
    sentences = [seg.strip() for seg in re.split(r"(?<=[.!?])\s+", text) if seg.strip()]
    tail_sent = sentences[-1] if sentences else text
    if COURTESY_SIGNOFF.search(tail_sent):
        return None
    if EXPLICIT_AGENT_ACTION.search(tail_sent):
        return private_text(tail_sent, 110)
    for sent in reversed(sentences[:-1]):
        if not COURTESY_SIGNOFF.search(sent) and EXPLICIT_AGENT_ACTION.search(sent):
            return private_text(sent, 110)
    if EXPLICIT_AGENT_ACTION.search(text):
        return private_text(text, 110)
    if re.search(r"\?[\s\"'”’)*_`]*$", tail_sent):
        words = tail_sent.split()
        if len(words) >= 3 or re.match(r"^(?:Approve|Proceed|Continue|Merge|Ready|Confirm)\b", tail_sent, re.I):
            return private_text(tail_sent, 110)
    return None


def _agent_end_message(lines, composer_index):
    """Detect plain-text assistant message at end of response awaiting user action."""
    if composer_index is None or composer_index <= 0:
        return None
    before = lines[:composer_index]
    prev_composers = [i for i, ln in enumerate(before) if _is_composer_line(ln, before, i)]
    if prev_composers:
        prev = prev_composers[-1]
        turn = list(before[prev + 1:])
        # If the previous user prompt was framed by RULE lines, advance past its closing RULE
        # so a multi-line user question with empty/interrupted output never spoofs assistant prose.
        if prev > 0 and BOX_TOP_RULE.fullmatch(before[prev - 1]):
            for j, ln in enumerate(turn[:14]):
                if RULE.fullmatch(ln):
                    turn = turn[j + 1:]
                    break
    else:
        turn = list(before[-24:])
    # Strip multi-line auto-generated recap trailers following turn-completion timing lines.
    scan_start = max(0, len(turn) - 12)
    for k in range(scan_start, len(turn) - 1):
        if CLAUDE_DONE_LINE.match(turn[k]) and re.match(r"^(?:※\s*recap:|↳\s*recap:)", turn[k + 1], re.I):
            turn = turn[:k]
            break
    while turn:
        last = turn[-1]
        if BOX_TOP_RULE.fullmatch(last) or _is_footer(last) or UI_TRAILER_LINE.match(last):
            turn.pop()
            continue
        break
    if not turn:
        return None
    if any(INTERRUPTED_LINE.match(ln) for ln in turn[-3:]):
        return None
    last_raw = turn[-1]
    if re.match(r"^(?:```|~~~|[\}\]]+[,.]?\s*$|\|.*\|$)", last_raw):
        return None
    # Reconstruct final paragraph (up to 4 wrapped prose lines).
    para_lines = []
    for ln in reversed(turn[-6:]):
        if RULE.fullmatch(ln) or re.match(r"^(?:```|~~~|#+\s|\|.*\|$)", ln):
            break
        is_bullet = bool(re.match(r"^(?:[•●○⏺✻\-*]|\d+[.)])\s+", ln))
        cleaned = re.sub(r"^(?:[•●○⏺✻\-*]+)\s*", "", ln).strip()
        if not cleaned:
            break
        para_lines.append(cleaned)
        if is_bullet or len(para_lines) >= 4:
            break
    if para_lines:
        para = " ".join(reversed(para_lines))
        direct = _extract_trailing_question_or_action(para)
        if direct:
            return direct
    # Shape B: user-directed question/confirmation immediately followed by 1-5 short option/detail bullets.
    list_count = 0
    idx = len(turn) - 1
    while idx >= 0 and list_count < 5:
        ln = turn[idx]
        if re.match(r"^(?:[-*•●]|\d+[.)])\s+\S", ln) and len(ln) <= 140:
            list_count += 1
            idx -= 1
        else:
            break
    if 1 <= list_count <= 5 and idx >= 0:
        lead_lines = []
        for j in range(idx, max(-1, idx - 3), -1):
            ln = turn[j]
            if RULE.fullmatch(ln) or re.match(r"^(?:```|~~~|#+\s)", ln):
                break
            is_b = bool(re.match(r"^[•●○⏺✻\-*]\s+", ln))
            lead_lines.append(re.sub(r"^[•●○⏺✻\-*]+\s*", "", ln).strip())
            if is_b:
                break
        lead = " ".join(reversed(lead_lines)).strip()
        if (lead and not re.match(r"^(?:Why|How did|What happened)\b", lead, re.I)
                and re.search(r"\b(?:you|your|approve|authorize|confirm|proceed|shall I|should I|should we|may I|can I|which)\b", lead, re.I)):
            extracted = _extract_trailing_question_or_action(lead)
            if extracted:
                return extracted
    return None


def _activity(lines, composer_index, footer_index, status, current_input=True):
    if status == "APPROVAL" and re.search(r"ctrl\+k\s+approve|needs approval", "\n".join(lines[-20:]), re.I):
        return "subagent awaiting command approval"
    end = composer_index if composer_index is not None and current_input else (footer_index if footer_index is not None else len(lines))
    candidates = lines[:end]
    previous = [i for i, line in enumerate(candidates) if _is_composer_line(line, candidates, i)]
    if previous:
        candidates = candidates[previous[-1] + 1:]
    # /goal detail panels repeat the objective and usage, rather than reporting
    # fresh work. Preserve the assistant's preceding result instead.
    for i, line in enumerate(candidates[:-2]):
        if line == "Goal" and candidates[i + 1].startswith("Status:") and candidates[i + 2].startswith("Objective:"):
            candidates = candidates[:i]
            break
    is_human_prompt = status in {"APPROVAL", "QUESTION", "SIGN-IN", "TRUST-PROMPT", "UPDATE-PROMPT"}
    paragraphs = []
    tail_candidates = candidates[-40:]
    for rel_idx, line in enumerate(tail_candidates):
        is_choice = _is_menu_choice(line, tail_candidates, rel_idx)
        if is_choice:
            continue
        raw_line = line
        bullet = bool(re.match(r"^" + SPINNER_PREFIX, line))
        line = re.sub(r"^(?:" + SPINNER_PREFIX + r"|[└├┬╰✔])+\s*", "", line)
        if (BOX_TOP_RULE.fullmatch(raw_line) or RULE.fullmatch(line) or _is_footer(line) or MENU_NAV_HINT.search(line)
                or _is_composer_line(raw_line, tail_candidates, rel_idx)
                or (is_human_prompt and re.match(r"^\d+[.)]\s+\S", line))
                or re.match(r"^[✻✽✶✢✳◐◓◑◒]\s+[A-Za-z\-]+(?:…|\.\.\.)", raw_line)
                or CLAUDE_GOAL_ACTIVE.match(raw_line)
                or line == "(ctrl+b to run in background)"
                or re.match(r"(?:Error ID:|Resume with|agy --conversation|\? for shortcuts|"
                            r"ctrl\+|alt\+|esc to |Verbosity|summaries\.|┃|\[\d+\]:|"
                            r"(?:Worked|Churned|Baked|Cogitated|Cooked|Brewed|Saut[eé]ed|Crunched)\s+for\b|"
                            r"new task\?\s*(?:/clear|\[path\])\b|Update installed\b|↓\s*Back to bottom\b|"
                            r"\d+ artifacts?\s*·|\d+ more lines\b|Tip:|Commands:|Usage:|"
                            r"← for agents|Keyboard shortcuts:)", line, re.I)
                or re.match(r"\+?\s*\d+ lines?\s*\([^)]*to expand\)", line)
                or re.match(r"(?:```|[\{\[]|[\}\]]+[,.]?\s*$|[\"'][\w.\-]+[\"']\s*:)", line)
                or UUID.fullmatch(line.strip(".,\"'"))
                or re.fullmatch(r"\S+\.(?:json|jsonl|yaml|yml|md|csv|tsv)[,.]?", line, re.I)
                or re.search(r"\b(?:GCP Project|Signed in as|account:)\b", line, re.I)
                or re.fullmatch(r"(?:Pursuing goal|Goal [\w ]+|Working)\s*\([^)]*\)(?:\s*[·•]\s*esc to (?:interrupt|cancel))?", line, re.I)):
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
                and not re.match(r"(?:Report|Completed|Finished|Reviewed|Validated|Saved|Waiting|Running|Reading|Thinking|Exploring|Editing|Searching|Writing|Checking|Testing|Analyzing)\b", text, re.I)
                and not re.fullmatch(r"[A-Z][A-Z0-9_ -]{2,}", text)):
            continue
        metrics = METRIC.findall(text)
        score = 4 if metrics else 1
        if is_human_prompt and (
            text.endswith(("?", ":"))
            or re.match(r"(?:\?\s+|Allow\b|Confirm\b|Requesting\b|Reviewing\b|Would you\b|Do you\b|Select\b|Choose\b|Which\b|Trust\b|Sign in\b|Update available\b)", text, re.I)
        ):
            score = max(score, 5)
        if re.match(r"(?:⚠\s*)?(?:Waiting|Stopping|Blocked|Error|Connection failed|There was a network issue)\b", text, re.I):
            score = 6
        if len(metrics) >= 2:
            text = " · ".join(metrics)
        candidates.append((score, index, private_text(text, 80)))
    return max(candidates)[2] if candidates else None


def _timer(footer, status, working_status, approval_status=""):
    is_goal = status in {"Pursuing goal", "Goal paused", "Goal stalled", "Goal achieved", "Goal hit usage limits"}
    if status == "APPROVAL" and approval_status:
        match = re.search(r"Reviewing (?:\d+\s+)?approval requests?\s*\(([^)]*)\)", approval_status, re.I)
        if match:
            duration_match = DURATION.search(match.group(1))
            if duration_match:
                return {"kind": "prompt", "text": duration_match.group(0).strip()}
    if is_goal:
        match = re.search(r"(?:" + GOAL.pattern + r")\s*\(([^)]*)\)", footer, re.I)
        if not match:
            return None
        duration_match = DURATION.search(match.group(1))
        return {"kind": "goal", "text": duration_match.group(0).strip()} if duration_match else None
    if status == "Working" and working_status:
        legacy = re.search(r"^(?:Working|Running|Reconnecting)\s*\(([^)]*)\)", working_status, re.I)
        if legacy:
            inner = legacy.group(1).strip()
            if DURATION.fullmatch(inner) or re.search(r"\besc to (?:interrupt|cancel)\b|\btokens\b", inner, re.I):
                duration_match = DURATION.search(inner)
                if duration_match:
                    return {"kind": "turn", "text": duration_match.group(0).strip()}
        for inner in re.findall(r"\(([^)]*)\)", working_status):
            if re.search(r"\besc to (?:interrupt|cancel)\b|\btokens\b", inner, re.I):
                duration_match = DURATION.search(inner)
                if duration_match:
                    return {"kind": "turn", "text": duration_match.group(0).strip()}
        ellip = re.search(r"(?:…|\.\.\.)\s*\(([^)]*)\)$", working_status)
        if ellip and DURATION.fullmatch(ellip.group(1).strip()):
            return {"kind": "turn", "text": ellip.group(1).strip()}
        sub_dur = re.search(
            r"\b(" + DURATION.pattern + r")\s*[·•]\s*[↓↑]\s*[\d.]+k?\s*tokens\b",
            working_status,
            re.I,
        )
        if sub_dur:
            return {"kind": "turn", "text": sub_dur.group(1).strip()}
    return None


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
    if composer_index is not None and composer_index > 0:
        for ln in reversed(lines[max(0, composer_index - 4):composer_index]):
            if BOX_TOP_RULE.fullmatch(ln):
                continue
            claude_goal = CLAUDE_GOAL_ACTIVE.match(ln)
            if claude_goal:
                inner = (claude_goal.group(1) or "").strip()
                return "Pursuing goal (" + inner + ")" if inner else "Pursuing goal"
            break
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
    end = min(len(lines), footer_index + 8) if footer_index is not None else len(lines)
    start = composer_index + 1 if composer_index is not None else max(0, end - 6)
    below_candidates = lines[start:end]

    # Inspect the active status slot immediately above the composer, skipping only
    # box rules and subordinate tool/tip trailers (never past completed prose bullets).
    above_candidates = []
    if composer_index is not None and composer_index > 0:
        scan_floor = max(0, composer_index - 6)
        for idx in range(composer_index - 1, scan_floor - 1, -1):
            ln = lines[idx]
            if BOX_TOP_RULE.fullmatch(ln) or CLAUDE_GOAL_ACTIVE.match(ln):
                continue
            if (re.match(r"^└\s", ln)
                    or re.match(r"^\+?\s*\d+\s+lines?\s*\([^)]*to expand\)", ln, re.I)
                    or re.match(r"^(?:[•●○]\s*)?(?:Commands:|Usage:|Tip:)", ln, re.I)
                    or UUID.fullmatch(ln.strip())):
                continue
            if ln == "(ctrl+b to run in background)":
                above_candidates.append(ln)
                continue
            above_candidates.append(ln)
            break

    for line in above_candidates:
        stripped = re.sub(r"^" + SPINNER_PREFIX + r"+\s*", "", line).strip()
        if re.match(r"Reviewing (?:\d+\s+)?approval requests?\b", stripped, re.I):
            continue
        if INTERRUPTIBLE_STATUS.match(line):
            return stripped
        if CLAUDE_ACTIVE_SPINNER.match(line) and not CLAUDE_DONE_LINE.match(line) and " done " not in (" " + stripped.lower() + " "):
            return stripped
        if AGY_BRAILLE_ACTIVE.match(line):
            return stripped
        if re.match(r"^[•●○⏺✻\u2800-\u28ff]+\s*Running\s*\(.+\)$", line):
            return stripped
        if re.match(r"^(?:[✻✽✶✢✳⏺•●○\u2800-\u28ff]+\s*)?Waiting for \d+ background agents? to finish\b", line, re.I):
            return stripped
        if line == "(ctrl+b to run in background)":
            return "Running foreground command (ctrl+b)"

    for line in reversed(below_candidates):
        text = re.sub(r"^" + SPINNER_PREFIX + r"+\s*", "", line).strip()
        if re.fullmatch(r"(?:Working|Running|Reconnecting)\s*\([^)]*\)(?:\s*[·•]\s*esc to interrupt)?", text, re.I):
            return text
        if INTERRUPTIBLE_STATUS.match(line) and not re.match(r"Reviewing (?:\d+\s+)?approval requests?\b", text, re.I):
            return text
        if re.fullmatch(r"\d+\s+task\(s\)\s+running", text, re.I):
            return text
        if CLAUDE_ACTIVE_SUBAGENT.match(line):
            return text

    if (footer_index is not None
            and (composer_index is None or footer_index > composer_index)
            and re.match(r"^esc to cancel\s+", lines[footer_index], re.I)):
        return "Working (esc to cancel)"
    return ""


def analyze_session(session, role_overrides=None):
    """Interpret screen evidence only; PID/title/age never establish activity."""
    lines = _lines(session.get("screen", ""))
    composers = [i for i, line in enumerate(lines) if _is_composer_line(line, lines, i)]
    latest_composer = composers[-1] if composers else None
    footer_indices = [i for i, line in enumerate(lines)
                      if _is_footer(line, latest_composer is not None and i > latest_composer)]
    footer_index = footer_indices[-1] if footer_indices else None
    footer = lines[footer_index] if footer_index is not None else ""
    model, compact = _model(footer)
    index, empty, drafted, composer_raw, is_pasted = _composer_details(lines, footer_index)
    approval_status = _approval_status(lines, index)
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
    composer_text = None
    if drafted and status in {"UNKNOWN", "Goal achieved", "Goal paused", "Goal stalled", "Goal hit usage limits"}:
        flags.append("draft")
        if is_pasted:
            flags.append("pasted-prompt")
        if composer_raw:
            composer_text = private_text(composer_raw, 110)
    keystroke_hint = _keystroke_hint(status, flags, lines, index)
    agent_kind = session.get("agent")
    skip_perm = session.get("skip_permissions")
    if agent_kind == "agy" and status == "APPROVAL":
        if skip_perm is False:
            agy_work_fixable = True
            agy_work_status = "fixable"
            flags.append("agy-work-fixable")
        elif skip_perm is True:
            agy_work_fixable = False
            agy_work_status = "already_enabled"
            flags.append("agy-work-active")
        else:
            agy_work_fixable = False
            agy_work_status = "unverified"
    else:
        agy_work_fixable = False
        agy_work_status = "not_applicable"
    agent_message = None
    if status == "idle" and empty and not drafted and index is not None:
        agent_message = _agent_end_message(lines, index)
        if agent_message:
            flags.append("agent-message")
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
    stable_activity = DURATION.sub("<elapsed>", activity or "") if re.match(r"(?:Thinking|Working|Running|Reconnecting|Thought for|Reviewing (?:\d+\s+)?approval requests?)\b", activity or "", re.I) else activity
    evidence = {"status": status, "model": model, "activity": stable_activity,
                "context": context, "flags": sorted(flags)}
    if composer_text is not None:
        evidence["composer_text"] = composer_text
    if agent_message is not None:
        evidence["agent_message"] = agent_message
    fingerprint = hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()
    role_info = classify_session_role(session, overrides=role_overrides)
    return {"status": status, "model": model, "model_compact": compact,
            "timer": _timer(goal_footer or footer, status, working_status, approval_status), "activity": activity,
            "activity_source": "screen" if activity else None, "context": context,
            "composer_text": composer_text, "keystroke_hint": keystroke_hint,
            "agent_message": agent_message,
            "agy_work_fixable": agy_work_fixable, "agy_work_status": agy_work_status,
            "evidence_fingerprint": fingerprint, "flags": flags,
            "repo_label": private_text(repo or "unknown", 40),
            "role": role_info["role"],
            "project_label": role_info["project_label"],
            "stream_label": role_info["stream_label"]}


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


def target_label(row):
    return _target(row)


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

STATE_SYMBOL_SPEC = {
    "APPROVAL": ("yellow", "🟡", "!", "approve", 10, "Awaiting command/tool approval"),
    "QUESTION": ("yellow", "🟡", "?", "question", 12, "Awaiting answer to an agent question"),
    "SIGN-IN": ("yellow", "🟡", "🔑", "sign-in", 14, "Sign-in or authentication required"),
    "TRUST-PROMPT": ("yellow", "🟡", "🛡", "trust", 15, "Workspace trust confirmation pending"),
    "UPDATE-PROMPT": ("yellow", "🟡", "↑", "update", 16, "CLI update prompt blocking input"),
    "Goal hit usage limits": ("red", "🔴", "⊘", "quota", 20, "Goal stopped at provider usage limit"),
    "Goal stalled": ("red", "🔴", "✖", "stalled", 22, "Goal reported stalled"),
    "Goal paused": ("orange", "🟠", "⏸", "paused", 30, "Goal paused"),
    "UNKNOWN": ("slate", "⚫", "?", "unknown", 35, "State unknown from current screen evidence"),
    "Working": ("green", "🟢", "▶", "work", 45, "Actively executing a turn or task"),
    "Pursuing goal": ("green", "🟢", "◎", "goal", 50, "Pursuing autonomous goal"),
    "Goal achieved": ("blue", "🔵", "✓", "done", 60, "Goal reported achieved (unverified screen claim)"),
    "idle": ("white", "⚪", "○", "idle", 70, "Idle at empty composer"),
}


_M1_HOST_RE = re.compile(r"(?:^|[^a-z0-9])m1(?:\s*(?:pro|max|ultra))?(?:$|[^a-z0-9])")
_M5_HOST_RE = re.compile(r"(?:^|[^a-z0-9])m5(?:\s*(?:pro|max|ultra))?(?:$|[^a-z0-9])")
_RESERVED_HOST_GLYPHS = frozenset({"M1", "M5", "IN"})


def is_m1_host(host_key, host_label=""):
    key_low = str(host_key or "").strip().lower()
    label_low = str(host_label or "").strip().lower()
    combined = key_low + " " + label_low
    return bool(_M1_HOST_RE.search(combined))


def host_symbol(host_key, host_label="", stale=False):
    key_low = str(host_key or "").strip().lower()
    label_low = str(host_label or "").strip().lower()
    combined = key_low + " " + label_low
    if _M1_HOST_RE.search(combined):
        tone, dot, glyph, rank = "purple", "🟣", "M1", 10
    elif _M5_HOST_RE.search(combined):
        tone, dot, glyph, rank = "blue", "🔷", "M5", 20
    elif "intel" in combined:
        tone, dot, glyph, rank = "orange", "🔶", "IN", 30
    else:
        parts = [part for part in re.split(r"[^A-Za-z0-9]+", str(host_key or "H")) if part]
        if len(parts) >= 2 and len(parts[-1]) == 1:
            glyph = (parts[0][0] + parts[-1]).upper()
            if glyph in _RESERVED_HOST_GLYPHS:
                glyph = (parts[0][:2] + parts[-1]).upper()
        elif parts:
            glyph = parts[0][:3].upper()
        else:
            glyph = "MAC"
        if glyph in _RESERVED_HOST_GLYPHS:
            glyph = glyph + "H"
        tone, dot, rank = "teal", "⚪", 40
    short = glyph + ("◌" if stale else "")
    detail = str(host_label or host_key or "Host")
    if stale:
        detail += " (cached observation; host currently unreachable or stale)"
    return {"tone": tone, "dot": dot, "glyph": glyph, "short": short, "detail": detail, "rank": rank}


def session_symbols(row, host_key="", host_label="", host_stale=False):
    """Build the 8 colored-symbol descriptors for dashboard table columns."""
    stale = bool(host_stale or row.get("stale_cached"))
    mac = host_symbol(host_key, host_label, stale=stale)

    agent = str(row.get("agent") or "?")
    compact = str(row.get("model_compact") or "").strip()
    tier_match = re.search(r"\b(MAX|XH|U|H|M|L)\b", compact)
    tier = tier_match.group(1) if tier_match else ""
    if agent == "codex":
        cli_tone, cli_dot, cli_glyph, cli_name, cli_rank = "blue", "🔵", "Cx", "Codex", 10
    elif agent == "agy":
        cli_tone, cli_dot, cli_glyph, cli_name, cli_rank = "purple", "🟣", "Ag", "Antigravity", 20
    elif agent == "claude":
        cli_tone, cli_dot, cli_glyph, cli_name, cli_rank = "orange", "🟠", "Cl", "Claude Code", 30
    else:
        cli_tone, cli_dot, cli_glyph, cli_name, cli_rank = "slate", "⚫", "?", agent, 40
    cli_short = cli_glyph + ("·" + tier if tier else "")
    cli_detail = cli_name + (" / " + compact if compact else "")
    cli = {"tone": cli_tone, "dot": cli_dot, "glyph": cli_glyph, "short": cli_short, "detail": cli_detail, "rank": cli_rank}

    status = str(row.get("status") or "UNKNOWN")
    s_tone, s_dot, s_glyph, s_short, s_rank, s_desc = STATE_SYMBOL_SPEC.get(status, STATE_SYMBOL_SPEC["UNKNOWN"])
    timer = row.get("timer")
    s_detail = status + " — " + s_desc
    if isinstance(timer, dict) and timer.get("text"):
        s_detail += " (" + str(timer.get("kind", "timer")) + ": " + str(timer["text"]) + ")"
    state = {"tone": s_tone, "dot": s_dot, "glyph": s_glyph, "short": s_short, "detail": s_detail, "rank": s_rank}

    flags = set(row.get("flags") or [])
    errors = set(row.get("errors") or [])
    ks_hint = row.get("keystroke_hint")
    is_agy_fixable = bool(
        row.get("agy_work_fixable")
        or "agy-work-fixable" in flags
        or (agent == "agy" and status == "APPROVAL" and row.get("skip_permissions") is False)
    )
    if status in {"APPROVAL", "QUESTION", "SIGN-IN", "TRUST-PROMPT", "UPDATE-PROMPT"} or "subagent-approval" in flags:
        if is_agy_fixable:
            g_detail = "AGY approval prompt (fixable via agy-work -c)"
            if ks_hint:
                g_detail += " · keys: " + str(ks_hint)
            guard = {"tone": "yellow", "dot": "🟡", "glyph": "⚡", "short": "agy-wk",
                     "detail": g_detail, "rank": 8}
        else:
            g_detail = "Human prompt active — never send automated input"
            if ks_hint:
                g_detail += " (" + str(ks_hint) + ")"
            guard = {"tone": "yellow", "dot": "🟡", "glyph": "▲", "short": "prompt",
                     "detail": g_detail, "rank": 10}
    elif "agent-message" in flags or row.get("agent_message"):
        guard = {"tone": "purple", "dot": "🟣", "glyph": "💬", "short": "reply",
                 "detail": "Agent message at end of response awaits user reply", "rank": 15}
    elif "network-retry" in flags or "last-error" in flags:
        short = "retry" if "network-retry" in flags else "error"
        guard = {"tone": "red", "dot": "🔴", "glyph": "⚡", "short": short,
                 "detail": "Network retry or connection/quota error noted on screen", "rank": 20}
    elif "draft" in flags:
        if "pasted-prompt" in flags:
            guard = {"tone": "orange", "dot": "🟠", "glyph": "📋", "short": "pasted",
                     "detail": "Pasted/multi-line prompt in composer — NOT running (press Enter)", "rank": 28}
        else:
            guard = {"tone": "orange", "dot": "🟠", "glyph": "✎", "short": "draft",
                     "detail": "Unsent prompt in composer — NOT running (press Enter)", "rank": 30}
    elif "screen-unavailable" in flags or "agent_process_unverified" in errors:
        guard = {"tone": "slate", "dot": "⚫", "glyph": "◌", "short": "blind",
                 "detail": "Screen or OS agent process unverified", "rank": 40}
    else:
        guard = {"tone": "green", "dot": "🟢", "glyph": "●", "short": "clear",
                 "detail": "No blocking prompt or composer draft detected", "rank": 80}

    is_cached = bool(row.get("stale_cached"))
    delta_raw = row.get("delta") or {}
    d_kind = delta_raw.get("kind")
    d_sec = delta_raw.get("seconds")
    if is_cached or d_kind == "stale_cached":
        delta = {"tone": "slate", "dot": "⚫", "glyph": "◌", "short": "cached",
                 "detail": "Cached snapshot from unreachable host; live change unknown", "rank": 65}
    elif d_kind == "changed":
        delta = {"tone": "green", "dot": "🟢", "glyph": "◆", "short": "Δnow",
                 "detail": "Screen evidence changed since previous sample", "rank": 10}
    elif d_kind == "unchanged" and isinstance(d_sec, (int, float)) and not isinstance(d_sec, bool) and math.isfinite(d_sec) and d_sec >= 0:
        d_text = "=" + duration(d_sec)
        if d_sec < 900:
            delta = {"tone": "blue", "dot": "🔵", "glyph": "◈", "short": d_text,
                     "detail": "Unchanged for " + duration(d_sec) + " (<15m)", "rank": 20}
        elif d_sec < 7200:
            delta = {"tone": "white", "dot": "⚪", "glyph": "◇", "short": d_text,
                     "detail": "Unchanged for " + duration(d_sec) + " (15m–2h)", "rank": 30}
        else:
            delta = {"tone": "orange", "dot": "🟠", "glyph": "◌", "short": d_text,
                     "detail": "Unchanged for " + duration(d_sec) + " (>=2h)", "rank": 40}
    elif d_kind == "first":
        delta = {"tone": "slate", "dot": "⚫", "glyph": "·", "short": "first",
                 "detail": "First observation in comparison cache", "rank": 50}
    else:
        delta = {"tone": "slate", "dot": "⚫", "glyph": "·", "short": "Δ?",
                 "detail": "Observation comparison history unavailable", "rank": 60}

    ctx_raw = row.get("context")
    pct = ctx_raw.get("remaining_percent") if isinstance(ctx_raw, dict) else None
    if isinstance(pct, (int, float)) and not isinstance(pct, bool) and math.isfinite(pct) and 0 <= pct <= 100:
        pct_num = int(pct) if float(pct).is_integer() else round(float(pct), 1)
        pct_str = str(pct_num) + "%"
        if pct < 20:
            ctx = {"tone": "red", "dot": "🔴", "glyph": "▮", "short": pct_str,
                   "detail": pct_str + " context window remaining (low)", "rank": 10}
        elif pct < 50:
            ctx = {"tone": "yellow", "dot": "🟡", "glyph": "▮", "short": pct_str,
                   "detail": pct_str + " context window remaining (moderate)", "rank": 20}
        else:
            ctx = {"tone": "green", "dot": "🟢", "glyph": "▮", "short": pct_str,
                   "detail": pct_str + " context window remaining", "rank": 30}
    else:
        ctx = {"tone": "slate", "dot": "⚪", "glyph": "—", "short": "—",
               "detail": "Context percentage not shown on screen", "rank": 90}

    listeners = row.get("listeners") or {}
    l_count = listeners.get("count")
    l_status = listeners.get("status")
    l_stale = bool(listeners.get("stale") or host_stale or is_cached)
    if l_stale:
        ports = {"tone": "slate", "dot": "⚫", "glyph": "◌", "short": "stale",
                 "detail": "Port-pressure report is stale; listener count withheld", "rank": 75}
    elif isinstance(l_count, int) and not isinstance(l_count, bool) and l_count >= 0:
        if l_count >= 1000 or l_status == "critical":
            ports = {"tone": "red", "dot": "🔴", "glyph": "‼", "short": str(l_count) + "!",
                     "detail": str(l_count) + " TCP listen sockets (critical listener pressure)", "rank": 10}
        elif l_count >= 500 or l_status == "warning":
            ports = {"tone": "orange", "dot": "🟠", "glyph": "!", "short": str(l_count) + "!",
                     "detail": str(l_count) + " TCP listen sockets (elevated listener count)", "rank": 20}
        elif l_count >= 100:
            ports = {"tone": "yellow", "dot": "🟡", "glyph": "•", "short": str(l_count),
                     "detail": str(l_count) + " TCP listen sockets", "rank": 30}
        else:
            ports = {"tone": "green", "dot": "🟢", "glyph": "•", "short": str(l_count),
                     "detail": str(l_count) + " TCP listen sockets (ok)", "rank": 50}
    else:
        ports = {"tone": "slate", "dot": "⚫", "glyph": "—", "short": "—",
                 "detail": "Listener count not in bounded port-watch report", "rank": 80}

    target = row.get("target") or {}
    transport = target.get("transport")
    if transport == "terminal":
        loc = "T" + str(target.get("window_id", "?")) + "/" + str(target.get("tab", 1))
        if target.get("minimized"):
            view = {"tone": "yellow", "dot": "🟡", "glyph": "▾", "short": "min",
                    "detail": "Minimized Terminal window " + loc, "rank": 30}
        elif target.get("selected") is False:
            view = {"tone": "blue", "dot": "🔵", "glyph": "▫", "short": "tab",
                    "detail": "Background Terminal tab " + loc, "rank": 25}
        else:
            view = {"tone": "green", "dot": "🟢", "glyph": "▣", "short": "win",
                    "detail": "Selected Terminal window/tab " + loc, "rank": 10}
    elif transport == "tmux":
        memberships = [str(item) for item in (target.get("tmux_sessions") or [])]
        has_viewer = any(item.lower().startswith(("xmb-view-", "view-", "viewer")) for item in memberships)
        pane_id = str(target.get("pane") or "%?")
        if has_viewer:
            view = {"tone": "green", "dot": "🟢", "glyph": "⊞", "short": "view",
                    "detail": "tmux pane " + pane_id + " with viewer session (" + ", ".join(memberships[:3]) + ")", "rank": 15}
        else:
            view = {"tone": "white", "dot": "⚪", "glyph": "⊟", "short": "tmux",
                    "detail": "tmux pane " + pane_id + (" in " + str(target.get("tmux_session")) if target.get("tmux_session") else ""), "rank": 20}
    else:
        view = {"tone": "slate", "dot": "⚫", "glyph": "◌", "short": "proc",
                "detail": "Headless OS process without Terminal tab or tmux pane", "rank": 40}

    return {
        "mac": mac,
        "cli": cli,
        "state": state,
        "guard": guard,
        "delta": delta,
        "ctx": ctx,
        "ports": ports,
        "view": view,
    }


def _state(row):
    text = STATE_DISPLAY.get(row.get("status"), STATE_DISPLAY["UNKNOWN"])
    timer = row.get("timer")
    if isinstance(timer, dict) and timer.get("text"):
        text += " · " + str(timer["text"])
    flag_list = list(row.get("flags", []))
    flag_set = set(flag_list)
    notes = {
        "agy-work-fixable": "agy-work fixable",
        "agent-message": "awaiting reply",
        "network-retry": "retry",
        "pasted-prompt": "pasted not running",
        "draft": "draft",
        "last-error": "last error",
        "screen-unavailable": "no screen",
        "background-task": "task",
        "task-present": "task present",
    }
    for flag in flag_list:
        if flag == "draft" and "pasted-prompt" in flag_set:
            continue
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
                re.fullmatch(r"(?:process|tmux|cwd|screen|agent_identity|ssh|local)_(?:probe_failed|probe_timed_out|probe_unavailable|probe_output_truncated|probe_size_limit|collection_deadline_exceeded|budget_exhausted)", value)):
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
    source = report.get("source", "ssh")
    offline = source == "snapshot"
    stale = bool(report.get("snapshot_stale"))
    provenance = "OFFLINE snapshot" if offline else ("LOCAL observed" if source == "local" else "SSH observed")
    if stale:
        provenance += " · STALE, last observed"
    heading = "**" + label + " · " + cell(_stamp(report.get("observed_at"))) + " · " + provenance + "**"
    errors = _error_codes(report.get("errors", []))
    if not report.get("reachable", False):
        detail = " Codes: " + ", ".join("`" + code + "`" for code in errors[:5]) + "." if errors else ""
        unreachable_heading = heading.replace("SSH observed", "UNREACHABLE").replace("LOCAL observed", "UNREACHABLE")
        return unreachable_heading + "\n\nCollection unavailable; current session count and state are unknown." + detail + "\n"
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
        has_row_deltas = any((row.get("delta") or {}).get("kind") in {"first", "changed", "unchanged"} for row in sessions)
        if report.get("cache_error") == "comparison_incomplete_inventory" and has_row_deltas:
            summary += " · departures uncounted"
        else:
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
