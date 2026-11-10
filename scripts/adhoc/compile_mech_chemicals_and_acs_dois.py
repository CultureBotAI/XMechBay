#!/usr/bin/env python3
"""
Purpose:
    Compile a structured inventory of chemicals across MediaIngredientMech (MIM),
    AntibioticMech, NaturalProductMech, and PathwayMech (including curation gaps
    and cross-Mech overlaps for LBNL Reaxys, SciFinder, CHEMnetBASE, ICSD, and
    WebCSD lookups), and compile all American Chemical Society (ACS, 10.1021/*)
    DOIs across the Mech fleet (including abstract-only PMIDs that resolve to
    10.1021/* DOIs) for the LBNL Library ACS Text and Data Mining request.

Inputs:
    - ~/Documents/VIMSS/ontology/Mechs/MediaIngredientMech/data/
    - ~/Documents/VIMSS/ontology/Mechs/AntibioticMech/data/
    - ~/Documents/VIMSS/ontology/Mechs/NaturalProductMech/data/
    - ~/Documents/VIMSS/ontology/Mechs/PathwayMech/data/
    - ~/Documents/VIMSS/ontology/Mechs/<all other Mech repos>/{data,reports,research,references_cache}/

Outputs:
    - ~/Documents/VIMSS/ontology/Mechs/XMechBay/.temp/mech-chemicals-and-acs-dois/
        - mim_chemicals.tsv
        - antibioticmech_chemicals.tsv
        - naturalproductmech_chemicals.tsv
        - pathwaymech_chemicals.tsv
        - cross_mech_chemical_overlaps.tsv
        - priority_chemicals_for_lbl_databases.tsv
        - acs_dois_master.tsv
        - acs_dois_list_only.txt
        - acs_journals_summary.tsv
        - summary_stats.json

Date: 2026-11-08
"""

from __future__ import annotations

import csv
import json
import re
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import yaml

MECHS_ROOT = Path.home() / "Documents" / "VIMSS" / "ontology" / "Mechs"
OUT_DIR = MECHS_ROOT / "XMechBay" / ".temp" / "mech-chemicals-and-acs-dois"

DOI_ACS_RE = re.compile(
    r"\b(10\.1021/[A-Za-z0-9._;()/-]+)",
    re.IGNORECASE,
)
PMID_RE = re.compile(r"\bPMID:(\d{4,10})\b", re.IGNORECASE)


def clean_doi(raw_doi: str) -> str:
    """Strip trailing punctuation/markdown/supplement suffixes from a 10.1021/* DOI."""
    d = raw_doi.strip().rstrip(".,;:)]}>'\"`*")
    # Strip trailing markdown or HTML fragments if any
    for sep in ("#", "?", "\\", '"', "'"):
        if sep in d:
            d = d.split(sep, 1)[0]
    d = d.rstrip(".,;:)]}>'\"`*")
    # Normalize supplement DOI to parent article DOI as well as keeping track
    d_lower = d.lower()
    if re.search(r"\.s00\d+$", d_lower):
        d_lower = re.sub(r"\.s00\d+$", "", d_lower)
    return d_lower


