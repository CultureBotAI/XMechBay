"""Offline input-safety regressions for public terminal control (2026-10-25).

Inputs are synthetic screens and mocked transports; output is unittest results.
No test sends input to Terminal, tmux or another host.
"""
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / ".agents/skills/mac-orchestration/scripts/mac_term_control.py"
spec = importlib.util.spec_from_file_location("mac_term_control", SCRIPT)
control = importlib.util.module_from_spec(spec)
previous_bytecode_setting = sys.dont_write_bytecode
try:
    sys.dont_write_bytecode = True
    spec.loader.exec_module(control)
finally:
    sys.dont_write_bytecode = previous_bytecode_setting
IDLE = "Earlier answer\n›\nGPT-example high · sample-project\n"


class FakeTransport:
    host = ""

    def __init__(self, screens=None):
        self.screens = list(screens or [IDLE])
        self.events = []
        self.session = "fleet"
        self.panes = "sample-project|%7\n"
        self.failure = None
        self.windows = "123|codex\n"

    def need_terminal(self):
        self.events.append(("need_terminal",))

    def screen(self):
        screen = self.screens.pop(0) if len(self.screens) > 1 else self.screens[0]
        if isinstance(screen, Exception):
            raise screen
        return screen

    def apple(self, script, *args):
        if script == control.WINDOWS_SCRIPT:
            return self.windows
        if script == control.SCREEN_SCRIPT:
            self.events.append(("screen", *args))
            return self.screen()
        self.events.append(("apple-input", script, *args))
        return ""

    def run(self, args, input=None, timeout=None):
        self.events.append(tuple(args))
        if self.failure and args[:2] == self.failure:
            raise RuntimeError("synthetic failure")
        if args[:2] == ["tmux", "list-panes"]:
            return self.panes
        if args[:2] == ["tmux", "display-message"]:
            return self.session
        if args[:2] == ["tmux", "capture-pane"]:
            return self.screen()
        return ""

    def cleanup(self, args):
        control.Transport.cleanup(self, args)

    @contextmanager
    def prompt_file(self, source):
        self.events.append(("stage", str(source)))
        try:
            yield "/tmp/fixture/prompt.txt"
        finally:
            self.events.append(("cleanup",))


