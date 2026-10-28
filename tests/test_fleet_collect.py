"""Synthetic collector tests: identities, coverage and observation-only probes.

Purpose: on-demand fleet table PR; inputs are fixtures/mocks, output unittest.
No SSH or live agent sessions; 2026-10-27.
"""

import ast
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / ".agents/skills/fleet-session-status/scripts/fleet_collect.py"
SPEC = importlib.util.spec_from_file_location("fleet_collect", SCRIPT)
COLLECTOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(COLLECTOR)
SID = "11111111-2222-4333-8444-555555555555"
SID2 = "22222222-3333-4444-8555-666666666666"


def process(pid, ppid, executable, tty="ttys001"):
    return {"pid": pid, "ppid": ppid, "executable": executable,
            "executable_path": executable, "tty": tty,
            "started_at": "2026-01-01T00:00:00+00:00", "age_seconds": 60,
            "errors": []}


def pane(pid=10, tty="ttys001", command="codex"):
    return {"pid": pid, "pane": "%1", "tty": tty, "tmux_session": "sample",
            "window": "example-project", "tmux_window_id": "@1", "pane_index": "0",
            "command": command, "cwd": "/srv/example-project"}


class IdentityTests(unittest.TestCase):
    def test_python39_syntax(self):
        ast.parse(SCRIPT.read_text(), feature_version=(3, 9))

    def test_process_age_and_start_are_distinct(self):
        rows, errors = COLLECTOR.parse_processes(
            "101 100 ttys001 2-03:04:05 Mon Jan  5 12:34:56 2026 /opt/bin/agy\n")
        self.assertFalse(errors)
        self.assertEqual(rows[101]["age_seconds"], 183845)
        self.assertTrue(rows[101]["started_at"].endswith("+00:00"))
        self.assertNotIn("args", rows[101])
        self.assertEqual(COLLECTOR.age_seconds("02:03"), 123)

    def test_metadata_errors_are_reported(self):
        rows, errors = COLLECTOR.parse_processes("broken\n")
        self.assertFalse(rows)
        self.assertEqual(errors, ["process_row_unparsed"])

    def test_allowlisted_explicit_resume_forms(self):
        for agent, text in (("agy", "agy --conversation=" + SID),
                            ("claude", "claude --resume=" + SID)):
            with self.subTest(text=text):
                self.assertEqual(COLLECTOR.argv_identity(text, agent)["session_id"], SID)

    def test_uuid_in_prompt_or_unknown_position_is_not_identity(self):
        for agent, text in (("agy", 'agy -p "use --conversation ' + SID + '"'),
                            ("codex", 'codex "resume ' + SID + '"'),
                            ("codex", "codex exec resume " + SID),
                            ("agy", "agy --unknown-flag --conversation=" + SID),
                            ("agy", "agy -c"), ("codex", "codex resume --last")):
            with self.subTest(text=text):
                self.assertIsNone(COLLECTOR.argv_identity(text, agent)["session_id"])

    def test_identity_changing_and_unknown_flags_invalidate_ids_in_either_order(self):
        for agent, launch in (("claude", "--resume=" + SID),
                              ("codex", "resume " + SID),
                              ("agy", "--conversation=" + SID)):
            for flag in ("--fork-session", "--fork-session=true", "--unknown-future-option", "--continue"):
                for args in (launch + " " + flag, flag + " " + launch):
                    with self.subTest(agent=agent, args=args):
                        self.assertIsNone(COLLECTOR.argv_identity(agent + " " + args, agent)["session_id"])

    def test_recognized_options_after_resume_are_checked(self):
        self.assertEqual(COLLECTOR.argv_identity("agy --conversation=" + SID + " --no-alt-screen", "agy")["session_id"], SID)
        self.assertIsNone(COLLECTOR.argv_identity("claude --resume=" + SID + " --model --fork-session", "claude")["session_id"])
        self.assertIsNone(COLLECTOR.argv_identity("claude --resume=" + SID + " --resume invalid", "claude")["session_id"])
        self.assertIsNone(COLLECTOR.argv_identity("claude --resume=" + SID + ' --prompt "example" --fork-session', "claude")["session_id"])

    def test_flat_positional_and_split_flag_ids_are_ambiguous(self):
        for agent, split in (("codex", ["resume", SID]),
                             ("agy", ["--conversation", SID]),
                             ("claude", ["--resume", SID]), ("claude", ["-r", SID])):
            separate_args = [agent] + split
            one_prompt_arg = [agent, " ".join(split)]
            self.assertEqual(" ".join(separate_args), " ".join(one_prompt_arg))
            with self.subTest(agent=agent, split=split):
                identities = COLLECTOR.parse_arguments(
                    "10 " + " ".join(one_prompt_arg) + "\n", {10: process(10, 1, agent)}, {10})
                self.assertIsNone(identities[10]["session_id"])
                self.assertFalse(identities[10]["session_id_inheritable"])

    def test_literal_quotes_and_backslashes_are_not_shell_interpreted(self):
        for option in ("'--resume=" + SID + "'", '"--resume=' + SID + '"',
                       "--resume=" + SID + "\\", "--resume=" + SID + " '"):
            with self.subTest(option=option):
                identity = COLLECTOR.argv_identity("claude " + option, "claude")
                self.assertEqual(identity["agent"], "claude")
                self.assertIsNone(identity["session_id"])
                self.assertFalse(identity["session_id_inheritable"])
                processes = {10: process(10, 1, "node"), 11: process(11, 10, "claude")}
                identities = {10: COLLECTOR.argv_identity("node /opt/bin/claude --resume=" + SID, "node"),
                              11: identity}
                self.assertIsNone(COLLECTOR.logical_agents(processes, identities)[0]["session_id"])

    def test_flat_ps_prompt_text_cannot_forge_an_id(self):
        processes = {10: process(10, 1, "agy")}
        # ps does not retain the quotes around the original single prompt value.
        identities = COLLECTOR.parse_arguments(
            "10 agy --prompt use --conversation " + SID + "\n", processes, {10})
        self.assertIsNone(identities[10]["session_id"])
        self.assertFalse(identities[10]["session_id_inheritable"])

    def test_any_prompt_option_declines_even_an_earlier_id(self):
        for agent, launch in (("agy", "--conversation=" + SID),
                              ("claude", "--resume=" + SID), ("codex", "resume " + SID)):
            for prompt in ("--prompt example", "--prompt=example", "--system-prompt use --resume " + SID):
                for args in (launch + " " + prompt, prompt + " " + launch):
                    with self.subTest(agent=agent, args=args):
                        self.assertIsNone(COLLECTOR.argv_identity(agent + " " + args, agent)["session_id"])

    def test_flat_value_boundaries_are_not_guessed(self):
        for value in ("--model example", "--config example", "--config=example", "--add-dir /srv/example"):
            for args in (value + " --conversation=" + SID,
                         "--conversation=" + SID + " " + value):
                with self.subTest(args=args):
                    self.assertIsNone(COLLECTOR.argv_identity("agy " + args, "agy")["session_id"])

    def test_known_node_cli_paths_are_verified_but_arbitrary_scripts_are_not(self):
        for path, expected in (("/opt/lib/node_modules/anthropic/claude-code/cli.js", "claude"),
                               ("/opt/lib/node_modules/@openai/codex/bin/codex.js", "codex"),
                               ("/srv/example/codex.js", None), ("/srv/example/cli.js", None)):
            with self.subTest(path=path):
                self.assertEqual(COLLECTOR.argv_identity("node " + path, "node")["agent"], expected)

    def test_raw_argv_and_unrequested_processes_never_serialize(self):
        processes = {10: process(10, 1, "agy")}
        identities = COLLECTOR.parse_arguments(
            "10 agy --conversation=" + SID + " --prompt PRIVATE_ARGUMENT\n11 unrelated SECRET\n",
            processes, {10})
        output = json.dumps(identities)
        self.assertNotIn("PRIVATE_ARGUMENT", output)
        self.assertNotIn("SECRET", output)
        self.assertEqual(set(identities), {10})

    def test_same_agent_wrapper_is_collapsed_and_id_inherited(self):
        processes = {10: process(10, 1, "node"), 11: process(11, 10, "claude")}
        identities = {10: COLLECTOR.argv_identity("node /opt/bin/claude --resume=" + SID, "node"),
                      11: COLLECTOR.argv_identity("claude", "claude")}
        rows = COLLECTOR.logical_agents(processes, identities)
        self.assertEqual([row["pid"] for row in rows], [11])
        self.assertEqual(rows[0]["session_id"], SID)

    def test_independent_agents_sharing_tty_are_not_collapsed(self):
        processes = {10: process(10, 1, "agy"), 11: process(11, 1, "agy")}
        self.assertEqual(len(COLLECTOR.logical_agents(processes, {})), 2)

    def test_native_child_agent_is_not_mistaken_for_launcher_duplicate(self):
        processes = {10: process(10, 1, "codex"), 11: process(11, 10, "codex")}
        self.assertEqual(len(COLLECTOR.logical_agents(processes, {})), 2)

    def test_native_child_does_not_inherit_its_native_parents_wrapper_id(self):
        processes = {10: process(10, 1, "node"), 11: process(11, 10, "claude"),
                     12: process(12, 11, "claude")}
        identities = {10: COLLECTOR.argv_identity("node /opt/bin/claude --resume=" + SID, "node")}
        rows = COLLECTOR.logical_agents(processes, identities)
        self.assertEqual([(row["pid"], row["session_id"]) for row in rows], [(11, SID), (12, None)])

    def test_nested_native_session_inherits_only_its_own_wrapper(self):
        processes = {10: process(10, 1, "node"), 11: process(11, 10, "claude"),
                     12: process(12, 11, "node"), 13: process(13, 12, "claude")}
        identities = {10: COLLECTOR.argv_identity("node /opt/bin/claude --resume=" + SID, "node"),
                      12: COLLECTOR.argv_identity("node /opt/bin/claude --resume=" + SID2, "node")}
        rows = COLLECTOR.logical_agents(processes, identities)
        self.assertEqual([(row["pid"], row["session_id"]) for row in rows], [(11, SID), (13, SID2)])

    def test_different_agent_runtime_is_an_inheritance_boundary(self):
        processes = {10: process(10, 1, "node"), 11: process(11, 10, "node"),
                     12: process(12, 11, "codex")}
        identities = {10: {"agent": "codex", "session_id": SID},
                      11: {"agent": "claude", "session_id": SID2}}
        rows = COLLECTOR.logical_agents(processes, identities)
        self.assertEqual([(row["pid"], row["session_id"]) for row in rows], [(10, SID), (11, SID2), (12, None)])

    def test_conflicting_ancestor_session_ids_are_unknown(self):
        processes = {10: process(10, 1, "node"), 11: process(11, 10, "codex")}
        identities = {10: {"agent": "codex", "session_id": SID},
                      11: {"agent": "codex", "session_id": SID2}}
        row = COLLECTOR.logical_agents(processes, identities)[0]
        self.assertIsNone(row["session_id"])
        self.assertIn("session_id_ambiguous", row["errors"])

    def test_unknown_native_options_cannot_restore_an_id_from_its_wrapper(self):
        processes = {10: process(10, 1, "node"), 11: process(11, 10, "claude")}
        identities = {10: COLLECTOR.argv_identity("node /opt/bin/claude --resume=" + SID, "node"),
                      11: COLLECTOR.argv_identity("claude --resume=" + SID + " --fork-session", "claude")}
        rows = COLLECTOR.logical_agents(processes, identities)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["session_id"])

    def test_daemon_subcommands_are_not_agent_sessions(self):
        processes = {10: process(10, 1, "codex", "??")}
        identities = {10: COLLECTOR.argv_identity("codex app-server", "codex")}
        self.assertEqual(COLLECTOR.logical_agents(processes, identities), [])

    def test_only_agent_candidates_and_known_runtime_ancestors_get_argv_read(self):
        processes = {1: process(1, 0, "launchd"), 10: process(10, 1, "bash"),
                     11: process(11, 10, "node"), 12: process(12, 11, "codex"),
                     20: process(20, 1, "node", "ttys002")}
        self.assertEqual(COLLECTOR.candidate_pids(processes, []), {10, 11, 12})

    def test_terminal_hints_select_only_local_runtime_candidates(self):
        processes = {10: process(10, 1, "node"), 11: process(11, 1, "node", "ttys002"),
                     12: process(12, 1, "node", "??")}
        for hint in ({"title": "Claude session", "process_names": []},
                     {"title": "example-project", "process_names": ["node"]}):
            with self.subTest(hint=hint):
                self.assertEqual(COLLECTOR.candidate_pids(processes, [], [{"tty": "ttys001", **hint}]), {10})
        self.assertEqual(COLLECTOR.candidate_pids(processes, [], [{"tty": "ttys001", "title": "ordinary shell"}]), set())

    @patch.object(COLLECTOR, "MAX_RUNTIME_CANDIDATES", 1)
    def test_terminal_runtime_candidates_have_a_hard_cap(self):
        processes = {10: process(10, 1, "node"), 11: process(11, 1, "node")}
        tabs = [{"tty": "ttys001", "title": "Codex"}]
        self.assertEqual(COLLECTOR.candidate_pids(processes, [], tabs), {10})

    def test_cyclic_ancestry_terminates(self):
        processes = {10: process(10, 11, "bash"), 11: process(11, 10, "codex")}
        self.assertIsNone(COLLECTOR.pane_for_process(processes[11], [pane(99)], processes))