def classify_acs_journal(doi: str) -> tuple[str, str]:
    """Map an ACS DOI (10.1021/...) to (journal_code, journal_title)."""
    suffix = doi.lower().split("10.1021/", 1)[-1]
    prefix_map = [
        ("acs.jafc.", "jafc", "Journal of Agricultural and Food Chemistry"),
        ("jf", "jafc", "Journal of Agricultural and Food Chemistry"),
        ("acs.jnatprod.", "jnp", "Journal of Natural Products"),
        ("np", "jnp", "Journal of Natural Products"),
        ("jacs.", "jacs", "Journal of the American Chemical Society"),
        ("ja", "jacs", "Journal of the American Chemical Society"),
        ("acs.biochem.", "biochem", "Biochemistry"),
        ("bi", "biochem", "Biochemistry"),
        ("acs.jmedchem.", "jmedchem", "Journal of Medicinal Chemistry"),
        ("jm", "jmedchem", "Journal of Medicinal Chemistry"),
        ("acs.est.", "est", "Environmental Science & Technology"),
        ("acs.estlett.", "estlett", "Environmental Science & Technology Letters"),
        ("acsestwater.", "estwater", "ACS ES&T Water"),
        ("acsestengg.", "estengg", "ACS ES&T Engineering"),
        ("es", "est", "Environmental Science & Technology"),
        ("acschembio.", "acschembio", "ACS Chemical Biology"),
        ("cb", "acschembio", "ACS Chemical Biology"),
        ("acssynbio.", "acssynbio", "ACS Synthetic Biology"),
        ("sb", "acssynbio", "ACS Synthetic Biology"),
        ("acsinfecdis.", "acsinfecdis", "ACS Infectious Diseases"),
        ("acscatal.", "acscatal", "ACS Catalysis"),
        ("cs", "acscatal", "ACS Catalysis"),
        ("acs.orglett.", "orglett", "Organic Letters"),
        ("ol", "orglett", "Organic Letters"),
        ("acs.joc.", "joc", "The Journal of Organic Chemistry"),
        ("jo", "joc", "The Journal of Organic Chemistry"),
        ("acs.inorgchem.", "inorgchem", "Inorganic Chemistry"),
        ("ic", "inorgchem", "Inorganic Chemistry"),
        ("acs.analchem.", "analchem", "Analytical Chemistry"),
        ("ac", "analchem", "Analytical Chemistry"),
        ("acs.jproteome.", "jproteome", "Journal of Proteome Research"),
        ("pr", "jproteome", "Journal of Proteome Research"),
        ("acs.biomac.", "biomac", "Biomacromolecules"),
        ("bm", "biomac", "Biomacromolecules"),
        ("acs.iecr.", "iecr", "Industrial & Engineering Chemistry Research"),
        ("ie", "iecr", "Industrial & Engineering Chemistry Research"),
        ("acs.chemrev.", "chemrev", "Chemical Reviews"),
        ("cr", "chemrev", "Chemical Reviews"),
        ("acs.accounts.", "accounts", "Accounts of Chemical Research"),
        ("ar", "accounts", "Accounts of Chemical Research"),
        ("acssuschemeng.", "suschemeng", "ACS Sustainable Chemistry & Engineering"),
        ("acsomega.", "acsomega", "ACS Omega"),
        ("acs.CGD.", "cgd", "Crystal Growth & Design"),
        ("acs.cgd.", "cgd", "Crystal Growth & Design"),
        ("cg", "cgd", "Crystal Growth & Design"),
        ("acs.jpcb.", "jpcb", "The Journal of Physical Chemistry B"),
        ("jp", "jpc", "The Journal of Physical Chemistry (A/B/C/Letters)"),
        ("acs.jcim.", "jcim", "Journal of Chemical Information and Modeling"),
        ("ci", "jcim", "Journal of Chemical Information and Modeling"),
        ("acs.jctc.", "jctc", "Journal of Chemical Theory and Computation"),
        ("ct", "jctc", "Journal of Chemical Theory and Computation"),
        ("acs.langmuir.", "langmuir", "Langmuir"),
        ("la", "langmuir", "Langmuir"),
        ("acs.macromol.", "macromol", "Macromolecules"),
        ("ma", "macromol", "Macromolecules"),
        ("acs.nanolett.", "nanolett", "Nano Letters"),
        ("nl", "nanolett", "Nano Letters"),
        ("acsnano.", "acsnano", "ACS Nano"),
        ("nn", "acsnano", "ACS Nano"),
        ("acsami.", "acsami", "ACS Applied Materials & Interfaces"),
        ("am", "acsami", "ACS Applied Materials & Interfaces"),
        ("acs.energyfuels.", "energyfuels", "Energy & Fuels"),
        ("ef", "energyfuels", "Energy & Fuels"),
        ("acs.chemmater.", "chemmater", "Chemistry of Materials"),
        ("cm", "chemmater", "Chemistry of Materials"),
        ("acs.molpharmaceut.", "molpharm", "Molecular Pharmaceutics"),
        ("mp", "molpharm", "Molecular Pharmaceutics"),
        ("acsmedchemlett.", "medchemlett", "ACS Medicinal Chemistry Letters"),
        ("ml", "medchemlett", "ACS Medicinal Chemistry Letters"),
        ("acscentsci.", "centsci", "ACS Central Science"),
        ("acsearthspacechem.", "earthspace", "ACS Earth and Space Chemistry"),
        ("acsfoodscitech.", "foodscitech", "ACS Food Science & Technology"),
        ("acsagscitech.", "agscitech", "ACS Agricultural Science & Technology"),
        ("acsenvironau.", "environau", "ACS Environmental Au"),
        ("acsbiomedchemau.", "biomedchemau", "ACS Bio & Med Chem Au"),
        ("bp", "biotechprog", "Biotechnology Progress (ACS/AIChE)"),
        ("bk-", "acsbooks", "ACS Symposium Series / Books"),
    ]
    for pref, code, title in prefix_map:
        if suffix.startswith(pref):
            # Avoid matching "ac" before "acs..."
            if pref == "ac" and suffix.startswith("acs"):
                continue
            return code, title
    return "other_acs", "Other ACS Publication"


