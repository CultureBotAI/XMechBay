"""2026-11-16: Synthetic refusal/redaction checks for MCP header activation.

Inputs: independently authored temporary env fixtures; outputs: unittest results.
No host configuration, inherited credentials, or network calls are used.
"""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/adhoc/mcp_env_headers.py"
SPEC = importlib.util.spec_from_file_location("mcp_env_headers", SCRIPT)
headers = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(headers)
SYNTHETIC = "example-token.ABC_123+/=="


class HeadersTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # Resolve the platform's temporary-directory aliases before link tests.
        self.directory = Path(self.temp.name).resolve()
        self.env_file = self.directory / "example.env"
        self.write("EXAMPLE_TOKEN=" + SYNTHETIC + "\n")

    def write(self, content):
        self.env_file.write_text(content)
        self.env_file.chmod(0o600)

    def run_cli(self, *args, path=None):
        return subprocess.run([sys.executable, "-B", str(SCRIPT), "--env-file",
                               str(path or self.env_file), "--variable", "EXAMPLE_TOKEN",
                               *args], text=True, capture_output=True, timeout=5)

    def assert_refused(self, result):
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, headers.ERROR + "\n")
        self.assertNotIn(SYNTHETIC, result.stderr)
        self.assertNotIn(str(self.directory), result.stderr)

    def test_check_does_not_emit_token_and_emit_is_exact_json(self):
        checked = self.run_cli()
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertEqual(json.loads(checked.stdout), {"configured": True})
        self.assertNotIn(SYNTHETIC, checked.stdout + checked.stderr)
        explicit = self.run_cli("--check")
        self.assertEqual(explicit.stdout, checked.stdout)
        emitted = self.run_cli("--emit-headers")
        self.assertEqual(emitted.returncode, 0, emitted.stderr)
        self.assertEqual(json.loads(emitted.stdout), {"Authorization": "Bearer " + SYNTHETIC})
        self.assertEqual(emitted.stderr, "")

    def test_literal_export_quotes_comments_and_unrelated_assignment(self):
        for expression in (SYNTHETIC, "'" + SYNTHETIC + "'", '"' + SYNTHETIC + '"'):
            with self.subTest(expression=expression):
                self.write("# synthetic example\nOTHER='example folder'\n"
                           "export EXAMPLE_TOKEN = " + expression + " # comment\n")
                self.assertEqual(headers.selected_token(headers.read_private_file(str(self.env_file)),
                                                        "EXAMPLE_TOKEN"), SYNTHETIC)

    def test_missing_or_empty_does_not_use_inherited_token(self):
        for content in ("OTHER=example\n", "EXAMPLE_TOKEN=''\n"):
            with self.subTest(content=content):
                self.write(content)
                with mock.patch.dict(os.environ, {"EXAMPLE_TOKEN": SYNTHETIC}):
                    self.assertEqual(json.loads(self.run_cli().stdout), {"configured": False})
                    self.assert_refused(self.run_cli("--emit-headers"))

    def test_rejects_malformed_expansion_duplicate_and_header_injection(self):
        bad = ["EXAMPLE_TOKEN=$OTHER", "EXAMPLE_TOKEN='$(touch example)'",
               "EXAMPLE_TOKEN=`example`", "EXAMPLE_TOKEN=abc;example",
               "EXAMPLE_TOKEN=abc|example", "EXAMPLE_TOKEN=abc&example",
               "EXAMPLE_TOKEN=abc>example", "EXAMPLE_TOKEN=abc\\def",
               "EXAMPLE_TOKEN='unterminated", "EXAMPLE_TOKEN='abc'junk",
               "EXAMPLE_TOKEN=abc def", "EXAMPLE_TOKEN=abc#truncated",
               "EXAMPLE_TOKEN=abc\r\nInjected: example", "EXAMPLE_TOKEN=abc\x00",
               "EXAMPLE_TOKEN='abc\nInjected: example'", "source example.env",
               "EXAMPLE_TOKEN=abc\nEXAMPLE_TOKEN=def",
               "OTHER=one\nOTHER=two\nEXAMPLE_TOKEN=abc"]
        for content in bad:
            with self.subTest(content=content):
                self.write(content)
                self.assert_refused(self.run_cli("--emit-headers"))

    def test_rejects_group_or_world_access(self):
        for mode in (0o640, 0o604, 0o660, 0o606):
            with self.subTest(mode=mode):
                self.env_file.chmod(mode)
                self.assert_refused(self.run_cli())

    def test_rejects_symlink_final_and_parent_and_hardlink(self):
        alias = self.directory / "alias.env"
        alias.symlink_to(self.env_file)
        self.assert_refused(self.run_cli(path=alias))
        directory_alias = self.directory / "alias"
        directory_alias.symlink_to(self.directory, target_is_directory=True)
        self.assert_refused(self.run_cli(path=directory_alias / self.env_file.name))
        hardlink = self.directory / "linked.env"
        os.link(self.env_file, hardlink)
        self.assert_refused(self.run_cli(path=hardlink))
        self.assert_refused(self.run_cli())

    def test_nonregular_missing_oversized_and_invalid_encoding_refuse(self):
        self.assert_refused(self.run_cli(path=self.directory))
        self.assert_refused(self.run_cli(path=self.directory / "missing.env"))
        fifo = self.directory / "pipe.env"
        os.mkfifo(fifo, 0o600)
        self.assert_refused(self.run_cli(path=fifo))
        self.write("#" * (headers.MAX_BYTES + 1))
        self.assert_refused(self.run_cli())
        self.env_file.write_bytes(b"EXAMPLE_TOKEN=\xff")
        self.assert_refused(self.run_cli())

    def test_foreign_owner_refuses(self):
        with mock.patch.object(headers.os, "geteuid", return_value=os.geteuid() + 1):
            with self.assertRaises(headers.Refused):
                headers.read_private_file(str(self.env_file))

    def test_library_exception_hides_filesystem_path(self):
        with self.assertRaises(headers.Refused) as caught:
            headers.read_private_file(str(self.directory / "missing.env"))
        self.assertEqual(str(caught.exception), "")
        self.assertIsNone(caught.exception.__context__)

    def test_changed_file_snapshot_refuses(self):
        real_fstat = os.fstat
        calls = 0

        def changed(descriptor):
            nonlocal calls
            info = real_fstat(descriptor)
            calls += 1
            if calls == 1:
                with self.env_file.open("a") as stream:
                    stream.write("# changed\n")
            return info

        with mock.patch.object(headers.os, "fstat", side_effect=changed):
            with self.assertRaises(headers.Refused):
                headers.read_private_file(str(self.env_file))

    def test_emit_refuses_terminal_before_reading(self):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors), \
                mock.patch.object(output, "isatty", return_value=True), \
                mock.patch.object(headers, "read_private_file") as reader:
            result = headers.main(["--env-file", "example.env", "--variable", "EXAMPLE_TOKEN",
                                   "--emit-headers"])
        self.assertEqual(result, 2)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(errors.getvalue(), headers.ERROR + "\n")
        reader.assert_not_called()

    def test_argument_error_does_not_echo_unknown_value(self):
        self.assert_refused(self.run_cli("--unknown", SYNTHETIC))
        self.assert_refused(self.run_cli("--variable", SYNTHETIC))
        self.assert_refused(self.run_cli("--check", "--emit-headers"))


if __name__ == "__main__":
    unittest.main()