class ObservationTests(unittest.TestCase):
    @patch.object(COLLECTOR, "MAX_PANES", 2)
    def test_linked_memberships_do_not_consume_unique_pane_budget(self):
        aliases = [f"%1\t10\t/dev/ttys001\tview-{index:03}\t@1\tproject-a\t0\tcodex\t/srv/a"
                   for index in range(150)]
        second = "%2\t20\t/dev/ttys002\tfleet\t@2\tproject-b\t0\tagy\t/srv/b"
        rows, errors = COLLECTOR.parse_panes("\n".join(aliases + [second]))
        self.assertFalse(errors)
        self.assertEqual([row["pane"] for row in rows], ["%1", "%2"])
        self.assertEqual(len(rows[0]["tmux_sessions"]), 150)
        reversed_rows, errors = COLLECTOR.parse_panes("\n".join(list(reversed(aliases)) + [second]))
        self.assertFalse(errors)
        self.assertEqual(rows[0], reversed_rows[0])

    @patch.object(COLLECTOR, "MAX_PANES", 1)
    def test_unique_pane_cap_keeps_later_memberships_of_admitted_pane(self):
        text = ("%1\t10\t/dev/ttys001\tfleet\t@1\tproject-a\t0\tcodex\t/srv/a\n"
                "%2\t20\t/dev/ttys002\tfleet\t@2\tproject-b\t0\tagy\t/srv/b\n"
                "%1\t10\t/dev/ttys001\tviewer\t@1\tproject-a\t0\tcodex\t/srv/a\n")
        rows, errors = COLLECTOR.parse_panes(text)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tmux_sessions"], ["fleet", "viewer"])
        self.assertEqual(errors, ["tmux_panes_truncated"])

    def test_conflicting_identity_for_one_pane_cannot_attach_an_agent(self):
        text = ("%1\t10\t/dev/ttys001\tfleet\t@1\tproject-a\t0\tcodex\t/srv/a\n"
                "%1\t20\t/dev/ttys002\tviewer\t@1\tproject-a\t0\tcodex\t/srv/a\n")
        rows, errors = COLLECTOR.parse_panes(text)
        self.assertFalse(rows)
        self.assertEqual(errors, ["pane_rows_invalid"])
        self.assertIsNone(COLLECTOR.pane_for_process(process(11, 10, "codex"), rows,
            {10: process(10, 1, "bash"), 11: process(11, 10, "codex")}))

    def test_terminal_absence_does_not_call_osascript(self):
        class NeverRun:
            def run(self, *args, **kwargs):
                raise AssertionError("must not launch Terminal")
        self.assertEqual(COLLECTOR.terminal_tabs(NeverRun(), {}, set()), ([], ["terminal_not_running"]))

    def test_hidden_minimized_and_background_tabs_are_preserved(self):
        fake = {"tabs": [{"window_id": 5, "tab": 2, "tty": "ttys001", "selected": False,
                          "visible": False, "minimized": True, "title": "", "screen": ">", "errors": []}], "errors": []}
        class FakeRunner:
            def run(self, command, **kwargs):
                self.command = command
                return json.dumps(fake)
        processes = {1: process(1, 0, "Terminal")}
        processes[1]["executable_path"] = "/System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal"
        rows, errors = COLLECTOR.terminal_tabs(FakeRunner(), processes, {"ttys001"})
        self.assertFalse(errors)
        self.assertTrue(rows[0]["minimized"])
        self.assertFalse(rows[0]["selected"])

    def test_tmux_ancestry_precedes_a_terminal_viewer(self):
        processes = {10: process(10, 1, "bash"), 11: process(11, 10, "codex"),
                     20: process(20, 1, "tmux", "ttys002")}
        self.assertEqual(COLLECTOR.pane_for_process(processes[11], [pane(10)], processes)["pane"], "%1")
        self.assertIsNone(COLLECTOR.pane_for_process(processes[20], [pane(10)], processes))

    def test_screen_bytes_are_bounded(self):
        text, truncated = COLLECTOR.screen_tail("x" * 50000)
        self.assertTrue(truncated)
        self.assertLessEqual(len(text.encode()), COLLECTOR.MAX_SCREEN_BYTES)

    def test_health_missing_invalid_and_valid_reports(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "health.json"
            self.assertIsNone(COLLECTOR.read_health(path)["report"])
            path.write_text('{"schema_version":1,"status":"ok"}')
            self.assertEqual(COLLECTOR.read_health(path)["error"], "health_report_invalid")
            path.write_text(json.dumps({"schema_version": 1, "observed_at": "2026-01-01T00:00:00Z",
                                        "status": "ok", "host": {}, "private_extra": "do not transport"}))
            health = COLLECTOR.read_health(path)
            self.assertIsNone(health["error"])
            self.assertNotIn("private_extra", health["report"])

    def test_health_symlink_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "health.json"
            real = Path(temporary) / "real.json"
            real.write_text("{}")
            path.symlink_to(real)
            self.assertEqual(COLLECTOR.read_health(path)["error"], "health_report_missing_or_unsupported")

    @patch.object(COLLECTOR.shutil, "which", return_value="/mock/tmux")
    @patch.object(COLLECTOR.Path, "is_file", return_value=False)
    def test_no_terminal_still_collects_tmux_without_any_input(self, is_file, which):
        commands = []
        class FakeRunner:
            def run(self, command, **kwargs):
                commands.append(command)
                if command[0] == "/usr/bin/env":
                    return "10 1 ttys001 01:00 Mon Jan 5 12:00:00 2026 /bin/bash\n11 10 ttys001 00:59 Mon Jan 5 12:00:01 2026 /opt/bin/codex\n"
                if command[1:2] == ["list-panes"]:
                    return ("%1\t10\t/dev/ttys001\tsample\t@1\texample-project\t0\tcodex\t/srv/example-project\n"
                            "%1\t10\t/dev/ttys001\tviewer\t@1\texample-project\t0\tcodex\t/srv/example-project\n")
                if command[0] == "/bin/ps":
                    return "10 /bin/bash\n11 codex resume " + SID + "\n"
                if command[0] == "/usr/sbin/lsof":
                    return "p11\nfcwd\nn/srv/example-project\n"
                if command[1:2] == ["capture-pane"]:
                    return "synthetic screen\n>\n"
                raise AssertionError(command)
        report = COLLECTOR.collect(Path("/example/absent-health.json"), FakeRunner())
        self.assertEqual(len(report["sessions"]), 1)
        self.assertEqual(report["sessions"][0]["target"]["transport"], "tmux")
        self.assertEqual(report["sessions"][0]["target"]["tmux_sessions"], ["sample", "viewer"])
        self.assertEqual(len([command for command in commands if command[1:2] == ["capture-pane"]]), 1)
        self.assertIsNone(report["sessions"][0]["session_id"])
        self.assertEqual(report["coverage"]["terminal"], "unavailable")
        self.assertEqual(report["coverage"]["tmux"], "complete")
        self.assertFalse(any("osascript" in command[0] for command in commands))
        self.assertFalse(any(word in {"send-keys", "paste-buffer", "kill", "do script"} for command in commands for word in command))
        self.assertNotIn("args", report["sessions"][0])

    @patch.object(COLLECTOR.shutil, "which", return_value="/mock/tmux")
    @patch.object(COLLECTOR.Path, "is_file", return_value=False)
    def test_linked_pane_without_verified_process_has_one_unknown_row(self, is_file, which):
        class FakeRunner:
            def run(self, command, **kwargs):
                if command[0] == "/usr/bin/env":
                    return "10 1 ttys001 01:00 Mon Jan 5 12:00:00 2026 /bin/bash\n"
                if command[1:2] == ["list-panes"]:
                    return ("%1\t10\t/dev/ttys001\tsample\t@1\texample-project\t0\tcodex\t/srv/example-project\n"
                            "%1\t10\t/dev/ttys001\tviewer\t@1\texample-project\t0\tcodex\t/srv/example-project\n")
                if command[0] == "/bin/ps":
                    return "10 /bin/bash\n"
                raise AssertionError("Unverified identity must not trigger a screen probe: " + str(command))
        report = COLLECTOR.collect(Path("/example/absent-health.json"), FakeRunner())
        self.assertEqual(len(report["sessions"]), 1)
        row = report["sessions"][0]
        self.assertIsNone(row["pid"])
        self.assertEqual(row["screen"], "")
        self.assertEqual(row["errors"], ["agent_process_unverified"])
        self.assertEqual(row["target"]["tmux_sessions"], ["sample", "viewer"])

    @patch.object(COLLECTOR.shutil, "which", return_value=None)
    @patch.object(COLLECTOR.Path, "is_file", return_value=False)
    def test_standalone_terminal_node_agent_is_verified_and_its_screen_captured(self, is_file, which):
        commands = []
        class FakeRunner:
            def run(self, command, **kwargs):
                commands.append(command)
                if command[0] == "/usr/bin/env":
                    return ("1 0 ?? 02:00 Mon Jan 5 12:00:00 2026 /System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal\n"
                            "10 1 ttys001 01:00 Mon Jan 5 12:00:00 2026 /bin/zsh\n"
                            "11 10 ttys001 00:59 Mon Jan 5 12:00:01 2026 /opt/bin/node\n"
                            "99 1 ?? 02:00 Mon Jan 5 12:00:00 2026 /opt/bin/node\n")
                if command[0] == "/usr/bin/osascript":
                    wanted = json.loads(command[-1])
                    return json.dumps({"tabs": [{"window_id": 4, "tab": 2, "tty": "ttys001",
                                      "selected": False, "visible": False, "minimized": True,
                                      "title": "example-project", "process_names": ["zsh", "node"],
                                      "screen": "verified agent screen" if "ttys001" in wanted else "", "errors": []}], "errors": []})
                if command[0] == "/bin/ps":
                    if command[2] != "10,11":
                        raise AssertionError("argv probes must remain confined to positive Terminal candidates")
                    return "10 /bin/zsh\n11 node /opt/lib/node_modules/anthropic/claude-code/cli.js --resume=" + SID + "\n"
                if command[0] == "/usr/sbin/lsof":
                    return "p11\nfcwd\nn/srv/example-project\n"
                raise AssertionError(command)
        report = COLLECTOR.collect(Path("/example/absent-health.json"), FakeRunner())
        self.assertEqual(len(report["sessions"]), 1)
        row = report["sessions"][0]
        self.assertEqual((row["agent"], row["pid"], row["session_id"]), ("claude", 11, SID))
        self.assertEqual(row["target"]["transport"], "terminal")
        self.assertEqual(row["screen"], "verified agent screen")
        self.assertTrue(row["target"]["minimized"])
        terminal_calls = [command for command in commands if command[0] == "/usr/bin/osascript"]
        self.assertEqual([json.loads(command[-1]) for command in terminal_calls], [[], ["ttys001"]])
        self.assertEqual(report["coverage"]["agent_discovery"], "partial")
        self.assertIn("agent_discovery_partial", report["errors"])

    @patch.object(COLLECTOR.shutil, "which", return_value=None)
    @patch.object(COLLECTOR.Path, "is_file", return_value=False)
    def test_unhinted_node_is_not_globally_inspected_and_discovery_is_partial(self, is_file, which):
        class FakeRunner:
            def run(self, command, **kwargs):
                if command[0] == "/usr/bin/env":
                    return "10 1 ?? 01:00 Mon Jan 5 12:00:00 2026 /opt/bin/node\n"
                raise AssertionError("an unhinted runtime must not trigger argv inspection")
        report = COLLECTOR.collect(Path("/example/absent-health.json"), FakeRunner())
        self.assertEqual(report["sessions"], [])
        self.assertEqual(report["coverage"]["processes"], "complete")
        self.assertEqual(report["coverage"]["agent_discovery"], "partial")

    @patch.object(COLLECTOR.shutil, "which", return_value=None)
    @patch.object(COLLECTOR.Path, "is_file", return_value=False)
    def test_title_hint_never_establishes_agent_or_uuid_without_argv_verification(self, is_file, which):
        class FakeRunner:
            def run(self, command, **kwargs):
                if command[0] == "/usr/bin/env":
                    return ("1 0 ?? 02:00 Mon Jan 5 12:00:00 2026 /System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal\n"
                            "10 1 ttys001 01:00 Mon Jan 5 12:00:00 2026 /opt/bin/node\n")
                if command[0] == "/usr/bin/osascript":
                    if json.loads(command[-1]):
                        raise AssertionError("unverified process must not get its screen captured")
                    return json.dumps({"tabs": [{"window_id": 4, "tab": 1, "tty": "ttys001",
                                      "title": "Claude --resume " + SID, "process_names": [], "screen": "", "errors": []}], "errors": []})
                if command[0] == "/bin/ps":
                    return "10 node /srv/example/ordinary-script.js --resume " + SID + "\n"
                raise AssertionError(command)
        report = COLLECTOR.collect(Path("/example/absent-health.json"), FakeRunner())
        self.assertEqual(report["sessions"], [])
        self.assertEqual(report["coverage"]["agent_discovery"], "complete")

    @patch.object(COLLECTOR, "MAX_SESSIONS", 1)
    @patch.object(COLLECTOR.shutil, "which", return_value=None)
    @patch.object(COLLECTOR.Path, "is_file", return_value=False)
    def test_logical_session_cap_is_explicit(self, is_file, which):
        class FakeRunner:
            def run(self, command, **kwargs):
                if command[0] == "/usr/bin/env":
                    return "10 1 ttys001 01:00 Mon Jan 5 12:00:00 2026 agy\n11 1 ttys002 01:00 Mon Jan 5 12:00:00 2026 agy\n"
                if command[0] == "/bin/ps":
                    return "10 agy -c\n11 agy -c\n"
                if command[0] == "/usr/sbin/lsof":
                    return "p10\nfcwd\nn/srv/example-project\n"
                raise AssertionError(command)
        report = COLLECTOR.collect(Path("/example/absent-health.json"), FakeRunner())
        self.assertEqual(len(report["sessions"]), 1)
        self.assertIn("session_inventory_truncated", report["errors"])
        self.assertEqual(report["coverage"]["processes"], "partial")


if __name__ == "__main__":
    unittest.main()