def extract_mim_chemicals() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Extract all mapped and unmapped ingredients from MediaIngredientMech."""
    mim_dir = MECHS_ROOT / "MediaIngredientMech" / "data" / "curated"
    rows: list[dict[str, Any]] = []
    stats: Counter[str] = Counter()

    for fname in ("mapped_ingredients.yaml", "unmapped_ingredients.yaml"):
        fpath = mim_dir / fname
        if not fpath.exists():
            continue
        data = yaml.safe_load(fpath.read_text(encoding="utf-8"))
        for rec in data.get("ingredients", []) or []:
            ident = str(rec.get("identifier", ""))
            pref = str(rec.get("preferred_term", ""))
            status = str(rec.get("mapping_status", ""))
            ing_type = str(rec.get("ingredient_type", "") or "")
            ont = rec.get("ontology_mapping") or {}
            ont_id = str(ont.get("ontology_id", "") or "")
            ont_source = str(ont.get("ontology_source", "") or "")
            map_qual = str(ont.get("mapping_quality", "") or "")
            chem = rec.get("chemical_properties") or {}
            cas_rn = str(chem.get("cas_rn", "") or "")
            formula = str(chem.get("molecular_formula", "") or "")
            smiles = str(chem.get("smiles", "") or "")
            inchi = str(chem.get("inchi", "") or "")
            occ = (rec.get("occurrence_statistics") or {}).get("total_occurrences", 0) or 0
            nut_roles = [r.get("role", "") for r in (rec.get("nutritional_roles") or []) if isinstance(r, dict)]
            phys_roles = [r.get("role", "") for r in (rec.get("physicochemical_roles") or []) if isinstance(r, dict)]
            cell_roles = [r.get("role", "") for r in (rec.get("cellular_metabolic_roles") or []) if isinstance(r, dict)]
            has_causal = bool(rec.get("causal_graphs"))
            has_components = bool(rec.get("components"))

            # Determine curation need for LBNL databases
            curation_needs: list[str] = []
            target_dbs: list[str] = []
            if status == "UNMAPPED":
                if ing_type == "SINGLE_INGREDIENT":
                    curation_needs.append("unmapped_single_chemical")
                    target_dbs.extend(["SciFinder", "Reaxys", "CHEMnetBASE"])
                    if any(tok in pref.lower() for tok in ("-nta", "-edta", "h2o", "ceno3", "fe", "co", "cu", "cd", "cr", "al")):
                        target_dbs.append("ICSD/WebCSD")
                else:
                    curation_needs.append(f"unmapped_{ing_type.lower() or 'unspecified'}")
                    target_dbs.append("CHEMnetBASE/Literature")
            elif status == "MAPPED":
                if ing_type == "SINGLE_INGREDIENT" and not (smiles or inchi or formula):
                    curation_needs.append("missing_chemical_structure")
                    target_dbs.extend(["SciFinder", "Reaxys"])
                if ident.startswith("cas:") or ident.startswith("kgmicrobe."):
                    curation_needs.append("fallback_registry_identity")
                    target_dbs.extend(["SciFinder", "Reaxys", "CHEMnetBASE"])
                if map_qual in ("NARROW_MATCH", "CLOSE_MATCH"):
                    curation_needs.append(f"non_exact_mapping({map_qual})")
                    target_dbs.append("SciFinder/ICSD")
                if ing_type in ("STOCK_SOLUTION", "UNDEFINED_MIXTURE", "NAMED_MEDIUM") and not has_components:
                    curation_needs.append("missing_partonomy_components")

            stats["total"] += 1
            stats[f"status_{status}"] += 1
            stats[f"type_{ing_type or 'NONE'}"] += 1
            if formula or smiles or inchi:
                stats["has_structure"] += 1
            if cas_rn or ident.startswith("cas:"):
                stats["has_cas"] += 1
            if has_causal:
                stats["has_causal_graph"] += 1
            for cn in curation_needs:
                stats[f"need_{cn}"] += 1

            rows.append(
                {
                    "identifier": ident,
                    "preferred_term": pref,
                    "mapping_status": status,
                    "ingredient_type": ing_type,
                    "ontology_id": ont_id,
                    "ontology_source": ont_source,
                    "mapping_quality": map_qual,
                    "cas_rn": cas_rn or (ident.split(":", 1)[1] if ident.startswith("cas:") else ""),
                    "molecular_formula": formula,
                    "smiles": smiles,
                    "inchi": inchi,
                    "occurrences": occ,
                    "roles": ";".join(nut_roles + phys_roles + cell_roles),
                    "has_causal_graph": has_causal,
                    "has_components": has_components,
                    "curation_needs": ";".join(curation_needs),
                    "recommended_lbl_dbs": ";".join(dict.fromkeys(target_dbs)),
                }
            )
    return rows, dict(stats)


def extract_antibioticmech_chemicals() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Extract all chemical structures from AntibioticMech."""
    ab_root = MECHS_ROOT / "AntibioticMech" / "data"
    cs_map = json.loads((ab_root / "embeddings" / "chemical-structure-map.json").read_text(encoding="utf-8"))
    recs_json = json.loads((ab_root / "embeddings" / "records.json").read_text(encoding="utf-8"))
    moa_by_id = {r["id"]: r.get("mode_of_action") for r in recs_json}

    # Load standard_inchi_key & formula from raw tables where available
    inchikey_by_id: dict[str, str] = {}
    formula_by_id: dict[str, str] = {}
    for raw_tsv in ("chebi_antimicrobials.tsv", "aro_antibiotics.tsv", "pubchem_structures.tsv"):
        p = ab_root / "raw" / raw_tsv
        if not p.exists():
            continue
        with p.open(encoding="utf-8") as fh:
            reader = csv.DictReader(fh, delimiter="\t")
            for r in reader:
                cid = r.get("chebi_id") or r.get("identifier") or r.get("aro_id") or ""
                ik = r.get("standard_inchi_key") or r.get("inchikey") or ""
                mf = r.get("molecular_formula") or r.get("formula") or ""
                if cid and ik and cid not in inchikey_by_id:
                    inchikey_by_id[cid] = ik
                if cid and mf and cid not in formula_by_id:
                    formula_by_id[cid] = mf

    dup_groups = cs_map.get("quality", {}).get("duplicate_inchi_key_groups", [])
    dup_ids = {cid for grp in dup_groups for cid in grp}

    rows: list[dict[str, Any]] = []
    stats: Counter[str] = Counter()
    for rec in cs_map.get("records", []):
        ident = rec.get("identifier", "")
        label = rec.get("label", "")
        ab_class = rec.get("antimicrobial_class", "")
        struct_class = rec.get("structural_class", "")
        cur_status = rec.get("curation_status", "")
        ground_status = rec.get("grounding_status", "")
        smiles = rec.get("canonical_isomeric_smiles", "")
        struct_input = rec.get("structure_input", "")
        frag_count = rec.get("fragment_count", 1)
        moa = moa_by_id.get(ident) or ""

        curation_needs: list[str] = []
        target_dbs: list[str] = []
        if not ident.startswith("CHEBI:"):
            curation_needs.append("minted_non_chebi_identity")
            target_dbs.extend(["SciFinder", "Reaxys", "CHEMnetBASE (Dictionary of Drugs)"])
        if ident in dup_ids:
            curation_needs.append("duplicate_inchikey_conflict")
            target_dbs.extend(["SciFinder", "WebCSD"])
        if struct_input == "INCHI_FALLBACK":
            curation_needs.append("inchi_fallback_no_canonical_smiles")
            target_dbs.append("Reaxys/SciFinder")
        if not moa:
            curation_needs.append("missing_mode_of_action")
            target_dbs.extend(["Reaxys", "CHEMnetBASE", "ACS J.Med.Chem./ACS Infect.Dis."])
        if not struct_class:
            curation_needs.append("missing_structural_class")
        if ab_class == "ANTIMICROBIAL_UNSPECIFIED":
            curation_needs.append("unspecified_antimicrobial_class")

        stats["total"] += 1
        stats[f"class_{ab_class}"] += 1
        stats[f"status_{cur_status}"] += 1
        stats[f"grounding_{ground_status}"] += 1
        if moa:
            stats["has_mode_of_action"] += 1
        if struct_class:
            stats["has_structural_class"] += 1
        for cn in curation_needs:
            stats[f"need_{cn}"] += 1

        rows.append(
            {
                "identifier": ident,
                "label": label,
                "antimicrobial_class": ab_class,
                "structural_class": struct_class,
                "mode_of_action": moa,
                "curation_status": cur_status,
                "grounding_status": ground_status,
                "standard_inchi_key": inchikey_by_id.get(ident, ""),
                "molecular_formula": formula_by_id.get(ident, ""),
                "canonical_isomeric_smiles": smiles,
                "structure_input": struct_input,
                "fragment_count": frag_count,
                "curation_needs": ";".join(curation_needs),
                "recommended_lbl_dbs": ";".join(dict.fromkeys(target_dbs)),
            }
        )
    return rows, dict(stats)


