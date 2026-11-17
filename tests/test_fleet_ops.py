"""Offline tests for the fleet-ops skill (synthetic fixtures only; no SSH, GitHub or sessions)."""
import contextlib
import importlib.util
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".agents/skills/fleet-ops/scripts"
EXAMPLES = ROOT / "examples"


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


OPS = load("fleet_ops")
PROBE = load("host_resources_probe")


def config(**extra):
    """The example config with overrides, written to a temporary directory (never into examples/)."""
    base = json.loads((EXAMPLES / "fleet-ops.json").read_text())
    base["fleet_status_config"] = str(EXAMPLES / "fleet-hosts.json")
    base.update(extra)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "fleet-ops.json"
        path.write_text(json.dumps(base))
        return OPS.load_config(path)


def session(host, role, project, status, repo=None, target="T1/1", tmux=None, window=None, **extra):
    row = {"host_key": host, "role": role, "project_label": project, "repo_label": repo or project,
           "status": status, "target_short": target, "tmux_session": tmux, "tmux_window": window or (project if tmux else None),
           "tty": "ttys001", "pid": 1, "agent": "codex"}
    row.update(extra)
    return row


def example_payload():
    sys.path.insert(0, str(OPS.STATUS_SCRIPTS))
    import fleet_dashboard
    reports, meta = fleet_dashboard.collect_dashboard_reports(snapshot_path=EXAMPLES / "fleet-snapshot.json")
    return fleet_dashboard.build_dashboard_payload(reports, mode=meta["mode"], role_overrides=meta["role_overrides"])


def healthy_host(**changes):
    host = json.loads((EXAMPLES / "fleet-ops-resources.json").read_text())["workstation-local"]
    host.update(changes)
    return host


class ProbeTests(unittest.TestCase):
    def test_df_and_swap(self):
        df = "Filesystem 1024-blocks Used Available Capacity\n/dev/disk3s5 1948404040 1864240152 23848520 99% /\n"
        self.assertEqual(PROBE.parse_df(df), {"free_gib": 22.7, "total_gib": 1858.1, "used_pct": 95.7})
        self.assertEqual(PROBE.parse_df(None)["free_gib"], None)
        swap = PROBE.parse_swap("total = 32768.00M  used = 31413.25M  free = 1354.75M  (encrypted)")
        self.assertEqual(swap, {"total_gib": 32.0, "used_gib": 30.68})
        self.assertEqual(PROBE.parse_swap(""), {"total_gib": None, "used_gib": None})

    def test_claude_retention_reports_only_existing_config_dirs(self):
        with tempfile.TemporaryDirectory() as home:
            (Path(home) / ".claude").mkdir()
            (Path(home) / ".claude/settings.json").write_text(json.dumps({"cleanupPeriodDays": 3650}))
            with mock.patch.object(PROBE, "HOME", home):
                self.assertEqual(PROBE.claude_retention(), {".claude": 3650})

    def test_guard_state_counts_failed_rescues(self):
        with tempfile.TemporaryDirectory() as home:
            state = Path(home) / "Library/Application Support/XMechBay/tmp-state-guard/state"
            state.mkdir(parents=True)
            (state / "latest.json").write_text(json.dumps({"status": "unrescued", "exit_code": 20, "entries": [
                {"rescue": {"status": "failed"}}, {"rescue": {"status": "skipped", "reason": "entry-cap"}}, {"rescue": {"status": "rescued"}}]}))
            with mock.patch.object(PROBE, "HOME", home):
                guard = PROBE.guard_state()
            self.assertEqual((guard["failed"], guard["skipped"], guard["at_risk"]), (1, {"entry-cap": 1}, 3))


class ManifestTests(unittest.TestCase):
    def test_manifest_repos_reads_only_the_mechs_section(self):
        text = ("version: 1\ncapability_catalogue:\n  github:\n    display_name: NotAMech\n"
                "mechs:\n  alpha:\n    display_name: AlphaMech\n    github: example-org/AlphaMech\n"
                "    capabilities:\n      x:\n        github: ignored/deeper\n"
                "  beta:\n    github: example-org/BetaMech\n    display_name: BetaMech\n"
                "vendored: {}\n")
        self.assertEqual(OPS.manifest_repos(text), {"AlphaMech": "example-org/AlphaMech"})


class CollectionTests(unittest.TestCase):
    def test_ssh_probe_words_are_shell_quoted(self):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return types.SimpleNamespace(returncode=0, stdout="{}", stderr="")

        watch = ["~/Données/x y", "~/$(touch pwned)/`id`"]
        with mock.patch.object(OPS.subprocess, "run", fake_run):
            OPS.probe_host("h", {"host": "worker.example.invalid", "python": "/usr/bin/python3"}, watch)
        remote = calls[0][-1]
        self.assertEqual(shlex.split(remote), ["/usr/bin/python3", "-", "--watch", watch[0], "--watch", watch[1]])
        self.assertIn("'~/$(touch pwned)/`id`'", remote)  # single-quoted: never expanded remotely
        self.assertIn("Données", remote)
        self.assertEqual(calls[0][-3:-1], ["--", "worker.example.invalid"])

    def test_non_json_probe_output_becomes_a_host_error(self):
        with mock.patch.object(OPS.subprocess, "run", lambda *a, **k: types.SimpleNamespace(returncode=0, stdout="Welcome!\n{", stderr="")):
            result = OPS.probe_host("h", {"transport": "local"}, [])
        self.assertIn("not JSON", result["error"])

    def test_each_failing_repository_keeps_its_own_error(self):
        def fake_graphql(chunk):
            if len(chunk) > 1:
                return {}, "batch error"
            return {}, f"error for {chunk[0][1]}"

        with mock.patch.object(OPS, "graphql", fake_graphql):
            out = OPS.collect_issues({"A": "o/a", "B": "o/b"}, batch=2)
        self.assertEqual(out["A"]["error"], "error for o/a")
        self.assertEqual(out["B"]["error"], "error for o/b")


class OwnershipTests(unittest.TestCase):
    def test_explicit_then_list_then_busiest_host(self):
        cfg = config(claws={"a": {"host": "h1", "mechs": ["*"]}, "b": {"host": "h2", "mechs": ["ListedMech"]}},
                     mech_owner={"PinnedMech": "b"})
        rows = [session("h1", "mech", "LoopMech", "idle"), session("h2", "mech", "LoopMech", "Goal stalled"),
                session("h1", "mech", "IdleMech", "idle")]
        by = OPS.mech_sessions(rows)
        self.assertEqual(OPS.claw_for_mech("PinnedMech", by, cfg), "b")
        self.assertEqual(OPS.claw_for_mech("ListedMech", by, cfg), "b")
        self.assertEqual(OPS.claw_for_mech("IdleMech", by, cfg), "a")
        self.assertEqual(OPS.claw_for_mech("LoopMech", by, cfg), "a")
        self.assertIsNone(OPS.claw_for_mech("Nowhere", by, config(claws={})))

    def test_unknown_owner_is_reported_and_ignored(self):
        cfg = config(claws={"a": {"host": "h1", "mechs": ["*"]}}, mech_owner={"M": "typo"})
        payload = {"sessions": [session("h1", "claw", "claw", "idle", repo="claw"), session("h1", "mech", "M", "idle")]}
        findings = OPS.analyze(payload, {}, {}, cfg)
        self.assertTrue(any("unknown claw 'typo'" in f["title"] for f in findings))
        self.assertEqual(OPS.claw_for_mech("M", OPS.mech_sessions(payload["sessions"]), cfg), "a")

    def test_a_live_catch_all_claw_is_preferred(self):
        cfg = config(claws={"dead": {"host": "h1", "mechs": ["*"], "repo": "claw"}, "live": {"host": "h1", "mechs": ["*"], "repo": "claw2"}})
        sessions = [session("h1", "claw", "claw2", "idle", repo="claw2"), session("h1", "mech", "M", "idle")]
        by = OPS.mech_sessions(sessions)
        self.assertEqual(OPS.claw_for_mech("M", by, cfg), "dead")
        self.assertEqual(OPS.claw_for_mech("M", by, cfg, OPS.live_claws(sessions, cfg)), "live")


class RuleTests(unittest.TestCase):
    def test_example_findings_cover_every_rule_family(self):
        payload = example_payload()
        resources = json.loads((EXAMPLES / "fleet-ops-resources.json").read_text())
        issues = json.loads((EXAMPLES / "fleet-ops-issues.json").read_text())
        findings = OPS.analyze(payload, resources, issues, config())
        titles = {f["title"]: f for f in findings}

        def one(fragment):
            hits = [f for t, f in titles.items() if fragment in t]
            self.assertTrue(hits, fragment)
            return hits[0]

        self.assertEqual(one("laptop-a: 31.0 GiB free")["severity"], "critical")
        self.assertEqual(one("left at-risk work unrescued (skipped 12")["severity"], "critical")
        self.assertEqual(one("claw claw-b has no live session")["owner"], {"kind": "user", "key": "claws"})
        self.assertEqual(one("waits for a human (APPROVAL)")["owner"]["kind"], "user")
        self.assertEqual(one("laptop-b: resource probe failed")["category"], "observation")
        self.assertEqual(one("swap 30.5 of 32.0")["owner"]["kind"], "xmechbay")
        self.assertEqual(one("221.0 GiB in a watched path")["owner"], {"kind": "claw", "key": "claw-a"})
        self.assertEqual(one("PRs with failing checks")["evidence"]["prs"], [102])
        self.assertEqual(one("PRs mergeable with green checks")["owner"], {"kind": "user", "key": "merges"})
        self.assertEqual(one("draft PRs untouched")["evidence"]["prs"], [90])
        self.assertIn("default 30", one("deletes old transcripts")["title"])
        self.assertIn("TraitMech", one("fleet Mechs have no session")["evidence"]["mechs"])
        self.assertFalse(any(f["mech"] == "DetachedMech" and "no session is working" in f["title"] for f in findings))  # UNKNOWN
        self.assertEqual([f["id"] for f in findings], [f"F{i:03d}" for i in range(1, len(findings) + 1)])
        severities = [OPS.SEVERITY_ORDER[f["severity"]] for f in findings]
        self.assertEqual(severities, sorted(severities))

    def test_a_flagged_mech_is_not_also_reported_as_not_working(self):
        cfg = config(claws={"a": {"host": "h1", "mechs": ["*"]}})
        payload = {"sessions": [session("h1", "claw", "claw", "idle", repo="claw"), session("h1", "mech", "M", "Goal stalled")],
                   "canonical_mechs": ["M"]}
        titles = [f["title"] for f in OPS.analyze(payload, {}, {}, cfg)]
        self.assertIn("M on h1: goal stalled", titles)
        self.assertFalse(any("no session is working" in t for t in titles))

    def test_prompts_on_claw_sessions_reach_the_user(self):
        cfg = config(claws={"a": {"host": "h1", "mechs": ["*"]}})
        payload = {"sessions": [session("h1", "claw", "claw", "APPROVAL", repo="claw")]}
        hits = [f for f in OPS.analyze(payload, {}, {}, cfg) if "waits for a human (APPROVAL)" in f["title"]]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["owner"], {"kind": "user", "key": "prompt"})

    def test_healthy_host_yields_no_resource_findings(self):
        self.assertEqual(OPS.resource_findings({"ok": healthy_host()}, config(), {}), [])

    def test_unmeasured_sizes_are_unobserved_not_zero(self):
        host = healthy_host(tmp_roots=[{"path": "/private/tmp", "gib": None, "error": "timeout", "children": []}],
                            watched=[{"path": "~/work/BigMech/.temp", "exists": True, "gib": None, "error": "timeout"}],
                            codex_sessions={"path": "~/.codex/sessions", "exists": True, "gib": None, "error": "timeout"})
        titles = [f["title"] for f in OPS.resource_findings({"h": host}, config(), {})]
        self.assertEqual(sum("unknown (timeout)" in t for t in titles), 3)

    def test_guard_failure_states(self):
        cases = {
            "failed": dict(status="rescued", exit_code=10, skipped={}, failed=2),
            "unrescued": dict(status="unrescued", exit_code=20, skipped={}, failed=0),
            "incomplete": dict(status="incomplete", exit_code=30, skipped={}, failed=0, unknown=4),
            "no report": {},
            "stale": dict(status="ok", exit_code=0, skipped={}, failed=0, finished_at="2020-01-01T00:00:00"),  # no zone
        }
        expected = {"failed": "unrescued (skipped 0, failed 2)", "unrescued": "unrescued", "incomplete": "run incomplete (4 unknown",
                    "no report": "installed but has no report", "stale": "report is"}
        for name, guard in cases.items():
            guard = {"installed": True, "finished_at": "2099-01-01T00:00:00+00:00", **guard}
            titles = [f["title"] for f in OPS.resource_findings({"h": healthy_host(guard=guard)}, config(), {})]
            self.assertTrue(any(expected[name] in t for t in titles), (name, titles))

    def test_watched_path_routes_by_component_not_substring(self):
        cfg = config(claws={"a": {"host": "h", "mechs": ["*"]}}, mech_owner={"TaxonMech": "a"})
        host = healthy_host(watched=[{"path": "~/work/NotTaxonMech/.temp", "exists": True, "gib": 500.0},
                                     {"path": "~/work/TaxonMech/.temp", "exists": True, "gib": 500.0}])
        findings = [f for f in OPS.resource_findings({"h": host}, cfg, {}, mechs={"TaxonMech"}) if "watched" in f["title"]]
        owners = sorted((f["evidence"]["path"], f["owner"]["kind"]) for f in findings)
        self.assertEqual(owners, [("~/work/NotTaxonMech/.temp", "xmechbay"), ("~/work/TaxonMech/.temp", "claw")])

    def test_only_checked_and_unblocked_prs_count_as_green(self):
        prs = [{"number": 1, "draft": False, "mergeable": "MERGEABLE", "review": None, "checks": "SUCCESS", "updated_at": "2099-01-01T00:00:00Z"},
               {"number": 2, "draft": False, "mergeable": "MERGEABLE", "review": None, "checks": None, "updated_at": "2099-01-01T00:00:00Z"},
               {"number": 3, "draft": False, "mergeable": "MERGEABLE", "review": "CHANGES_REQUESTED", "checks": "SUCCESS", "updated_at": "2099-01-01T00:00:00Z"}]
        findings = OPS.backlog_findings({"M": {"repo": "o/m", "open_prs": 3, "open_issues": 0, "prs": prs}}, config(), {})
        green = [f for f in findings if "green" in f["title"]][0]
        self.assertEqual((green["evidence"]["prs"], green["evidence"]["mergeable_without_checks"]), ([1], [2]))


