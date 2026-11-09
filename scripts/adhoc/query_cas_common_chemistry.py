#!/usr/bin/env python3
"""Cache-backed CAS Common Chemistry REST API client and Mech chemical enricher.

Purpose: Query the CAS Common Chemistry Direct API (`https://commonchemistry.cas.org/direct-api/detail`
  and `https://commonchemistry.cas.org/direct-api/search`) using `CAS_COMMON_CHEMISTRY_API_KEY`
  from `~/.config/lbnl-tdm.env`, cache all responses in SQLite, enrich priority chemical gaps
  across MediaIngredientMech, AntibioticMech, NaturalProductMech, and PathwayMech, and emit a
  residual batch file for compounds requiring full 165M-substance CAS SciFinder lookup.
Inputs:
  - `/Users/marcin/Documents/VIMSS/ontology/Mechs/XMechBay/.temp/mech-chemicals-and-acs-dois/mim_chemicals.tsv`
  - `/Users/marcin/Documents/VIMSS/ontology/Mechs/XMechBay/.temp/mech-chemicals-and-acs-dois/antibioticmech_chemicals.tsv`
  - `/Users/marcin/Documents/VIMSS/ontology/Mechs/XMechBay/.temp/mech-chemicals-and-acs-dois/naturalproductmech_chemicals.tsv`
  - `/Users/marcin/Documents/VIMSS/ontology/Mechs/XMechBay/.temp/mech-chemicals-and-acs-dois/pathwaymech_chemicals.tsv`
Outputs:
  - SQLite cache: `/Users/marcin/Documents/VIMSS/ontology/Mechs/XMechBay/.temp/mech-chemicals-and-acs-dois/cas_common_chemistry_cache.sqlite`
  - Enriched TSV: `/Users/marcin/Documents/VIMSS/ontology/Mechs/XMechBay/.temp/mech-chemicals-and-acs-dois/cas_common_chemistry_enriched.tsv`
  - Residual TSV: `/Users/marcin/Documents/VIMSS/ontology/Mechs/XMechBay/.temp/mech-chemicals-and-acs-dois/scifinder_residual_batch.tsv`
Serves: Mech chemical structure & CAS-RN enrichment workflow (2026-11-09).
Date: 2026-11-09
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import os
import re
import sqlite3
import time
import urllib.parse
from pathlib import Path
from typing import Any

import requests

BASE_DIR = Path(
    "/Users/marcin/Documents/VIMSS/ontology/Mechs/XMechBay/.temp/mech-chemicals-and-acs-dois"
)
CACHE_DB = BASE_DIR / "cas_common_chemistry_cache.sqlite"
ENRICHED_TSV = BASE_DIR / "cas_common_chemistry_enriched.tsv"
RESIDUAL_TSV = BASE_DIR / "scifinder_residual_batch.tsv"
ENV_FILE = Path.home() / ".config" / "lbnl-tdm.env"

CAS_API_BASE = "https://commonchemistry.cas.org/direct-api"
CAS_ORIGIN = "https://commonchemistry.cas.org/api-overview"
CAS_RN_RE = re.compile(r"^\d{2,7}-\d{2}-\d$")
HTML_TAG_RE = re.compile(r"<[^>]+>")


def load_cas_key() -> str:
    key = (
        os.environ.get("CAS_COMMON_CHEMISTRY_API_KEY")
        or os.environ.get("CAS_API_KEY")
        or ""
    ).strip()
    if key:
        return key
    if ENV_FILE.is_file():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            m = re.match(
                r"^(?:export\s+)?(CAS_COMMON_CHEMISTRY_API_KEY|CAS_API_KEY)=(.*)$",
                line,
            )
            if m:
                val = m.group(2).strip().strip("\"'")
                if val and not val.startswith("$"):
                    return val
    return ""


def strip_html(text: str | None) -> str:
    if not text:
        return ""
    return html.unescape(HTML_TAG_RE.sub("", text)).strip()


class CasCommonChemistryClient:
    def __init__(self, api_key: str, cache_path: Path = CACHE_DB, delay_s: float = 0.25) -> None:
        self.api_key = api_key
        self.cache_path = cache_path
        self.delay_s = delay_s
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.cache_path))
        self._init_db()
        self.session = requests.Session()
        self.session.headers.update(
            {
                "accept": "application/json",
                "x-origin": CAS_ORIGIN,
                "X-API-KEY": self.api_key,
                "User-Agent": "XMechBay-CASCommonChemistry/1.0",
            }
        )
        self.network_calls = 0
        self.cache_hits = 0

    def _init_db(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cas_cache (
                endpoint TEXT NOT NULL,
                query_key TEXT NOT NULL,
                status_code INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                fetched_at REAL NOT NULL,
                PRIMARY KEY (endpoint, query_key)
            )
            """
        )
        self.conn.commit()

    def _get_cached(self, endpoint: str, query_key: str) -> tuple[int, dict[str, Any]] | None:
        row = self.conn.execute(
            "SELECT status_code, payload_json FROM cas_cache WHERE endpoint = ? AND query_key = ?",
            (endpoint, query_key),
        ).fetchone()
        if row is None:
            return None
        self.cache_hits += 1
        return int(row[0]), json.loads(row[1])

    def _put_cached(self, endpoint: str, query_key: str, status_code: int, payload: dict[str, Any]) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO cas_cache (endpoint, query_key, status_code, payload_json, fetched_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (endpoint, query_key, status_code, json.dumps(payload, ensure_ascii=False), time.time()),
        )
        self.conn.commit()

    def detail(self, cas_rn: str) -> tuple[int, dict[str, Any]]:
        cas_rn = cas_rn.strip()
        cached = self._get_cached("detail", cas_rn)
        if cached is not None:
            return cached
        if self.network_calls > 0 and self.delay_s > 0:
            time.sleep(self.delay_s)
        url = f"{CAS_API_BASE}/detail?cas_rn={urllib.parse.quote(cas_rn)}"
        resp = self.session.get(url, timeout=20)
        self.network_calls += 1
        try:
            payload = resp.json() if resp.text else {}
        except Exception:
            payload = {"raw_error": resp.text[:300]}
        if resp.status_code in (200, 404):
            self._put_cached("detail", cas_rn, resp.status_code, payload)
        return resp.status_code, payload

    def search(self, query: str) -> tuple[int, dict[str, Any]]:
        query = query.strip()
        cached = self._get_cached("search", query)
        if cached is not None:
            return cached
        if self.network_calls > 0 and self.delay_s > 0:
            time.sleep(self.delay_s)
        url = f"{CAS_API_BASE}/search?q={urllib.parse.quote(query)}"
        resp = self.session.get(url, timeout=20)
        self.network_calls += 1
        try:
            payload = resp.json() if resp.text else {}
        except Exception:
            payload = {"raw_error": resp.text[:300]}
        if resp.status_code in (200, 404):
            self._put_cached("search", query, resp.status_code, payload)
        return resp.status_code, payload


