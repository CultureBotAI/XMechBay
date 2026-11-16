"""Synthetic offline checks for the private publication inventory helpers.

Inputs: temporary API/log fixtures. Output: unittest assertions; 2026-10-25.
Serves publication review; makes no network requests.
"""

from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile


ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


gate = load("inventory_gate", "scripts/check_public_content.py")
summary = load("inventory_summary", "scripts/adhoc/summarize_publication_inventory.py")
inventory = load("inventory_export", "scripts/adhoc/inventory_github_publication.py")


class PublicationInventoryTests(unittest.TestCase):
    def test_all_json_surfaces_and_log_contents_are_scanned(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            private = "/" + "Users/" + "example-user/private"
            for name in ("issue_comments", "pr_2_reviews", "review_comments", "check_annotations_1", "edits_reviews_3_0"):
                (directory / (name + ".json")).write_text(json.dumps({"body": private}))
            with zipfile.ZipFile(directory / "run_1_logs.zip", "w") as archive:
                archive.writestr("job.txt", "host" + ".local")
            findings = summary.scan_exports(directory, gate)
            self.assertEqual(len(findings), 6)
            self.assertTrue(all("personal-home-path" in values for name, values in findings.items() if name.endswith(".json")))
            self.assertEqual(findings["run_1_logs.zip"], ["local-hostname"])
            self.assertNotIn(private, json.dumps(findings))

    def test_review_body_edit_histories_are_requested(self):
        queries = []

        def fake_run(command, **kwargs):
            if command[0] == "git":
                return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
            endpoint = command[2]
            if endpoint == "graphql":
                queries.append(command[-1])
                value = {"data": {"node": {"userContentEdits": {"nodes": [], "pageInfo": {"hasNextPage": False}}}}}
            elif endpoint == "repos/example/repository":
                value = {"private": True}
            elif "/pulls?" in endpoint:
                value = [[{"number": 2}]]
            elif "/pulls/2/reviews?" in endpoint:
                value = [[{"id": 3, "node_id": "synthetic-review-node"}]]
            elif "/actions/runs?" in endpoint:
                value = [{"workflow_runs": []}]
            else:
                value = [[]]
            return SimpleNamespace(returncode=0, stdout=json.dumps(value).encode(), stderr=b"")

        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(sys, "argv", ["inventory", "example/repository", temporary]), patch.object(inventory.subprocess, "run", side_effect=fake_run), redirect_stdout(io.StringIO()):
                inventory.main()
        self.assertTrue(any('node(id:"synthetic-review-node")' in query for query in queries))
        self.assertTrue(any("... on PullRequestReview { userContentEdits" in query for query in queries))


if __name__ == "__main__":
    unittest.main()
