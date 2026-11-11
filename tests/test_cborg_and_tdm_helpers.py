"""Unit tests for CBORG MCP permission and LBNL TDM key setup helpers."""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_module(name: str, rel_path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


allow_mcp = _load_module("allow_cborg_mcp_tools", "scripts/adhoc/allow_cborg_mcp_tools.py")
setup_tdm = _load_module("setup_lbnl_tdm_keys", "scripts/adhoc/setup_lbnl_tdm_keys.py")
query_cas = _load_module("query_cas_common_chemistry", "scripts/adhoc/query_cas_common_chemistry.py")
worklists = _load_module(
    "build_unpromoted_provenance_worklists",
    "scripts/adhoc/build_unpromoted_provenance_worklists.py",
)
tier2_batches = _load_module(
    "build_tier2_lbnl_database_batches",
    "scripts/adhoc/build_tier2_lbnl_database_batches.py",
)
public_gate = _load_module("check_public_content", "scripts/check_public_content.py")


class CborgMcpHelperTests(unittest.TestCase):
    def test_new_helpers_and_docs_pass_public_content_scan(self):
        rel_paths = [
            "docs/lbnl-tdm-and-cborg-mcp.md",
            "scripts/adhoc/allow_cborg_mcp_tools.py",
            "scripts/adhoc/build_tier2_lbnl_database_batches.py",
            "scripts/adhoc/build_unpromoted_provenance_worklists.py",
            "scripts/adhoc/compile_mech_chemicals_and_acs_dois.py",
            "scripts/adhoc/configure_cborg_tavily.sh",
            "scripts/adhoc/enable_cborg_mcp.py",
            "scripts/adhoc/probe_cborg_tavily.py",
            "scripts/adhoc/query_cas_common_chemistry.py",
            "scripts/adhoc/rename_agy_session_and_tab.py",
            "scripts/adhoc/setup_lbnl_tdm_keys.py",
            "tests/test_cborg_and_tdm_helpers.py",
        ]
        findings = set()
        for rel in rel_paths:
            findings.update(public_gate.scan_path(rel, f"worktree:{rel}", []))
            findings.update(
                public_gate.scan_blob(
                    (ROOT / rel).read_bytes(),
                    f"worktree:{rel}",
                    public_gate.DEFAULT_MAX_BYTES,
                    [],
                )
            )
        self.assertEqual(findings, set(), [f.render() for f in sorted(findings)])

    def test_agy_rules_use_slash_syntax_and_keep_feedback_on_ask(self):
        allow_rules = allow_mcp.desired_agy_allow_rules()
        ask_rules = allow_mcp.desired_agy_ask_rules()

        self.assertIn("mcp(context7/*)", allow_rules)
        self.assertIn("mcp(tavily/*)", allow_rules)
        self.assertIn("mcp(valency/*)", allow_rules)
        self.assertIn("mcp(valency/valency-list_sources)", allow_rules)
        self.assertNotIn("mcp(valency/valency-submit_feedback)", allow_rules)
        self.assertIn("mcp(valency/valency-submit_feedback)", ask_rules)
        self.assertTrue(all(":" not in rule for rule in allow_rules + ask_rules))


class LbnlTdmSetupHelperTests(unittest.TestCase):
    def test_mask_never_exposes_secret(self):
        self.assertEqual(setup_tdm.mask(""), "(unset)")
        self.assertEqual(setup_tdm.mask("secret-value-1234"), "set (len=17)")

    def test_load_existing_and_rc_sourcing_are_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / "lbnl-tdm.env"
            env_path.write_text(
                'export WILEY_TDM_TOKEN="test-uuid"\n'
                'export SPRINGER_API_KEY="$SPRINGER_OPENACCESS_API_KEY"\n',
                encoding="utf-8",
            )
            parsed = setup_tdm.load_existing(env_path)
            self.assertEqual(parsed, {"WILEY_TDM_TOKEN": "test-uuid"})

            rc_path = Path(tmp) / ".zshrc"
            rc_path.write_text("# existing rc\n", encoding="utf-8")
            changed1, bak1 = setup_tdm.ensure_rc_sourced(rc_path)
            self.assertTrue(changed1)
            self.assertIsNotNone(bak1)
            changed2, _ = setup_tdm.ensure_rc_sourced(rc_path)
            self.assertFalse(changed2)

    def test_exact_header_preserves_casing_and_cas_format_row_strips_prefix(self):
        hdr = setup_tdm._ExactHeader("X-API-KEY")
        self.assertEqual(hdr.capitalize(), "X-API-KEY")
        self.assertEqual(hdr.title(), "X-API-KEY")

        row = query_cas.format_enriched_row(
            {
                "group": "mim_cas_fallbacks",
                "mech": "MediaIngredientMech",
                "identifier": "cas:68399-81-5",
                "name": "TAPSO",
            },
            {
                "rn": "68399-81-5",
                "name": "TAPSO",
                "molecularFormula": "C<sub>7</sub>H<sub>17</sub>NO<sub>7</sub>S",
                "molecularMass": "259.28",
                "canonicalSmile": "O=S(=O)(O)CC(O)CNC(CO)(CO)CO",
                "inchi": "InChI=1S/C7H17NO7S/...",
                "inchiKey": "InChIKey=RZQXOGQSPBYUKH-UHFFFAOYSA-N",
                "experimentalProperties": [{"name": "Density", "property": "1.03691 g/cm³"}],
                "synonyms": ["TAPSO"],
            },
            "detail(cas_rn=68399-81-5)",
        )
        self.assertEqual(row["molecular_formula"], "C7H17NO7S")
        self.assertEqual(row["inchikey"], "RZQXOGQSPBYUKH-UHFFFAOYSA-N")

    def test_ingest_acs_tdm_feed_counts_xml_and_pdf_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            master = root / "acs_dois_master.tsv"
            master.write_text(
                "doi\tjournal_code\n10.1021/acschembio.3c00167\tacschembio\n",
                encoding="utf-8",
            )
            feed = root / "feed"
            feed.mkdir()
            (feed / "10.1021_acschembio.3c00167.xml").write_text(
                "<article>10.1021/acschembio.3c00167</article>",
                encoding="utf-8",
            )
            (feed / "10.1021_acschembio.3c00167.pdf").write_bytes(b"%PDF-1.4")
            stats = setup_tdm.ingest_acs_tdm_feed(feed, master)
            self.assertEqual(stats["xml_files"], 1)
            self.assertEqual(stats["pdf_files"], 1)
            self.assertEqual(stats["matched_dois"], 1)

    def test_unpromoted_worklists_and_tier2_batches_partition_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            master = root / "acs_dois_master.tsv"
            master.write_text(
                "doi\tjournal_code\tjournal_title\tpriority\tmech_count\tmechs\tsource_kinds\tpmid\ttitle\tsample_contexts\n"
                "10.1021/acs.biochem.0c00005\tbiochem\tBiochemistry\tSTANDARD\t1\tProteinTraitsMech\traw_inventory\t\tSample\tProteinTraitsMech/data/raw/mcsa.json\n"
                "10.1021/ja8025278\tjacs\tJACS\tHIGH\t1\tPathwayMech\tcurated_data\t\tCurated\tPathwayMech/data/pathways/p.yaml\n"
                "10.1021/bi062052f\tbiochem\tBiochemistry\tHIGH\t1\tPathwayMech\tmissing_pdf_or_report\t\tReport\tPathwayMech/reports/r.md\n",
                encoding="utf-8",
            )
            wl_summary = worklists.build_worklists(master, root / "worklists")
            self.assertEqual(wl_summary["tier_a_raw_inventory_only_standard"], 1)
            self.assertEqual(wl_summary["tier_b_missing_pdf_or_report"], 1)

            (root / "scifinder_residual_batch.tsv").write_text(
                "group\tmech\tidentifier\tname\tcas_rn\tinchikey\treason\n"
                "mim_missing_structure\tMediaIngredientMech\tcas:1\tCo-EDTA complex\t\t\tnot_found\n"
                "mim_missing_structure\tMediaIngredientMech\tcas:2\tCerium chloride heptahydrate\t\t\tnot_found\n"
                "naturalproduct_review_needed\tNaturalProductMech\tnpm:1\tstreptothricin D\t\t\tnot_found\n",
                encoding="utf-8",
            )
            (root / "antibioticmech_chemicals.tsv").write_text(
                "identifier\tlabel\tantimicrobial_class\tstandard_inchi_key\n"
                "CHEBI:1\ttest-ab\tANTIMICROBIAL_UNSPECIFIED\tAAAA-BBBB-N\n",
                encoding="utf-8",
            )
            (root / "mediaingredientmech_cas_audit.tsv").write_text(
                "group\tidentifier\tpreferred_term\tcas_rn\tcas_name\tnormalized_formula\tcanonical_smiles\tinchikey\tstructure_guard\tlabel_guard\n"
                "mim_missing_structure\tCHEBI:2\tAmmonium iron citrate\t1185-57-5\tCitrate\tC6H8O7.Fe.H3N\tC.N\tKEY\tSTRUCTURE_COMPOSITION_MISMATCH(missing={'Fe'})\tPASS\n",
                encoding="utf-8",
            )
            (root / "pathwaymech_chemicals.tsv").write_text(
                "identifier\tlabel\tcategory\tpathway_count\tsample_pathways\n"
                "MetaCyc:CPD-1\tcompound 1\tsmall_molecule\t2\tMetaCyc:PWY-1\n",
                encoding="utf-8",
            )
            t2_summary = tier2_batches.build_tier2_batches(root)
            self.assertEqual(t2_summary["tier2_scifinder_n_cas_lookup_count"], 3)
            self.assertEqual(t2_summary["tier2_chemnetbase_dnp_dod_count"], 2)
            self.assertEqual(t2_summary["tier2_icsd_webcsd_hydrates_chelates_count"], 3)
            self.assertEqual(t2_summary["tier2_icsd_webcsd_polysalt_mismatch_count"], 1)
            self.assertEqual(t2_summary["tier2_biocyc_pathway_participants_count"], 1)


if __name__ == "__main__":
    unittest.main()
