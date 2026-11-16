"""Unit tests for reusable CBORG permissions and library-key setup helpers."""

from __future__ import annotations

import importlib.util
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
import urllib.error

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
    def test_helpers_and_docs_pass_public_content_scan(self):
        rel_paths = [
            "docs/lbnl-tdm-and-cborg-mcp.md",
            "scripts/adhoc/allow_cborg_mcp_tools.py",
            "scripts/adhoc/configure_cborg_tavily.sh",
            "scripts/adhoc/enable_cborg_mcp.py",
            "scripts/adhoc/probe_cborg_tavily.py",
            "scripts/adhoc/setup_lbnl_tdm_keys.py",
            "tests/test_cborg_and_tdm_helpers.py",
        ]
        findings = set()
        for rel in rel_paths:
            findings.update(public_gate.scan_path(rel, f"worktree:{rel}", []))
            findings.update(public_gate.scan_blob((ROOT / rel).read_bytes(), f"worktree:{rel}", public_gate.DEFAULT_MAX_BYTES, []))
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
    def test_requests_exception_does_not_echo_credential_url(self):
        secret = "synthetic-sensitive-value"
        url = "https://provider.example.invalid/check?api_key=" + secret
        requests = MagicMock()
        requests.get.side_effect = RuntimeError("Connection failed for " + url)
        with patch.object(setup_tdm, "_requests", requests):
            self.assertEqual(setup_tdm._probe(url), (False, "request failed"))

    def test_requests_http_error_omits_reason_headers_and_body(self):
        secret = "synthetic-sensitive-value"
        response = MagicMock(status_code=401, reason=secret)
        response.headers = {"WWW-Authenticate": secret, "x-amzn-ErrorType": secret}
        response.iter_content.return_value = iter([secret.encode()])
        requests = MagicMock()
        requests.get.return_value.__enter__.return_value = response
        with patch.object(setup_tdm, "_requests", requests):
            self.assertEqual(setup_tdm._probe("https://provider.example.invalid/check"), (False, "HTTP 401"))
        response.iter_content.assert_not_called()

    def test_urllib_exception_does_not_echo_credential_url(self):
        secret = "synthetic-sensitive-value"
        url = "https://provider.example.invalid/check?api_key=" + secret
        with patch.object(setup_tdm, "_requests", None), patch.object(
            setup_tdm.urllib.request, "urlopen", side_effect=urllib.error.URLError(url)
        ):
            self.assertEqual(setup_tdm._probe(url), (False, "request failed"))

    def test_urllib_http_error_omits_reason_headers_and_body(self):
        secret = "synthetic-sensitive-value"
        body = io.BytesIO(secret.encode())
        self.addCleanup(body.close)
        error = urllib.error.HTTPError(
            "https://provider.example.invalid/check?api_key=" + secret,
            403, secret, {"WWW-Authenticate": secret}, body,
        )
        with patch.object(setup_tdm, "_requests", None), patch.object(
            setup_tdm.urllib.request, "urlopen", side_effect=error
        ):
            self.assertEqual(setup_tdm._probe(error.url), (False, "HTTP 403"))
        self.assertEqual(body.tell(), 0)

    def test_success_diagnostics_report_byte_count_without_body(self):
        body = b"synthetic-sensitive-value"
        response = MagicMock(status_code=200, status=200)
        response.iter_content.return_value = iter([body])
        response.read.return_value = body
        requests = MagicMock()
        requests.get.return_value.__enter__.return_value = response
        opened = MagicMock()
        opened.__enter__.return_value = response
        expected = (True, "HTTP 200 (25 bytes read)")
        with patch.object(setup_tdm, "_requests", requests):
            self.assertEqual(setup_tdm._probe("https://provider.example.invalid/check"), expected)
        with patch.object(setup_tdm, "_requests", None), patch.object(
            setup_tdm.urllib.request, "urlopen", return_value=opened
        ):
            self.assertEqual(setup_tdm._probe("https://provider.example.invalid/check"), expected)

    def test_mask_never_exposes_secret(self):
        self.assertEqual(setup_tdm.mask(""), "(unset)")
        self.assertEqual(setup_tdm.mask("secret-value-1234"), "set (len=17)")

    def test_load_existing_and_rc_sourcing_are_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / "lbnl-tdm.env"
            env_path.write_text('export WILEY_TDM_TOKEN="test-uuid"\nexport SPRINGER_API_KEY="$SPRINGER_OPENACCESS_API_KEY"\n', encoding="utf-8")
            self.assertEqual(setup_tdm.load_existing(env_path), {"WILEY_TDM_TOKEN": "test-uuid"})
            rc_path = Path(tmp) / ".zshrc"
            rc_path.write_text("# existing rc\n", encoding="utf-8")
            changed1, backup = setup_tdm.ensure_rc_sourced(rc_path)
            self.assertTrue(changed1)
            self.assertIsNotNone(backup)
            changed2, _ = setup_tdm.ensure_rc_sourced(rc_path)
            self.assertFalse(changed2)

    def test_exact_header_preserves_casing(self):
        header = setup_tdm._ExactHeader("X-API-KEY")
        self.assertEqual(header.capitalize(), "X-API-KEY")
        self.assertEqual(header.title(), "X-API-KEY")


if __name__ == "__main__":
    unittest.main()
