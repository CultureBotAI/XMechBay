#!/usr/bin/env python3
"""Read-only audit of an explicitly supplied permissions settings JSON file.

Inputs: settings JSON and optional command text; output: locations and diagnostics.
Public infrastructure extraction, 2026-10-25. Requires Python 3.10 or later.
No setting, permission, credential, or sandbox policy is changed.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

SECTIONS = ("allow", "ask", "deny")
TOOLS = ("command", "unsandboxed", "read_file", "write_file")
LIMITATION = (
    "Diagnostic evidence only: literal rules use exact string equality and regex rules "
    "use Python re.fullmatch. This is not RE2 validation or a prediction of an agent's "
    "permission decision. Shell expansion, command decomposition, path matching, policy "
    "precedence, and generated sandbox profiles are not simulated."
)


def permissions(data: Any) -> dict[str, list[str]]:
    if not isinstance(data, dict) or not isinstance(data.get("permissions"), dict):
        raise ValueError("settings must have a permissions object")
    result = {}
    for section in SECTIONS:
        rules = data["permissions"].get(section, [])
        if not isinstance(rules, list) or any(not isinstance(rule, str) for rule in rules):
            raise ValueError("permission sections must contain lists of strings")
        result[section] = rules
    return result


def parse_rule(rule: str) -> tuple[str, str, bool] | None:
    for tool in TOOLS:
        prefix = tool + "("
        if rule.startswith(prefix) and rule.endswith(")"):
            payload = rule[len(prefix):-1]
            regex = payload.startswith("regex:")
            return tool, payload[6:] if regex else payload, regex
    return None


def location(section: str, index: int) -> str:
    return f"permissions.{section}[{index}]"


def audit_settings(data: Any) -> dict[str, Any]:
    rules_by_section = permissions(data)
    findings = []
    seen: dict[str, str] = {}
    regex_count = path_count = 0
    for section, rules in rules_by_section.items():
        for index, rule in enumerate(rules):
            loc = location(section, index)
            if rule in seen:
                findings.append({"kind": "duplicate_rule", "location": loc, "first_location": seen[rule]})
            else:
                seen[rule] = loc
            parsed = parse_rule(rule)
            if parsed is None:
                findings.append({"kind": "unrecognized_rule_syntax", "location": loc})
                continue
            tool, payload, regex = parsed
            if not payload:
                findings.append({"kind": "empty_rule_payload", "location": loc})
            if tool in ("read_file", "write_file"):
                path_count += 1
            if regex:
                regex_count += 1
                try:
                    re.compile(payload)
                except (re.error, OverflowError, RecursionError):
                    findings.append({"kind": "invalid_python_regex", "location": loc})
    return {
        "counts": {section: len(rules) for section, rules in rules_by_section.items()},
        "regex_rule_count": regex_count,
        "path_rule_count": path_count,
        "serialized_permission_bytes": len(json.dumps(rules_by_section, ensure_ascii=False).encode()),
        "findings": findings,
        "limitations": LIMITATION,
        "privacy": "Rule contents, command text, and settings paths are omitted from reports.",
    }


def command_diagnostics(data: Any, command: str, tool: str = "command") -> dict[str, Any]:
    """Pure approximation; the CLI runs it in a worker with a time limit."""
    evidence = []
    skipped = 0
    for section, rules in permissions(data).items():
        for index, rule in enumerate(rules):
            parsed = parse_rule(rule)
            if not parsed or parsed[0] != tool:
                skipped += 1
                continue
            _, payload, regex = parsed
            entry: dict[str, Any] = {"location": location(section, index),
                                     "method": "python_regex_approximation" if regex else "exact_literal_comparison"}
            if regex:
                try:
                    entry["matched"] = re.fullmatch(payload, command) is not None
                    if not entry["matched"] and "\n" in command and re.fullmatch(payload, command, re.DOTALL):
                        entry["note"] = "matches_only_when_dot_matches_newlines_in_python"
                except (re.error, OverflowError, RecursionError):
                    entry["matched"] = None
                    entry["note"] = "invalid_python_regex"
            else:
                entry["matched"] = payload == command
            evidence.append(entry)
    return {"tool": tool, "evidence": evidence, "skipped_rule_count": skipped,
            "complete": True, "limitations": LIMITATION}


def bounded_diagnostics(data: Any, command: str, tool: str) -> dict[str, Any]:
    # A separate process bounds pathological Python regex evaluation. Input is
    # passed via stdin, never exposed as command-line arguments or a temp file.
    try:
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--regex-worker"],
                                input=json.dumps({"settings": data, "command": command, "tool": tool}),
                                text=True, capture_output=True, timeout=3, check=True)
        return json.loads(result.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return {"complete": False, "error": "regex_worker_failed_or_timed_out", "limitations": LIMITATION}


def main() -> int:
    if sys.argv[1:] == ["--regex-worker"]:
        request = json.load(sys.stdin)
        print(json.dumps(command_diagnostics(request["settings"], request["command"], request["tool"])))
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", type=Path, required=True, help="settings JSON to read; no default or discovery")
    command = parser.add_mutually_exclusive_group()
    command.add_argument("--check-command", help="command text to diagnose without executing it")
    command.add_argument("--check-command-file", type=Path, help="UTF-8 command text file, useful for multiline input")
    parser.add_argument("--tool", choices=("command", "unsandboxed"), default="command")
    args = parser.parse_args()
    try:
        data = json.loads(args.settings.read_text(encoding="utf-8"))
        report = audit_settings(data)
        cmd = args.check_command
        if args.check_command_file is not None:
            cmd = args.check_command_file.read_text(encoding="utf-8")
        if cmd is not None:
            report["command_check"] = bounded_diagnostics(data, cmd, args.tool)
        print(json.dumps(report, indent=2))
        return 0 if report.get("command_check", {}).get("complete", True) else 2
    except (OSError, ValueError, TypeError, OverflowError, RecursionError) as exc:
        print(json.dumps({"error": "settings could not be audited", "error_type": type(exc).__name__}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