def extract_naturalproductmech_chemicals() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Extract all chemical structures from NaturalProductMech."""
    np_root = MECHS_ROOT / "NaturalProductMech" / "data"
    cs_map = json.loads((np_root / "embeddings" / "chemical-structure-map.json").read_text(encoding="utf-8"))
    recs_json = json.loads((np_root / "embeddings" / "records.json").read_text(encoding="utf-8"))
    producers_by_id = {r["id"]: r.get("producers", []) for r in recs_json}

    # Check causal graph directories
    cg_dir = np_root / "causal_graphs"
    cg_inchikeys = {d.name for d in cg_dir.iterdir() if d.is_dir()} if cg_dir.exists() else set()

    # Map identifier -> standard_inchi_key from npclassifier.tsv or mibig_compounds.tsv / chebi_structures.tsv
    inchikey_by_id: dict[str, str] = {}
    chebi_ik: dict[str, str] = {}
    chebi_struct_path = np_root / "raw" / "chebi_structures.tsv"
    if chebi_struct_path.exists():
        with chebi_struct_path.open(encoding="utf-8") as fh:
            for r in csv.DictReader(fh, delimiter="\t"):
                cid = r.get("chebi_id") or r.get("identifier") or ""
                ik = r.get("standard_inchi_key") or r.get("inchikey") or ""
                if cid and ik:
                    if not cid.startswith("CHEBI:"):
                        cid = f"CHEBI:{cid}"
                    chebi_ik[cid] = ik

    rows: list[dict[str, Any]] = []
    stats: Counter[str] = Counter()
    for rec in cs_map.get("records", []):
        ident = rec.get("identifier", "")
        label = rec.get("label", "")
        np_pathway = rec.get("np_pathway", "")
        comp_classes = rec.get("compound_classes", "")
        cur_status = rec.get("curation_status", "")
        ground_status = rec.get("grounding_status", "")
        smiles = rec.get("canonical_isomeric_smiles", "")
        producers = producers_by_id.get(ident, [])
        ik = chebi_ik.get(ident, inchikey_by_id.get(ident, ""))
        has_cg = ik in cg_inchikeys if ik else False

        curation_needs: list[str] = []
        target_dbs: list[str] = []
        if not ident.startswith("CHEBI:"):
            curation_needs.append("minted_mibig_non_chebi_identity")
            target_dbs.extend(["CHEMnetBASE (Dictionary of Natural Products)", "SciFinder", "Reaxys"])
        if np_pathway == "UNCLASSIFIED":
            curation_needs.append("unclassified_np_pathway")
            target_dbs.append("CHEMnetBASE (DNP)")
        if not producers:
            curation_needs.append("occurrence_only_no_confirmed_producer")
            target_dbs.extend(["ACS J.Nat.Prod.", "CHEMnetBASE (DNP)", "SciFinder"])
        if not has_cg:
            curation_needs.append("missing_causal_graph")

        stats["total"] += 1
        stats[f"pathway_{np_pathway}"] += 1
        stats[f"status_{cur_status}"] += 1
        stats[f"grounding_{ground_status}"] += 1
        if producers:
            stats["has_producers"] += 1
        else:
            stats["no_producers"] += 1
        if has_cg:
            stats["has_causal_graph"] += 1
        for cn in curation_needs:
            stats[f"need_{cn}"] += 1

        rows.append(
            {
                "identifier": ident,
                "label": label,
                "np_pathway": np_pathway,
                "compound_classes": comp_classes,
                "curation_status": cur_status,
                "grounding_status": ground_status,
                "standard_inchi_key": ik,
                "canonical_isomeric_smiles": smiles,
                "producer_count": len(producers),
                "sample_producers": "; ".join(dict.fromkeys(producers[:3])),
                "has_causal_graph": has_cg,
                "curation_needs": ";".join(curation_needs),
                "recommended_lbl_dbs": ";".join(dict.fromkeys(target_dbs)),
            }
        )
    return rows, dict(stats)


def extract_pathwaymech_chemicals() -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    """Extract all chemical participants, BGC products, and reaction conflicts from PathwayMech."""
    pw_dir = MECHS_ROOT / "PathwayMech" / "data" / "pathways"
    chem_categories = {"small_molecule", "chemical_entity", "cofactor", "lipid", "polymer"}

    chem_map: dict[str, dict[str, Any]] = {}
    abstract_only_refs: list[dict[str, Any]] = []
    stats: Counter[str] = Counter()

    for fpath in sorted(pw_dir.glob("*.yaml")):
        rec = yaml.safe_load(fpath.read_text(encoding="utf-8"))
        pw_id = str(rec.get("id", ""))
        pw_label = str(rec.get("label", ""))
        pw_type = str(rec.get("pathway_type", ""))
        stats["total_pathways"] += 1

        # Track conflicts and proposed/inferred mechanism steps per participant
        conflict_by_part: dict[str, list[str]] = defaultdict(list)
        for rm in rec.get("reaction_mechanisms") or []:
            stats["total_reaction_mechanisms"] += 1
            for sc in rm.get("source_conflicts") or []:
                stats["total_source_conflicts"] += 1
                kind = sc.get("kind", "")
                status = sc.get("status", "")
                for pid_key in ("source_participant_id", "interpreted_participant_id"):
                    pid = sc.get(pid_key, "")
                    if pid:
                        conflict_by_part[pid].append(f"{kind}:{status}")

        for p in rec.get("participants") or []:
            cat = str(p.get("category", "") or "")
            pid = str(p.get("id", ""))
            plabel = str(p.get("label", ""))
            # Include explicitly typed chemical categories or CHEBI/MetaCyc CPD/KEGG C/PubChem/CAS/HMDB/LIPIDMAPS
            is_chem = (
                cat in chem_categories
                or pid.startswith(("CHEBI:", "CAS:", "ChemSpider:", "HMDB:", "LIPIDMAPS:", "PubChem:", "metanetx.chemical:"))
                or pid.startswith("MetaCyc:CPD")
            )
            if not is_chem:
                continue

            entry = chem_map.setdefault(
                pid,
                {
                    "identifier": pid,
                    "label": plabel,
                    "category": cat or "chemical_participant",
                    "pathway_count": 0,
                    "pathways": [],
                    "pathway_types": set(),
                    "conflicts": set(),
                    "curation_needs": set(),
                    "recommended_lbl_dbs": set(),
                },
            )
            entry["pathway_count"] += 1
            entry["pathways"].append(f"{pw_id} ({pw_label})")
            if pw_type:
                entry["pathway_types"].add(pw_type)
            for c in conflict_by_part.get(pid, []):
                entry["conflicts"].add(c)
                entry["curation_needs"].add(f"reaction_source_conflict({c})")
                entry["recommended_lbl_dbs"].update(["Reaxys", "SciFinder", "ACS Biochemistry/JACS"])
            if not pid.startswith("CHEBI:"):
                entry["curation_needs"].add("non_chebi_participant_curie")
                entry["recommended_lbl_dbs"].update(["SciFinder", "Reaxys"])

        # Also check gene_clusters products
        for gc in rec.get("gene_clusters") or []:
            gc_id = gc.get("id", "")
            for prod in gc.get("products") or []:
                key = f"{gc_id}:{prod}"
                entry = chem_map.setdefault(
                    key,
                    {
                        "identifier": gc_id,
                        "label": prod,
                        "category": "bgc_product",
                        "pathway_count": 0,
                        "pathways": [],
                        "pathway_types": set(),
                        "conflicts": set(),
                        "curation_needs": {"bgc_product_string_unmapped"},
                        "recommended_lbl_dbs": {"CHEMnetBASE (DNP)", "SciFinder", "ACS J.Nat.Prod./JACS"},
                    },
                )
                entry["pathway_count"] += 1
                entry["pathways"].append(f"{pw_id} ({pw_label})")
                if pw_type:
                    entry["pathway_types"].add(pw_type)

        # Check references for abstract-only or author-manuscript fallback
        for ref in rec.get("references") or []:
            rid = str(ref.get("id", ""))
            rtitle = str(ref.get("title", "") or ref.get("citation", ""))
            rver = str(ref.get("source_version", "") or "")
            rurl = str(ref.get("url", "") or "")
            if "abstract only" in rver.lower() or "author manuscript" in rver.lower() or "10.1021/" in rid.lower() or "10.1021/" in rurl.lower():
                abstract_only_refs.append(
                    {
                        "pathway_file": fpath.name,
                        "pathway_id": pw_id,
                        "pathway_label": pw_label,
                        "reference_id": rid,
                        "title": rtitle,
                        "source_version": rver,
                        "url": rurl,
                    }
                )

    rows: list[dict[str, Any]] = []
    for pid, entry in sorted(chem_map.items(), key=lambda kv: (-kv[1]["pathway_count"], kv[0])):
        stats["unique_chemicals"] += 1
        stats[f"cat_{entry['category']}"] += 1
        if entry["conflicts"]:
            stats["chemicals_with_conflicts"] += 1
        if not pid.startswith("CHEBI:"):
            stats["non_chebi_chemicals"] += 1
        rows.append(
            {
                "identifier": entry["identifier"],
                "label": entry["label"],
                "category": entry["category"],
                "pathway_count": entry["pathway_count"],
                "sample_pathways": "; ".join(entry["pathways"][:4]),
                "pathway_types": "; ".join(sorted(entry["pathway_types"])),
                "conflicts": "; ".join(sorted(entry["conflicts"])),
                "curation_needs": "; ".join(sorted(entry["curation_needs"])),
                "recommended_lbl_dbs": "; ".join(sorted(entry["recommended_lbl_dbs"])),
            }
        )
    return rows, dict(stats), abstract_only_refs


def compute_cross_mech_overlaps(
    mim_rows: list[dict[str, Any]],
    ab_rows: list[dict[str, Any]],
    np_rows: list[dict[str, Any]],
    pw_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Find chemicals shared across 2 or more of the 4 chemical Mechs by CHEBI ID or normalized label."""
    by_chebi: dict[str, dict[str, Any]] = {}

    def register(chebi_id: str, label: str, mech: str, detail: str) -> None:
        if not chebi_id.startswith("CHEBI:"):
            return
        rec = by_chebi.setdefault(
            chebi_id,
            {"chebi_id": chebi_id, "label": label, "mechs": set(), "details": {}},
        )
        if not rec["label"] and label:
            rec["label"] = label
        rec["mechs"].add(mech)
        rec["details"][mech] = detail

    for r in mim_rows:
        cid = r["identifier"] if r["identifier"].startswith("CHEBI:") else r["ontology_id"]
        if cid.startswith("CHEBI:"):
            register(cid, r["preferred_term"], "MediaIngredientMech", f"{r['mapping_status']} (occ={r['occurrences']})")

    for r in ab_rows:
        if r["identifier"].startswith("CHEBI:"):
            register(r["identifier"], r["label"], "AntibioticMech", f"{r['antimicrobial_class']} ({r['curation_status']})")

    for r in np_rows:
        if r["identifier"].startswith("CHEBI:"):
            register(r["identifier"], r["label"], "NaturalProductMech", f"{r['np_pathway']} (producers={r['producer_count']})")

    for r in pw_rows:
        if r["identifier"].startswith("CHEBI:"):
            register(r["identifier"], r["label"], "PathwayMech", f"{r['category']} (pathways={r['pathway_count']})")

    overlaps: list[dict[str, Any]] = []
    for cid, info in by_chebi.items():
        if len(info["mechs"]) >= 2:
            overlaps.append(
                {
                    "chebi_id": cid,
                    "label": info["label"],
                    "mech_count": len(info["mechs"]),
                    "mechs": "; ".join(sorted(info["mechs"])),
                    "mim_detail": info["details"].get("MediaIngredientMech", ""),
                    "antibioticmech_detail": info["details"].get("AntibioticMech", ""),
                    "naturalproductmech_detail": info["details"].get("NaturalProductMech", ""),
                    "pathwaymech_detail": info["details"].get("PathwayMech", ""),
                }
            )
    overlaps.sort(key=lambda x: (-x["mech_count"], x["label"].lower()))
    return overlaps