class TargetTests(unittest.TestCase):
    def test_hint_forms_and_unmatched_hint(self):
        target = OPS.claw_targets({"sessions": [session("h", "claw", "c", "idle", target="T11/1", tty="ttys009")]}, {"host": "h"})[0]
        for hint in ("11", "T11", "T11/1", "ttys009", "/dev/ttys009"):
            self.assertTrue(OPS.hint_matches(hint, target), hint)
        self.assertFalse(OPS.hint_matches("1", target))
        chosen, reason = OPS.choose_target([target], {"target_hint": "12"}, "fleet")
        self.assertIsNone(chosen)
        self.assertIn("matches 0 of 1", reason)

    def test_non_front_tabs_foreign_tmux_and_process_rows_are_not_chosen(self):
        rows = [session("h", "claw", "c", "idle", target="T11/2"), session("h", "claw", "c", "idle", target="P123"),
                session("h", "claw", "c", "idle", target="%3", tmux="mechs", window="claw"),
                session("h", "claw", "c", "idle", target="T12/1", stale_cached=True)]
        targets = OPS.claw_targets({"sessions": rows}, {"host": "h"})
        self.assertEqual([t["target"] for t in targets], ["T11/2", "%3"])
        self.assertIsNone(OPS.choose_target([targets[0]], {}, "fleet")[0])
        self.assertIn("tab 2", OPS.choose_target([targets[0]], {}, "fleet")[1])
        self.assertIsNone(OPS.choose_target([targets[1]], {}, "fleet")[0])
        self.assertEqual(OPS.choose_target([targets[1]], {}, "mechs")[0]["tmux_window"], "claw")

    def test_two_claws_on_one_host_are_never_guessed(self):
        payload = {"sessions": [session("h1", "claw", "claw", "idle", target="T1/1"), session("h1", "claw", "claw", "idle", target="T2/1")]}
        targets = OPS.claw_targets(payload, {"host": "h1", "repo": "claw"})
        chosen, reason = OPS.choose_target(targets, {}, "fleet")
        self.assertIsNone(chosen)
        self.assertIn("2 live claw sessions", reason)

    def test_goal_text_limit_is_enforced(self):
        with self.assertRaises(ValueError):
            OPS.render_goal("r", "x" * 1000, "y")