def load_targets(group: str) -> list[dict[str, str]]:
    targets: list[dict[str, str]] = []

    mim_path = BASE_DIR / "mim_chemicals.tsv"
    if mim_path.is_file() and group in (
        "canary",
        "mim_cas_fallbacks",
        "mim_missing_structure",
        "mim_unmapped_single",
        "all_priority",
    ):
        with mim_path.open(encoding="utf-8") as fh:
            for row in csv.DictReader(fh, delimiter="\t"):
                ident = row["identifier"]
                name = row["preferred_term"]
                ing_type = row["ingredient_type"]
                status = row["mapping_status"]
                cas_rn = row["cas_rn"].strip()
                if not cas_rn and ident.startswith("cas:"):
                    cas_rn = ident.split(":", 1)[1].strip()
                needs = row["curation_needs"]

                if group == "canary":
                    if ident == "cas:68399-81-5":
                        targets.append(
                            {
                                "group": "mim_cas_fallbacks",
                                "mech": "MediaIngredientMech",
                                "identifier": ident,
                                "name": name,
                                "cas_rn": cas_rn,
                                "inchikey": "",
                            }
                        )
                        return targets
                    continue

                if group in ("mim_cas_fallbacks", "all_priority") and ident.startswith("cas:"):
                    targets.append(
                        {
                            "group": "mim_cas_fallbacks",
                            "mech": "MediaIngredientMech",
                            "identifier": ident,
                            "name": name,
                            "cas_rn": cas_rn,
                            "inchikey": "",
                        }
                    )
                elif (
                    group in ("mim_missing_structure", "all_priority")
                    and status == "MAPPED"
                    and ing_type == "SINGLE_INGREDIENT"
                    and "missing_chemical_structure" in needs
                ):
                    targets.append(
                        {
                            "group": "mim_missing_structure",
                            "mech": "MediaIngredientMech",
                            "identifier": ident,
                            "name": name,
                            "cas_rn": cas_rn,
                            "inchikey": "",
                        }
                    )
                elif (
                    group in ("mim_unmapped_single", "all_priority")
                    and status == "UNMAPPED"
                    and ing_type == "SINGLE_INGREDIENT"
                ):
                    targets.append(
                        {
                            "group": "mim_unmapped_single",
                            "mech": "MediaIngredientMech",
                            "identifier": ident,
                            "name": name,
                            "cas_rn": cas_rn,
                            "inchikey": "",
                        }
                    )

    ab_path = BASE_DIR / "antibioticmech_chemicals.tsv"
    if ab_path.is_file() and group in ("antibiotic_inchi_fallback", "antibiotic_minted", "all_priority"):
        with ab_path.open(encoding="utf-8") as fh:
            for row in csv.DictReader(fh, delimiter="\t"):
                ident = row["identifier"]
                name = row["label"]
                ik = row["standard_inchi_key"].strip()
                needs = row["curation_needs"]
                if group in ("antibiotic_inchi_fallback", "all_priority") and "inchi_fallback" in needs:
                    targets.append(
                        {
                            "group": "antibiotic_inchi_fallback",
                            "mech": "AntibioticMech",
                            "identifier": ident,
                            "name": name,
                            "cas_rn": "",
                            "inchikey": ik,
                        }
                    )
                elif group in ("antibiotic_minted", "all_priority") and "minted_non_chebi_identity" in needs:
                    targets.append(
                        {
                            "group": "antibiotic_minted",
                            "mech": "AntibioticMech",
                            "identifier": ident,
                            "name": name,
                            "cas_rn": "",
                            "inchikey": ik,
                        }
                    )

    np_path = BASE_DIR / "naturalproductmech_chemicals.tsv"
    if np_path.is_file() and group in ("naturalproduct_review_needed", "all_priority"):
        with np_path.open(encoding="utf-8") as fh:
            for row in csv.DictReader(fh, delimiter="\t"):
                if row["grounding_status"] == "REVIEW_NEEDED":
                    targets.append(
                        {
                            "group": "naturalproduct_review_needed",
                            "mech": "NaturalProductMech",
                            "identifier": row["identifier"],
                            "name": row["label"],
                            "cas_rn": "",
                            "inchikey": row["standard_inchi_key"].strip(),
                        }
                    )

    return targets