def resolve_pmids_to_acs_dois(pmids: list[str]) -> dict[str, dict[str, str]]:
    """Query NCBI esummary anonymously (no mailto) in batches of 150 to find PMIDs with 10.1021/* DOIs."""
    resolved: dict[str, dict[str, str]] = {}
    unique_pmids = sorted(set(pmids))
    batch_size = 150
    for i in range(0, len(unique_pmids), batch_size):
        batch = unique_pmids[i : i + batch_size]
        url = (
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?"
            + urllib.parse.urlencode({"db": "pubmed", "id": ",".join(batch), "retmode": "json"})
        )
        req = urllib.request.Request(url, headers={"User-Agent": "FullTextAccess/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                payload = json.loads(resp.read().decode("utf-8", errors="replace"))
            result = payload.get("result", {})
            for pmid in batch:
                item = result.get(pmid) or {}
                doi = ""
                for aid in item.get("articleids") or []:
                    if aid.get("idtype") == "doi":
                        doi = str(aid.get("value", "")).strip()
                        break
                if doi.lower().startswith("10.1021/"):
                    resolved[pmid] = {
                        "doi": clean_doi(doi),
                        "title": str(item.get("title", "")).strip(),
                        "source": str(item.get("source", "")).strip(),
                        "pubdate": str(item.get("pubdate", "")).strip(),
                    }
        except Exception:
            continue
    return resolved


def scan_fleet_for_acs_dois(
    abstract_only_pw_refs: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Scan all Mech repositories (including gitignored research/reports) for 10.1021/* DOIs and PMIDs."""
    mech_repos = [
        "MediaIngredientMech",
        "AntibioticMech",
        "NaturalProductMech",
        "PathwayMech",
        "CommunityMech",
        "CultureMech",
        "HabitatMech",
        "TraitMech",
        "ProteinTraitsMech",
        "CMMMech",
        "CellStructureMech",
        "TaxonMech",
        "DUFMech",
        "EdgeMech",
    ]

    doi_records: dict[str, dict[str, Any]] = {}

    def add_doi(
        raw_doi: str,
        repo: str,
        source_kind: str,
        context: str,
        title: str = "",
        pmid: str = "",
    ) -> None:
        doi = clean_doi(raw_doi)
        if not doi.startswith("10.1021/") or len(doi) < 11:
            return
        jcode, jtitle = classify_acs_journal(doi)
        entry = doi_records.setdefault(
            doi,
            {
                "doi": doi,
                "journal_code": jcode,
                "journal_title": jtitle,
                "repos": set(),
                "source_kinds": set(),
                "contexts": [],
                "title": title,
                "pmid": pmid,
            },
        )
        entry["repos"].add(repo)
        entry["source_kinds"].add(source_kind)
        if context and len(entry["contexts"]) < 5 and context not in entry["contexts"]:
            entry["contexts"].append(context)
        if title and not entry["title"]:
            entry["title"] = title
        if pmid and not entry["pmid"]:
            entry["pmid"] = pmid

    # 1. Scan structured raw TSVs in NaturalProductMech and AntibioticMech for high-context DOIs and PMIDs
    pmids_to_check: dict[str, list[tuple[str, str, str]]] = defaultdict(list)

    for repo in mech_repos:
        rpath = MECHS_ROOT / repo
        if not rpath.exists():
            continue
        scan_dirs = [
            rpath / "data",
            rpath / "reports",
            rpath / "research",
            rpath / "reviews",
            rpath / "references_cache",
            rpath / "docs",
        ]
        for sdir in scan_dirs:
            if not sdir.exists():
                continue
            for fpath in sdir.rglob("*"):
                if not fpath.is_file():
                    continue
                if fpath.suffix.lower() not in (".yaml", ".yml", ".tsv", ".csv", ".md", ".json", ".txt"):
                    continue
                # Skip huge embedding vectors/maps
                if fpath.name in ("chemical-structure-map.json", "corpus_map.json", "sequence_map.json"):
                    continue
                if fpath.stat().st_size > 25_000_000:
                    continue
                try:
                    text = fpath.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue

                rel = f"{repo}/{fpath.relative_to(rpath)}"
                skind = "curated_data" if "data/" in rel and "data/raw/" not in rel else (
                    "raw_inventory" if "data/raw/" in rel else (
                        "missing_pdf_or_report" if "reports/" in rel or "reviews/" in rel else "research_notes"
                    )
                )

                for m in DOI_ACS_RE.finditer(text):
                    add_doi(m.group(1), repo, skind, rel)

                # Collect PMIDs from PathwayMech, AntibioticMech raw, NaturalProductMech raw, CMMMech, ProteinTraitsMech
                if repo in ("PathwayMech", "CMMMech", "ProteinTraitsMech") or "bindingdb" in fpath.name or "phibase" in fpath.name or "mibig" in fpath.name:
                    for pm in PMID_RE.finditer(text):
                        pmid_val = pm.group(1)
                        if len(pmids_to_check[pmid_val]) < 4:
                            pmids_to_check[pmid_val].append((repo, skind, rel))

    # Also explicitly add abstract-only PathwayMech PMIDs
    for ref in abstract_only_pw_refs:
        rid = ref["reference_id"]
        if rid.startswith("PMID:"):
            pmid_val = rid.split(":", 1)[1]
            pmids_to_check[pmid_val].append(
                ("PathwayMech", "pathway_abstract_only_ref", f"{ref['pathway_file']} ({ref['pathway_label']})")
            )

    # Resolve collected PMIDs to check which ones are ACS (10.1021/*) papers
    resolved_pmids = resolve_pmids_to_acs_dois(list(pmids_to_check.keys()))
    for pmid_val, meta in resolved_pmids.items():
        for repo, skind, ctx in pmids_to_check.get(pmid_val, []):
            add_doi(
                meta["doi"],
                repo,
                f"{skind}_via_pmid",
                f"{ctx} [PMID:{pmid_val}]",
                title=meta["title"],
                pmid=pmid_val,
            )

    # Build master DOI table and journal summary table
    master_rows: list[dict[str, Any]] = []
    journal_counts: dict[tuple[str, str], dict[str, Any]] = {}

    for doi, info in sorted(doi_records.items(), key=lambda kv: (kv[1]["journal_code"], kv[0])):
        repos_sorted = sorted(info["repos"])
        skinds_sorted = sorted(info["source_kinds"])
        # Priority score: multi-repo, curated_data or pathway_abstract_only_ref or missing_pdf_or_report
        priority = "HIGH" if (
            len(repos_sorted) >= 2
            or any("abstract_only" in s or "curated_data" in s or "missing_pdf" in s for s in skinds_sorted)
        ) else "STANDARD"

        master_rows.append(
            {
                "doi": doi,
                "journal_code": info["journal_code"],
                "journal_title": info["journal_title"],
                "priority": priority,
                "mech_count": len(repos_sorted),
                "mechs": "; ".join(repos_sorted),
                "source_kinds": "; ".join(skinds_sorted),
                "pmid": info["pmid"],
                "title": info["title"],
                "sample_contexts": "; ".join(info["contexts"][:3]),
            }
        )

        jkey = (info["journal_code"], info["journal_title"])
        jentry = journal_counts.setdefault(
            jkey,
            {
                "journal_code": info["journal_code"],
                "journal_title": info["journal_title"],
                "doi_count": 0,
                "high_priority_count": 0,
                "mechs": set(),
                "sample_dois": [],
            },
        )
        jentry["doi_count"] += 1
        if priority == "HIGH":
            jentry["high_priority_count"] += 1
        jentry["mechs"].update(repos_sorted)
        if len(jentry["sample_dois"]) < 4:
            jentry["sample_dois"].append(doi)

    journal_rows = [
        {
            "journal_code": v["journal_code"],
            "journal_title": v["journal_title"],
            "doi_count": v["doi_count"],
            "high_priority_count": v["high_priority_count"],
            "mechs": "; ".join(sorted(v["mechs"])),
            "sample_dois": "; ".join(v["sample_dois"]),
        }
        for v in sorted(journal_counts.values(), key=lambda x: (-x["doi_count"], x["journal_title"]))
    ]
    return master_rows, journal_rows


def write_tsv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = list(rows[0].keys())
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)


def summarize_existing() -> None:
    """Fast post-processing on existing TSVs in OUT_DIR to write high-priority DOI list and print sample tables."""
    master_path = OUT_DIR / "acs_dois_master.tsv"
    if master_path.is_file():
        with master_path.open("r", encoding="utf-8") as fh:
            reader = list(csv.DictReader(fh, delimiter="\t"))
        high_dois = [r["doi"] for r in reader if r.get("priority") == "HIGH"]
        (OUT_DIR / "acs_dois_high_priority_only.txt").write_text(
            "\n".join(high_dois) + "\n",
            encoding="utf-8",
        )
        by_mech_high: dict[str, int] = defaultdict(int)
        by_mech_all: dict[str, int] = defaultdict(int)
        for r in reader:
            for m in (r.get("mechs") or "").split("; "):
                if m:
                    by_mech_all[m] += 1
                    if r.get("priority") == "HIGH":
                        by_mech_high[m] += 1
        print("ACS DOIs by Mech (total / HIGH):")
        for m in sorted(by_mech_all, key=lambda k: -by_mech_all[k]):
            print(f"  {m}: {by_mech_all[m]} total / {by_mech_high[m]} HIGH")

    for name in ("mim_chemicals.tsv", "antibioticmech_chemicals.tsv", "naturalproductmech_chemicals.tsv", "pathwaymech_chemicals.tsv"):
        p = OUT_DIR / name
        if not p.is_file():
            continue
        with p.open("r", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh, delimiter="\t"))
        print(f"\n=== {name} ({len(rows)} rows) ===")
        if name == "mim_chemicals.tsv":
            unmapped_single = [r for r in rows if r["mapping_status"] == "UNMAPPED" and r["ingredient_type"] == "SINGLE_INGREDIENT"]
            print(f"Unmapped SINGLE_INGREDIENT ({len(unmapped_single)}): " + ", ".join(r["preferred_term"] for r in unmapped_single[:35]))
        elif name == "antibioticmech_chemicals.tsv":
            dups = [r for r in rows if "duplicate_inchikey" in r["curation_needs"]]
            minted = [r for r in rows if r["grounding_status"] == "MINTED"]
            print(f"Duplicate InChIKey records ({len(dups)}): " + ", ".join(f"{r['identifier']} ({r['label']})" for r in dups[:16]))
            print(f"Sample MINTED records ({len(minted)}): " + ", ".join(f"{r['identifier']} ({r['label']})" for r in minted[:15]))
        elif name == "naturalproductmech_chemicals.tsv":
            rev = [r for r in rows if r["grounding_status"] == "REVIEW_NEEDED"]
            cg = [r for r in rows if r["has_causal_graph"] == "True"]
            noprod = [r for r in rows if r["producer_count"] == "0"]
            print(f"REVIEW_NEEDED ({len(rev)}): " + ", ".join(f"{r['identifier']} ({r['label']})" for r in rev))
            print(f"Has causal_graph ({len(cg)}): " + ", ".join(f"{r['identifier']} ({r['label']})" for r in cg))
            print(f"No confirmed producer ({len(noprod)} sample): " + ", ".join(f"{r['identifier']} ({r['label']})" for r in noprod[:15]))
        elif name == "pathwaymech_chemicals.tsv":
            conf = [r for r in rows if r["conflicts"]]
            bgc = [r for r in rows if r["category"] == "bgc_product"]
            non_chebi = [r for r in rows if not r["identifier"].startswith("CHEBI:")]
            print(f"Conflict chemicals ({len(conf)}): " + ", ".join(f"{r['identifier']} ({r['label']}: {r['conflicts']})" for r in conf))
            print(f"BGC products ({len(bgc)}): " + ", ".join(f"{r['identifier']} ({r['label']})" for r in bgc))
            print(f"Sample non-ChEBI ({len(non_chebi)}): " + ", ".join(f"{r['identifier']} ({r['label']})" for r in non_chebi[:20]))


def main() -> None:
    import sys
    if "--summarize-existing" in sys.argv:
        summarize_existing()
        return
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    mim_rows, mim_stats = extract_mim_chemicals()
    ab_rows, ab_stats = extract_antibioticmech_chemicals()
    np_rows, np_stats = extract_naturalproductmech_chemicals()
    pw_rows, pw_stats, abstract_only_pw_refs = extract_pathwaymech_chemicals()
    overlaps = compute_cross_mech_overlaps(mim_rows, ab_rows, np_rows, pw_rows)

    write_tsv(OUT_DIR / "mim_chemicals.tsv", mim_rows)
    write_tsv(OUT_DIR / "antibioticmech_chemicals.tsv", ab_rows)
    write_tsv(OUT_DIR / "naturalproductmech_chemicals.tsv", np_rows)
    write_tsv(OUT_DIR / "pathwaymech_chemicals.tsv", pw_rows)
    write_tsv(OUT_DIR / "cross_mech_chemical_overlaps.tsv", overlaps)

    # Compile a unified priority chemical lookup list for LBNL chemistry databases
    priority_chems: list[dict[str, Any]] = []
    for r in mim_rows:
        if r["curation_needs"] and r["ingredient_type"] == "SINGLE_INGREDIENT":
            priority_chems.append(
                {
                    "mech": "MediaIngredientMech",
                    "identifier": r["identifier"],
                    "name": r["preferred_term"],
                    "category_or_class": r["ingredient_type"],
                    "curation_need": r["curation_needs"],
                    "recommended_lbl_dbs": r["recommended_lbl_dbs"],
                }
            )
    for r in pw_rows:
        if r["conflicts"] or r["category"] == "bgc_product":
            priority_chems.append(
                {
                    "mech": "PathwayMech",
                    "identifier": r["identifier"],
                    "name": r["label"],
                    "category_or_class": r["category"],
                    "curation_need": r["curation_needs"],
                    "recommended_lbl_dbs": r["recommended_lbl_dbs"],
                }
            )
    for r in ab_rows:
        if "duplicate_inchikey_conflict" in r["curation_needs"] or "minted_non_chebi_identity" in r["curation_needs"]:
            priority_chems.append(
                {
                    "mech": "AntibioticMech",
                    "identifier": r["identifier"],
                    "name": r["label"],
                    "category_or_class": r["antimicrobial_class"],
                    "curation_need": r["curation_needs"],
                    "recommended_lbl_dbs": r["recommended_lbl_dbs"],
                }
            )
    for r in np_rows:
        if "minted_mibig_non_chebi_identity" in r["curation_needs"] or r["has_causal_graph"]:
            priority_chems.append(
                {
                    "mech": "NaturalProductMech",
                    "identifier": r["identifier"],
                    "name": r["label"],
                    "category_or_class": r["np_pathway"],
                    "curation_need": r["curation_needs"] or "causal_graph_expansion",
                    "recommended_lbl_dbs": r["recommended_lbl_dbs"] or "Reaxys;SciFinder;ACS J.Nat.Prod.",
                }
            )
    write_tsv(OUT_DIR / "priority_chemicals_for_lbl_databases.tsv", priority_chems)

    acs_master, acs_journals = scan_fleet_for_acs_dois(abstract_only_pw_refs)
    write_tsv(OUT_DIR / "acs_dois_master.tsv", acs_master)
    write_tsv(OUT_DIR / "acs_journals_summary.tsv", acs_journals)
    (OUT_DIR / "acs_dois_list_only.txt").write_text(
        "\n".join(r["doi"] for r in acs_master) + "\n",
        encoding="utf-8",
    )
    (OUT_DIR / "acs_dois_high_priority_only.txt").write_text(
        "\n".join(r["doi"] for r in acs_master if r["priority"] == "HIGH") + "\n",
        encoding="utf-8",
    )

    summary = {
        "MediaIngredientMech": mim_stats,
        "AntibioticMech": ab_stats,
        "NaturalProductMech": np_stats,
        "PathwayMech": pw_stats,
        "cross_mech_overlaps_count": len(overlaps),
        "priority_chemicals_count": len(priority_chems),
        "acs_dois_total_unique": len(acs_master),
        "acs_dois_high_priority": sum(1 for r in acs_master if r["priority"] == "HIGH"),
        "acs_journals_breakdown": acs_journals,
        "pathwaymech_abstract_or_acs_refs": abstract_only_pw_refs,
    }
    (OUT_DIR / "summary_stats.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(
        {
            "mim_total": mim_stats.get("total", 0),
            "antibioticmech_total": ab_stats.get("total", 0),
            "naturalproductmech_total": np_stats.get("total", 0),
            "pathwaymech_unique_chemicals": pw_stats.get("unique_chemicals", 0),
            "cross_mech_overlaps": len(overlaps),
            "priority_chemicals": len(priority_chems),
            "acs_dois_total": len(acs_master),
            "acs_dois_high_priority": sum(1 for r in acs_master if r["priority"] == "HIGH"),
        },
        indent=2,
    ))


if __name__ == "__main__":
    main()
