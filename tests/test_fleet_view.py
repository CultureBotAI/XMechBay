"""Synthetic screen and table behavior for fleet session status; 2026-10-27."""

import importlib.util
from pathlib import Path
import unittest


SPEC = importlib.util.spec_from_file_location(
    "fleet_view", Path(__file__).parents[1] / ".agents/skills/fleet-session-status/scripts/fleet_view.py"
)
VIEW = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VIEW)


def session(screen, agent="codex", **extra):
    return {"agent": agent, "pid": 123, "tty": "ttys001", "cwd": "examples/SampleMech",
            "screen": screen, "target": {"transport": "tmux", "pane": "%1"}, **extra}


class ScreenEvidenceTests(unittest.TestCase):
    def test_codex_goal_model_timer_context_are_separate(self):
        result = VIEW.analyze_session(session("""› review records
• Completed batch 8: 24 records reviewed.
» Ask Codex to do anything
GPT-6-Astra ultra fast · examples/SampleMech · Pursuing goal (2d 3h 4m)
52% context left · ? for shortcuts
"""))
        self.assertEqual(result["status"], "Pursuing goal")
        self.assertEqual(result["timer"], {"kind": "goal", "text": "2d 3h 4m"})
        self.assertEqual(result["model"], "GPT-6-Astra ultra fast")
        self.assertEqual(result["model_compact"], "Astra U fast")
        self.assertEqual(result["context"]["remaining_percent"], 52)
        self.assertIn("24 records", result["activity"])
        self.assertEqual(result["activity_source"], "screen")

    def test_codex_placeholder_is_idle(self):
        result = VIEW.analyze_session(session("• Report complete.\n» Ask Codex to do anything\nGPT-6-Astra ultra · examples/sample\n"))
        self.assertEqual(result["status"], "idle")
        self.assertEqual(result["activity"], "Report complete.")

    def test_agency_empty_composer_overrules_historical_draft_and_error(self):
        result = VIEW.analyze_session(session("""> continue
There was a network issue connecting to the server.
Error ID: 11111111-1111-4111-8111-111111111111
> reply READY only
READY
────────────────
>
────────────────
? for shortcuts Gemini 4 Argon · high
""", "agy"))
        self.assertEqual(result["status"], "idle")
        self.assertEqual(result["activity"], "READY")
        self.assertNotIn("draft", result["flags"])
        self.assertNotIn("last-error", result["flags"])

    def test_subagent_approval_is_not_idle(self):
        result = VIEW.analyze_session(session("""┃ self needs approval for Bash
┃ command details
┃ ctrl+k approve · alt+j manage
────────────────
>
────────────────
● Agent(self: Reviewer) Blocked · 19s
? for shortcuts Gemini 4 Argon · high · 1 subagent(s)
""", "agy"))
        self.assertEqual(result["status"], "APPROVAL")
        self.assertIn("subagent-approval", result["flags"])

    def test_old_approval_words_in_answer_are_not_current_prompt(self):
        result = VIEW.analyze_session(session("""> what happened?
The previous self needs approval message was resolved.
The earlier Requesting permission prompt was answered.
>
? for shortcuts Gemini 4 Argon · high
""", "agy"))
        self.assertEqual(result["status"], "idle")

    def test_wrapped_draft_stays_unknown(self):
        result = VIEW.analyze_session(session("""────────────────
> please review
the next sample
────────────────
? for shortcuts Gemini 4 Argon · high
""", "agy"))
        self.assertEqual(result["status"], "UNKNOWN")

    def test_simple_draft_is_marked(self):
        result = VIEW.analyze_session(session("> do the next task\n? for shortcuts Gemini 4 Argon · high", "agy"))
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertIn("draft", result["flags"])

    def test_active_work_after_user_turn_has_activity(self):
        result = VIEW.analyze_session(session("""› inspect samples
• Running validation on the sample set.
Working (43s · esc to interrupt)
GPT-6-Astra high · examples/sample
"""))
        self.assertEqual(result["status"], "Working")
        self.assertIn("validation", result["activity"])
        self.assertEqual(result["timer"], {"kind": "turn", "text": "43s"})

    def test_historical_working_phrase_is_not_current_status_or_timer(self):
        result = VIEW.analyze_session(session("The previous run showed Working (12s).\n>\nGPT-Example high · ExampleMech"))
        self.assertEqual(result["status"], "idle")
        self.assertIsNone(result["timer"])

    def test_old_dedicated_working_line_before_composer_is_history(self):
        result = VIEW.analyze_session(session("Working (12s)\n>\nGPT-Example high · ExampleMech"))
        self.assertEqual(result["status"], "idle")
        self.assertIsNone(result["timer"])

    def test_current_interrupt_line_above_composer_is_retained(self):
        result = VIEW.analyze_session(session("• Checking sample consistency.\n• Working (43s · esc to interrupt)\n>\nGPT-Example high · ExampleMech"))
        self.assertEqual(result["status"], "Working")
        self.assertEqual(result["timer"], {"kind": "turn", "text": "43s"})
        self.assertEqual(result["activity"], "Checking sample consistency.")

    def test_human_prompt_precedes_goal(self):
        result = VIEW.analyze_session(session("""Would you like to run the following command?
1. Yes, allow once
2. No
GPT-6-Astra ultra · examples/sample · Pursuing goal (1h)
"""))
        self.assertEqual(result["status"], "APPROVAL")

    def test_signin_trust_and_update_screens(self):
        for screen, status in (("Sign in with ChatGPT\nChoose an option", "SIGN-IN"),
                               ("Do you trust the contents of this project?", "TRUST-PROMPT"),
                               ("Update available\nUpdate now\nSkip until next version", "UPDATE-PROMPT")):
            self.assertEqual(VIEW.analyze_session(session(screen))["status"], status)

    def test_claude_prompt_and_model(self):
        result = VIEW.analyze_session(session("""⏺ Finished checking examples.
>
[WORK] sample@example.invalid · Claude Opus 5.5
""", "claude"))
        self.assertEqual(result["status"], "idle")
        self.assertEqual(result["model"], "Claude Opus 5.5")
        self.assertNotIn("example.invalid", result["model"])

    def test_no_state_from_title_or_pid(self):
        result = VIEW.analyze_session(session("", title="agy ▸ python · Running"))
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertIsNone(result["activity"])
        self.assertIn("screen-unavailable", result["flags"])

    def test_agy_background_task_needs_screen_and_title_evidence(self):
        screen = "Running validation\n>\n? for shortcuts Gemini 4 Argon · high · 1 task(s)"
        working = VIEW.analyze_session(session(screen, "agy", title="agy ▸ python"))
        self.assertEqual(working["status"], "Working")
        self.assertIn("background-task", working["flags"])
        self.assertEqual(VIEW.analyze_session(session(screen, "agy", title="agy"))["status"], "UNKNOWN")

    def test_unknown_goal_wording_not_idle(self):
        result = VIEW.analyze_session(session(">\nGPT-6-Astra high · examples/sample · Goal pending scheduler"))
        self.assertEqual(result["status"], "UNKNOWN")

    def test_network_retry_is_visible_without_general_connectivity_claim(self):
        result = VIEW.analyze_session(session("Reconnecting... waiting for network\nWorking (4s · esc to interrupt)\nGPT-6-Astra high · examples/sample"))
        self.assertEqual(result["status"], "Working")
        self.assertIn("network-retry", result["flags"])

    def test_only_explicit_context_remaining_is_reported(self):
        result = VIEW.analyze_session(session("• 40% of the records reviewed\n>\nGPT-6-Astra high · examples/sample"))
        self.assertIsNone(result["context"])

    def test_private_paths_accounts_and_links_are_redacted(self):
        sample_path = "/" + "Users" + "/" + "sample-user/private/result.md"
        result = VIEW.analyze_session(session("• Saved " + sample_path + " for sample@example.invalid\n>\nGPT-6-Astra high · examples/sample"))
        self.assertNotIn(sample_path, result["activity"])
        self.assertNotIn("example.invalid", result["activity"])
        self.assertIn("[path]", result["activity"])

    def test_timer_ticks_do_not_change_evidence_but_progress_does(self):
        first = "• Batch 8 reviewed\n>\nGPT-6-Astra ultra · examples/sample · Pursuing goal (3h 1m)"
        a = VIEW.analyze_session(session(first))
        b = VIEW.analyze_session(session(first.replace("3h 1m", "3h 2m")))
        c = VIEW.analyze_session(session(first.replace("Batch 8", "Batch 9")))
        self.assertEqual(a["evidence_fingerprint"], b["evidence_fingerprint"])
        self.assertNotEqual(a["evidence_fingerprint"], c["evidence_fingerprint"])

    def test_standalone_research_model_footer(self):
        result = VIEW.analyze_session(session("• Review complete.\n>\nGPT-Example Research xhigh\nGoal achieved (2h 14m)"))
        self.assertEqual(result["model"], "GPT-Example Research xhigh")
        self.assertEqual(result["status"], "Goal achieved")
        self.assertEqual(result["timer"], {"kind": "goal", "text": "2h 14m"})

    def test_generic_model_footer_after_composer_has_no_release_allowlist(self):
        result = VIEW.analyze_session(session("• Review complete.\n>\nExampleModel-7 Research xhigh · ExampleMech Goal achieved (2h 14m)"))
        self.assertEqual(result["model"], "ExampleModel-7 Research xhigh")
        self.assertEqual(result["model_compact"], "ExampleModel-7 Research XH")
        self.assertEqual(result["status"], "Goal achieved")
        self.assertEqual(result["timer"], {"kind": "goal", "text": "2h 14m"})

    def test_generic_model_like_history_does_not_establish_footer(self):
        result = VIEW.analyze_session(session("ExampleModel-7 Research xhigh\nReport is ready.\n>"))
        self.assertIsNone(result["model"])
        self.assertEqual(result["status"], "UNKNOWN")

    def test_historical_goal_prose_before_empty_composer_is_not_current(self):
        result = VIEW.analyze_session(session("The previous Goal achieved claim was incorrect.\n>\nGPT-Example high · ExampleMech"))
        self.assertEqual(result["status"], "idle")
        self.assertIsNone(result["timer"])

    def test_old_dedicated_goal_before_latest_composer_is_history(self):
        result = VIEW.analyze_session(session("Goal achieved (2h)\nA follow-up is ready.\n>\nGPT-Example high · ExampleMech"))
        self.assertEqual(result["status"], "idle")

    def test_wrapped_goal_below_current_footer_is_retained(self):
        result = VIEW.analyze_session(session("• Reviewed the sample set.\n>\nGPT-Example high · ExampleMech\nPursuing goal (3h 20m)\n61% context left"))
        self.assertEqual(result["status"], "Pursuing goal")
        self.assertEqual(result["timer"], {"kind": "goal", "text": "3h 20m"})

    def test_goal_status_itself_may_wrap_at_footer_boundary(self):
        result = VIEW.analyze_session(session("• Report ready.\n>\nGPT-Example high · ExampleMech · Goal\nachieved (12m)"))
        self.assertEqual(result["status"], "Goal achieved")
        self.assertEqual(result["timer"], {"kind": "goal", "text": "12m"})

    def test_activity_skips_duration_artifact_and_command_hints(self):
        screen = """• Completed review of the sample set.
1 artifact · /artifact to review
Worked for 5h 2m • 8:00 PM
Commands: /goal
Usage: /goal objective
Tip: /subagents lists background agents
>
GPT-Example high · examples/sample
"""
        self.assertEqual(VIEW.analyze_session(session(screen))["activity"], "Completed review of the sample set.")

    def test_activity_skips_prefixed_ui_and_receipt_fragments(self):
        screen = """• Completed review of the sample set.
  └ {"outcome": {"path": "examples/receipt.json", "sha256": "example"}}
  + 2 lines (⌃t to expand)
  • Working (43s · esc to interrupt)
  └ Tip: Use /help to inspect commands
  • Commands: /goal
  └ 11111111-1111-4111-8111-111111111111
>
GPT-Example high · examples/sample
"""
        self.assertEqual(VIEW.analyze_session(session(screen))["activity"], "Completed review of the sample set.")

    def test_receipt_only_screen_has_no_activity_claim(self):
        result = VIEW.analyze_session(session('└ {"path": "examples/receipt.json"}\n"approved": false,\n11111111-1111-4111-8111-111111111111\n>\nGPT-Example high · examples/sample'))
        self.assertIsNone(result["activity"])

    def test_goal_detail_panel_does_not_replace_reported_result(self):
        screen = """• Validated 18 records and opened 2 PRs.
Goal
Status: complete
Objective: Review the example collection and create a report.
Time used: 1h 20m
Tokens used: 20000
Commands: /goal edit, /goal clear
>
GPT-Example high · examples/sample · Goal achieved (1h 20m)
"""
        result = VIEW.analyze_session(session(screen))
        self.assertEqual(result["activity"], "18 records · 2 PRs")

    def test_wrapped_metrics_remain_reported_and_compact(self):
        screen = """› stats
• 8 Mechs, 40 PRs (38 merged, 2 open), 12 completed goals, 7 completed
cycles
Tracker flags 2 sessions requiring intervention.
The dashboard needs a refresh.
Worked for 3h 1m • 8:00 PM
>
GPT-Example high · examples/sample
"""
        result = VIEW.analyze_session(session(screen))
        self.assertEqual(result["activity"], "8 Mechs · 40 PRs · 12 completed goals · 7 completed cycles")
        self.assertEqual(result["activity_source"], "screen")
        self.assertLessEqual(len(result["activity"]), 80)

    def test_fragments_are_skipped_and_filenames_do_not_become_counters(self):
        screen = """• Wrote a receipt for the current review.
false.
\ufffajson.
review-batch-200.json
>
GPT-Example high · examples/sample
"""
        result = VIEW.analyze_session(session(screen))
        self.assertEqual(result["activity"], "Wrote a receipt for the current review.")
        self.assertNotIn("200", result["activity"])

    def test_known_slash_commands_are_not_file_paths(self):
        self.assertEqual(VIEW.private_text("Use /resume to recover a session."), "Use /resume to recover a session.")


