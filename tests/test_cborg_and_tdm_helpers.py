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
public_gate = _load_module("check_public_content", "scripts/check_public_content.py")


class CborgMcpHelperTests(unittest.TestCase):
    def test_new_helpers_and_docs_pass_public_content_scan(self):
        rel_paths = [
            "docs/lbnl-tdm-and-cborg-mcp.md",
            "scripts/adhoc/allow_cborg_mcp_tools.py",
            "scripts/adhoc/configure_cborg_tavily.sh",
            "scripts/adhoc/enable_cborg_mcp.py",
            "scripts/adhoc/probe_cborg_tavily.py",
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


if __name__ == "__main__":
    unittest.main()