class PlanTests(unittest.TestCase):
    def test_observe_and_plan_offline(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "fleet-ops.json"
            data = json.loads((EXAMPLES / "fleet-ops.json").read_text())
            data["fleet_status_config"] = str(EXAMPLES / "fleet-hosts.json")
            cfg_path.write_text(json.dumps(data))
            run = Path(tmp) / "run-1"
            argv = ["--config", str(cfg_path), "observe", "--snapshot", str(EXAMPLES / "fleet-snapshot.json"),
                    "--resources-json", str(EXAMPLES / "fleet-ops-resources.json"),
                    "--issues-json", str(EXAMPLES / "fleet-ops-issues.json"), "--out", str(run)]
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(OPS.main(argv), 0)
                self.assertEqual(OPS.main(["--config", str(cfg_path), "plan", str(run)]), 0)
            for name in ("sessions.json", "resources.json", "issues.json", "findings.json", "REPORT.md"):
                self.assertTrue((run / name).stat().st_size > 0, name)
            plan = run / "plan"
            goal = (plan / "goals/claw-a.txt").read_text()
            self.assertLess(len(goal), OPS.GOAL_LIMIT)
            self.assertNotIn("@", goal + (plan / "briefs/claw-a.md").read_text())
            self.assertIn("~/agent-work/claw/fleet-ops/run-1/brief.md", goal)
            dispatch = (plan / "DISPATCH.md").read_text()
            goal_path = str((plan / "goals/claw-a.txt").resolve())
            self.assertIn(f"XMECHBAY_TMUX_SESSION=fleet bash {(ROOT / '.agents/skills/mac-orchestration/scripts/mac-term.sh').resolve()} "
                          f"-H worker-a.example.invalid tmux-paste sample-claw {goal_path}", dispatch)
            self.assertIn(str((plan / "briefs/claw-a.md").resolve()), dispatch)  # absolute paths: runs from any directory
            self.assertIn("Nothing here has been sent", dispatch)
            summary = json.loads((plan / "summary.json").read_text())
            self.assertTrue(summary["claws"]["claw-a"]["target_resolved"])
            # claw-b's only Mech is UNKNOWN (no claw finding), so it gets no brief; its absence is a user finding instead
            self.assertNotIn("claw-b", summary["claws"])
            self.assertIn("claw claw-b has no live session", (plan / "DECISIONS.md").read_text())
            self.assertEqual(sorted(p.name for p in EXAMPLES.iterdir() if p.suffix == ".json" and p.name.startswith("tmp")), [])

    def test_terminal_dispatch_checks_the_selected_tab_tty(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "run-2"
            run.mkdir()
            cfg = config(claws={"c": {"host": "laptop-b", "repo": "claw", "mechs": ["*"]}})
            payload = {"sessions": [session("laptop-b", "claw", "claw", "idle", repo="claw", target="T7/1", tty="ttys044"),
                                    session("laptop-b", "mech", "M", "Goal stalled")]}
            (run / "sessions.json").write_text(json.dumps(payload))
            (run / "findings.json").write_text(json.dumps(OPS.analyze(payload, {}, {}, cfg)))
            OPS.plan(run, cfg)
            dispatch = (run / "plan/DISPATCH.md").read_text()
            self.assertIn("# Proceed only if this line shows tty ttys044", dispatch)
            self.assertIn("-H worker-b.example.invalid windows | grep '|7|'", dispatch)
            self.assertIn("-H worker-b.example.invalid paste 7 ", dispatch)


class ObservationAndDraftTests(unittest.TestCase):
    def test_host_observation_blind_clusters_and_port_watch_health(self):
        cfg = config(claws={})
        payload = {
            "hosts": [
                {"key": "h-down", "reachable": False, "errors": ["ssh_probe_timed_out"], "last_reachable": None},
                {"key": "h-partial", "reachable": True, "inventory_state": "partial",
                 "errors": ["agent_discovery_partial", "tmux_probe_failed"], "screens_text": "1/4",
                 "health": {"status": "unknown", "process_status": "unknown", "occupancy_pct": None}},
                {"key": "h-ports", "reachable": True, "inventory_state": "complete", "errors": [], "screens_text": "2/2",
                 "health": {"status": "warning", "process_status": "critical", "occupancy_pct": 42.5}},
            ],
            "sessions": [
                session("h-partial", "mech", "AlphaMech", "UNKNOWN", target="P101", transport="process"),
                session("h-partial", "mech", "BetaMech", "UNKNOWN", target="P102", transport="process"),
                session("h-down", "mech", "GhostMech", "UNKNOWN", target="P999", transport="process", stale_cached=True),
            ],
        }
        findings = OPS.analyze(payload, {}, {}, cfg)
        by_title = {f["title"]: f for f in findings}
        self.assertEqual(by_title["h-down: session collection unreachable"]["severity"], "critical")
        self.assertIn("agent_discovery_partial, tmux_probe_failed",
                      [t for t in by_title if t.startswith("h-partial: session collection partial")][0])
        self.assertEqual(by_title["h-partial: port-watch health report missing or unknown"]["owner"]["kind"], "xmechbay")
        self.assertEqual(by_title["h-partial: 2 agent sessions have no readable screen"]["evidence"]["count"], 2)
        self.assertFalse(any("h-down:" in t and "no readable screen" in t for t in by_title))
        self.assertEqual(by_title["h-ports: port-watch health critical (TCP 42.5%, process critical)"]["severity"], "critical")

    def test_other_role_prompts_stalls_sprawl_and_composer_draft_guard(self):
        cfg = config(claws={"c": {"host": "h1", "repo": "claw", "mechs": ["*"]}},
                     thresholds={"non_working_sessions_warn": 3, "achieved_goal_age_hours": 1})
        rows = [
            session("h1", "claw", "claw", "UNKNOWN", repo="claw", target="T10/1", human_block_kind="draft", flags=["draft"]),
            session("h1", "other", "sys_maint", "APPROVAL", target="T11/1"),
            session("h1", "other", "ext_repo", "Goal stalled", target="T12/1"),
            session("h1", "mech", "AlphaMech", "Goal achieved", target="%1", tmux="fleet", up_seconds=7200, wait_seconds=7200),
            session("h1", "mech", "BetaMech", "Goal achieved", target="%2", tmux="fleet", up_seconds=7200,
                    delta={"kind": "unchanged", "seconds": 5400}),
            session("h2", "mech", "AlphaMech", "idle", target="T20/1"),
            session("h2", "mech", "GammaMech", "Goal achieved", target="T21/1", up_seconds=86400, timer_text="◎ 14h", wait_seconds=15),
            session("h2", "mech", "DeltaMech", "Goal achieved", target="T22/1", up_seconds=86400, timer_text="◎ 9h", wait_seconds=-1),
        ]
        payload = {"sessions": rows}
        findings = OPS.analyze(payload, {}, {}, cfg)
        titles = [f["title"] for f in findings]
        self.assertIn("other session sys_maint on h1 waits for a human (APPROVAL)", titles)
        self.assertIn("non-Mech session ext_repo on h1: goal stalled", titles)
        self.assertIn("claw session claw on h1 has an unsent composer draft (T10/1)", titles)
        self.assertIn("h1: 5 non-working agent sessions across all roles", titles)
        self.assertIn("h1: 2 sessions parked at Goal achieved for 1h+", titles)
        self.assertNotIn("h2: 2 sessions parked at Goal achieved for 1h+", titles)
        self.assertIn("1 Mechs have live sessions on multiple hosts", titles)

        targets = OPS.claw_targets(payload, cfg["claws"]["c"])
        chosen, reason = OPS.choose_target(targets, cfg["claws"]["c"], "fleet")
        self.assertIsNone(chosen)
        self.assertIn("unsent composer draft", reason)


class RecommendTests(unittest.TestCase):
    def test_recommend_deduplicates_tracked_issues_and_sanitizes_new_drafts(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cfg_path = tmp_path / "fleet-ops.json"
            cfg_data = json.loads((EXAMPLES / "fleet-ops.json").read_text())
            cfg_data["fleet_status_config"] = str(EXAMPLES / "fleet-hosts.json")
            cfg_data["infra_repos"] = {"xmechbay": "example-org/XMechBay", "claw": "example-org/claw"}
            cfg_path.write_text(json.dumps(cfg_data))

            run = tmp_path / "run-rec"
            run.mkdir()
            payload = {
                "hosts": [
                    {"key": "host-a", "reachable": True, "inventory_state": "complete",
                     "errors": ["tmux_probe_failed"], "screens_text": "2/2",
                     "health": {"status": "unknown", "process_status": "unknown", "occupancy_pct": None}},
                    {"key": "host-b", "reachable": True, "inventory_state": "partial",
                     "errors": ["agent_discovery_partial", "terminal_window_unreadable", "tmux_probe_failed"],
                     "screens_text": "1/7",
                     "health": {"status": "ok", "process_status": "ok", "occupancy_pct": 1.2}},
                ],
                "sessions": [
                    session("host-a", "claw", "sample-claw", "UNKNOWN", repo="sample-claw", target="T1/1", human_block_kind="draft"),
                    session("host-a", "mech", "SampleMech", "Goal stalled", target="%1", tmux="fleet"),
                    session("host-b", "mech", "SampleMech", "UNKNOWN", target="P10", transport="process"),
                    session("host-b", "mech", "OtherMech", "UNKNOWN", target="P11", transport="process"),
                    session("host-b", "mech", "ThirdMech", "UNKNOWN", target="P12", transport="process"),
                    session("host-b", "mech", "FourthMech", "UNKNOWN", target="P13", transport="process"),
                    session("host-b", "mech", "FifthMech", "UNKNOWN", target="P14", transport="process"),
                ],
            }
            resources = {
                "host-a": healthy_host(swap={"total_gib": 24.0, "used_gib": 22.8},
                                       claude_cleanup_period_days={".claude": "absent"}),
                "host-b": healthy_host(swap={"total_gib": 32.0, "used_gib": 30.1},
                                       codex_sessions={"path": "~/.codex/sessions", "exists": True, "gib": 80.0}),
            }
            prs = [{"number": n, "title": f"PR {n}", "draft": True, "updated_at": "2026-01-01T00:00:00Z",
                    "mergeable": "UNKNOWN", "review": None, "checks": "FAILURE"} for n in range(1, 11)]
            issues = {"BrokenMech": {"repo": "example-org/BrokenMech", "open_issues": 5, "open_prs": 10, "prs": prs}}
            (run / "sessions.json").write_text(json.dumps(payload))
            (run / "resources.json").write_text(json.dumps(resources))
            (run / "issues.json").write_text(json.dumps(issues))

            open_issues = {
                "example-org/XMechBay": [
                    {"number": 116, "title": "Maintained, verified archiving of closed Codex session logs", "url": "https://example.invalid/116"},
                    {"number": 117, "title": "Fleet memory: one primary loop per Mech, retire duplicate idle sessions", "url": "https://example.invalid/117"},
                    {"number": 144, "title": "Make tmux the standard session transport with explicit server identity", "url": "https://example.invalid/144"},
                ],
                "example-org/claw": [
                    {"number": 1227, "title": "Claw sessions should publish a machine-readable role and status file", "url": "https://example.invalid/1227"},
                ],
                "example-org/BrokenMech": [
                    {"number": 459, "title": "10 of 10 open PRs fail checks with one shared pin error", "url": "https://example.invalid/459"},
                ],
            }
            open_path = tmp_path / "open-issues.json"
            open_path.write_text(json.dumps(open_issues))

            with contextlib.redirect_stdout(io.StringIO()) as out_buf:
                rc = OPS.main(["--config", str(cfg_path), "recommend", str(run), "--open-issues-json", str(open_path)])
            self.assertEqual(rc, 0)
            cli_summary = json.loads(out_buf.getvalue())
            self.assertGreaterEqual(cli_summary["by_status"].get("tracked", 0), 3)
            self.assertGreaterEqual(cli_summary["by_status"].get("new_issue", 0), 3)

            rec_dir = run / "recommendations"
            self.assertEqual(rec_dir.stat().st_mode & 0o777, 0o700)
            for fname in ("RECOMMENDATIONS.md", "ISSUE_DRAFTS.md", "recommendations.json"):
                fpath = rec_dir / fname
                self.assertEqual(fpath.stat().st_mode & 0o777, 0o600)
                self.assertGreater(fpath.stat().st_size, 0)

            full = json.loads((rec_dir / "recommendations.json").read_text())
            by_rule = {r["rule"]: r for r in full["recommendations"]}
            self.assertEqual(by_rule["R-CI-COHORT-BROKENMECH"]["tracking"]["status"], "tracked")
            self.assertEqual(by_rule["R-CI-COHORT-BROKENMECH"]["tracking"]["matched_issues"][0]["number"], 459)
            self.assertEqual(by_rule["R-SWAP-SESSION-SPRAWL"]["tracking"]["status"], "tracked")
            self.assertEqual(by_rule["R-CODEX-LOG-ARCHIVE"]["tracking"]["status"], "tracked")
            self.assertEqual(by_rule["R-CLAW-DISPATCH"]["tracking"]["status"], "tracked")

            self.assertEqual(by_rule["R-TMUX-COLLECT"]["tracking"]["status"], "new_issue")
            self.assertIn("example-org/XMechBay#144", by_rule["R-TMUX-COLLECT"]["tracking"]["issue_draft"]["body"])
            self.assertEqual(by_rule["R-PORT-WATCH"]["tracking"]["status"], "new_issue")
            self.assertEqual(by_rule["R-CLAUDE-RETENTION"]["tracking"]["status"], "new_issue")

            drafts_md = (rec_dir / "ISSUE_DRAFTS.md").read_text()
            self.assertNotIn("@", drafts_md)
            self.assertNotIn("/Users/", drafts_md)
            self.assertNotIn("host-a", drafts_md)
            self.assertNotIn("host-b", drafts_md)

    def test_recommend_no_github_and_lookup_failure_never_claim_new_issue(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cfg = config(infra_repos={"xmechbay": "example-org/XMechBay", "claw": "example-org/claw"})
            run = tmp_path / "run-offline"
            run.mkdir()
            payload = {"hosts": [{"key": "h1", "reachable": True, "errors": ["tmux_probe_failed"],
                                  "health": {"status": "ok", "process_status": "ok"}}], "sessions": []}
            (run / "sessions.json").write_text(json.dumps(payload))
            summary_off = OPS.recommend(run, cfg, no_github=True)
            self.assertEqual(summary_off["new_issue_drafts"], [])
            self.assertIn("unverified_offline", summary_off["by_status"])

            with mock.patch.object(OPS, "fetch_open_issues_for_repo", return_value=([], "gh auth failed")):
                summary_err = OPS.recommend(run, cfg, no_github=False)
            self.assertEqual(summary_err["new_issue_drafts"], [])
            self.assertIn("lookup_failed", summary_err["by_status"])

    def test_paginated_github_issue_fetch_and_pr_filtering(self):
        calls = []
        page_1 = json.dumps([
            {"number": 201, "title": "First page issue", "html_url": "https://example.invalid/201"},
            {"number": 202, "title": "Open PR disguised in issues API", "pull_request": {"url": "https://example.invalid/pr/202"}},
        ])
        page_2 = json.dumps([
            {"number": 5, "title": "Older page issue beyond 100 ceiling", "html_url": "https://example.invalid/5"},
        ])

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return types.SimpleNamespace(returncode=0, stdout=page_1 + "\n" + page_2, stderr="")

        with mock.patch.object(OPS.subprocess, "run", fake_run):
            rows, err = OPS.fetch_open_issues_for_repo("example-org/XMechBay")
        self.assertIsNone(err)
        self.assertEqual(calls[0][:3], ["gh", "api", "--paginate"])
        self.assertIn("repos/example-org/XMechBay/issues?state=open&per_page=100", calls[0][3])
        self.assertEqual([r["number"] for r in rows], [201, 5])

    def test_repo_slug_validation_and_infra_repo_guards(self):
        for bad in ("-flag/repo", "org/../repo", "single", "", None, "org/-bad"):
            self.assertFalse(OPS.is_valid_repo_slug(bad), bad)
        self.assertTrue(OPS.is_valid_repo_slug("example-org/XMechBay_1.0"))

        with mock.patch.object(OPS.subprocess, "run") as run_mock:
            rows, err = OPS.fetch_open_issues_for_repo("-evil/flag")
        self.assertEqual(rows, [])
        self.assertIn("invalid repository slug", err)
        run_mock.assert_not_called()

        with self.assertRaises(SystemExit):
            config(infra_repos={"typo": "example-org/XMechBay"})
        with self.assertRaises(SystemExit):
            config(infra_repos={"xmechbay": "../escape"})
        with self.assertRaises(ValueError):
            OPS.load_open_issue_index({"example-org/XMechBay"}, open_issues_json=Path("x.json"), no_github=True)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cfg_path = tmp_path / "fleet-ops.json"
            cfg_data = json.loads((EXAMPLES / "fleet-ops.json").read_text())
            cfg_data["fleet_status_config"] = str(EXAMPLES / "fleet-hosts.json")
            cfg_path.write_text(json.dumps(cfg_data))
            run = tmp_path / "run-cli"
            run.mkdir()
            (run / "sessions.json").write_text(json.dumps({"hosts": [], "sessions": []}))
            open_path = tmp_path / "open.json"
            open_path.write_text("{}")
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    OPS.main(["--config", str(cfg_path), "recommend", str(run), "--open-issues-json", str(open_path), "--no-github"])
                with self.assertRaises(SystemExit):
                    OPS.main(["--config", str(cfg_path), "recommend", str(run), "--no-github", "--infra-repo", "unknown=example-org/x"])

    def test_open_issues_json_missing_repo_and_malformed_shapes(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bad_list = tmp_path / "list.json"
            bad_list.write_text(json.dumps(["not", "a", "dict"]))
            idx, errs, mode = OPS.load_open_issue_index({"example-org/XMechBay"}, open_issues_json=bad_list)
            self.assertEqual((idx, mode), ({}, "file"))
            self.assertIn("must be a JSON object", errs["example-org/XMechBay"])

            partial = tmp_path / "partial.json"
            partial.write_text(json.dumps({"example-org/XMechBay": []}))
            idx2, errs2, _ = OPS.load_open_issue_index({"example-org/XMechBay", "example-org/claw"}, open_issues_json=partial)
            self.assertEqual(idx2, {"example-org/XMechBay": []})
            self.assertIn("missing from --open-issues-json", errs2["example-org/claw"])

            cfg = config(infra_repos={"xmechbay": "example-org/XMechBay", "claw": "example-org/claw"},
                         claws={"c": {"host": "h1", "repo": "claw", "mechs": ["*"]}})
            run = tmp_path / "run-missing"
            run.mkdir()
            (run / "sessions.json").write_text(json.dumps({"hosts": [], "sessions": []}))
            summary = OPS.recommend(run, cfg, open_issues_json=partial)
            self.assertEqual(summary["new_issue_drafts"], [])
            self.assertEqual(summary["by_status"], {"lookup_failed": 1})

    def test_word_bounded_issue_matching_avoids_false_positives(self):
        ci_groups = [("open prs", "fail*"), ("prs", "failing checks"), ("prs", "fail*", "check*"), ("shared", "ci", "fail*")]
        ci_index = {
            "example-org/SampleMech": [
                {"number": 10, "title": "Citation validation fails on missing shared DOI"},
                {"number": 11, "title": "Species lookup fails when shared cache is cold"},
                {"number": 12, "title": "CI: 8 of 10 sampled open PRs fail checks"},
            ]
        }
        hits = OPS.match_open_issues(ci_index, ["example-org/SampleMech"], ci_groups)
        self.assertEqual([h["number"] for h in hits], [12])

        cfg = config(infra_repos={"xmechbay": "example-org/XMechBay", "claw": "example-org/claw"}, claws={})
        guard = {"installed": True, "status": "unrescued", "exit_code": 20, "skipped": {"entry-cap": 2}, "failed": 0,
                 "finished_at": "2099-01-01T00:00:00+00:00"}
        resources = {"h1": healthy_host(guard=guard)}
        findings = OPS.analyze({"hosts": [], "sessions": []}, resources, {}, cfg)
        unrelated_guard_issues = {
            "example-org/XMechBay": [
                {"number": 141, "title": "tmp-state guard: survey other system-owned top-level names"},
                {"number": 67, "title": "tmp-state guard: incomplete status on gutted clones"},
            ]
        }
        recs = OPS.synthesize_recommendations({"hosts": [], "sessions": []}, resources, {}, findings, cfg,
                                              open_index=unrelated_guard_issues, source_mode="file")
        by_rule = {r["rule"]: r for r in recs}
        self.assertEqual(by_rule["R-TMP-GUARD"]["tracking"]["status"], "new_issue")

    def test_sanitize_public_text_and_new_issue_code_tokens(self):
        dbl_handle = "@" * 2 + "maintainer"
        real_email = "alice" + "@" + "lab.edu"
        tagged_real_email = "@" * 2 + real_email
        example_email = "alice@example.invalid"
        tagged_example_email = "@" + example_email
        mac_path = "/" + "Users/sample_user/work/repo"
        var_path = "/" + "private/var/folders/ab/cd123/T/scratch.json"
        tmp_subpath = "/" + "tmp/run_dir/state.json"
        home_tilde = "~" + "/agent-work/claw/brief.md"
        win_path = "C:\\" + "Users\\sample_user\\repo\\out.txt"
        mdns_host = "builder-node" + ".local"
        priv_ip1 = ".".join(("192", "168", "1", "25"))
        priv_ip2 = ".".join(("10", "4", "8", "12"))
        priv_ip3 = ".".join(("172", "20", "1", "9"))
        gh_token = "ghp_" + ("A" * 24)

        raw = (
            f"Ping {dbl_handle}, {real_email}, {tagged_real_email}, keep {example_email} and {tagged_example_email}. "
            f"Paths: {mac_path}. ({var_path}) `{tmp_subpath}`: {home_tilde}; {win_path}. "
            f"Net: {mdns_host} at {priv_ip1}, {priv_ip2}, {priv_ip3}; token {gh_token}; host box-m5 vs `/tmp` and `.codex/sessions`."
        )
        cleaned = OPS.sanitize_public_text(raw, private_terms={"box-m5", "local", "tmux"})
        self.assertNotIn("@maintainer", cleaned)
        self.assertIn("Ping maintainer,", cleaned)
        self.assertNotIn(real_email, cleaned)
        self.assertIn("<redacted-email>", cleaned)
        self.assertIn(example_email, cleaned)
        self.assertNotIn("@" + example_email, cleaned)
        self.assertIn("Paths: <local-path>. (<local-path>) `<local-path>`: <local-path>; <local-path>.", cleaned)
        self.assertNotIn("sample_user", cleaned)
        self.assertNotIn(mdns_host, cleaned)
        self.assertNotIn(priv_ip1, cleaned)
        self.assertNotIn(priv_ip2, cleaned)
        self.assertNotIn(priv_ip3, cleaned)
        self.assertNotIn(gh_token, cleaned)
        self.assertNotIn("box-m5", cleaned)
        self.assertIn("host <host> vs `/tmp` and `.codex/sessions`.", cleaned)

        cfg = config(infra_repos={"xmechbay": "example-org/XMechBay", "claw": "example-org/claw"}, claws={})
        resources = {"box-m5": healthy_host(codex_sessions={"path": "~/.codex/sessions", "exists": True, "gib": 60.0},
                                            claude_cleanup_period_days={".claude": "absent"})}
        findings = OPS.analyze({"hosts": [], "sessions": []}, resources, {}, cfg)
        recs = OPS.synthesize_recommendations({"hosts": [], "sessions": []}, resources, {}, findings, cfg,
                                              open_index={"example-org/XMechBay": [], "example-org/claw": []},
                                              source_mode="file", private_terms={"box-m5"})
        by_rule = {r["rule"]: r for r in recs}
        self.assertIn("`.codex/sessions`", by_rule["R-CODEX-LOG-ARCHIVE"]["tracking"]["issue_draft"]["body"])
        self.assertIn("`.claude`", by_rule["R-CLAUDE-RETENTION"]["tracking"]["issue_draft"]["body"])

        drafts_md = OPS.render_issue_drafts_md(f"run-box-m5-{dbl_handle}", recs, private_terms={"box-m5"})
        self.assertNotIn("box-m5", drafts_md)
        self.assertNotIn("@maintainer", drafts_md)

    def test_private_dir_and_atomic_file_writes_reject_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            real_outside = base / "outside"
            real_outside.mkdir()
            victim = real_outside / "victim.txt"
            victim.write_text("preserve-me")

            sym_dir = base / "sym-run"
            sym_dir.symlink_to(real_outside)
            with self.assertRaises(ValueError):
                OPS.ensure_private_dir(sym_dir)

            run = base / "clean-run"
            run.mkdir()
            (run / "sessions.json").write_text(json.dumps({"hosts": [], "sessions": []}))
            (run / "recommendations").symlink_to(real_outside)
            with self.assertRaises(ValueError):
                OPS.recommend(run, config(claws={}), no_github=True)

            (run / "recommendations").unlink()
            rec_dir = OPS.ensure_private_dir(run / "recommendations")
            sym_file = rec_dir / "RECOMMENDATIONS.md"
            sym_file.symlink_to(victim)
            with self.assertRaises(ValueError):
                OPS.write_private_file(sym_file, "overwritten")
            self.assertEqual(victim.read_text(), "preserve-me")

            sym_file.unlink()
            written = OPS.write_private_file(sym_file, "safe-private-content\n")
            self.assertEqual(written.stat().st_mode & 0o777, 0o600)
            self.assertEqual(written.read_text(), "safe-private-content\n")
            self.assertEqual([p.name for p in rec_dir.iterdir() if p.name.startswith(".")], [])


class HardeningEdgeCaseTests(unittest.TestCase):
    def test_live_vs_snapshot_stale_port_watch_health(self):
        cfg = config(infra_repos={"xmechbay": "example-org/XMechBay", "claw": "example-org/claw"}, claws={}, mech_owner={})
        live_payload = {
            "mode": "live",
            "hosts": [{"key": "h-live", "reachable": True, "errors": [],
                       "health": {"status": "stale", "process_status": "ok", "occupancy_pct": 3.0}}],
            "sessions": [],
        }
        live_findings = OPS.analyze(live_payload, {}, {}, cfg)
        self.assertIn("h-live: port-watch health report is stale", [f["title"] for f in live_findings])
        recs = OPS.synthesize_recommendations(live_payload, {}, {}, live_findings, cfg, source_mode="disabled")
        port_rec = [r for r in recs if r["rule"] == "R-PORT-WATCH"][0]
        self.assertIn("stale `port-watch` telemetry", port_rec["conclusion"])
        self.assertNotIn("0 of", port_rec["conclusion"])
        self.assertNotIn("0 host(s)", port_rec["conclusion"])

        snap_payload = {**live_payload, "mode": "snapshot"}
        self.assertEqual(OPS.analyze(snap_payload, {}, {}, cfg), [])
        self.assertEqual(OPS.synthesize_recommendations(snap_payload, {}, {}, [], cfg, source_mode="disabled"), [])

    def test_supervisor_stalls_usage_limits_and_target_guards(self):
        cfg = config(claws={"c": {"host": "h1", "repo": "claw", "mechs": ["*"]}})
        payload = {
            "sessions": [
                session("h1", "claw", "claw", "Goal stalled", repo="claw", target="T1/1"),
                session("h1", "mechbay", "XMechBay", "Goal hit usage limits", target="T2/1"),
            ]
        }
        titles = [f["title"] for f in OPS.analyze(payload, {}, {}, cfg)]
        self.assertIn("claw session claw on h1: goal stalled", titles)
        self.assertIn("mechbay session XMechBay on h1 hit usage limits", titles)

        self.assertEqual(OPS.choose_target([], {}, "fleet")[0], None)
        prompt_target = OPS.claw_targets(
            {"sessions": [session("h1", "claw", "claw", "APPROVAL", repo="claw", target="T1/1")]}, cfg["claws"]["c"]
        )
        self.assertIsNone(OPS.choose_target(prompt_target, cfg["claws"]["c"], "fleet")[0])
        self.assertIn("waits for a human", OPS.choose_target(prompt_target, cfg["claws"]["c"], "fleet")[1])

        limit_target = OPS.claw_targets(
            {"sessions": [session("h1", "claw", "claw", "Goal hit usage limits", repo="claw", target="T1/1")]}, cfg["claws"]["c"]
        )
        self.assertIsNone(OPS.choose_target(limit_target, cfg["claws"]["c"], "fleet")[0])
        self.assertIn("usage limits", OPS.choose_target(limit_target, cfg["claws"]["c"], "fleet")[1])

        nameless_tmux = [{"target": "%9", "transport": "tmux", "window": None, "tab": None,
                          "tmux_session": "fleet", "tmux_window": None, "tty": "ttys009", "pid": 9,
                          "status": "idle", "has_draft": False}]
        self.assertIsNone(OPS.choose_target(nameless_tmux, {}, "fleet")[0])
        self.assertIn("no tmux window name", OPS.choose_target(nameless_tmux, {}, "fleet")[1])

    def test_nullable_config_and_malformed_rows_do_not_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cfg_path = tmp_path / "nulls.json"
            cfg_path.write_text(json.dumps({
                "schema_version": 1, "thresholds": None, "infra_repos": None,
                "claws": None, "hosts": None, "mech_owner": None, "repos": None,
            }))
            cfg = OPS.load_config(cfg_path)
            payload = {
                "hosts": [{"key": None, "reachable": True}, {"reachable": True}],
                "sessions": [session(None, "mech", None, "idle"), session("h1", "mech", "AlphaMech", "idle")],
            }
            findings = OPS.analyze(payload, {}, {}, cfg)
            report = OPS.render_report("r-null", payload, {}, {}, findings, cfg)
            self.assertIn("| h1 |", report)

            malformed_open = {"CultureBotAI/XMechBay": [None, "bad", {"number": "nan"}, {"number": True, "title": "skip"}]}
            self.assertEqual(OPS.match_open_issues(malformed_open, ["CultureBotAI/XMechBay"], [("skip",)]), [])

    def test_decoupled_disk_headroom_and_no_false_manifest_drift(self):
        cfg = config(infra_repos={"xmechbay": "example-org/XMechBay", "claw": "example-org/claw"}, claws={})
        payload = {"hosts": [], "sessions": [session("h1", "mech", "AlphaMech", "Working")]}
        resources = {"h1": healthy_host(disk={"free_gib": 32.0, "total_gib": 1000.0, "used_pct": 96.8},
                                        codex_sessions={"path": "~/.codex/sessions", "exists": True, "gib": 2.5})}
        issues = {"AlphaMech": {"repo": "example-org/AlphaMech", "open_issues": 1, "open_prs": 0, "prs": []}}
        findings = OPS.analyze(payload, resources, issues, cfg)
        recs = OPS.synthesize_recommendations(payload, resources, issues, findings, cfg, source_mode="disabled")
        rules = {r["rule"]: r for r in recs}
        self.assertIn("R-DISK-HEADROOM", rules)
        self.assertEqual(rules["R-DISK-HEADROOM"]["priority"], "P0")
        self.assertNotIn("R-CODEX-LOG-ARCHIVE", rules)
        self.assertNotIn("R-MANIFEST-CLASSIFICATION", rules)
        self.assertNotIn("0 host(s)", rules["R-DISK-HEADROOM"]["conclusion"])

    def test_report_live_claw_and_recommend_recomputes_findings(self):
        cfg = config(claws={
            "dead": {"host": "h1", "mechs": ["*"], "repo": "claw-dead"},
            "live": {"host": "h1", "mechs": ["*"], "repo": "claw-live"},
        })
        payload = {
            "hosts": [{"key": "h1", "reachable": True, "errors": [], "health": {"status": "ok", "process_status": "ok"}}],
            "sessions": [
                session("h1", "claw", "claw-live", "idle", repo="claw-live", target="T1/1"),
                session("h1", "mech", "AlphaMech", "Working", target="T2/1"),
                session("h1", "mech", "GhostMech", "idle", target="T3/1", stale_cached=True),
            ],
        }
        report = OPS.render_report("r-live", payload, {"h1": healthy_host()}, {}, [], cfg)
        self.assertIn("| AlphaMech | h1: Working | live |", report)
        self.assertTrue(any(line.startswith("| h1 |") and line.endswith("| 2 |") for line in report.splitlines()), report)

        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "run-recompute"
            run.mkdir()
            parked_payload = {
                "hosts": [],
                "sessions": [
                    session("h1", "mech", "AlphaMech", "Goal achieved", wait_seconds=5000),
                    session("h1", "mech", "BetaMech", "Goal achieved", wait_seconds=5000),
                ],
            }
            (run / "sessions.json").write_text(json.dumps(parked_payload))
            (run / "findings.json").write_text("[]")  # stale empty findings with 12h default
            override_cfg = config(infra_repos={"xmechbay": "example-org/XMechBay", "claw": "example-org/claw"},
                                  claws={}, thresholds={"achieved_goal_age_hours": 1})
            summary = OPS.recommend(run, override_cfg, no_github=True)
            self.assertEqual(summary["total"], 1)

        self.assertIn("No systemic recommendations were triggered", OPS.render_issue_drafts_md("clean", []))
        tracked_only = [{"id": "R01", "priority": "P1", "tracking": {"status": "tracked", "issue_draft": None}}]
        self.assertIn("already tracked by open GitHub issues", OPS.render_issue_drafts_md("tracked", tracked_only))

    def test_sentence_final_punctuation_hostname_harvest_and_repo_sanitization(self):
        real_email = "alice" + "@" + "lab.edu"
        tagged_email = "@" * 2 + real_email
        ip_a = ".".join(("192", "168", "1", "42"))
        ip_b = ".".join(("10", "0", "1", "5"))
        inline_secret = "api_" + "key=" + ("Z" * 20)
        prose = f"Contact {real_email}. Also {tagged_email}. IPs {ip_a}. and {ip_b}. Secret ({inline_secret}). Host Studio-WS04."
        cfg = config(infra_repos={"xmechbay": "example-org/XMechBay", "claw": "example-org/claw"},
                     claws={"c": {"host": "main", "mechs": ["*"]}}, mech_owner={})
        resources = {"main": healthy_host(hostname="studio-ws04.corp.net")}
        terms = OPS.collect_private_host_terms(cfg, {"hosts": [{"key": "main"}], "sessions": [session("remote-rig", "mech", "M", "idle")]}, resources)
        self.assertIn("studio-ws04.corp.net", terms)
        self.assertIn("studio-ws04", terms)
        self.assertIn("remote-rig", terms)

        cleaned = OPS.sanitize_public_text(prose, private_terms=terms)
        self.assertIn("Contact <redacted-email>.", cleaned)
        self.assertIn("Also <redacted-email>.", cleaned)
        self.assertNotIn(real_email, cleaned)
        self.assertIn("IPs <redacted-ip>. and <redacted-ip>.", cleaned)
        self.assertNotIn(ip_a, cleaned)
        self.assertNotIn(ip_b, cleaned)
        self.assertNotIn("Z" * 20, cleaned)
        self.assertIn("Host <host>.", cleaned)

        rec = OPS.build_recommendation(
            "R-TEST", "P1", "observability", "t", "c", "r", {},
            ["studio-ws04/Repo"], [("match",)], "studio-ws04/Repo", "Title on Studio-WS04", "Body on studio-ws04.",
            {"studio-ws04/Repo": []}, {}, "file", private_terms=terms,
        )
        self.assertEqual(rec["tracking"]["issue_draft"]["repo"], "<host>/Repo")
        md = OPS.render_issue_drafts_md("run-1", [rec], private_terms=terms)
        self.assertNotIn("studio-ws04", md.lower())

    def test_all_rules_self_deduplicate_and_skip_prs_in_open_issues_json(self):
        cfg = config(infra_repos={"xmechbay": "example-org/XMechBay", "claw": "example-org/claw"},
                     claws={"c": {"host": "h1", "repo": "claw", "mechs": ["*"]}}, mech_owner={})
        prs = [{"number": n, "title": f"PR {n}", "draft": True, "updated_at": "2020-01-01T00:00:00Z",
                "mergeable": "UNKNOWN", "review": None, "checks": "FAILURE"} for n in range(1, 7)]
        issues = {
            "AlphaMech": {"repo": "example-org/AlphaMech", "open_issues": 3, "open_prs": 105, "prs": prs},
        }
        payload = {
            "mode": "live",
            "hosts": [{"key": "h1", "reachable": True, "inventory_state": "partial",
                       "errors": ["tmux_probe_failed", "agent_discovery_partial"],
                       "health": {"status": "stale", "process_status": "ok"}}],
            "sessions": [
                session("h1", "claw", "claw", "UNKNOWN", repo="claw", target="T1/1", human_block_kind="draft"),
                session("h1", "mech", "AlphaMech", "Goal stalled", target="P1", transport="process"),
                session("h1", "mech", "BetaMech", "Goal stalled", target="P2", transport="process"),
                session("h1", "mech", "GammaMech", "Goal stalled", target="P3", transport="process"),
                session("h1", "mech", "DeltaMech", "APPROVAL", target="T4/1", agent="agy",
                        skip_permissions=False, agy_work_fixable=True, agy_work_status="fixable"),
                session("h2", "mech", "AlphaMech", "idle", target="T2/1"),
                session("h2", "mech", "BetaMech", "idle", target="T3/1"),
            ],
        }
        guard_variants = [
            {"installed": True, "status": "unrescued", "exit_code": 20, "skipped": {"cap": 1}, "failed": 0, "finished_at": "2099-01-01T00:00:00+00:00"},
            {"installed": True, "status": "incomplete", "exit_code": 30, "skipped": {}, "failed": 0, "unknown": 2, "finished_at": "2099-01-01T00:00:00+00:00"},
            {"installed": False},
        ]
        seen_rules = set()
        for guard in guard_variants:
            resources = {
                "h1": healthy_host(disk={"free_gib": 30.0, "total_gib": 1000.0, "used_pct": 97.0},
                                   swap={"total_gib": 32.0, "used_gib": 29.0},
                                   codex_sessions={"path": "~/.codex/sessions", "exists": True, "gib": 55.0},
                                   claude_cleanup_period_days={".claude": "absent"},
                                   guard=guard),
                "h2": healthy_host(swap={"total_gib": 32.0, "used_gib": 28.0}),
            }
            findings = OPS.analyze(payload, resources, issues, cfg)
            empty_idx = {"example-org/XMechBay": [], "example-org/claw": [], "example-org/AlphaMech": []}
            first_pass = OPS.synthesize_recommendations(payload, resources, issues, findings, cfg,
                                                        open_index=empty_idx, source_mode="file")
            seen_rules |= {r["rule"] for r in first_pass}
            seeded_idx = {"example-org/XMechBay": [], "example-org/claw": [], "example-org/AlphaMech": []}
            for idx_num, rec in enumerate(first_pass, 1):
                draft = rec["tracking"]["issue_draft"]
                # Also inject a PR with the same title first to prove PR entries are ignored
                seeded_idx[draft["repo"]].append({"number": 900 + idx_num, "title": draft["title"], "pull_request": {}})
            pr_only_pass = OPS.synthesize_recommendations(payload, resources, issues, findings, cfg,
                                                          open_index=seeded_idx, source_mode="file")
            self.assertTrue(all(r["tracking"]["status"] == "new_issue" for r in pr_only_pass))

            for idx_num, rec in enumerate(first_pass, 1):
                draft = rec["tracking"]["issue_draft"]
                seeded_idx[draft["repo"]].append({"number": idx_num, "title": draft["title"]})
            second_pass = OPS.synthesize_recommendations(payload, resources, issues, findings, cfg,
                                                         open_index=seeded_idx, source_mode="file")
            untracked = [r["rule"] for r in second_pass if r["tracking"]["status"] != "tracked"]
            self.assertEqual(untracked, [], guard)
        self.assertEqual(len(seen_rules), 13)

    def test_null_host_claw_does_not_match_null_session_and_missing_run_dir_not_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cfg_path = tmp_path / "inner-nulls.json"
            cfg_path.write_text(json.dumps({
                "schema_version": 1,
                "claws": {"broken": None},
                "hosts": {"h1": None},
                "mech_owner": {},
                "repos": {},
            }))
            cfg = OPS.load_config(cfg_path)
            payload = {
                "hosts": None,
                "canonical_mechs": None,
                "stats": None,
                "sessions": [
                    session(None, "claw", "claw", "idle", target="T1/1"),
                    session(None, "mech", "OrphanMech", "idle", target="T2/1"),
                ],
            }
            self.assertEqual(OPS.claw_targets(payload, cfg["claws"]["broken"]), [])
            self.assertIsNone(OPS.claw_for_mech("OrphanMech", {"OrphanMech": payload["sessions"][1:]}, cfg))
            findings = OPS.analyze(payload, {}, {}, cfg)
            titles = [f["title"] for f in findings]
            self.assertIn("claw 'broken' has no host", titles)
            self.assertIn("claw broken has no live session on None", titles)
            self.assertIn("| OrphanMech |", OPS.render_report("r-orphan", payload, {}, {}, findings, cfg))

            missing_run = tmp_path / "does-not-exist"
            with self.assertRaises(SystemExit):
                OPS.plan(missing_run, cfg)
            self.assertFalse(missing_run.exists())
            with self.assertRaises(SystemExit):
                OPS.recommend(missing_run, cfg, no_github=True)
            self.assertFalse(missing_run.exists())


class TwoFleetRoleRoutingTests(unittest.TestCase):
    def test_empty_mechs_list_never_acts_as_catch_all(self):
        cfg = config(
            claws={
                "a-maintainer": {"host": "h1", "repo": "claw", "mechs": []},
                "b-supervisor": {"host": "h1", "repo": "claw", "mechs": ["*"]},
            },
            mech_owner={},
        )
        rows = [
            session("h1", "claw", "claw", "idle", repo="claw", target="T1/1"),
            session("h1", "mech", "AlphaMech", "idle", target="T2/1"),
        ]
        by = OPS.mech_sessions(rows)
        self.assertEqual(OPS.claw_mechs(cfg["claws"]["a-maintainer"]), [])
        self.assertEqual(OPS.claw_for_mech("AlphaMech", by, cfg), "b-supervisor")

    def test_two_fleet_four_claw_workload_routing_and_plan_resolution(self):
        cfg = config(
            hosts={
                "laptop-a": {"tmux_session": "claw-primary", "watch": ["~/work/DiscoverMech/.temp"]},
                "laptop-b": {"watch": ["~/work/HighPrMech/.temp"]},
            },
            claws={
                "io-discovery": {
                    "host": "laptop-b",
                    "label": "IO discovery supervisor",
                    "repo": "sample-claw",
                    "mechs": ["*"],
                    "target_hint": "T10/1",
                    "roles": ["discovery_supervisor"],
                },
                "compute-steward": {
                    "host": "laptop-a",
                    "label": "Compute stewardship supervisor",
                    "repo": "sample-claw",
                    "mechs": ["*"],
                    "tmux_session": "claw-primary",
                    "roles": ["stewardship_supervisor"],
                },
                "compute-infra": {
                    "host": "laptop-a",
                    "label": "Compute infrastructure supervisor",
                    "repo": "sample-claw",
                    "mechs": ["BootstrapMech"],
                    "tmux_session": "claw-secondary",
                    "roles": ["infrastructure_supervisor"],
                },
                "io-closure-cleanup": {
                    "host": "laptop-b",
                    "label": "IO closure steward and maintainer",
                    "repo": "sample-claw",
                    "mechs": [],
                    "target_hint": "T20/1",
                    "roles": ["closure_steward", "fleet_maintainer"],
                },
            },
            mech_owner={},
            thresholds={"non_working_sessions_warn": 3},
        )
        payload = {
            "mode": "live",
            "generated_at": "2026-11-15T20:00:00Z",
            "hosts": [
                {"key": "laptop-a", "reachable": True, "errors": [], "health": {"status": "ok", "process_status": "ok"}},
                {"key": "laptop-b", "reachable": True, "errors": ["tmux_probe_failed"], "health": {"status": "ok", "process_status": "ok"}},
                {"key": "workstation-local", "reachable": False, "errors": ["ssh_probe_timed_out"], "last_reachable": None},
            ],
            "canonical_mechs": [
                "DiscoverMech", "HighPrMech", "HighIssueMech", "SingleFailMech",
                "CohortFailMech", "BootstrapMech", "UnobservedMech",
            ],
            "sessions": [
                session("laptop-a", "claw", "sample-claw", "idle", repo="sample-claw", target="%1",
                        tmux="claw-primary", window="primary", tty="ttys001"),
                session("laptop-a", "claw", "sample-claw", "idle", repo="sample-claw", target="%2",
                        tmux="claw-secondary", window="primary", tty="ttys002"),
                session("laptop-b", "claw", "sample-claw", "idle", repo="sample-claw", target="T10/1", tty="ttys003"),
                session("laptop-b", "claw", "sample-claw", "idle", repo="sample-claw", target="T20/1", tty="ttys004"),
                session("laptop-a", "mech", "DiscoverMech", "idle", target="T101/1"),
                session("laptop-b", "mech", "DiscoverMech", "idle", target="T102/1"),
                session("laptop-a", "mech", "HighPrMech", "Goal stalled", target="T103/1"),
                session("laptop-a", "mech", "HighIssueMech", "Working", target="T104/1"),
                session("laptop-b", "mech", "SingleFailMech", "Working", target="T105/1"),
                session("laptop-a", "mech", "CohortFailMech", "Goal stalled", target="T106/1"),
                session("laptop-b", "mech", "BootstrapMech", "idle", target="T107/1"),
                session("laptop-b", "mech", "UnobservedMech", "idle", target="T108/1"),
            ],
        }
        resources = {
            "laptop-a": healthy_host(
                swap={"total_gib": 32.0, "used_gib": 30.0},
                watched=[{"path": "~/work/DiscoverMech/.temp", "exists": True, "gib": 150.0}],
            ),
            "laptop-b": healthy_host(
                swap={"total_gib": 24.0, "used_gib": 22.0},
                codex_sessions={"path": "~/.codex/sessions", "exists": True, "gib": 75.0},
                watched=[{"path": "~/work/HighPrMech/.temp", "exists": True, "gib": 160.0}],
            ),
            "workstation-local": {"error": "probe timeout"},
        }
        cohort_prs = [{"number": n, "title": f"Fail {n}", "draft": True, "updated_at": "2026-11-14T00:00:00Z",
                       "mergeable": "UNKNOWN", "review": None, "checks": "FAILURE"} for n in range(1, 9)]
        issues = {
            "DiscoverMech": {
                "repo": "example-org/DiscoverMech",
                "open_issues": 14,
                "open_prs": 3,
                "prs": [
                    {"number": 11, "title": "Unreviewed green", "draft": False, "updated_at": "2026-11-15T00:00:00Z",
                     "mergeable": "MERGEABLE", "review": None, "checks": "SUCCESS"},
                    {"number": 12, "title": "Approved green", "draft": False, "updated_at": "2026-11-15T00:00:00Z",
                     "mergeable": "MERGEABLE", "review": "APPROVED", "checks": "SUCCESS"},
                    {"number": 13, "title": "Old draft", "draft": True, "updated_at": "2026-09-01T00:00:00Z",
                     "mergeable": "MERGEABLE", "review": None, "checks": None},
                ],
            },
            "HighPrMech": {"repo": "example-org/HighPrMech", "open_issues": 20, "open_prs": 40, "prs": []},
            "HighIssueMech": {"repo": "example-org/HighIssueMech", "open_issues": 150, "open_prs": None, "prs": []},
            "SingleFailMech": {
                "repo": "example-org/SingleFailMech",
                "open_issues": 10,
                "open_prs": 1,
                "prs": [{"number": 21, "title": "Broken", "draft": False, "updated_at": "2026-11-15T00:00:00Z",
                         "mergeable": "MERGEABLE", "review": None, "checks": "FAILURE"}],
            },
            "CohortFailMech": {"repo": "example-org/CohortFailMech", "open_issues": 12, "open_prs": 10, "prs": cohort_prs},
            "BootstrapMech": {"repo": "example-org/BootstrapMech", "open_issues": 0, "open_prs": 0, "prs": []},
            "UnobservedMech": {"repo": "example-org/UnobservedMech", "error": "gh timeout"},
        }

        findings = OPS.analyze(payload, resources, issues, cfg)
        self.assertNotIn("_live_claws", cfg)
        by_title = {f["title"]: f for f in findings}

        self.assertEqual(by_title["DiscoverMech: no session is working"]["owner"], {"kind": "claw", "key": "io-discovery"})
        self.assertIn("source discovery", by_title["DiscoverMech: no session is working"]["action"])
        self.assertEqual(by_title["HighPrMech on laptop-a: goal stalled"]["owner"], {"kind": "claw", "key": "compute-steward"})
        self.assertEqual(by_title["HighIssueMech: 150 open issues, 0 open PRs"]["owner"], {"kind": "claw", "key": "compute-steward"})
        self.assertEqual(by_title["SingleFailMech: 1 PRs with failing checks"]["owner"], {"kind": "claw", "key": "compute-steward"})
        self.assertEqual(by_title["CohortFailMech: 8 PRs with failing checks"]["owner"], {"kind": "claw", "key": "compute-infra"})
        self.assertEqual(by_title["CohortFailMech on laptop-a: goal stalled"]["owner"], {"kind": "claw", "key": "compute-infra"})
        # Pinned BootstrapMech routes to compute-infra and never receives discovery-lane action text when idle
        self.assertEqual(by_title["BootstrapMech: no session is working"]["owner"], {"kind": "claw", "key": "compute-infra"})
        self.assertNotIn("source discovery", by_title["BootstrapMech: no session is working"]["action"])
        self.assertEqual(by_title["BootstrapMech: no session is working"]["evidence"]["mode"], "pinned")
        # UnobservedMech on laptop-b (where io-discovery runs) still routes to compute-steward, never blind discovery
        self.assertEqual(by_title["UnobservedMech: no session is working"]["owner"], {"kind": "claw", "key": "compute-steward"})

        self.assertEqual(by_title["DiscoverMech: 2 PRs mergeable with green checks"]["owner"], {"kind": "user", "key": "merges"})
        self.assertEqual(by_title["DiscoverMech: 2 PRs mergeable with green checks"]["evidence"]["prs"], [11, 12])
        self.assertEqual(by_title["DiscoverMech: 1 green PRs awaiting closure-steward diff review"]["owner"],
                         {"kind": "claw", "key": "io-closure-cleanup"})
        self.assertEqual(by_title["DiscoverMech: 1 green PRs awaiting closure-steward diff review"]["evidence"]["prs"], [11])
        self.assertEqual(by_title["DiscoverMech: 1 draft PRs untouched for 14+ days"]["owner"],
                         {"kind": "claw", "key": "io-closure-cleanup"})

        # Host-local laptop-b hygiene routes to io-closure-cleanup; coordinator/cross-host & laptop-a stay on XMechBay
        self.assertEqual(by_title["laptop-b: swap 22.0 of 24.0 GiB in use"]["owner"], {"kind": "claw", "key": "io-closure-cleanup"})
        self.assertEqual(by_title["laptop-b: 75.0 GiB of Codex session logs"]["owner"], {"kind": "claw", "key": "io-closure-cleanup"})
        self.assertEqual(by_title["laptop-b: 160.0 GiB in a watched path"]["owner"], {"kind": "claw", "key": "io-closure-cleanup"})
        self.assertEqual(by_title["laptop-a: 150.0 GiB in a watched path"]["owner"], {"kind": "xmechbay", "key": "maintenance"})
        self.assertEqual(by_title["1 Mechs have live sessions on multiple hosts"]["owner"], {"kind": "xmechbay", "key": "maintenance"})
        self.assertEqual(by_title["UnobservedMech: GitHub query failed"]["owner"], {"kind": "xmechbay", "key": "maintenance"})
        self.assertEqual(by_title["laptop-a: swap 30.0 of 32.0 GiB in use"]["owner"], {"kind": "xmechbay", "key": "maintenance"})
        self.assertEqual(by_title["workstation-local: session collection unreachable"]["owner"], {"kind": "xmechbay", "key": "maintenance"})
        self.assertEqual(by_title["workstation-local: resource probe failed"]["owner"], {"kind": "xmechbay", "key": "maintenance"})

        report = OPS.render_report("run-two-fleet", payload, resources, issues, findings, cfg)
        self.assertIn("### Claw roles", report)
        self.assertIn("| DiscoverMech | discover |", report)
        self.assertIn("| CohortFailMech | infra |", report)
        self.assertIn("| BootstrapMech | pinned |", report)
        self.assertIn("| UnobservedMech | unknown |", report)

        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "run-two-fleet"
            run.mkdir()
            (run / "sessions.json").write_text(json.dumps(payload))
            (run / "findings.json").write_text(json.dumps(findings))
            summary = OPS.plan(run, cfg)
            for claw_key in ("io-discovery", "compute-steward", "compute-infra", "io-closure-cleanup"):
                self.assertIn(claw_key, summary["claws"])
                self.assertTrue(summary["claws"][claw_key]["target_resolved"], summary["claws"][claw_key])
            dispatch = (run / "plan/DISPATCH.md").read_text()
            self.assertIn("XMECHBAY_TMUX_SESSION=claw-primary", dispatch)
            self.assertIn("XMECHBAY_TMUX_SESSION=claw-secondary", dispatch)
            self.assertIn("fleet-ops/compute-steward/run-two-fleet/brief.md", dispatch)
            self.assertIn("fleet-ops/compute-infra/run-two-fleet/brief.md", dispatch)
            self.assertIn("## Role boundaries", (run / "plan/briefs/io-discovery.md").read_text())
            self.assertIn("Never merge, push to another session's branch", (run / "plan/briefs/io-closure-cleanup.md").read_text())

    def test_sibling_claw_death_role_fallbacks_closure_scoping_and_cohort_guards(self):
        cfg = config(
            infra_repos={"xmechbay": "example-org/XMechBay", "claw": "example-org/claw"},
            claws={
                "compute-steward": {
                    "host": "laptop-a", "repo": "sample-claw", "mechs": ["*"],
                    "tmux_session": "claw-primary", "roles": ["stewardship_supervisor"],
                },
                "compute-infra": {
                    "host": "laptop-a", "repo": "sample-claw", "mechs": ["BootstrapMech"],
                    "tmux_session": "claw-secondary", "roles": ["infrastructure_supervisor"],
                },
                "io-discovery": {
                    "host": "laptop-b", "repo": "sample-claw", "mechs": ["*"],
                    "target_hint": "T10/1", "roles": ["discovery_supervisor"],
                },
                "io-closure": {
                    "host": "laptop-b", "repo": "sample-claw", "mechs": ["ScopedMech"],
                    "target_hint": "T20/1", "roles": ["closure_steward"],
                },
            },
            mech_owner={"ScopedMech": "io-closure"},  # invalid non-writer mech_owner must warn and fall back
            thresholds={"pr_backlog_warn": 200},
        )
        # claw-secondary and T20/1 are absent while siblings claw-primary and T10/1 stay alive
        partial_payload = {
            "hosts": [{"key": "laptop-a", "reachable": True, "errors": [], "health": {"status": "ok", "process_status": "ok"}}],
            "sessions": [
                session("laptop-a", "claw", "sample-claw", "idle", repo="sample-claw", target="%1",
                        tmux="claw-primary", window="primary", tty="ttys001"),
                session("laptop-b", "claw", "sample-claw", "idle", repo="sample-claw", target="T10/1", tty="ttys003"),
                session("laptop-a", "mech", "CohortFailMech", "Goal stalled", target="T101/1"),
                session("laptop-b", "mech", "ScopedMech", "idle", target="T102/1"),
            ],
        }
        cohort_prs = [{"number": n, "title": f"Fail {n}", "draft": True, "updated_at": "2026-11-14T00:00:00Z",
                       "mergeable": "UNKNOWN", "review": None, "checks": "FAILURE"} for n in range(1, 7)]
        green_100 = [{"number": n, "title": f"Green {n}", "draft": False, "updated_at": "2026-11-15T00:00:00Z",
                      "mergeable": "MERGEABLE", "review": None, "checks": "SUCCESS"} for n in range(1, 101)]
        issues = {
            "CohortFailMech": {"repo": "example-org/CohortFailMech", "open_issues": 2, "open_prs": 6, "prs": cohort_prs},
            "ScopedMech": {"repo": "example-org/ScopedMech", "open_issues": 1, "open_prs": 1, "prs": green_100[:1]},
            "TruncatedMech": {"repo": "example-org/TruncatedMech", "open_issues": 1, "open_prs": 120, "prs": green_100},
        }
        live = OPS.live_claws(partial_payload["sessions"], cfg)
        self.assertEqual(live, {"compute-steward", "io-discovery"})
        self.assertEqual(OPS.mech_mode("TruncatedMech", issues["TruncatedMech"], cfg["thresholds"]), "steward")

        findings = OPS.analyze(partial_payload, {}, issues, cfg)
        by_title = {f["title"]: f for f in findings}
        # Missing sibling claws are flagged even though another claw runs on the same host
        self.assertIn("claw compute-infra has no live session on laptop-a", by_title)
        self.assertIn("claw io-closure has no live session on laptop-b", by_title)
        # Dead compute-infra falls back to live compute-steward for CohortFailMech
        self.assertEqual(by_title["CohortFailMech: 6 PRs with failing checks"]["owner"], {"kind": "claw", "key": "compute-steward"})
        # Non-writer io-closure never captures writer ownership or pinned lane of ScopedMech
        self.assertIn("mech_owner assigns ScopedMech to non-writer claw 'io-closure'", by_title)
        self.assertEqual(OPS.mech_lane_label("ScopedMech", cfg, issues=issues), "discover")
        self.assertEqual(by_title["ScopedMech: no session is working"]["owner"], {"kind": "claw", "key": "io-discovery"})

        recs = OPS.synthesize_recommendations(partial_payload, {}, issues, findings, cfg, source_mode="disabled")
        by_rule = {r["rule"]: r for r in recs}
        self.assertEqual(by_rule["R-CLAW-DISPATCH"]["evidence"]["dead_claws"], ["compute-infra", "io-closure"])

        # Combined supervisor + closure_steward reviews green PRs fleet-wide unless closure_mechs narrows it;
        # accept_infra_cohorts=False keeps cohorts on stewardship_supervisor
        combined_cfg = config(
            claws={
                "steward": {"host": "h1", "repo": "claw", "mechs": ["*"], "roles": ["stewardship_supervisor"]},
                "infra-closure": {
                    "host": "h2", "repo": "claw", "mechs": ["BootstrapMech"], "accept_infra_cohorts": False,
                    "roles": ["infrastructure_supervisor", "closure_steward"],
                },
            },
            mech_owner={},
        )
        live_both = {"steward", "infra-closure"}
        self.assertEqual(OPS.claw_for_mech("CohortFailMech", {}, combined_cfg, live_both, issues=issues), "steward")
        self.assertEqual(OPS.claw_for_mech("BootstrapMech", {}, combined_cfg, live_both, issues=issues), "infra-closure")
        self.assertEqual(OPS.closure_claw_for_mech("ScopedMech", combined_cfg, live=live_both), "infra-closure")

    def test_role_config_validation_and_dead_role_claw_fallback(self):
        cfg = config(
            claws={
                "bad-claw": {
                    "host": "h1", "mechs": "not-a-list", "closure_mechs": [""],
                    "accept_infra_cohorts": "false", "roles": ["unknown_role", ""], "tmux_session": "bad:name",
                },
                "dup-1": {"host": "h1", "mechs": ["*"], "roles": ["discovery_supervisor"]},
                "dup-2": {"host": "h1", "mechs": ["*"], "roles": ["discovery_supervisor"]},
                "offline-closure": {"host": "h2", "repo": "claw-off", "mechs": [], "roles": ["closure_steward", "fleet_maintainer"]},
                "live-steward": {"host": "h1", "repo": "claw-live", "mechs": ["*"], "roles": ["stewardship_supervisor"]},
            },
            mech_owner={},
        )
        payload = {
            "hosts": [{"key": "h2", "reachable": True, "errors": ["tmux_probe_failed"], "health": {"status": "ok", "process_status": "ok"}}],
            "sessions": [session("h1", "claw", "claw-live", "idle", repo="claw-live", target="T1/1")],
        }
        issues = {
            "StaleMech": {
                "repo": "example-org/StaleMech",
                "open_issues": 5,
                "open_prs": 30,
                "prs": [
                    {"number": 1, "title": "Green", "draft": False, "updated_at": "2026-11-15T00:00:00Z",
                     "mergeable": "MERGEABLE", "review": None, "checks": "SUCCESS"},
                    {"number": 2, "title": "Stale draft", "draft": True, "updated_at": "2026-09-01T00:00:00Z",
                     "mergeable": "MERGEABLE", "review": None, "checks": None},
                ],
            }
        }
        findings = OPS.analyze(payload, {}, issues, cfg)
        by_title = {f["title"]: f for f in findings}
        self.assertIn("claw 'bad-claw' mechs must be a list of non-empty strings", by_title)
        self.assertIn("claw 'bad-claw' closure_mechs must be a list of non-empty strings", by_title)
        self.assertIn("claw 'bad-claw' accept_infra_cohorts must be a boolean", by_title)
        self.assertIn("claw 'bad-claw' roles must be a list of non-empty strings", by_title)
        self.assertIn("claw 'bad-claw' names unknown role 'unknown_role'", by_title)
        self.assertIn("claw 'bad-claw' tmux_session must be a valid session name", by_title)
        self.assertIn("host 'h1' assigns role 'discovery_supervisor' to multiple claws (dup-1, dup-2)", by_title)
        # Dead offline-closure claw does not capture h2 maintenance, green PR review, or stale drafts
        self.assertEqual(by_title["h2: session collection partial (tmux_probe_failed)"]["owner"], {"kind": "xmechbay", "key": "maintenance"})
        self.assertNotIn("StaleMech: 1 green PRs awaiting closure-steward diff review", by_title)
        self.assertEqual(by_title["StaleMech: 1 draft PRs untouched for 14+ days"]["owner"], {"kind": "claw", "key": "live-steward"})


class DashboardBlockerAndHtmlTests(unittest.TestCase):
    def test_observe_dashboard_html_extracts_payload_and_guards_errors(self):
        sys.path.insert(0, str(OPS.STATUS_SCRIPTS))
        import fleet_dashboard

        payload = example_payload()
        payload["sessions"][0]["activity"] = "Check </script> tag safety & escaping"
        html_doc = fleet_dashboard.render_dashboard_html(payload, csrf_token="", static_export=True)
        extracted = OPS.extract_dashboard_html_payload(html_doc)
        self.assertEqual(len(extracted["sessions"]), len(payload["sessions"]))
        self.assertEqual(extracted["sessions"][0]["activity"], "Check </script> tag safety & escaping")

        slash_escaped = '<!doctype html><script id="initial-payload" type="application/json">{"sessions":[{"activity":"a<\\/script>b"}]}</script>'
        self.assertEqual(OPS.extract_dashboard_html_payload(slash_escaped)["sessions"][0]["activity"], "a</script>b")

        for bad_html in ("<html><body>no script</body></html>",
                         '<script id="initial-payload">{bad json}</script>',
                         '<script id="initial-payload">["not-an-object"]</script>',
                         '<script id="initial-payload">{"hosts": []}</script>'):
            with self.assertRaises(SystemExit):
                OPS.extract_dashboard_html_payload(bad_html)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cfg_path = tmp_path / "fleet-ops.json"
            data = json.loads((EXAMPLES / "fleet-ops.json").read_text())
            data["fleet_status_config"] = str(EXAMPLES / "fleet-hosts.json")
            cfg_path.write_text(json.dumps(data))
            html_path = tmp_path / "dashboard-latest.html"
            html_path.write_text(html_doc)
            run = tmp_path / "run-html"
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                rc = OPS.main([
                    "--config", str(cfg_path), "observe",
                    "--dashboard-html", str(html_path),
                    "--resources-json", str(EXAMPLES / "fleet-ops-resources.json"),
                    "--no-issues", "--out", str(run),
                ])
                self.assertEqual(rc, 0)
                with self.assertRaises(SystemExit):
                    OPS.main([
                        "--config", str(cfg_path), "observe",
                        "--dashboard-html", str(html_path),
                        "--dashboard-url", "http://127.0.0.1:8765",
                        "--no-issues", "--out", str(tmp_path / "run-conflict"),
                    ])
            saved = json.loads((run / "sessions.json").read_text())
            self.assertEqual(len(saved["sessions"]), len(payload["sessions"]))

    def test_three_way_human_blockers_decisions_order_dispatch_guards_and_agy_work_rec(self):
        cfg = config(
            infra_repos={"xmechbay": "example-org/XMechBay", "claw": "example-org/claw"},
            claws={
                "claw-pasted": {"host": "h1", "repo": "claw-pasted", "mechs": ["AlphaMech"]},
                "claw-reply": {"host": "h1", "repo": "claw-reply", "mechs": ["BetaMech"]},
                "claw-subapproval": {"host": "h1", "repo": "claw-sub", "mechs": ["GammaMech"]},
                "claw-ready": {"host": "h2", "repo": "claw-ready", "mechs": ["*"]},
            },
            mech_owner={},
            thresholds={"idle_sessions_warn": 2},
        )
        rows = [
            # Claws blocked by pasted prompt, agent message, and subagent approval vs ready claw
            session("h1", "claw", "claw-pasted", "idle", repo="claw-pasted", target="T1/1",
                    human_block_kind="pasted_prompt", is_pasted_prompt=True, flags=["draft", "pasted-prompt"],
                    composer_text="[Pasted text #1 +18 lines]"),
            session("h1", "claw", "claw-reply", "idle", repo="claw-reply", target="T2/1",
                    human_block_kind="agent_message", flags=["agent-message"],
                    agent_message="Should I open the PR for BetaMech?"),
            session("h1", "claw", "claw-sub", "Working", repo="claw-sub", target="T3/1",
                    flags=["subagent-approval"], keystroke_hint="1 / 2 / 3"),
            session("h2", "claw", "claw-ready", "idle", repo="claw-ready", target="T4/1"),
            # Mechs with all 3 human blocker classes + agy-work fixable vs already enabled
            session("h1", "mech", "AlphaMech", "APPROVAL", target="T10/1", agent="agy",
                    human_block_kind="keystroke", skip_permissions=False,
                    agy_work_fixable=True, agy_work_status="fixable",
                    keystroke_hint="1 / y / n", activity="Allow run_command pytest?"),
            session("h1", "mech", "BetaMech", "APPROVAL", target="T11/1", agent="agy",
                    human_block_kind="keystroke", skip_permissions=True,
                    agy_work_fixable=False, agy_work_status="already_enabled",
                    keystroke_hint="y / n", activity="Approve git push origin --delete?"),
            session("h2", "mech", "GammaMech", "idle", target="%1", tmux="fleet", window="gamma",
                    human_block_kind="agent_message", flags=["agent-message"],
                    agent_message="Ready to merge PR #42; reply yes or no."),
            session("h2", "mech", "DeltaMech", "UNKNOWN", target="T12/1",
                    human_block_kind="pasted_prompt", is_pasted_prompt=True, flags=["draft", "pasted-prompt"],
                    composer_text="Audit open curation issues\nand summarize priorities"),
            session("h2", "mech", "EpsilonMech", "idle", target="T13/1",
                    human_block_kind="pasted_prompt", is_pasted_prompt=False, flags=["draft"],
                    composer_text="check status"),
            session("h2", "mech", "StalledMech", "Goal stalled", target="T14/1"),
            # Non-Mech session with agent_message
            session("h2", "other", "ExampleTool", "idle", target="T15/1",
                    human_block_kind="agent_message", flags=["agent-message"],
                    agent_message="Confirm export target directory?"),
        ]
        payload = {
            "mode": "live",
            "generated_at": "2026-11-16T23:00:00Z",
            "hosts": [{"key": "h1", "reachable": True, "errors": [], "health": {"status": "ok", "process_status": "ok"}},
                      {"key": "h2", "reachable": True, "errors": [], "health": {"status": "ok", "process_status": "ok"}}],
            "canonical_mechs": ["AlphaMech", "BetaMech", "GammaMech", "DeltaMech", "EpsilonMech", "StalledMech", "DormantMech"],
            "sessions": rows,
        }
        issues = {
            "StalledMech": {
                "repo": "example-org/StalledMech", "open_issues": 1, "open_prs": 1,
                "prs": [{"number": 7, "title": "Green PR", "draft": False, "updated_at": "2026-11-16T00:00:00Z",
                         "mergeable": "MERGEABLE", "review": None, "checks": "SUCCESS"}],
            }
        }
        findings = OPS.analyze(payload, {}, issues, cfg)
        by_title = {f["title"]: f for f in findings}

        # Human-blocked Mechs do not trigger false "no session is working", and blocked idle rows do not count as idle sprawl
        for m in ("AlphaMech", "BetaMech", "GammaMech", "DeltaMech", "EpsilonMech"):
            self.assertNotIn(f"{m}: no session is working", by_title)
        self.assertFalse(any("idle Mech/claw sessions" in t for t in by_title), list(by_title))

        # Verify owners and tailored actions across all 3 blocker classes
        alpha_f = by_title["AlphaMech on h1 waits for a human (APPROVAL)"]
        self.assertEqual(alpha_f["owner"], {"kind": "user", "key": "prompt"})
        self.assertTrue(alpha_f["evidence"]["agy_work_fixable"])
        self.assertIn("agy-work -c", alpha_f["action"])
        self.assertIn("1 / y / n", alpha_f["action"])

        beta_f = by_title["BetaMech on h1 waits for a human (APPROVAL)"]
        self.assertFalse(beta_f["evidence"].get("agy_work_fixable", False))
        self.assertIn("already running agy-work, stopped on a standing Ask rule", beta_f["action"])

        gamma_f = by_title["GammaMech on h2 awaits a user reply (%1)"]
        self.assertEqual(gamma_f["owner"], {"kind": "user", "key": "agent-message"})
        self.assertIn("tmux fleet:gamma", gamma_f["action"])
        self.assertIn("Ready to merge PR #42", gamma_f["action"])

        delta_f = by_title["DeltaMech on h2 has a pasted prompt not running (T12/1)"]
        self.assertEqual(delta_f["owner"], {"kind": "user", "key": "draft"})
        self.assertIn("NOT running", delta_f["action"])

        eps_f = by_title["EpsilonMech on h2 has an unsent composer draft (T13/1)"]
        self.assertEqual(eps_f["owner"], {"kind": "user", "key": "draft"})

        other_f = by_title["other session ExampleTool on h2 awaits a user reply (T15/1)"]
        self.assertEqual(other_f["owner"], {"kind": "user", "key": "agent-message"})

        # Verify REPORT.md annotations and Human blockers summary
        report = OPS.render_report("run-hb", payload, {}, issues, findings, cfg)
        self.assertIn("Human blockers: 9 (3 keystroke/TUI prompts [1 fixable via agy-work -c], 3 agent messages awaiting reply, 3 pasted/unsent composer prompts).", report)
        self.assertIn("h1: APPROVAL [agy-work fixable]", report)
        self.assertIn("h2: idle [awaiting reply]", report)
        self.assertIn("h2: UNKNOWN [pasted not running]", report)
        self.assertIn("h2: idle [draft]", report)

        # Verify choose_target guards on pasted_prompt, agent_message, and subagent-approval claws
        for c_key, expected_reason in (
            ("claw-pasted", "has a pasted prompt not running"),
            ("claw-reply", "awaits a user reply"),
            ("claw-subapproval", "waits for a human (subagent-approval)"),
        ):
            t_list = OPS.claw_targets(payload, cfg["claws"][c_key])
            chosen, reason = OPS.choose_target(t_list, cfg["claws"][c_key], "fleet")
            self.assertIsNone(chosen, c_key)
            self.assertIn(expected_reason, reason)

        # Verify plan() DECISIONS.md ordering and prepared claw brief summary
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "run-hb"
            run.mkdir()
            (run / "sessions.json").write_text(json.dumps(payload))
            (run / "issues.json").write_text(json.dumps(issues))
            (run / "findings.json").write_text(json.dumps(findings))
            summary = OPS.plan(run, cfg)
            self.assertTrue(summary["claws"]["claw-ready"]["target_resolved"])
            decisions_md = (run / "plan/DECISIONS.md").read_text()
            headings = [line for line in decisions_md.splitlines() if line.startswith("## ")]
            self.assertEqual(headings, [
                "## 1. Pasted / unsent prompts not running (press Enter or clear)",
                "## 2. Antigravity approvals fixable via agy-work -c",
                "## 3. Interactive TUI prompts & keystrokes",
                "## 4. Agent messages awaiting your plain-text reply",
                "## 5. PR merges & fleet scope",
                "## Prepared claw briefs (`DISPATCH.md`)",
            ])
            self.assertIn("Ready for canary-first dispatch after approval: `claw-ready`", decisions_md)

            rec_summary = OPS.recommend(run, cfg, open_issues_json=None, no_github=True)
            self.assertGreaterEqual(rec_summary["total"], 2)
            full_recs = json.loads((run / "recommendations/recommendations.json").read_text())["recommendations"]
            by_rule = {r["rule"]: r for r in full_recs}
            self.assertIn("R-AGY-WORK-PERMISSIONS", by_rule)
            self.assertEqual(by_rule["R-AGY-WORK-PERMISSIONS"]["evidence"]["count"], 1)
            self.assertEqual(by_rule["R-CLAW-DISPATCH"]["evidence"]["reply_claws"], ["h1:T2/1"])
            drafts_md = (run / "recommendations/ISSUE_DRAFTS.md").read_text()
            self.assertNotIn("@", drafts_md)
            self.assertNotIn("h1", drafts_md)
            self.assertNotIn("h2", drafts_md)

        # Non-subagent keystroke_prompt row preserves its own status/PROMPT label in choose_target
        non_sub_payload = {
            "sessions": [
                session("h1", "claw", "claw-custom", "idle", repo="claw-custom", target="T9/1",
                        human_block_kind="keystroke", flags=[]),
            ],
        }
        non_sub_claw = {"host": "h1", "repo": "claw-custom"}
        non_sub_targets = OPS.claw_targets(non_sub_payload, non_sub_claw)
        chosen_ns, reason_ns = OPS.choose_target(non_sub_targets, non_sub_claw, "fleet")
        self.assertIsNone(chosen_ns)
        self.assertNotIn("subagent-approval", reason_ns)
        self.assertIn("waits for a human (idle)", reason_ns)


if __name__ == "__main__":
    unittest.main()