class CompactTableTests(unittest.TestCase):
    def report(self, **extra):
        row = session("• Batch 8 reviewed\n>\nGPT-6-Astra ultra · examples/sample")
        row.update(VIEW.analyze_session(row))
        row.update({"age_seconds": 7200, "delta": {"kind": "unchanged", "seconds": 120},
                    "listeners": {"count": None, "status": "unknown", "stale": False},
                    "session_id": "11111111-1111-4111-8111-111111111111"})
        return {"label": "Laptop A", "observed_at": "2026-01-01T12:00:00+00:00", "reachable": True,
                "source": "ssh", "sessions": [row], "coverage": {"screens_read": 1, "sessions_seen": 1},
                "errors": [], "health": {"status": "ok", "occupancy_ratio": .12, "age_seconds": 5,
                                          "process_status": "ok"}, **extra}

    def test_compact_table_keeps_provenance_and_missing_socket_count(self):
        text = VIEW.render_host(self.report())
        self.assertIn("~Batch 8 reviewed", text)
        self.assertIn("launch#11111111", text)
        self.assertIn("reconfirm the current session ID before control", text)
        self.assertIn("2h · =2m", text)
        self.assertIn("TCP 12.0% ok", text)
        self.assertIn("health age 5s", text)
        self.assertIn("screens 1/1", text)
        self.assertIn("| — |", text)

    def test_duplicate_sessions_remain_and_claw_sorts_first(self):
        report = self.report()
        duplicate = dict(report["sessions"][0], target={"transport": "terminal", "window_id": 51, "tab": 2})
        claw = dict(duplicate, repo_label="sample-claw", target={"transport": "terminal", "window_id": 15, "tab": 1})
        report["sessions"].extend([duplicate, claw])
        text = VIEW.render_host(report)
        self.assertEqual(text.count("~Batch 8 reviewed"), 3)
        self.assertLess(text.index("sample-claw"), text.index("SampleMech"))
        self.assertIn("T51/2", text)

    def test_unreachable_host_does_not_claim_zero_sessions(self):
        text = VIEW.render_host(self.report(reachable=False, sessions=[]))
        self.assertIn("UNREACHABLE", text)
        self.assertIn("unknown", text)
        self.assertNotIn("0 sessions", text)
        self.assertNotIn("No agent sessions", text)

    def test_offline_and_stale_snapshot_are_explicit(self):
        text = VIEW.render_host(self.report(source="snapshot", snapshot_stale=True))
        self.assertIn("OFFLINE snapshot", text)
        self.assertIn("STALE, last observed", text)
        self.assertNotIn("SSH observed", text)

    def test_partial_coverage_and_cache_failure_are_visible(self):
        text = VIEW.render_host(self.report(errors=["terminal_probe_failed"], cache_error="cache_unreadable"))
        self.assertIn("collection partial (1 errors)", text)
        self.assertIn("change history unavailable", text)

    def test_markdown_and_html_are_escaped(self):
        report = self.report(label="Laptop | <tag>")
        report["sessions"][0]["repo_label"] = "sample|[link](https://example.invalid)"
        report["sessions"][0]["activity"] = "<script>bad</script> | **claim**"
        text = VIEW.render_host(report)
        self.assertIn("Laptop \\| &lt;tag&gt;", text)
        self.assertNotIn("<script>", text)
        self.assertIn("\\*\\*claim\\*\\*", text)

    def test_stale_health_and_process_warning_are_distinct(self):
        report = self.report(health={"status": "stale", "occupancy_ratio": .2, "age_seconds": 400, "process_status": "critical"})
        report["sessions"][0]["listeners"] = {"count": None, "status": "unknown", "stale": True}
        text = VIEW.render_host(report)
        self.assertIn("TCP 20.0% stale", text)
        self.assertIn("process maintenance critical", text)
        self.assertIn("— stale", text)

    def test_invalid_durations_do_not_crash_or_invent_age(self):
        for value in (None, True, -1, float("nan"), float("inf"), "4m"):
            self.assertEqual(VIEW.duration(value), "?")

    def test_first_observation_does_not_claim_new_session(self):
        report = self.report()
        report["sessions"][0]["delta"] = {"kind": "first", "seconds": None}
        text = VIEW.render_host(report)
        self.assertIn("2h · first", text)
        self.assertNotIn("· new", text)
        report["sessions"][0]["delta"] = {"kind": "unavailable", "seconds": None}
        self.assertIn("2h · Δ?", VIEW.render_host(report))

    def test_unavailable_process_inventory_never_claims_zero_sessions(self):
        report = self.report(sessions=[], coverage={"processes": "unknown", "screens_read": 0, "sessions_seen": 0},
                             errors=["process_probe_failed"])
        text = VIEW.render_host(report)
        self.assertIn("count unknown (0 observed)", text)
        self.assertIn("Process inventory unavailable", text)
        self.assertNotIn("0 sessions", text)
        self.assertIn("process_probe_failed", text)

    def test_partial_inventory_has_observed_count(self):
        text = VIEW.render_host(self.report(coverage={"processes": "partial", "screens_read": 1, "sessions_seen": 1}))
        self.assertIn("1 observed; inventory partial", text)

    def test_partial_agent_discovery_never_claims_exact_count(self):
        report = self.report(coverage={"processes": "complete", "agent_discovery": "partial"},
                             errors=["agent_discovery_partial"])
        text = VIEW.render_host(report)
        self.assertIn("1 observed; inventory partial", text)
        self.assertNotIn("1 sessions", text)
        self.assertIn("`agent_discovery_partial`", text)
        self.assertNotIn("`collection_error`", text)

    def test_exact_count_requires_complete_processes_and_agent_discovery(self):
        report = self.report(coverage={"processes": "complete", "agent_discovery": "complete"})
        self.assertIn("1 sessions", VIEW.render_host(report))
        report["coverage"]["agent_discovery"] = "unknown"
        self.assertIn("count unknown (1 observed)", VIEW.render_host(report))

    def test_unknown_process_inventory_remains_unknown_with_partial_discovery(self):
        report = self.report(sessions=[], coverage={"processes": "unknown", "agent_discovery": "partial"})
        text = VIEW.render_host(report)
        self.assertIn("count unknown (0 observed)", text)
        self.assertIn("Process inventory unavailable", text)

    def test_optional_tmux_absence_is_not_failed_collection(self):
        report = self.report(coverage={"processes": "complete", "screens_read": 1, "sessions_seen": 1,
                                      "terminal": "complete", "tmux": "unavailable"},
                             errors=["tmux_executable_unavailable"])
        text = VIEW.render_host(report)
        self.assertIn("terminal complete, tmux n/a", text)
        self.assertNotIn("collection partial", text)
        self.assertNotIn("Collection codes", text)

    def test_state_counts_and_attention_list(self):
        report = self.report()
        report["sessions"][0]["status"] = "APPROVAL"
        report["sessions"].append(dict(report["sessions"][0], repo_label="OtherMech", status="UNKNOWN"))
        text = VIEW.render_host(report)
        self.assertIn("✋1 ?1", text)
        self.assertIn("Attention:", text)
        self.assertIn("OtherMech", text)

    def test_unreachable_error_codes_are_bounded_and_not_raw_text(self):
        text = VIEW.render_host(self.report(reachable=False, errors=["ssh_probe_failed", "raw private stderr text"]))
        self.assertIn("ssh_probe_failed", text)
        self.assertIn("collection_error", text)
        self.assertNotIn("raw private", text)


if __name__ == "__main__":
    unittest.main()