def resolve_record(
    client: CasCommonChemistryClient, target: dict[str, str]
) -> tuple[bool, dict[str, Any], str]:
    cas_rn = target.get("cas_rn", "").strip()
    inchikey = target.get("inchikey", "").strip()
    name = target.get("name", "").strip()

    if cas_rn and CAS_RN_RE.match(cas_rn):
        code, detail = client.detail(cas_rn)
        if code == 200 and detail.get("rn"):
            return True, detail, f"detail(cas_rn={cas_rn})"

    if inchikey:
        for q_try in (f"InChIKey={inchikey}", inchikey):
            code, srch = client.search(q_try)
            if code == 200 and srch.get("count", 0) > 0:
                first_rn = srch["results"][0].get("rn", "")
                if first_rn:
                    d_code, detail = client.detail(first_rn)
                    if d_code == 200 and detail.get("rn"):
                        return True, detail, f"search(inchikey={inchikey})->detail({first_rn})"

    if name:
        code, srch = client.search(name)
        if code == 200 and srch.get("count", 0) > 0:
            first_rn = srch["results"][0].get("rn", "")
            if first_rn:
                d_code, detail = client.detail(first_rn)
                if d_code == 200 and detail.get("rn"):
                    return True, detail, f"search(name={name})->detail({first_rn})"

    return False, {}, "not_found_in_common_chemistry"


