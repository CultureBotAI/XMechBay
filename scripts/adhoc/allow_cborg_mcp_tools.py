#!/usr/bin/env python3
"""Allow read-only CBORG MCP tools (context7, tavily, valency) in Antigravity and Claude Code.

Purpose:
  1. Fix Antigravity's MCP permission syntax in ~/.gemini/antigravity-cli/settings.json:
     Antigravity uses slash-separated `mcp(<server>/*)` and `mcp(<server>/<tool>)`, not
     Claude Code's colon syntax `mcp(<server>:*)`. Replaces `:*` with `/*`, adds explicit
     `mcp(<server>/<tool>)` allow entries for all read-only tools on `context7` (2),
     `tavily` (5), and `valency` (35), and places outbound `mcp(valency/valency-submit_feedback)`
     plus `mcp(gmail/*)` / `mcp(gdrive/*)` in `permissions.ask`.
  2. Sync equivalent `mcp__context7__*`, `mcp__tavily__*`, and `mcp__valency__*` allow rules
     (and `mcp__valency__valency-submit_feedback` ask rule) into ~/.claude/settings.json.

Inputs:
  - ~/.gemini/antigravity-cli/settings.json
  - ~/.claude/settings.json (optional, if present)
Outputs:
  - Timestamped backups: *.bak-cborg-mcp-allow-20261107
  - Updated settings.json files (only when --apply is passed; default is dry-run)
Date: 2026-11-07
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

HOME = Path.home()
AGY_SETTINGS = HOME / ".gemini" / "antigravity-cli" / "settings.json"
AGY_BACKUP = AGY_SETTINGS.with_name("settings.json.bak-cborg-mcp-allow-20261107")

CLAUDE_SETTINGS = HOME / ".claude" / "settings.json"
CLAUDE_BACKUP = CLAUDE_SETTINGS.with_name("settings.json.bak-cborg-mcp-allow-20261107")

CONTEXT7_TOOLS = [
    "context7-resolve-library-id",
    "context7-query-docs",
]

TAVILY_TOOLS = [
    "tavily-tavily_search",
    "tavily-tavily_extract",
    "tavily-tavily_crawl",
    "tavily-tavily_map",
    "tavily-tavily_research",
]

VALENCY_READONLY_TOOLS = [
    "valency-search_by_title",
    "valency-search_by_abstract",
    "valency-search_by_abstract_batch",
    "valency-search_by_author",
    "valency-search_by_category",
    "valency-filter_by_date_range",
    "valency-filter_by_categories",
    "valency-filter_papers_with_doi",
    "valency-analyze_corpus_metrics",
    "valency-identify_research_domains",
    "valency-identify_prolific_authors",
    "valency-count_papers",
    "valency-semantic_search_papers",
    "valency-find_similar_papers",
    "valency-get_citing_papers",
    "valency-export_papers_json",
    "valency-export_papers_csv",
    "valency-export_papers_bibtex",
    "valency-get_paper_versions",
    "valency-get_paper_by_id",
    "valency-search_by_venue",
    "valency-search_by_comments",
    "valency-filter_by_license",
    "valency-get_publication_trends",
    "valency-get_publication_trends_batch",
    "valency-get_keyword_trends",
    "valency-get_author_coauthors",
    "valency-get_author_profile",
    "valency-compare_authors",
    "valency-batch_author_categories",
    "valency-get_author_papers",
    "valency-search_cross_category",
    "valency-export_from_filter",
    "valency-get_field_coverage",
    "valency-list_sources",
]

VALENCY_ASK_TOOLS = [
    "valency-submit_feedback",
]


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def desired_agy_allow_rules() -> list[str]:
    rules = [
        "mcp(context7/*)",
        "mcp(tavily/*)",
        "mcp(valency/*)",
    ]
    for t in CONTEXT7_TOOLS:
        rules.append(f"mcp(context7/{t})")
    for t in TAVILY_TOOLS:
        rules.append(f"mcp(tavily/{t})")
    for t in VALENCY_READONLY_TOOLS:
        rules.append(f"mcp(valency/{t})")
    return rules


def desired_agy_ask_rules() -> list[str]:
    rules = [
        "mcp(gmail)",
        "mcp(gmail/*)",
        "mcp(gdrive)",
        "mcp(gdrive/*)",
    ]
    for t in VALENCY_ASK_TOOLS:
        rules.append(f"mcp(valency/{t})")
    return rules


def update_agy_settings(apply: bool) -> int:
    if not AGY_SETTINGS.exists():
        print(f"ERROR: {AGY_SETTINGS} not found", file=sys.stderr)
        return 1

    before_bytes = AGY_SETTINGS.stat().st_size
    before_sha = sha256_of(AGY_SETTINGS)
    data = json.loads(AGY_SETTINGS.read_text(encoding="utf-8"))
    perms = data.setdefault("permissions", {})
    allow: list[str] = perms.setdefault("allow", [])
    ask: list[str] = perms.setdefault("ask", [])

    n_allow_before, n_ask_before = len(allow), len(ask)

    # Drop invalid colon-syntax MCP wildcards
    colon_allow_drop = {"mcp(context7:*)", "mcp(tavily:*)", "mcp(valency:*)"}
    colon_ask_drop = {"mcp(gmail:*)", "mcp(gdrive:*)"}

    new_allow = [r for r in allow if r not in colon_allow_drop]
    for r in desired_agy_allow_rules():
        if r not in new_allow:
            new_allow.append(r)

    new_ask = [r for r in ask if r not in colon_ask_drop]
    for r in desired_agy_ask_rules():
        if r not in new_ask:
            new_ask.append(r)

    mode = "APPLY" if apply else "DRY-RUN"
    print(f"[{mode}] Antigravity settings: {AGY_SETTINGS}")
    print(f"  permissions.allow: {n_allow_before} -> {len(new_allow)}")
    print(f"  permissions.ask:   {n_ask_before} -> {len(new_ask)}")

    if apply:
        if not AGY_BACKUP.exists():
            shutil.copy2(AGY_SETTINGS, AGY_BACKUP)
            print(f"  Created backup:    {AGY_BACKUP}")
        else:
            print(f"  Existing backup:   {AGY_BACKUP}")
        perms["allow"] = new_allow
        perms["ask"] = new_ask
        AGY_SETTINGS.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        after_bytes = AGY_SETTINGS.stat().st_size
        after_sha = sha256_of(AGY_SETTINGS)
        print(f"  bytes:             {before_bytes:,} -> {after_bytes:,}")
        print(f"  sha256:            {before_sha[:16]}... -> {after_sha[:16]}...")
    return 0


def update_claude_settings(apply: bool) -> int:
    if not CLAUDE_SETTINGS.exists():
        print(f"[SKIP] {CLAUDE_SETTINGS} not found (skipping Claude Code settings update)")
        return 0

    before_bytes = CLAUDE_SETTINGS.stat().st_size
    before_sha = sha256_of(CLAUDE_SETTINGS)
    data = json.loads(CLAUDE_SETTINGS.read_text(encoding="utf-8"))
    perms = data.setdefault("permissions", {})
    allow: list[str] = perms.setdefault("allow", [])
    ask: list[str] = perms.setdefault("ask", [])

    n_allow_before, n_ask_before = len(allow), len(ask)

    claude_allow_add = [
        "mcp__context7__*",
        "mcp__tavily__*",
        "mcp__valency__*",
    ]
    claude_ask_add = [
        "mcp__valency__valency-submit_feedback",
    ]

    new_allow = list(allow)
    for r in claude_allow_add:
        if r not in new_allow:
            new_allow.append(r)

    new_ask = list(ask)
    for r in claude_ask_add:
        if r not in new_ask:
            new_ask.append(r)

    mode = "APPLY" if apply else "DRY-RUN"
    print(f"[{mode}] Claude Code settings: {CLAUDE_SETTINGS}")
    print(f"  permissions.allow: {n_allow_before} -> {len(new_allow)}")
    print(f"  permissions.ask:   {n_ask_before} -> {len(new_ask)}")

    if apply:
        if not CLAUDE_BACKUP.exists():
            shutil.copy2(CLAUDE_SETTINGS, CLAUDE_BACKUP)
            print(f"  Created backup:    {CLAUDE_BACKUP}")
        else:
            print(f"  Existing backup:   {CLAUDE_BACKUP}")
        perms["allow"] = new_allow
        perms["ask"] = new_ask
        CLAUDE_SETTINGS.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        after_bytes = CLAUDE_SETTINGS.stat().st_size
        after_sha = sha256_of(CLAUDE_SETTINGS)
        print(f"  bytes:             {before_bytes:,} -> {after_bytes:,}")
        print(f"  sha256:            {before_sha[:16]}... -> {after_sha[:16]}...")
    return 0


def run_undo() -> int:
    rc = 0
    for target, bak in ((AGY_SETTINGS, AGY_BACKUP), (CLAUDE_SETTINGS, CLAUDE_BACKUP)):
        if bak.exists():
            shutil.copy2(bak, target)
            print(f"Restored {target} from {bak} ({target.stat().st_size:,} bytes, sha256={sha256_of(target)[:16]}...)")
        else:
            print(f"No backup found at {bak} (skipping {target})")
    return rc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write backups and apply changes (default is dry-run)")
    parser.add_argument("--undo", action="store_true", help="Restore settings.json files from backup")
    parser.add_argument("--agy-only", action="store_true", help="Only inspect/update Antigravity settings.json")
    args = parser.parse_args()

    if args.undo:
        return run_undo()

    rc = update_agy_settings(apply=args.apply)
    if not args.agy_only:
        try:
            rc |= update_claude_settings(apply=args.apply)
        except PermissionError:
            print(
                f"[NOTE] {CLAUDE_SETTINGS} is protected inside the agy sandbox; "
                "run this script from your terminal to update both Antigravity and Claude Code.",
                file=sys.stderr,
            )
    if not args.apply:
        print("\nDry-run complete. Re-run with --apply in your terminal to write backups and apply.")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
