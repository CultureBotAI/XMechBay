"""Tests for the fleet-coordinator skill: its files, and the serial merge-queue script.

The merge script is exercised against a fake `gh` that reads and writes a JSON state
file, so no network or real repository is touched.
"""

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / ".agents" / "skills" / "fleet-coordinator"
SCRIPT = SKILL / "scripts" / "serial_merge_queue.sh"

FAKE_GH = r'''#!PYTHON
import json, re, sys
state_path = STATE
with open(state_path) as fh:
    state = json.load(fh)
args = sys.argv[1:]
def save():
    with open(state_path, "w") as fh:
        json.dump(state, fh)
if args[:2] == ["pr", "view"]:
    pr = state["prs"][args[2]]
    if pr.get("after_merge_of") and pr["after_merge_of"] in state["merged"]:
        pr.update(pr.pop("after"))
    print(pr["head"], pr["mergeable"], pr["status"], pr["state"])
elif args[:2] == ["pr", "merge"]:
    number = args[2]
    head = args[args.index("--match-head-commit") + 1]
    pr = state["prs"][number]
    state["log"].append(["merge", number, head])
    if pr.get("refuse_merge"):
        save(); sys.exit(1)
    pr["queued"] = True
    save()
elif args[:2] == ["api", "graphql"]:
    query = args[args.index("-f") + 1]
    number = re.search(r"number:(\d+)", query).group(1)
    pr = state["prs"][number]
    if pr.get("drop_from_queue"):
        print("OPEN - none")
    elif pr.get("queued"):
        pr["state"] = "MERGED"
        state["merged"].append(number)
        save()
        print("MERGED " + "f" * 40 + " none")
    else:
        print("OPEN - none")
else:
    sys.exit("unexpected gh call: %r" % (args,))
'''


def head(char):
    return char * 40


class SkillFilesTest(unittest.TestCase):
    def test_skill_front_matter_and_templates(self):
        text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\nname: fleet-coordinator\ndescription: "))
        for name in ("brief.md", "claims.md", "handoff.md"):
            self.assertTrue((SKILL / "templates" / name).is_file(), name)
        self.assertTrue(os.access(SCRIPT, os.X_OK), "serial_merge_queue.sh must be executable")

    def test_skill_names_no_real_hosts(self):
        for path in SKILL.rglob("*"):
            if path.is_file():
                text = path.read_text(encoding="utf-8")
                self.assertNotRegex(text, r"\b[\w-]+\.local\b", path)


class SerialMergeQueueTest(unittest.TestCase):
    def run_script(self, prs, *specs, merged=()):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            state_path.write_text(json.dumps({"prs": prs, "log": [], "merged": list(merged)}))
            fake = Path(tmp) / "gh"
            fake.write_text(FAKE_GH.replace("PYTHON", sys.executable).replace("STATE", repr(str(state_path))))
            fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
            env = dict(os.environ, GH=str(fake), INTERVAL="0", MERGEABILITY_WAIT="0",
                       MERGEABILITY_TRIES="2", MAX_POLLS="3")
            result = subprocess.run(["bash", str(SCRIPT), "example/repo", *specs],
                                    capture_output=True, text=True, env=env, timeout=60)
            final = json.loads(state_path.read_text())
        return result, final

    @staticmethod
    def pr(char, mergeable="MERGEABLE", status="CLEAN", state="OPEN", **extra):
        return dict(head=head(char), mergeable=mergeable, status=status, state=state, **extra)

    def test_merges_in_order_one_at_a_time(self):
        result, final = self.run_script({"12": self.pr("a"), "13": self.pr("b")}, "12:aaaaaaa", "13:bbbbbbb")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("all merged", result.stdout)
        self.assertEqual([entry[1] for entry in final["log"]], ["12", "13"])
        self.assertEqual(final["log"][0][2], head("a"))

    def test_stops_on_head_mismatch_before_merging(self):
        result, final = self.run_script({"12": self.pr("c")}, "12:aaaaaaa")
        self.assertEqual(result.returncode, 1)
        self.assertIn("is not the reviewed", result.stdout)
        self.assertEqual(final["log"], [])

    def test_stops_when_a_later_pr_conflicts_after_the_first_merges(self):
        later = self.pr("b", after_merge_of="12", after={"mergeable": "CONFLICTING", "status": "DIRTY"})
        result, final = self.run_script({"12": self.pr("a"), "13": later}, "12:aaaaaaa", "13:bbbbbbb")
        self.assertEqual(result.returncode, 1)
        self.assertIn("#12 MERGED", result.stdout)
        self.assertIn("STOP #13: CONFLICTING DIRTY", result.stdout)
        self.assertEqual([entry[1] for entry in final["log"]], ["12"])

    def test_skips_an_already_merged_pr(self):
        result, final = self.run_script({"12": self.pr("a", state="MERGED"), "13": self.pr("b")},
                                        "12:aaaaaaa", "13:bbbbbbb")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("#12 already MERGED", result.stdout)
        self.assertEqual([entry[1] for entry in final["log"]], ["13"])

    def test_stops_when_a_pr_leaves_the_queue(self):
        result, _ = self.run_script({"12": self.pr("a", drop_from_queue=True)}, "12:aaaaaaa")
        self.assertEqual(result.returncode, 1)
        self.assertIn("left the queue without merging", result.stdout)

    def test_stops_when_gh_refuses_the_merge(self):
        result, _ = self.run_script({"12": self.pr("a", refuse_merge=True)}, "12:aaaaaaa")
        self.assertEqual(result.returncode, 1)
        self.assertIn("gh pr merge refused", result.stdout)

    def test_rejects_malformed_arguments(self):
        for spec, message in (("12:abc", "shorter than 7"), ("x:aaaaaaa", "not a PR number"),
                              ("12:zzzzzzz", "not a hexadecimal"), ("12", "not PR:HEAD_PREFIX")):
            result, final = self.run_script({"12": self.pr("a")}, spec)
            self.assertEqual(result.returncode, 1, spec)
            self.assertIn(message, result.stdout, spec)
            self.assertEqual(final["log"], [], spec)


if __name__ == "__main__":
    unittest.main()