def format_enriched_row(
    target: dict[str, str], detail: dict[str, Any], match_method: str
) -> dict[str, str]:
    exp_props = detail.get("experimentalProperties") or []
    prop_strs = [
        f"{p.get('name')}: {p.get('property')}"
        for p in exp_props
        if isinstance(p, dict) and p.get("name") and p.get("property")
    ]
    synonyms = detail.get("synonyms") or []
    raw_ik = str(detail.get("inchiKey") or "").strip()
    if raw_ik.upper().startswith("INCHIKEY="):
        raw_ik = raw_ik.split("=", 1)[1].strip()
    return {
        "group": target["group"],
        "mech": target["mech"],
        "identifier": target["identifier"],
        "query_name": target["name"],
        "match_method": match_method,
        "cas_rn": str(detail.get("rn") or ""),
        "cas_name": strip_html(detail.get("name")),
        "molecular_formula": strip_html(detail.get("molecularFormula")),
        "molecular_mass": str(detail.get("molecularMass") or ""),
        "canonical_smiles": str(detail.get("canonicalSmile") or detail.get("smiles") or ""),
        "inchi": str(detail.get("inchi") or ""),
        "inchikey": raw_ik,
        "experimental_properties": " | ".join(prop_strs[:5]),
        "synonyms_sample": " | ".join(strip_html(s) for s in synonyms[:8]),
        "replaced_rns": "|".join(detail.get("replacedRns") or []),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--plan",
        "--dry-run",
        dest="plan",
        action="store_true",
        help="Show target counts per priority group without making API calls",
    )
    parser.add_argument(
        "--canary",
        action="store_true",
        help="Run a 1-record end-to-end canary (cas:68399-81-5, TAPSO) and verify cache/TSV output",
    )
    parser.add_argument(
        "--group",
        default="mim_cas_fallbacks",
        choices=[
            "canary",
            "mim_cas_fallbacks",
            "mim_missing_structure",
            "mim_unmapped_single",
            "antibiotic_inchi_fallback",
            "antibiotic_minted",
            "naturalproduct_review_needed",
            "all_priority",
        ],
        help="Target chemical group to query (default: mim_cas_fallbacks)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of records to process in this run",
    )
    args = parser.parse_args()

    if args.plan:
        print("=== CAS Common Chemistry Enrichment Plan (Dry-Run) ===")
        for grp in (
            "mim_cas_fallbacks",
            "mim_missing_structure",
            "mim_unmapped_single",
            "antibiotic_inchi_fallback",
            "antibiotic_minted",
            "naturalproduct_review_needed",
            "all_priority",
        ):
            rows = load_targets(grp)
            print(f"  {grp:<30} : {len(rows):>5} records")
        return 0

    cas_key = load_cas_key()
    if not cas_key:
        print("ERROR: CAS_COMMON_CHEMISTRY_API_KEY not found in environment or ~/.config/lbnl-tdm.env")
        return 1

    group = "canary" if args.canary else args.group
    targets = load_targets(group)
    if args.limit is not None:
        targets = targets[: args.limit]

    client = CasCommonChemistryClient(api_key=cas_key)
    enriched_rows: list[dict[str, str]] = []
    residual_rows: list[dict[str, str]] = []

    print(
        f"=== Running CAS Common Chemistry Lookup (group={group}, count={len(targets)}) ===",
        flush=True,
    )
    for idx, t in enumerate(targets, 1):
        ok, detail, method = resolve_record(client, t)
        if ok:
            row = format_enriched_row(t, detail, method)
            enriched_rows.append(row)
            if args.canary or idx <= 10 or idx % 25 == 0:
                print(
                    f"  [{idx}/{len(targets)}] HIT  {t['identifier']} ({t['name']}) -> "
                    f"CAS {row['cas_rn']} | {row['molecular_formula']} | MW={row['molecular_mass']} | "
                    f"InChIKey={row['inchikey']}",
                    flush=True,
                )
        else:
            residual_rows.append(
                {
                    "group": t["group"],
                    "mech": t["mech"],
                    "identifier": t["identifier"],
                    "name": t["name"],
                    "cas_rn": t["cas_rn"],
                    "inchikey": t["inchikey"],
                    "reason": method,
                }
            )
            if args.canary or idx <= 10 or idx % 25 == 0:
                print(
                    f"  [{idx}/{len(targets)}] MISS {t['identifier']} ({t['name']}) -> queued for SciFinder web batch",
                    flush=True,
                )

    if enriched_rows:
        fieldnames = list(enriched_rows[0].keys())
        with ENRICHED_TSV.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames, delimiter="\t")
            writer.writeheader()
            writer.writerows(enriched_rows)

    if residual_rows:
        fieldnames = list(residual_rows[0].keys())
        with RESIDUAL_TSV.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames, delimiter="\t")
            writer.writeheader()
            writer.writerows(residual_rows)

    cache_count = client.conn.execute("SELECT COUNT(*) FROM cas_cache").fetchone()[0]
    print("\n=== Summary & Side-Effect Verification ===")
    print(f"  Processed        : {len(targets)}")
    print(f"  Tier-1 CAS Hits  : {len(enriched_rows)}")
    print(f"  Tier-2 Residuals : {len(residual_rows)}")
    print(f"  Network Calls    : {client.network_calls} (Cache Hits: {client.cache_hits})")
    print(f"  SQLite Cache     : {CACHE_DB} ({cache_count} total cached entries, {CACHE_DB.stat().st_size} bytes)")
    if enriched_rows and ENRICHED_TSV.is_file():
        print(f"  Enriched TSV     : {ENRICHED_TSV} ({ENRICHED_TSV.stat().st_size} bytes)")
    if residual_rows and RESIDUAL_TSV.is_file():
        print(f"  Residual TSV     : {RESIDUAL_TSV} ({RESIDUAL_TSV.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
