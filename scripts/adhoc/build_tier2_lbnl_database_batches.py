#!/usr/bin/env python3
"""Partition Tier-2 chemical residuals into LBNL web-authenticated database lookup batches.

Purpose: Package chemical records that were not resolved by Tier-1 public CAS Common Chemistry
  or ChEBI OLS4 into structured lookup packets for LBNL Library's web-authenticated databases:
  - CAS SciFinder-n (165M-substance CAS Registry)
  - CHEMnetBASE (Dictionary of Natural Products / Dictionary of Drugs)
  - ICSD & WebCSD (Inorganic Crystal Structure Database & Cambridge Structural Database)
  - BioCyc (MetaCyc/EcoCyc compound cross-references)
Inputs:
  - ~/Documents/VIMSS/ontology/Mechs/XMechBay/.temp/mech-chemicals-and-acs-dois/scifinder_residual_batch.tsv
  - ~/Documents/VIMSS/ontology/Mechs/XMechBay/.temp/mech-chemicals-and-acs-dois/antibioticmech_chemicals.tsv
  - ~/Documents/VIMSS/ontology/Mechs/XMechBay/.temp/mech-chemicals-and-acs-dois/mediaingredientmech_cas_audit.tsv
  - ~/Documents/VIMSS/ontology/Mechs/XMechBay/.temp/mech-chemicals-and-acs-dois/pathwaymech_chemicals.tsv
Outputs:
  - ~/Documents/VIMSS/ontology/Mechs/XMechBay/.temp/mech-chemicals-and-acs-dois/tier2_scifinder_n_cas_lookup.tsv
  - ~/Documents/VIMSS/ontology/Mechs/XMechBay/.temp/mech-chemicals-and-acs-dois/tier2_chemnetbase_dnp_dod.tsv
  - ~/Documents/VIMSS/ontology/Mechs/XMechBay/.temp/mech-chemicals-and-acs-dois/tier2_icsd_webcsd_hydrates_chelates.tsv
  - ~/Documents/VIMSS/ontology/Mechs/XMechBay/.temp/mech-chemicals-and-acs-dois/tier2_biocyc_pathway_participants.tsv
  - ~/Documents/VIMSS/ontology/Mechs/XMechBay/.temp/mech-chemicals-and-acs-dois/tier2_batches_summary.json
Serves: Track 5 Tier-2 LBNL web-authenticated database batch packaging (2026-11-11).
Date: 2026-11-11
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path
from typing import Any

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

CHELATE_RE = re.compile(r"\b(edta|nta|dtpa|egta|citrat|oxalat|nitrilotriacet|ethylenediaminetetraacet)\b", re.IGNORECASE)
INORGANIC_HYDRATE_RE = re.compile(
    r"(\b\d+\s*h2o\b|•\s*\d*\s*h2o|\bx\s*\d+\s*h2o\b|hydrate|heptahydrate|hexahydrate|pentahydrate|tetrahydrate|dihydrate|monohydrate|anhydrous|lanthan|cerium|neodymium|gadolinium|yttrium|selenite|tungstate|molybdate|vanadate)",
    re.IGNORECASE,
)
NATURAL_PRODUCT_HINT_RE = re.compile(
    r"(mycin|statin|fungin|cillin|cycline|bactin|peptin|oside|koviromycin|alanosine|streptothricin|cycloserine|menaquinone|ubiquinone|cobamide|cobalamin|bacteriochlorophyll|pheophytin|siderophore)",
    re.IGNORECASE,
)


def classify_residual_for_tier2(row: dict[str, str]) -> dict[str, str]:
    """Classify a scifinder_residual_batch.tsv row into primary and secondary LBNL Tier-2 databases."""
    group = (row.get("group") or "").strip()
    mech = (row.get("mech") or "").strip()
    identifier = (row.get("identifier") or "").strip()
    name = (row.get("name") or "").strip()
    cas_rn = (row.get("cas_rn") or "").strip()
    inchikey = (row.get("inchikey") or "").strip()

    if CHELATE_RE.search(name):
        target_db = "WebCSD;SciFinder-n"
        subcohort = "METAL_CHELATE_COMPLEX"
        query_strategy = "Search WebCSD by ligand+metal coordination formula; confirm CAS RN in SciFinder-n"
    elif mech == "MediaIngredientMech" and INORGANIC_HYDRATE_RE.search(name):
        target_db = "ICSD;SciFinder-n"
        subcohort = "INORGANIC_HYDRATE_OR_SALT"
        query_strategy = "Search ICSD for crystal hydrate stoichiometry; confirm exact CAS RN in SciFinder-n"
    elif group == "naturalproduct_review_needed" or (
        mech in {"NaturalProductMech", "MediaIngredientMech"} and NATURAL_PRODUCT_HINT_RE.search(name)
    ):
        target_db = "CHEMnetBASE_DNP;SciFinder-n"
        subcohort = "NATURAL_PRODUCT_CONGENER"
        query_strategy = "Query CHEMnetBASE Dictionary of Natural Products by trivial name/synonym; verify stereoisomer in SciFinder-n"
    elif mech == "AntibioticMech":
        target_db = "SciFinder-n;CHEMnetBASE_DoD"
        subcohort = "ANTIBIOTIC_MINTED_OR_INCHI_RESIDUAL"
        query_strategy = "Query SciFinder-n by antibiotic synonym/ARO name or InChIKey; cross-check Dictionary of Drugs"
    else:
        target_db = "SciFinder-n"
        subcohort = "SPECIALTY_CHEMICAL_OR_BUFFER"
        query_strategy = "Query 165M-substance CAS Registry in SciFinder-n by chemical name or CAS RN"

    return {
        "mech": mech,
        "group": group,
        "subcohort": subcohort,
        "identifier": identifier,
        "name": name,
        "cas_rn": cas_rn,
        "inchikey": inchikey,
        "target_lbnl_databases": target_db,
        "query_strategy": query_strategy,
    }


def _write_tsv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)


def build_tier2_batches(base_dir: Path, out_dir: Path | None = None) -> dict[str, Any]:
    """Build the 4 Tier-2 LBNL database batch TSVs from the XMechBay chemical inventories."""
    target_dir = out_dir or base_dir
    target_dir.mkdir(parents=True, exist_ok=True)

    residual_path = base_dir / "scifinder_residual_batch.tsv"
    ab_path = base_dir / "antibioticmech_chemicals.tsv"
    mim_audit_path = base_dir / "mediaingredientmech_cas_audit.tsv"
    pw_path = base_dir / "pathwaymech_chemicals.tsv"

    with residual_path.open(encoding="utf-8") as fh:
        residuals = list(csv.DictReader(fh, delimiter="\t"))

    scifinder_rows: list[dict[str, str]] = []
    chemnetbase_rows: list[dict[str, str]] = []
    icsd_webcsd_rows: list[dict[str, str]] = []

    for row in residuals:
        classified = classify_residual_for_tier2(row)
        scifinder_rows.append(classified)
        if "CHEMnetBASE" in classified["target_lbnl_databases"]:
            chemnetbase_rows.append(
                {
                    "mech": classified["mech"],
                    "subcohort": classified["subcohort"],
                    "identifier": classified["identifier"],
                    "name": classified["name"],
                    "cas_rn": classified["cas_rn"],
                    "inchikey": classified["inchikey"],
                    "target_dictionary": "DNP (Dictionary of Natural Products)",
                    "curation_goal": "Resolve exact congener stereochemistry and ChEBI/CAS cross-reference",
                }
            )
        if "ICSD" in classified["target_lbnl_databases"] or "WebCSD" in classified["target_lbnl_databases"]:
            icsd_webcsd_rows.append(
                {
                    "mech": classified["mech"],
                    "subcohort": classified["subcohort"],
                    "identifier": classified["identifier"],
                    "name": classified["name"],
                    "cas_rn": classified["cas_rn"],
                    "inchikey": "",
                    "formula_or_smiles_note": "Not found in public CAS Common Chemistry",
                    "target_database": classified["target_lbnl_databases"].split(";")[0],
                    "curation_goal": classified["query_strategy"],
                }
            )

    if ab_path.is_file():
        with ab_path.open(encoding="utf-8") as fh:
            for ab in csv.DictReader(fh, delimiter="\t"):
                if (ab.get("antimicrobial_class") or "").strip() == "ANTIMICROBIAL_UNSPECIFIED":
                    chemnetbase_rows.append(
                        {
                            "mech": "AntibioticMech",
                            "subcohort": "ANTIMICROBIAL_UNSPECIFIED_SPECTRUM",
                            "identifier": (ab.get("identifier") or "").strip(),
                            "name": (ab.get("label") or "").strip(),
                            "cas_rn": "",
                            "inchikey": (ab.get("standard_inchi_key") or "").strip(),
                            "target_dictionary": "DoD/DNP (Dictionary of Drugs / Natural Products)",
                            "curation_goal": "Classify specific antimicrobial_class (ANTIBACTERIAL, ANTIFUNGAL, ANTIVIRAL, ANTIPROTOZOAL, ANTIMYCOBACTERIAL, BIOCIDE)",
                        }
                    )

    polysalt_count = 0
    if mim_audit_path.is_file():
        with mim_audit_path.open(encoding="utf-8") as fh:
            for ma in csv.DictReader(fh, delimiter="\t"):
                s_guard = (ma.get("structure_guard") or "").strip()
                if s_guard.startswith("STRUCTURE_COMPOSITION_MISMATCH"):
                    polysalt_count += 1
                    icsd_webcsd_rows.append(
                        {
                            "mech": "MediaIngredientMech",
                            "subcohort": "POLYSALT_OR_HYDRATE_DEDUPLICATED_SMILES",
                            "identifier": (ma.get("identifier") or "").strip(),
                            "name": (ma.get("preferred_term") or "").strip(),
                            "cas_rn": (ma.get("cas_rn") or "").strip(),
                            "inchikey": (ma.get("inchikey") or "").strip(),
                            "formula_or_smiles_note": f"{ma.get('normalized_formula', '')} | {s_guard}",
                            "target_database": "ICSD/WebCSD",
                            "curation_goal": "Obtain full stoichiometric multi-component crystal/salt representation matching molecular formula",
                        }
                    )

    biocyc_rows: list[dict[str, str]] = []
    if pw_path.is_file():
        with pw_path.open(encoding="utf-8") as fh:
            for pw in csv.DictReader(fh, delimiter="\t"):
                ident = (pw.get("identifier") or "").strip()
                if ident and not ident.startswith("CHEBI:"):
                    biocyc_rows.append(
                        {
                            "mech": "PathwayMech",
                            "identifier": ident,
                            "label": (pw.get("label") or "").strip(),
                            "category": (pw.get("category") or "").strip(),
                            "pathway_count": (pw.get("pathway_count") or "").strip(),
                            "sample_pathways": (pw.get("sample_pathways") or "").strip(),
                            "target_database": "BioCyc (MetaCyc/EcoCyc)",
                            "curation_goal": "Retrieve BioCyc compound structure (InChIKey/SMILES) and ChEBI/CAS cross-reference",
                        }
                    )

    _write_tsv(
        target_dir / "tier2_scifinder_n_cas_lookup.tsv",
        [
            "mech",
            "group",
            "subcohort",
            "identifier",
            "name",
            "cas_rn",
            "inchikey",
            "target_lbnl_databases",
            "query_strategy",
        ],
        scifinder_rows,
    )
    _write_tsv(
        target_dir / "tier2_chemnetbase_dnp_dod.tsv",
        [
            "mech",
            "subcohort",
            "identifier",
            "name",
            "cas_rn",
            "inchikey",
            "target_dictionary",
            "curation_goal",
        ],
        chemnetbase_rows,
    )
    _write_tsv(
        target_dir / "tier2_icsd_webcsd_hydrates_chelates.tsv",
        [
            "mech",
            "subcohort",
            "identifier",
            "name",
            "cas_rn",
            "inchikey",
            "formula_or_smiles_note",
            "target_database",
            "curation_goal",
        ],
        icsd_webcsd_rows,
    )
    _write_tsv(
        target_dir / "tier2_biocyc_pathway_participants.tsv",
        [
            "mech",
            "identifier",
            "label",
            "category",
            "pathway_count",
            "sample_pathways",
            "target_database",
            "curation_goal",
        ],
        biocyc_rows,
    )

    summary = {
        "tier2_scifinder_n_cas_lookup_count": len(scifinder_rows),
        "tier2_chemnetbase_dnp_dod_count": len(chemnetbase_rows),
        "tier2_icsd_webcsd_hydrates_chelates_count": len(icsd_webcsd_rows),
        "tier2_icsd_webcsd_polysalt_mismatch_count": polysalt_count,
        "tier2_biocyc_pathway_participants_count": len(biocyc_rows),
    }
    (target_dir / "tier2_batches_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-dir",
        type=Path,
        default=DEFAULT_BASE_DIR,
        help="Directory containing scifinder_residual_batch.tsv and Mech chemical TSVs",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Output directory for Tier-2 batch TSVs (defaults to --base-dir)",
    )
    args = parser.parse_args()
    summary = build_tier2_batches(args.base_dir, args.out_dir)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