class StateTests(unittest.TestCase):
    def test_every_goal_word(self):
        for word in ("Pursuing goal", "Goal paused", "Goal stalled", "Goal achieved", "Goal hit usage limits"):
            with self.subTest(word=word):
                self.assertEqual(control.status_state(IDLE + "GPT-example high · " + word), word)

    def test_prompt_precedes_working_and_goal(self):
        for state, text in (("APPROVAL", "Requesting permission for: git"),
                            ("QUESTION", "Queued follow-up"),
                            ("SIGN-IN", "Sign in with ChatGPT"),
                            ("TRUST-PROMPT", "Do you trust this folder?"),
                            ("UPDATE-PROMPT", "Update available")):
            with self.subTest(state=state):
                self.assertIn(state, control.status_state(IDLE + text + "\nPursuing goal"))

    def test_idle_requires_empty_latest_composer(self):
        self.assertEqual(control.status_state(IDLE), "idle")
        self.assertEqual(control.status_state(IDLE + "› unfinished input"), "")
        self.assertEqual(control.status_state("Some response\nGPT-example high"), "")
        self.assertEqual(control.status_state(""), "")
        self.assertEqual(control.status_state(IDLE + "GPT-example Goal abandoned"), "")

    def test_working_word_and_latest_footer(self):
        self.assertEqual(control.status_state(IDLE + "Working (3s) · esc to interrupt"), "Working")
        self.assertEqual(control.status_state("GPT-example Pursuing goal\nGPT-example Goal paused"), "Goal paused")

    def test_wrapped_draft_is_not_idle(self):
        for screen in ("›\n  unfinished second line\nGPT-example high · sample-project",
                       "›\n  unfinished second line",
                       "GPT-example high · sample-project\n›\n  unfinished second line"):
            with self.subTest(screen=screen):
                self.assertEqual(control.status_state(screen), "")
        self.assertEqual(control.status_state("›\n\nGPT-example high · sample-project"), "idle")

    def test_known_numbered_choices_are_questions(self):
        for screen in ("Select an option\n1. Custom value\n2. Another value",
                       "❯ 1. Yes, proceed\n  2. No, cancel",
                       "1) Allow once\n2) Deny"):
            with self.subTest(screen=screen):
                self.assertIn("QUESTION", control.prompt_states(screen))

    def test_installed_codex_question_cues(self):
        for screen in ("Type your answer", "Type your answer (optional)",
                       "Enter to submit answer", "Tab to navigate questions",
                       "Select an option to add notes"):
            with self.subTest(screen=screen):
                self.assertIn("QUESTION", control.prompt_states(screen))


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.records = Path(self.temp.name)
        self.sleep = patch.object(control.time, "sleep")
        self.sleep.start()
        self.addCleanup(self.sleep.stop)

    def controller(self, fake, override=None, command="send"):
        return control.Controller(fake, command, override, self.records, session="fleet")

    def test_refuses_each_prompt_without_input_or_content_logging(self):
        for screen in ("Requesting permission secret", "Queued follow-up secret", "Sign in with ChatGPT secret",
                       "Trust this folder secret", "Update available secret"):
            fake = FakeTransport([screen])
            with self.assertRaises(control.Refused):
                self.controller(fake).send("123", "private text")
            self.assertFalse(any(event[0] == "apple-input" for event in fake.events))
        log = next(self.records.glob("*.jsonl")).read_text()
        self.assertNotIn("secret", log)
        self.assertNotIn("private text", log)
        self.assertEqual(len(log.splitlines()), 5)

    def test_unreadable_and_empty_screens_fail_closed_even_with_override(self):
        for screen in (RuntimeError("read failed"), "", "\n  "):
            fake = FakeTransport([screen])
            with self.assertRaises(control.Refused):
                self.controller(fake, "User requested Enter").send("123", "")
            self.assertFalse(any(event[0] == "apple-input" for event in fake.events))

    def test_sign_in_is_user_only_even_with_override(self):
        fake = FakeTransport(["Sign in with ChatGPT"])
        with self.assertRaises(control.Refused):
            self.controller(fake, "User requested Enter").send("123", "")
        self.assertFalse(any(event[0] == "apple-input" for event in fake.events))
        row = json.loads(next(self.records.glob("*.jsonl")).read_text())
        self.assertEqual(row["event"], "refused")
        self.assertIn("SIGN-IN", row["states"])

    def test_existing_approval_wordings_refuse_input(self):
        for text in ("Allow access to this file?", "Run this command?", "Accept changes?",
                     "Would you like to make the following edits?",
                     "Would you like to grant these permissions?",
                     "Would you like to send input to the existing terminal?"):
            with self.subTest(text=text):
                fake = FakeTransport([text])
                self.assertIn("APPROVAL", control.prompt_states(text))
                with self.assertRaises(control.Refused):
                    self.controller(fake).send("123", "private text")
                self.assertFalse(any(event[0] == "apple-input" for event in fake.events))

    def test_explicit_override_is_logged_before_literal_send_and_post_read(self):
        fake = FakeTransport(["Update available", IDLE])
        payload = "'$HOME' `touch bad` $(touch bad) \\\""
        self.assertEqual(self.controller(fake, "User instructed skip update").send("123", payload), IDLE)
        inputs = [event for event in fake.events if event[0] == "apple-input"]
        self.assertEqual(inputs[0][-1], payload)
        self.assertEqual(fake.events[-1][0], "screen")
        log = json.loads(next(self.records.glob("*.jsonl")).read_text())
        self.assertEqual(log["event"], "explicit-user-override")

    def test_second_key_is_blocked_if_first_opens_prompt(self):
        fake = FakeTransport([IDLE, "Requesting permission", "Requesting permission"])
        controller = self.controller(fake)
        controller.send("123", "1")
        with self.assertRaises(control.OutcomeUnknown):
            controller.send("123", "", after_input=True)
        self.assertEqual(sum(event[0] == "apple-input" for event in fake.events), 1)

    def test_override_cannot_authorize_next_key_prompt(self):
        for later in ("Requesting permission", "Update available"):
            with self.subTest(later=later):
                fake = FakeTransport(["Update available", later, later])
                controller = self.controller(fake, "User instructed skip update")
                controller.send("123", "")
                with self.assertRaises(control.OutcomeUnknown):
                    controller.send("123", "", after_input=True)
                self.assertEqual(sum(event[0] == "apple-input" for event in fake.events), 1)

    def test_override_does_not_arm_on_initially_unprompted_screen(self):
        fake = FakeTransport([IDLE, "Requesting permission"])
        controller = self.controller(fake, "User instructed skip update")
        controller.guard("123")
        with self.assertRaises(control.Refused):
            controller.send("123", "")
        self.assertFalse(any(event[0] == "apple-input" for event in fake.events))

    def test_override_cannot_follow_changed_prompt_before_first_input(self):
        fake = FakeTransport(["Requesting permission for command A",
                              "Requesting permission for command B"])
        controller = self.controller(fake, "User approved command A")
        controller.guard("123")
        with self.assertRaises(control.Refused):
            controller.send("123", "")
        self.assertFalse(any(event[0] == "apple-input" for event in fake.events))

    def test_paste_preflights_keep_override_but_enter_cannot_reuse_it(self):
        for tmux in (False, True):
            with self.subTest(tmux=tmux):
                fake = FakeTransport(["Type your answer", "Type your answer", "Requesting permission"])
                controller = self.controller(fake, "User supplied the answer", command="paste")
                with self.assertRaises(control.OutcomeUnknown):
                    controller.paste("sample-project" if tmux else "123", Path("answer.txt"), tmux)
                if tmux:
                    self.assertTrue(any(event[:2] == ("tmux", "paste-buffer") for event in fake.events))
                    self.assertFalse(any(event[:2] == ("tmux", "send-keys") for event in fake.events))
                    self.assertTrue(any(event[:2] == ("tmux", "delete-buffer") for event in fake.events))
                else:
                    inputs = [event for event in fake.events if event[0] == "apple-input"]
                    self.assertEqual(len(inputs), 1)
                    self.assertEqual(inputs[0][1], control.PASTE_SCRIPT)
                self.assertEqual(fake.events[-1], ("cleanup",))

    def test_tmux_file_paste_order_and_cleanup(self):
        fake = FakeTransport()
        self.controller(fake, command="tmux-paste").paste("sample-project", Path("goal.txt"), True)
        commands = [event[1] for event in fake.events if event[0] == "tmux"]
        self.assertLess(commands.index("load-buffer"), commands.index("paste-buffer"))
        self.assertLess(commands.index("paste-buffer"), commands.index("send-keys"))
        paste = next(event for event in fake.events if event[:2] == ("tmux", "paste-buffer"))
        self.assertIn("-p", paste)
        self.assertIn(("tmux", "send-keys", "-t", "%7", "Enter"), fake.events)
        self.assertEqual(fake.events[-1], ("cleanup",))
        self.assertIn("delete-buffer", commands)

    def test_tmux_refuses_other_session_and_split_panes(self):
        for selector in ("worker-123:0", "%7", "sample-project;touch bad"):
            fake = FakeTransport()
            with self.assertRaises(control.Refused):
                self.controller(fake).paste(selector, Path("goal.txt"), True)
            self.assertEqual(fake.events, [])
        fake = FakeTransport()
        fake.panes += "sample-project|%8\n"
        with self.assertRaises(control.Refused):
            self.controller(fake).paste("sample-project", Path("goal.txt"), True)
        self.assertFalse(any(event[0] == "stage" for event in fake.events))
        fake = FakeTransport()
        fake.session = "worker-123"
        with self.assertRaises(control.Refused):
            self.controller(fake, "user instruction").paste("sample-project", Path("goal.txt"), True)
        self.assertFalse(any(event[0] == "stage" for event in fake.events))

    def test_prompt_between_paste_and_enter_prevents_submit_and_cleans(self):
        fake = FakeTransport([IDLE, IDLE, "Requesting permission"])
        with self.assertRaises(control.OutcomeUnknown):
            self.controller(fake).paste("sample-project", Path("goal.txt"), True)
        self.assertFalse(any(event[:2] == ("tmux", "send-keys") for event in fake.events))
        self.assertTrue(any(event[:2] == ("tmux", "delete-buffer") for event in fake.events))
        self.assertEqual(fake.events[-1], ("cleanup",))

    def test_failed_paste_cleans_buffer_and_file(self):
        fake = FakeTransport()
        fake.failure = ["tmux", "paste-buffer"]
        with self.assertRaises(RuntimeError):
            self.controller(fake).paste("sample-project", Path("goal.txt"), True)
        self.assertTrue(any(event[:2] == ("tmux", "delete-buffer") for event in fake.events))
        self.assertEqual(fake.events[-1], ("cleanup",))


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        settings = patch.dict(control.os.environ, {
            "MAC_TERM_HOST": "local", "XMECHBAY_TMUX_SESSION": "fleet",
            "XMECHBAY_RECORDS_DIR": self.temp.name,
        })
        settings.start()
        self.addCleanup(settings.stop)

    def test_send_control_characters_rejected_before_transport(self):
        for char in ("\r", "\n", "\x1b", "\x03", "\t", "\x7f", "\x85", "\0"):
            with self.subTest(char=repr(char)):
                with patch.object(control.Transport, "run") as run:
                    with redirect_stderr(io.StringIO()) as stderr:
                        with self.assertRaises(SystemExit) as raised:
                            control.main(["send", "123", "text" + char + "more"])
                    self.assertEqual(raised.exception.code, 2)
                    self.assertIn("control characters", stderr.getvalue())
                    run.assert_not_called()

    def test_status_inventory_skips_blank_rows(self):
        fake = FakeTransport()
        fake.windows = "123|codex\n\n456|agy\n  \n789|claude\n"
        with patch.object(control, "Transport", return_value=fake):
            with redirect_stdout(io.StringIO()) as output:
                control.main(["status"])
        self.assertEqual(len(output.getvalue().splitlines()), 3)
        self.assertIn("123|codex|idle|", output.getvalue())
        self.assertIn("789|claude|idle|", output.getvalue())

    def test_status_inventory_rejects_malformed_row_clearly(self):
        fake = FakeTransport()
        fake.windows = "123|codex\nbroken raw title\n"
        with patch.object(control, "Transport", return_value=fake):
            with redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(control.Refused, "invalid window inventory row"):
                    control.main(["status"])

    def test_bash_cli_dispatch_without_override(self):
        # This invokes only argument validation/help, never an OS transport.
        result = subprocess.run(["/bin/bash", str(SCRIPT.with_name("mac-term.sh")),
                                 "send", "--help"], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("USER_INSTRUCTION", result.stdout)

    def test_bash_cli_rejects_invalid_host_before_transport(self):
        result = subprocess.run(["/bin/bash", str(SCRIPT.with_name("mac-term.sh")),
                                 "-H", "invalid;host", "status"], text=True, capture_output=True)
        self.assertEqual(result.returncode, 5)
        self.assertIn("bare hostname", result.stderr)

    def test_ssh_quotes_each_argument_once(self):
        with patch.object(control.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "ok", "")) as run:
            value = "two words; $(touch bad) ' `echo bad`"
            control.Transport("example.invalid").run(["osascript", "-", "123", value], input="script")
            argv = run.call_args.args[0]
            self.assertEqual(control.shlex.split(argv[-1]), ["osascript", "-", "123", value])
            self.assertEqual(run.call_args.kwargs["input"], "script")

    def test_remote_scp_staging_and_cleanup_on_exception(self):
        transport = control.Transport("example.invalid")
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "a prompt with spaces.txt"
            source.write_text("/goal literal text")
            with patch.object(transport, "run", side_effect=["/tmp/xmechbay-mac-term.ABCD1234\n", ""]) as run:
                with patch.object(control.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as scp:
                    with self.assertRaisesRegex(RuntimeError, "stop"):
                        with transport.prompt_file(source) as staged:
                            self.assertTrue(staged.endswith("/prompt.txt"))
                            self.assertEqual(Path(scp.call_args.args[0][-2]).read_text(), "/goal literal text")
                            raise RuntimeError("stop")
                self.assertEqual(run.call_args.args[0], ["rm", "-rf", "--", "/tmp/xmechbay-mac-term.ABCD1234"])

    def test_prompt_control_bytes_rejected(self):
        for data in (b"", b"x" * 4001, b"\x1b[201~", b"\0", b"\x03", b"\r"):
            with self.assertRaises(ValueError):
                control.validate_prompt(data)

    def test_local_stage_retains_literal_bytes_and_is_removed(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "goal.txt"
            data = b"/goal keep 'quotes' and $(literal)\nsecond line"
            path.write_bytes(data)
            with control.Transport("local").prompt_file(path) as staged:
                self.assertEqual(Path(staged).read_bytes(), data)
                self.assertEqual(Path(staged).stat().st_mode & 0o777, 0o600)
            self.assertFalse(Path(staged).exists())


class AuxiliaryCommandTests(unittest.TestCase):
    def test_path_resolution_handles_shell_metacharacters_literally(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder) / "demo $(touch injected) ' quote"
            directory.mkdir()
            result = subprocess.run(["/bin/sh", "-c", control.RESOLVE_DIRECTORY_SCRIPT,
                                     "xmechbay", str(directory)], cwd=folder, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(Path(result.stdout.strip()).resolve(), directory.resolve())
            self.assertFalse((Path(folder) / "injected").exists())

    def test_wait_file_metacharacters_are_data_not_shell_source(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "$(touch injected) ' quote.txt"
            path.write_text("synthetic")
            result = subprocess.run(["/bin/sh", "-c", control.WAIT_FILE_SCRIPT,
                                     "xmechbay", str(path), "1"], cwd=folder, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "present\n")
            self.assertFalse((Path(folder) / "injected").exists())

    def test_open_passes_directory_and_command_only_as_arguments(self):
        fake = FakeTransport()
        directory = "example folder/$(literal) ' quote"
        command = "printf '%s' 'explicit shell command'"
        with patch.object(fake, "run", return_value=directory + "\n") as run:
            with patch.object(fake, "apple", return_value="123") as apple:
                with patch.object(control, "Transport", return_value=fake):
                    with redirect_stdout(io.StringIO()):
                        control.main(["open", directory, command])
        self.assertEqual(run.call_args.args[0], ["sh", "-c", control.RESOLVE_DIRECTORY_SCRIPT, "xmechbay", directory])
        self.assertEqual(apple.call_args.args, (control.OPEN_SCRIPT, directory, command))
        self.assertNotIn(directory, control.OPEN_SCRIPT)
        self.assertIn("quoted form of", control.OPEN_SCRIPT)

    def test_invalid_numeric_selectors_and_timeouts_never_reach_transport(self):
        cases = (["screen", "1;false"], ["screen", "1", "2;false"],
                 ["front", "1;false"], ["wait-file", "example.txt", "1;false"],
                 ["cascade", "--w", "1;false"])
        for argv in cases:
            with self.subTest(argv=argv):
                with patch.object(control.Transport, "run") as run:
                    with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                        control.main(argv)
                run.assert_not_called()

    def test_literal_control_key_requires_named_alias(self):
        for key in ("\x1b", "\n", "\x7f", "\x85"):
            with self.subTest(key=repr(key)):
                with patch.object(control.Transport, "run") as run:
                    with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                        control.main(["key", "123", key])
                run.assert_not_called()

    def test_pinned_model_picker_command_is_not_exposed(self):
        with patch.object(control.Transport, "run") as run:
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                control.main(["codex-model", "123", "high"])
        run.assert_not_called()

    def test_cascade_layout_is_computed_without_shell(self):
        rows = control.window_rows("1|123|0,0,600,400|tty0|demo\n2|456|20,20,620,420|tty1|demo two\n")
        moves = control.cascade_moves(rows, (1600, 25, 1000), 600, 400, 20, 30, 1, "", "bottom-right", "")
        self.assertEqual(moves, [(["123"], (1000, 600, 1600, 1000)), (["456"], (980, 570, 1580, 970))])
        with self.assertRaises(control.Refused):
            control.cascade_moves(rows, (1000, 25, 1000), 600, 400, 20, 30, 2, "", "bottom-right", "")
        with self.assertRaises(ValueError):
            control.cascade_moves(rows, (1600, 25, 1000), 600, 400, 20, 30, 1, "", "bottom-right", "123;false")

    def test_cascade_dry_run_reads_without_mutating_windows(self):
        fake = FakeTransport()
        inventory = "1|123|0,0,600,400|tty0|demo\n"
        with patch.object(fake, "run", return_value="1600 25 1000"):
            with patch.object(fake, "apple", return_value=inventory) as apple:
                with patch.object(control, "Transport", return_value=fake):
                    with redirect_stdout(io.StringIO()) as output:
                        control.main(["cascade", "--w", "600", "--h", "400", "--dry-run"])
        apple.assert_called_once_with(control.ALL_WINDOWS_SCRIPT)
        self.assertIn("would move 123", output.getvalue())


class DeadlineTests(unittest.TestCase):
    """Synthetic subprocess and screen failures; never contact a live target."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name) / "prompt.txt"
        self.source.write_text("/goal synthetic task", encoding="utf-8")
        sleeping = patch.object(control.time, "sleep")
        sleeping.start()
        self.addCleanup(sleeping.stop)

    def controller(self, transport):
        return control.Controller(transport, "paste", records=Path(self.temp.name), session="fleet")

    def test_local_and_remote_commands_have_deadlines(self):
        for host in ("local", "worker.example.invalid"):
            with self.subTest(host=host):
                with patch.object(control.subprocess, "run",
                                  return_value=subprocess.CompletedProcess([], 0, "ok", "")) as run:
                    self.assertEqual(control.Transport(host).run(["true"]), "ok")
                    self.assertEqual(run.call_args.kwargs.get("timeout"), 120)
                    if host != "local":
                        argv = run.call_args.args[0]
                        for option in ("BatchMode=yes", "ConnectTimeout=10",
                                       "ServerAliveInterval=15", "ServerAliveCountMax=2"):
                            self.assertIn(option, argv)

    def test_timeout_is_unknown_and_does_not_disclose_payload(self):
        for host in ("local", "worker.example.invalid"):
            with self.subTest(host=host):
                error = subprocess.TimeoutExpired(["osascript", "synthetic-private-input"], 120,
                                                  output="synthetic-private-output", stderr="synthetic-private-error")
                with patch.object(control.subprocess, "run", side_effect=error):
                    with self.assertRaises(RuntimeError) as raised:
                        control.Transport(host).apple("synthetic-private-script", "123", "synthetic-private-input")
                self.assertIsInstance(raised.exception, control.TransportTimeout)
                self.assertNotIsInstance(raised.exception, control.Refused)
                self.assertIn("outcome unknown", str(raised.exception))
                self.assertNotIn("synthetic-private", str(raised.exception))
                self.assertTrue(raised.exception.__suppress_context__)

    def test_ssh_disconnect_is_unknown_but_local_exit_is_not(self):
        with patch.object(control.subprocess, "run",
                          return_value=subprocess.CompletedProcess([], 255, "private-output", "private-error")):
            with self.assertRaises(RuntimeError) as remote:
                control.Transport("worker.example.invalid").run(["tmux", "send-keys", "private-input"])
            with self.assertRaises(RuntimeError) as local:
                control.Transport("local").run(["tmux", "send-keys", "private-input"])
        self.assertIsInstance(remote.exception, control.OutcomeUnknown)
        self.assertNotIsInstance(local.exception, control.OutcomeUnknown)
        self.assertNotIn("private-", str(remote.exception))
        self.assertNotIn("private-", str(local.exception))

    def test_cleanup_is_bounded_and_warning_omits_exception_and_arguments(self):
        transport = control.Transport("worker.example.invalid")
        with patch.object(transport, "run", side_effect=OSError("synthetic-private-error")) as run:
            with redirect_stderr(io.StringIO()) as warning:
                transport.cleanup(["rm", "-rf", "--", "synthetic-private-path"])
        self.assertEqual(run.call_args.kwargs, {"timeout": 15})
        self.assertIn("cleanup", warning.getvalue())
        self.assertNotIn("synthetic-private", warning.getvalue())

    def test_scp_is_bounded_and_cleanup_cannot_mask_primary_failure(self):
        transport = control.Transport("worker.example.invalid")
        primary = RuntimeError("synthetic primary failure")
        with patch.object(transport, "run",
                          side_effect=["/tmp/xmechbay-mac-term.ABCD\n", OSError("private cleanup")]) as run:
            with patch.object(control.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as scp:
                with redirect_stderr(io.StringIO()):
                    with self.assertRaises(RuntimeError) as raised:
                        with transport.prompt_file(self.source):
                            raise primary
        self.assertIs(raised.exception, primary)
        self.assertEqual(scp.call_args.kwargs.get("timeout"), 120)
        self.assertIn("ServerAliveInterval=15", scp.call_args.args[0])
        self.assertEqual(run.call_args.kwargs, {"timeout": 15})

    def test_scp_timeout_never_yields_and_still_cleans(self):
        transport = control.Transport("worker.example.invalid")
        with patch.object(transport, "run", side_effect=["/tmp/xmechbay-mac-term.ABCD\n", ""]) as run:
            with patch.object(control.subprocess, "run",
                              side_effect=subprocess.TimeoutExpired(["scp", "private-path"], 120)):
                with self.assertRaises(RuntimeError) as raised:
                    with transport.prompt_file(self.source):
                        self.fail("a failed copy must not reach the input step")
        self.assertIsInstance(raised.exception, control.TransportTimeout)
        self.assertIn("no input sent", str(raised.exception))
        self.assertNotIn("private-path", str(raised.exception))
        self.assertTrue(raised.exception.__suppress_context__)
        self.assertEqual(run.call_args.args[0][:3], ["rm", "-rf", "--"])
        self.assertEqual(run.call_args.kwargs, {"timeout": 15})

    def test_wait_file_deadline_includes_requested_wait(self):
        for values, timeout in (([], 1920), (["1"], 121), (["86400"], 86520)):
            with self.subTest(values=values):
                with patch.object(control.Transport, "run", return_value="ready") as run:
                    with redirect_stdout(io.StringIO()):
                        control.main(["-H", "local", "wait-file", "synthetic-output", *values])
                self.assertEqual(run.call_args.kwargs.get("timeout"), timeout)

    def test_unreadable_screen_before_input_remains_refusal(self):
        fake = FakeTransport([RuntimeError("synthetic screen error")])
        with self.assertRaises(control.Refused):
            self.controller(fake).send("123", "synthetic command")
        self.assertFalse(any(event[0] == "apple-input" for event in fake.events))

    def test_unreadable_screen_after_send_is_unknown(self):
        for screen in ("", RuntimeError("synthetic screen error")):
            with self.subTest(screen=type(screen).__name__):
                fake = FakeTransport([IDLE, screen])
                with self.assertRaises(RuntimeError) as raised:
                    self.controller(fake).send("123", "synthetic command")
                self.assertIsInstance(raised.exception, control.OutcomeUnknown)
                self.assertNotIsInstance(raised.exception, control.Refused)
                self.assertEqual(sum(event[0] == "apple-input" for event in fake.events), 1)

    def test_unreadable_screen_after_paste_stops_before_enter(self):
        for tmux in (False, True):
            with self.subTest(tmux=tmux):
                fake = FakeTransport([IDLE, IDLE, RuntimeError("synthetic screen error")])
                with self.assertRaises(RuntimeError) as raised:
                    self.controller(fake).paste("sample-project" if tmux else "123", self.source, tmux)
                self.assertIsInstance(raised.exception, control.OutcomeUnknown)
                self.assertNotIsInstance(raised.exception, control.Refused)
                if tmux:
                    self.assertFalse(any(event[:2] == ("tmux", "send-keys") for event in fake.events))
                    self.assertTrue(any(event[:2] == ("tmux", "delete-buffer") for event in fake.events))
                else:
                    self.assertEqual(sum(event[0] == "apple-input" for event in fake.events), 1)
                self.assertEqual(fake.events[-1], ("cleanup",))

    def test_unreadable_screen_after_paste_enter_is_unknown(self):
        for tmux in (False, True):
            with self.subTest(tmux=tmux):
                fake = FakeTransport([IDLE, IDLE, IDLE, ""])
                with self.assertRaises(RuntimeError) as raised:
                    self.controller(fake).paste("sample-project" if tmux else "123", self.source, tmux)
                self.assertIsInstance(raised.exception, control.OutcomeUnknown)
                if tmux:
                    self.assertEqual(sum(event[:2] == ("tmux", "send-keys") for event in fake.events), 1)
                else:
                    self.assertEqual(sum(event[0] == "apple-input" for event in fake.events), 2)
                self.assertEqual(fake.events[-1], ("cleanup",))

    def test_screen_failure_receipt_matches_input_phase_without_private_content(self):
        cases = [
            ("send", False, 0), ("send", False, 1),
            ("paste", False, 1), ("paste", False, 2), ("paste", False, 3),
            ("paste", True, 1), ("paste", True, 2), ("paste", True, 3),
        ]
        for index, (operation, tmux, successful_reads) in enumerate(cases):
            with self.subTest(operation=operation, tmux=tmux, successful_reads=successful_reads):
                fake = FakeTransport([IDLE] * successful_reads + [RuntimeError("synthetic-private-screen")])
                controller = self.controller(fake)
                after_input = successful_reads >= (1 if operation == "send" else 2)
                expected = control.OutcomeUnknown if after_input else control.Refused
                with self.assertRaises(expected):
                    if operation == "send":
                        controller.send("123", "synthetic-private-command")
                    else:
                        controller.paste("sample-project" if tmux else "123", self.source, tmux)
                raw = next(Path(self.temp.name).glob("mac-term-guard-*.jsonl")).read_text(encoding="utf-8")
                rows = [json.loads(line) for line in raw.splitlines()]
                self.assertEqual(len(rows), index + 1)
                self.assertEqual(rows[-1]["event"], "outcome-unknown" if after_input else "refused")
                self.assertEqual(rows[-1]["states"], ["screen-unavailable"])
                self.assertEqual(rows[-1]["target"], "%7" if tmux else "123")
                self.assertEqual(set(rows[-1]), {"time", "host", "target", "command", "event", "states"})
                self.assertNotIn("synthetic-private", raw)
                self.assertNotIn("/goal", raw)

    def test_timed_out_buffer_load_still_cleans_without_pasting(self):
        fake = FakeTransport()
        original = fake.run

        def run(args, **kwargs):
            if args[:2] == ["tmux", "load-buffer"]:
                fake.events.append(tuple(args))
                raise control.TransportTimeout("synthetic timeout")
            return original(args, **kwargs)

        with patch.object(fake, "run", side_effect=run):
            with self.assertRaises(RuntimeError) as raised:
                self.controller(fake).paste("sample-project", self.source, True)
        self.assertIsInstance(raised.exception, control.TransportTimeout)
        self.assertFalse(any(event[:2] == ("tmux", "paste-buffer") for event in fake.events))
        self.assertTrue(any(event[:2] == ("tmux", "delete-buffer") for event in fake.events))
        self.assertEqual(fake.events[-1], ("cleanup",))

    def test_timeout_during_paste_and_failed_cleanup_preserve_original_outcome(self):
        fake = FakeTransport()
        original = fake.run
        primary = control.TransportTimeout("synthetic paste timeout")

        def run(args, **kwargs):
            if args[:2] == ["tmux", "paste-buffer"]:
                raise primary
            if args[:2] == ["tmux", "delete-buffer"]:
                raise OSError("synthetic-private-error")
            return original(args, **kwargs)

        with patch.object(fake, "run", side_effect=run):
            with redirect_stderr(io.StringIO()) as warning:
                with self.assertRaises(RuntimeError) as raised:
                    self.controller(fake).paste("sample-project", self.source, True)
        self.assertIs(raised.exception, primary)
        self.assertNotIn("synthetic-private-error", warning.getvalue())
        self.assertFalse(any(event[:2] == ("tmux", "send-keys") for event in fake.events))
        self.assertEqual(fake.events[-1], ("cleanup",))

    def test_cleanup_failure_does_not_change_verified_success(self):
        fake = FakeTransport()
        fake.failure = ["tmux", "delete-buffer"]
        with redirect_stderr(io.StringIO()) as warning:
            screen = self.controller(fake).paste("sample-project", self.source, True)
        self.assertEqual(screen, IDLE)
        self.assertIn("cleanup", warning.getvalue())
        self.assertEqual(fake.events[-1], ("cleanup",))

    def test_key_sequence_stops_after_input_timeout(self):
        fake = FakeTransport()
        original = fake.apple

        def apple(script, *args):
            if script == control.SEND_SCRIPT:
                fake.events.append(("apple-input", script, *args))
                raise control.TransportTimeout("synthetic timeout")
            return original(script, *args)

        with patch.object(control, "Transport", return_value=fake):
            with patch.object(fake, "apple", side_effect=apple):
                with self.assertRaises(control.TransportTimeout):
                    control.main(["-H", "local", "key", "123", "enter", "enter"])
        self.assertEqual(sum(event[0] == "apple-input" for event in fake.events), 1)

    def test_prompt_after_paste_records_partial_outcome(self):
        prompts = ("Requesting permission", "Type your answer", "Trust this folder",
                   "Update available", "Sign in with ChatGPT")
        for tmux in (False, True):
            for prompt in prompts:
                with self.subTest(tmux=tmux, prompt=prompt):
                    fake = FakeTransport([IDLE, IDLE, prompt + " synthetic-private-context"])
                    with self.assertRaises(RuntimeError) as raised:
                        self.controller(fake).paste("sample-project" if tmux else "123", self.source, tmux)
                    self.assertIsInstance(raised.exception, control.OutcomeUnknown)
                    self.assertNotIsInstance(raised.exception, control.Refused)
                    message = str(raised.exception)
                    self.assertIn("earlier input was attempted", message)
                    self.assertIn("no further input sent", message)
                    self.assertIn("do not repeat earlier input", message)
                    self.assertNotIn("; no input sent", message)
                    self.assertNotIn("synthetic-private-context", message)
                    raw = next(Path(self.temp.name).glob("mac-term-guard-*.jsonl")).read_text(encoding="utf-8")
                    receipt = json.loads(raw.splitlines()[-1])
                    self.assertEqual(receipt["event"], "outcome-unknown")
                    self.assertEqual(receipt["states"], control.prompt_states(prompt))
                    self.assertNotIn("synthetic-private-context", raw)
                    self.assertNotIn("/goal", raw)
                    if tmux:
                        self.assertEqual(sum(event[:2] == ("tmux", "paste-buffer") for event in fake.events), 1)
                        self.assertFalse(any(event[:2] == ("tmux", "send-keys") for event in fake.events))
                        self.assertTrue(any(event[:2] == ("tmux", "delete-buffer") for event in fake.events))
                    else:
                        self.assertEqual(sum(event[0] == "apple-input" for event in fake.events), 1)
                    self.assertEqual(fake.events[-1], ("cleanup",))

    def test_key_sequence_prompt_records_partial_outcome_and_stops(self):
        for sent in (1, 2):
            for override in (False, True):
                with self.subTest(sent=sent, override=override):
                    screens = [IDLE] * (sent * 2) + ["Type your answer synthetic-private-context"]
                    if override:
                        screens[0] = "Update available"
                    fake = FakeTransport(screens)
                    args = ["-H", "local", "key", "123", "enter", "enter", "enter"]
                    if override:
                        args[:0] = ["--override-prompt", "User requested one update response"]
                    with patch.dict(control.os.environ, {"XMECHBAY_RECORDS_DIR": self.temp.name}):
                        with patch.object(control, "Transport", return_value=fake):
                            with self.assertRaises(RuntimeError) as raised:
                                control.main(args)
                    self.assertIsInstance(raised.exception, control.OutcomeUnknown)
                    self.assertNotIsInstance(raised.exception, control.Refused)
                    self.assertIn("do not repeat earlier input", str(raised.exception))
                    self.assertEqual(sum(event[0] == "apple-input" for event in fake.events), sent)
                    raw = next(Path(self.temp.name).glob("mac-term-guard-*.jsonl")).read_text(encoding="utf-8")
                    receipt = json.loads(raw.splitlines()[-1])
                    self.assertEqual(receipt["event"], "outcome-unknown")
                    self.assertEqual(receipt["states"], ["QUESTION"])
                    self.assertNotIn("synthetic-private-context", raw)

    def test_unreadable_later_key_guard_retains_prior_input_phase(self):
        fake = FakeTransport([IDLE, IDLE, RuntimeError("synthetic-private-screen")])
        with patch.dict(control.os.environ, {"XMECHBAY_RECORDS_DIR": self.temp.name}):
            with patch.object(control, "Transport", return_value=fake):
                with self.assertRaises(RuntimeError) as raised:
                    control.main(["-H", "local", "key", "123", "enter", "enter"])
        self.assertIsInstance(raised.exception, control.OutcomeUnknown)
        self.assertNotIsInstance(raised.exception, control.Refused)
        self.assertEqual(sum(event[0] == "apple-input" for event in fake.events), 1)
        raw = next(Path(self.temp.name).glob("mac-term-guard-*.jsonl")).read_text(encoding="utf-8")
        receipt = json.loads(raw)
        self.assertEqual(receipt["event"], "outcome-unknown")
        self.assertEqual(receipt["states"], ["screen-unavailable"])
        self.assertNotIn("synthetic-private-screen", raw)


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        settings = patch.dict(control.os.environ, {}, clear=True)
        settings.start()
        self.addCleanup(settings.stop)

    def test_explicit_hosts_are_not_remapped(self):
        self.assertEqual(control.Transport("local").host, "")
        for host in ("example.invalid", "192.0.2.10", "lab-node"):
            with self.subTest(host=host):
                self.assertEqual(control.Transport(host).host, host)
        for host in ("user@example.invalid", "-example.invalid", "host;command", "host name"):
            with self.subTest(host=host):
                with self.assertRaisesRegex(ValueError, "bare hostname"):
                    control.Transport(host)

    def test_long_host_option_reaches_explicit_transport(self):
        fake = FakeTransport()
        with patch.object(control, "Transport", return_value=fake) as transport:
            with redirect_stdout(io.StringIO()):
                control.main(["--host", "example.invalid", "status"])
        transport.assert_called_once_with("example.invalid")

    def test_shell_host_validation_also_covers_read_commands(self):
        result = subprocess.run(["/bin/bash", str(SCRIPT.with_name("mac-term.sh")),
                                 "--host", "user@example.invalid", "windows"],
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 5)
        self.assertIn("bare hostname", result.stderr)

    def test_session_defaults_to_fleet(self):
        controller = control.Controller(FakeTransport(), "status")
        self.assertEqual(controller.session, "fleet")

    def test_configured_session_uses_exact_lookup_and_owned_prefix(self):
        fake = FakeTransport()
        fake.session = "example-fleet"
        with patch.dict(control.os.environ, {"XMECHBAY_TMUX_SESSION": "example-fleet"}):
            controller = control.Controller(fake, "tmux-paste", records=self.temp.name)
        self.assertEqual(controller.resolve_pane("example-fleet:sample-project"), "%7")
        self.assertEqual(controller.screen("%7", tmux=True), IDLE)
        self.assertIn(("tmux", "list-panes", "-s", "-t", "=example-fleet",
                       "-F", "#{window_name}|#{pane_id}"), fake.events)
        with self.assertRaises(control.Refused):
            controller.resolve_pane("fleet:sample-project")

    def test_session_validation_rejects_selectors_before_transport(self):
        for session in ("", "fleet:", "fleet.0", "=fleet", "fleet;command", "fleet name", "-fleet"):
            with self.subTest(session=session):
                fake = FakeTransport()
                with patch.dict(control.os.environ, {"XMECHBAY_TMUX_SESSION": session}):
                    with self.assertRaisesRegex(ValueError, "tmux session"):
                        control.Controller(fake, "tmux-paste")
                self.assertEqual(fake.events, [])

    def test_similar_session_or_window_names_do_not_authorize_input(self):
        fake = FakeTransport()
        fake.session = "fleet-extra"
        controller = control.Controller(fake, "tmux-paste", records=self.temp.name)
        with self.assertRaises(control.Refused):
            controller.paste("sample-project", Path("goal.txt"), tmux=True)
        self.assertFalse(any(event[0] == "stage" for event in fake.events))
        fake = FakeTransport()
        fake.panes = "sample-project-extra|%7\n"
        controller = control.Controller(fake, "tmux-paste", records=self.temp.name)
        with self.assertRaises(control.Refused):
            controller.resolve_pane("sample-project")

    def test_records_default_to_private_runtime_directory(self):
        controller = control.Controller(FakeTransport(), "status")
        self.assertEqual(controller.records, control.ROOT / ".local" / "records")

    def test_records_environment_and_explicit_argument(self):
        environment_path = Path(self.temp.name) / "runtime-records"
        explicit_path = Path(self.temp.name) / "explicit-records"
        with patch.dict(control.os.environ, {"XMECHBAY_RECORDS_DIR": str(environment_path)}):
            self.assertEqual(control.Controller(FakeTransport(), "status").records, environment_path)
            self.assertEqual(control.Controller(FakeTransport(), "status", records=explicit_path).records,
                             explicit_path)

    def test_refusal_metadata_is_private_and_omits_screen_and_input(self):
        records = Path(self.temp.name) / "runtime-records"
        fake = FakeTransport(["Requesting permission: synthetic screen content"])
        controller = control.Controller(fake, "send", records=records)
        with self.assertRaises(control.Refused):
            controller.send("123", "synthetic private input")
        path = next(records.glob("*.jsonl"))
        row = json.loads(path.read_text())
        self.assertEqual(row["event"], "refused")
        self.assertEqual(row["states"], ["APPROVAL"])
        self.assertNotIn("synthetic", path.read_text())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(records.stat().st_mode & 0o777, 0o700)

    def test_failed_override_record_stops_before_input(self):
        records = Path(self.temp.name) / "not-a-directory"
        records.write_text("synthetic obstruction")
        fake = FakeTransport(["Update available"])
        controller = control.Controller(fake, "send", "User requested skip update", records)
        with self.assertRaises(OSError):
            controller.send("123", "")
        self.assertFalse(any(event[0] == "apple-input" for event in fake.events))

    def test_symlinked_mac_term_wrapper_resolves_script_dir(self):
        bin_a = Path(self.temp.name) / "bin-a"
        bin_b = Path(self.temp.name) / "bin-b"
        decoy = Path(self.temp.name) / "cdpath-decoy"
        (decoy / ".agents" / "skills" / "mac-orchestration" / "scripts").mkdir(parents=True)
        (decoy / "bin-a").mkdir(parents=True)
        (decoy / "bin-b").mkdir(parents=True)
        bin_a.mkdir()
        bin_b.mkdir()
        hop1 = bin_b / "mac-term"
        hop1.symlink_to(SCRIPT.with_name("mac-term.sh"))
        hop2 = bin_a / "mac-term"
        hop2.symlink_to(Path("..") / "bin-b" / "mac-term")
        env = dict(
            control.os.environ,
            XMECHBAY_PYTHON=control.sys.executable,
            CDPATH=f".:{decoy}",
        )
        direct = subprocess.run(
            ["/bin/bash", ".agents/skills/mac-orchestration/scripts/mac-term.sh", "send", "--help"],
            cwd=control.ROOT,
            env=env,
            text=True,
            capture_output=True,
        )
        self.assertEqual(direct.returncode, 0, direct.stderr)
        self.assertIn("USER_INSTRUCTION", direct.stdout)

        result = subprocess.run(
            ["/bin/bash", "bin-a/mac-term", "send", "--help"],
            cwd=self.temp.name,
            env=env,
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("USER_INSTRUCTION", result.stdout)

    def test_host_key_alias_propagates_to_ssh_and_scp(self):
        ts_ip = ".".join(["100", "73", "4", "5"])
        tr = control.Transport(ts_ip, host_key_alias="worker-a.example.invalid")
        seen = []

        class OkRes:
            returncode = 0
            stdout = "/tmp/xmechbay-mac-term.AbCdEf12\n"
            stderr = ""

        def fake_run(cmd, **kwargs):
            seen.append(cmd)
            return OkRes()

        prompt_p = Path(self.temp.name) / "goal.txt"
        prompt_p.write_text("Check status\n")
        with patch.object(control.subprocess, "run", side_effect=fake_run):
            with tr.prompt_file(prompt_p):
                pass
        self.assertEqual(seen[0][0], "ssh")
        self.assertIn("HostKeyAlias=worker-a.example.invalid", seen[0])
        self.assertEqual(seen[1][0], "scp")
        self.assertIn("HostKeyAlias=worker-a.example.invalid", seen[1])


if __name__ == "__main__":
    unittest.main()
