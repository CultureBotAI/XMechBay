#!/usr/bin/env python3
"""Build per-Mech curation worklists for unpromoted Tier-A (data/raw/) and Tier-B (research/) ACS DOIs.

Purpose: Partition unpromoted 10.1021/* DOIs from acs_dois_master.tsv (DOIs present in
  data/raw/ inventories, research/ notes, or missing_pdf_or_report queues that have not yet
  been promoted into curated data/ YAML records) into actionable Tier-A and Tier-B worklists
  with open ACS Figshare SI (.s001) candidate URLs and kg-microbe-sources commands.
Inputs:
  - ~/Documents/VIMSS/ontology/Mechs/XMechBay/.temp/mech-chemicals-and-acs-dois/acs_dois_master.tsv
Outputs:
  - ~/Documents/VIMSS/ontology/Mechs/XMechBay/.temp/mech-chemicals-and-acs-dois/unpromoted_provenance_worklists/tier_a_raw_inventory_worklist.tsv
  - ~/Documents/VIMSS/ontology/Mechs/XMechBay/.temp/mech-chemicals-and-acs-dois/unpromoted_provenance_worklists/tier_b_research_notes_worklist.tsv
  - ~/Documents/VIMSS/ontology/Mechs/XMechBay/.temp/mech-chemicals-and-acs-dois/unpromoted_provenance_worklists/<Mech>_unpromoted_acs_worklist.tsv
  - ~/Documents/VIMSS/ontology/Mechs/XMechBay/.temp/mech-chemicals-and-acs-dois/unpromoted_provenance_worklists/worklist_summary.json
Serves: Track 4C fleet-wide local-provenance & ACS Figshare SI indexing (2026-11-11).
Date: 2026-11-11
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

SI_SUFFIX_RE = re.compile(r"\.s00\d+$", re.IGNORECASE)

DEFAULT_BASE_DIR = (
    Path.home()
    / "Documents"
    / "VIMSS"
    / "ontology"
    / "Mechs"
    / "XMechBay"
    / ".temp"
    / "mech-chemicals-and-acs-dois"
)

WORKLIST_COLUMNS = [
    "doi",
    "tier",
    "subcohort",
    "priority",
    "journal_code",
    "journal_title",
    "mechs",
    "source_kinds",
    "pmid",
    "title",
    "acs_figshare_si_candidate_url",
    "recommended_cli",
    "sample_contexts",
]


def classify_unpromoted_row(row: dict[str, str]) -> tuple[str, str] | None:
    """Return (tier, subcohort) for an unpromoted ACS DOI row, or None if already in curated_data."""
    source_kinds = {s.strip() for s in (row.get("source_kinds") or "").split(";") if s.strip()}
    if "curated_data" in source_kinds:
        return None
    priority = (row.get("priority") or "").strip()
    mechs = (row.get("mechs") or "").strip()
    contexts = (row.get("sample_contexts") or "").strip()

    if "raw_inventory" in source_kinds:
        if source_kinds == {"raw_inventory"} and priority == "STANDARD":
            return "TIER_A_RAW_INVENTORY", "RAW_INVENTORY_ONLY_STANDARD"
        return "TIER_A_RAW_INVENTORY", "RAW_INVENTORY_MULTI_OR_HIGH"

    if "missing_pdf_or_report" in source_kinds:
        return "TIER_B_RESEARCH_AND_REPORTS", "MISSING_PDF_OR_REPORT"

    if mechs == "MediaIngredientMech" and "edison-literature" in contexts:
        return "TIER_B_RESEARCH_AND_REPORTS", "MIM_EDISON_LITERATURE_CACHE"

    return "TIER_B_RESEARCH_AND_REPORTS", "ACTIVE_RESEARCH_NOTES"


def enrich_worklist_row(row: dict[str, str], tier: str, subcohort: str) -> dict[str, str]:
    """Build an enriched worklist row with ACS Figshare SI URL and CLI command."""
    raw_doi = (row.get("doi") or "").strip()
    doi = SI_SUFFIX_RE.sub("", raw_doi)
    si_url = f"https://doi.org/{doi}.s001" if doi.lower().startswith("10.1021/") else ""
    cli = f"uv run kg-microbe-sources acs-si --doi {doi}"
    return {
        "doi": doi,
        "tier": tier,
        "subcohort": subcohort,
        "priority": (row.get("priority") or "").strip(),
        "journal_code": (row.get("journal_code") or "").strip(),
        "journal_title": (row.get("journal_title") or "").strip(),
        "mechs": (row.get("mechs") or "").strip(),
        "source_kinds": (row.get("source_kinds") or "").strip(),
        "pmid": (row.get("pmid") or "").strip(),
        "title": (row.get("title") or "").strip(),
        "acs_figshare_si_candidate_url": si_url,
        "recommended_cli": cli,
        "sample_contexts": (row.get("sample_contexts") or "").strip(),
    }


def _write_tsv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=WORKLIST_COLUMNS, delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)


def build_worklists(master_tsv: Path, out_dir: Path) -> dict[str, Any]:
    """Read acs_dois_master.tsv and write Tier-A, Tier-B, and per-Mech worklist TSVs."""
    with master_tsv.open(encoding="utf-8") as fh:
        master_rows = list(csv.DictReader(fh, delimiter="\t"))

    tier_a_rows: list[dict[str, str]] = []
    tier_b_rows: list[dict[str, str]] = []
    per_mech_rows: dict[str, list[dict[str, str]]] = defaultdict(list)
    subcohort_counts: Counter[str] = Counter()
    tier_a_mech_counts: Counter[str] = Counter()
    tier_b_actionable_mech_counts: Counter[str] = Counter()

    for row in master_rows:
        classified = classify_unpromoted_row(row)
        if classified is None:
            continue
        tier, subcohort = classified
        enriched = enrich_worklist_row(row, tier, subcohort)
        subcohort_counts[subcohort] += 1

        if tier == "TIER_A_RAW_INVENTORY":
            tier_a_rows.append(enriched)
        else:
            tier_b_rows.append(enriched)

        for mech in [m.strip() for m in enriched["mechs"].split(";") if m.strip()]:
            if subcohort != "MIM_EDISON_LITERATURE_CACHE":
                per_mech_rows[mech].append(enriched)
            if tier == "TIER_A_RAW_INVENTORY":
                tier_a_mech_counts[mech] += 1
            elif subcohort != "MIM_EDISON_LITERATURE_CACHE":
                tier_b_actionable_mech_counts[mech] += 1

    out_dir.mkdir(parents=True, exist_ok=True)
    _write_tsv(out_dir / "tier_a_raw_inventory_worklist.tsv", tier_a_rows)
    _write_tsv(
        out_dir / "tier_b_research_notes_worklist.tsv",
        [r for r in tier_b_rows if r["subcohort"] != "MIM_EDISON_LITERATURE_CACHE"],
    )

    for mech, rows in sorted(per_mech_rows.items()):
        _write_tsv(out_dir / f"{mech}_unpromoted_acs_worklist.tsv", rows)

    summary = {
        "master_total_dois": len(master_rows),
        "unpromoted_total_dois": len(tier_a_rows) + len(tier_b_rows),
        "tier_a_raw_inventory_total": len(tier_a_rows),
        "tier_a_raw_inventory_only_standard": subcohort_counts["RAW_INVENTORY_ONLY_STANDARD"],
        "tier_a_raw_inventory_multi_or_high": subcohort_counts["RAW_INVENTORY_MULTI_OR_HIGH"],
        "tier_b_actionable_total": (
            subcohort_counts["MISSING_PDF_OR_REPORT"] + subcohort_counts["ACTIVE_RESEARCH_NOTES"]
        ),
        "tier_b_missing_pdf_or_report": subcohort_counts["MISSING_PDF_OR_REPORT"],
        "tier_b_active_research_notes": subcohort_counts["ACTIVE_RESEARCH_NOTES"],
        "tier_b_mim_edison_cache_deferred": subcohort_counts["MIM_EDISON_LITERATURE_CACHE"],
        "tier_a_by_mech": dict(tier_a_mech_counts.most_common()),
        "tier_b_actionable_by_mech": dict(tier_b_actionable_mech_counts.most_common()),
    }
    (out_dir / "worklist_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--master-tsv",
        type=Path,
        default=DEFAULT_BASE_DIR / "acs_dois_master.tsv",
        help="Path to acs_dois_master.tsv",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=DEFAULT_BASE_DIR / "unpromoted_provenance_worklists",
        help="Output directory for per-Mech worklists",
    )
    args = parser.parse_args()
    summary = build_worklists(args.master_tsv, args.out_dir)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
