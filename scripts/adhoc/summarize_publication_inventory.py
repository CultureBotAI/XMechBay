#!/usr/bin/env python3
"""Summarize a private GitHub export without echoing private values.

Input: external inventory directory. Outputs: private JSON and count summary.
Serves publication review; 2026-10-25. No network or GitHub mutations.
"""

import argparse
import importlib.util
import json
from pathlib import Path
import re
import sys
import zipfile


def scan_exports(directory, gate):
    """Scan all exported JSON values and log entries, returning categories only."""
    findings = {}
    generated = {"summary.json", "publication-summary.json", "additional-summary.json"}
    for path in sorted(directory.glob("*.json")):
        if path.name in generated:
            continue
        try:
            data = json.loads(path.read_text())
        except (ValueError, UnicodeError):
            findings[path.name] = ["unreadable-json-export"]
            continue
        # Decode strings individually so JSON escaping cannot conceal a path.
        pending = [data]
        categories = set()
        while pending:
            value = pending.pop()
            if isinstance(value, dict):
                pending.extend(value.keys())
                pending.extend(value.values())
            elif isinstance(value, list):
                pending.extend(value)
            elif isinstance(value, str):
                categories.update(item.category for item in gate.scan_text(value, "export-value", []))
        categories.update(item.category for item in gate.scan_text(json.dumps(data, indent=2), "export-json", []))
        if categories:
            findings[path.name] = sorted(categories)
    for path in sorted(directory.glob("*_logs.zip")):
        categories = set()
        with zipfile.ZipFile(path) as archive:
            for entry in archive.infolist():
                categories.update(item.category for item in gate.scan_text(entry.filename, "log-entry-name", []))
                if not entry.is_dir():
                    categories.update(item.category for item in gate.scan_blob(archive.read(entry), "log-entry-content", gate.DEFAULT_MAX_BYTES, []))
        if categories:
            findings[path.name] = sorted(categories)
    return findings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    directory = args.directory.resolve()
    if directory == root or root in directory.parents:
        parser.error("private inventory must be outside the publication repository")
    spec = importlib.util.spec_from_file_location("privacy_gate", root / "scripts/check_public_content.py")
    gate = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = gate
    spec.loader.exec_module(gate)

    def read(name):
        return json.loads((directory / (name + ".json")).read_text())

    def flattened(name):
        return [item for page in read(name) for item in page]

    issue_findings = {}
    attachments = 0
    issues = flattened("issues")
    for issue in issues:
        content = issue["title"] + "\n" + (issue["body"] or "")
        findings = gate.scan_text(content, "body", [])
        if findings:
            issue_findings[str(issue["number"])] = sorted({finding.category for finding in findings})
        attachments += len(re.findall(r"https://(?:user-images\.githubusercontent\.com|private-user-images\.githubusercontent\.com|github\.com/user-attachments)/", content))
    revisions = {}
    for path in sorted(directory.glob("edits_*.json")):
        data = json.loads(path.read_text())
        connection = ((data.get("data") or {}).get("node") or {}).get("userContentEdits") or {}
        nodes = connection.get("nodes") or []
        if nodes:
            revisions[path.stem] = {"count": len(nodes), "categories": sorted({finding.category for node in nodes for finding in gate.scan_text(node.get("diff") or "", "revision", [])})}
    runs = [run for page in read("actions_runs") for run in page["workflow_runs"]]
    log_entries = {}
    for path in directory.glob("run_*_logs.zip"):
        with zipfile.ZipFile(path) as archive:
            log_entries[path.stem] = len(archive.namelist())
    summary = {
        "issues_excluding_prs": sum("pull_request" not in item for item in issues),
        "prs": len(flattened("pulls")),
        "issues_or_prs_with_pattern_findings": issue_findings,
        "content_edit_histories": revisions,
        "issue_comments": len(flattened("issue_comments")),
        "review_comments": len(flattened("review_comments")),
        "commit_comments": len(flattened("commit_comments")),
        "actions_runs": len(runs),
        "run_ids": [run["id"] for run in runs],
        "run_log_file_counts": log_entries,
        "artifacts": read("artifacts")[0]["total_count"],
        "caches": read("caches")[0]["total_count"],
        "releases": len(flattened("releases")),
        "forks": len(flattened("forks")),
        "deployments": len(flattened("deployments")),
        "environments": read("environments")[0]["total_count"],
        "attachment_links_in_issue_bodies": attachments,
        "export_findings": scan_exports(directory, gate),
        "scope": "All exported JSON values (including comments, review bodies, edit histories, check annotations and metadata when exported) plus downloaded log entry names/content; regex findings are a lower bound, not complete semantic privacy review. Absent or failed API exports remain unknown.",
    }
    (directory / "publication-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
