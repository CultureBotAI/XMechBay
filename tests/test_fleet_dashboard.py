"""Local HTML dashboard server, CSRF/loopback guards, and rendering tests; 2026-11-14."""

import ast
from contextlib import redirect_stdout
import http.client
import importlib.util
import io
import json
from pathlib import Path
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest


ROOT = Path(__file__).parents[1]
SCRIPTS = ROOT / ".agents/skills/fleet-session-status/scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("fleet_dashboard_test", SCRIPTS / "fleet_dashboard.py")
DASH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DASH)


class DashboardSyntaxAndSecurityTests(unittest.TestCase):
    def test_all_skill_scripts_parse_as_python39(self):
        for name in ("fleet_collect.py", "fleet_view.py", "fleet_status.py", "fleet_excel.py", "fleet_dashboard.py"):
            with self.subTest(script=name):
                ast.parse((SCRIPTS / name).read_text(), feature_version=(3, 9))

    def test_non_loopback_bind_hosts_are_refused(self):
        for bad in ("0.0.0.0", "::", "worker.example.invalid", ""):
            with self.subTest(host=bad):
                with self.assertRaises(ValueError):
                    DASH.validate_bind_host(bad)
        for allowed in ("127.0.0.1", "localhost", "::1"):
            self.assertEqual(DASH.validate_bind_host(allowed), allowed)

    def test_snapshot_fixture_populates_all_three_role_tables_and_other_drawer(self):
        reports, meta = DASH.collect_dashboard_reports(snapshot_path=ROOT / "examples/fleet-snapshot.json")
        payload = DASH.build_dashboard_payload(reports, mode=meta["mode"])
        roles = payload["stats"]["role_counts_live"]
        self.assertGreaterEqual(roles["mechbay"], 1)
        self.assertGreaterEqual(roles["claw"], 1)
        self.assertGreaterEqual(roles["mech"], 4)
        self.assertGreaterEqual(roles["other"], 1)
        # Boss worker mim-example-01 in /tmp/.../worktree resolves to MediaIngredientMech
        mim_rows = [r for r in payload["sessions"] if r["project_label"] == "MediaIngredientMech"]
        self.assertEqual(len(mim_rows), 1)
        self.assertEqual(mim_rows[0]["stream_label"], "mim-example-01")
        self.assertEqual(mim_rows[0]["role"], "mech")

    def test_host_load_bar_validates_cpu_count_and_renders_colored_meter_bands(self):
        def make_host(key, load_val, cpu_val, reachable=True):
            return {
                "key": key,
                "label": key.upper(),
                "source": "local",
                "reachable": reachable,
                "observed_at": "2026-01-01T12:00:00+00:00",
                "snapshot_stale": False,
                "health": {"status": "ok", "occupancy_ratio": 0.05, "age_seconds": 3, "process_status": "ok"},
                "system": {"arch": "arm64", "cpu_count": cpu_val, "loadavg_1m": load_val, "disk_free_gib": 100.0, "disk_total_gib": 500.0},
                "coverage": {"processes": "complete", "agent_discovery": "complete", "screens_read": 0, "sessions_seen": 0},
                "errors": [],
                "sessions": [],
            }

        # End-to-end check through collect_dashboard_reports -> normalize_host -> _normalize_system.
        snap_reports, _ = DASH.collect_dashboard_reports(snapshot_path=ROOT / "examples/fleet-snapshot.json")
        snap_payload = DASH.build_dashboard_payload(snap_reports, mode="snapshot")
        snap_cpus = {h["key"]: h["system"]["cpu_count"] for h in snap_payload["hosts"]}
        self.assertEqual(snap_cpus, {"workstation-local": 10, "laptop-a": 12, "laptop-b": None})

        unreach_cached = make_host("h-unreach", None, None, reachable=False)
        unreach_cached["system"] = {}
        unreach_cached["last_reachable"] = {
            "observed_at": "2026-01-01T09:00:00+00:00",
            "age_seconds": 10800,
            "sessions": [],
            "system": {"arch": "arm64", "cpu_count": 10, "loadavg_1m": 3.5, "disk_free_gib": 100.0, "disk_total_gib": 500.0},
        }
        payload = DASH.build_dashboard_payload([
            make_host("h-low", 2.0, 10),
            make_host("h-warn", 7.0, 10),
            make_host("h-high", 9.5, 10),
            make_host("h-crit", 14.0, 10),
            make_host("h-bad", -1.0, True),
            unreach_cached,
        ])
        by_key = {h["key"]: h["system"] for h in payload["hosts"]}
        self.assertEqual((by_key["h-low"]["cpu_count"], by_key["h-low"]["loadavg_1m"]), (10, 2.0))
        # Boolean cpu_count and negative loadavg_1m are sanitized to None (never treated as 1 core or valid load).
        self.assertIsNone(by_key["h-bad"]["cpu_count"])
        self.assertIsNone(by_key["h-bad"]["loadavg_1m"])

        doc = DASH.render_dashboard_html(payload, csrf_token="", static_export=True)
        for cls in ("load-bar-track", "load-bar-fill", "load-green", "load-yellow", "load-orange", "load-red"):
            self.assertIn(cls, doc)
        self.assertIn('role: "meter"', doc)

        import re as _re
        import shutil as _shutil
        import subprocess as _subprocess

        node_bin = _shutil.which("node")
        if node_bin:
            script_body = _re.findall(r"<script>(.*?)</script>", doc, _re.S)[0]
            dom_test = r"""
const fs = require("fs"), vm = require("vm");
const src = fs.readFileSync(process.argv[2], "utf8");
const payloadJson = fs.readFileSync(process.argv[3], "utf8");
function makeNode(tag, init = {}) {
  const attrs = Object.assign({}, init);
  return {
    tagName: String(tag).toUpperCase(),
    className: init.className || "",
    textContent: init.textContent || "",
    value: init.value || "",
    checked: true,
    disabled: false,
    style: {},
    children: [],
    classList: { add() {}, remove() {}, contains: () => false },
    setAttribute(k, v) { attrs[k] = String(v); },
    getAttribute: k => attrs[k] || null,
    removeAttribute(k) { delete attrs[k]; },
    addEventListener() {},
    appendChild(c) { this.children.push(c); return c; },
    replaceChildren(...cs) { this.children = cs.slice(); },
  };
}
const byId = {
  "initial-payload": makeNode("script", { textContent: payloadJson }),
  "mode-badge": makeNode("span"), "stamp-line": makeNode("div"),
  "btn-update": makeNode("button"), "btn-update-label": makeNode("span"),
  "select-autorefresh": makeNode("select"), "autorefresh-countdown": makeNode("span"),
  "link-export": makeNode("a"), "btn-theme": makeNode("button"),
  "error-toast": makeNode("div"), "host-cards": makeNode("section"),
  "fleet-rollup": makeNode("section"), "attention-banner": makeNode("section"),
  "mac-filters": makeNode("div"), "filter-search": makeNode("input"),
  "cached-toggle-wrap": makeNode("label"), "toggle-cached": makeNode("input"),
  "tables-container": makeNode("main"),
};
const ctx = vm.createContext({
  window: { location: { protocol: "http:" } },
  document: {
    documentElement: makeNode("html"), activeElement: null,
    getElementById: id => byId[id] || makeNode("div"),
    querySelectorAll: () => [],
    createElement: t => makeNode(t),
    createTextNode: s => ({ textContent: String(s) }),
    addEventListener() {},
  },
  setInterval: () => 1,
  Date, Math, Number, String, Boolean, Array, Object, Set, JSON, RegExp, Error, parseInt, isNaN,
});
vm.runInContext(src, ctx);
const fills = [], titles = [];
(function walk(n) {
  if (n && typeof n.className === "string" && n.className.startsWith("load-bar-fill ")) {
    fills.push(n.className + "@" + (n.getAttribute("style") || ""));
  }
  if (n && n.getAttribute && n.getAttribute("title")) {
    titles.push(n.getAttribute("title"));
  }
  for (const c of (n && n.children) || []) walk(c);
})(byId["host-cards"]);
const expected = [
  "load-bar-fill load-green@width:20%",
  "load-bar-fill load-yellow@width:70%",
  "load-bar-fill load-orange@width:95%",
  "load-bar-fill load-red@width:100%",
];
if (JSON.stringify(fills) !== JSON.stringify(expected)) {
  throw new Error("Mismatch: " + JSON.stringify(fills));
}
if (!titles.includes("Host unreachable (cached 1m load was 3.5, 3h ago)")) {
  throw new Error("Missing unreachable cached-load tooltip: " + JSON.stringify(titles));
}
console.log("LOAD_BARS_VERIFIED");
"""
            with tempfile.TemporaryDirectory() as tmp:
                js_p = Path(tmp) / "client.js"
                pl_p = Path(tmp) / "payload.json"
                dr_p = Path(tmp) / "driver.js"
                js_p.write_text(script_body, encoding="utf-8")
                pl_p.write_text(json.dumps(payload), encoding="utf-8")
                dr_p.write_text(dom_test, encoding="utf-8")
                proc = _subprocess.run([node_bin, str(dr_p), str(js_p), str(pl_p)], capture_output=True, text=True, timeout=10)
                self.assertEqual(proc.returncode, 0, msg=proc.stderr or proc.stdout)
                self.assertIn("LOAD_BARS_VERIFIED", proc.stdout)

    def test_xss_payloads_cannot_break_out_of_embedded_json_or_html(self):
        malicious_report = {
            "key": "m1",
            "label": "M1 </script><script>alert(1)</script>",
            "source": "local",
            "reachable": True,
            "observed_at": "2026-01-01T12:00:00+00:00",
            "snapshot_stale": False,
            "health": {"status": "ok", "occupancy_ratio": 0.05, "age_seconds": 3, "process_status": "ok"},
            "system": {"arch": "arm64", "loadavg_1m": 1.0, "disk_free_gib": 100.0, "disk_total_gib": 500.0},
            "coverage": {"processes": "complete", "agent_discovery": "complete", "screens_read": 1, "sessions_seen": 1},
            "errors": [],
            "sessions": [{
                "agent": "codex",
                "pid": 42,
                "tty": "ttys001",
                "started_at": "2026-01-01T11:00:00+00:00",
                "age_seconds": 3600,
                "repo_label": "XMechBay",
                "role": "mechbay",
                "project_label": "XMechBay",
                "stream_label": "<img src=x onerror=alert(2)>",
                "status": "Working",
                "activity": "</script><script>alert(3)</script>",
                "model": "GPT-Example high",
                "model_compact": "Example H",
                "timer": {"kind": "turn", "text": "12s"},
                "context": {"remaining_percent": 65},
                "listeners": {"count": 1, "status": "ok", "stale": False},
                "delta": {"kind": "changed", "seconds": 0},
                "target": {"transport": "terminal", "window_id": 1, "tab": 1, "selected": True, "minimized": False},
                "flags": [],
                "errors": [],
            }],
        }
        payload = DASH.build_dashboard_payload([malicious_report])
        doc = DASH.render_dashboard_html(payload, csrf_token="safe_token_123", static_export=False)
        self.assertNotIn("<script>alert", doc)
        self.assertNotIn("</script><script>", doc)
        self.assertNotIn("<img src=x", doc)
        self.assertIn("1 · MechBay Sessions", doc)
        self.assertIn("2 · Claw Sessions", doc)
        self.assertIn("3 · Mech Sessions", doc)
        self.assertIn("other-sessions-drawer", doc)
        self.assertIn('<option value="0" selected>Off</option>', doc)
        self.assertIn('<option value="60">60s</option>', doc)

    def test_unreachable_host_retains_cached_sessions_in_payload(self):
        unreachable = {
            "key": "intel",
            "label": "Intel MacBook Pro",
            "source": "ssh",
            "reachable": False,
            "observed_at": "2026-01-01T12:00:00+00:00",
            "sessions": [],
            "errors": ["ssh_probe_timed_out"],
            "coverage": {},
            "health": {"status": "unknown"},
            "system": {},
            "last_reachable": {
                "observed_at": "2026-01-01T09:00:00+00:00",
                "age_seconds": 10800,
                "sessions": [{
                    "agent": "agy",
                    "pid": 500,
                    "tty": "ttys002",
                    "started_at": "2026-01-01T08:00:00+00:00",
                    "age_seconds": 3600,
                    "repo_label": "CultureMech",
                    "status": "Pursuing goal",
                    "activity": "Curating recipes",
                    "target": {"transport": "terminal", "window_id": 2, "tab": 1},
                    "stale_cached": True,
                }],
            },
        }
        payload = DASH.build_dashboard_payload([unreachable])
        self.assertEqual(payload["stats"]["hosts_unreachable"], 1)
        self.assertEqual(payload["stats"]["live_sessions"], 0)
        self.assertEqual(payload["stats"]["cached_sessions"], 1)
        self.assertTrue(payload["sessions"][0]["stale_cached"])
        self.assertEqual(payload["sessions"][0]["symbols"]["mac"]["short"], "IN◌")

    def test_export_html_writes_private_0600_static_file_without_csrf_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_file = Path(tmp) / "exported.html"
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = DASH.main([
                    "--snapshot", str(ROOT / "examples/fleet-snapshot.json"),
                    "--export-html", str(out_file),
                ])
            self.assertEqual(rc, 0)
            self.assertTrue(out_file.is_file())
            self.assertEqual(stat.S_IMODE(out_file.stat().st_mode), 0o600)
            html_text = out_file.read_text(encoding="utf-8")
            self.assertIn("const STATIC_EXPORT = true;", html_text)
            self.assertIn('const CSRF_TOKEN = "";', html_text)

    def test_atomic_private_html_refuses_symlink_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            target_file = Path(tmp) / "precious.txt"
            target_file.write_text("do-not-overwrite", encoding="utf-8")
            link_path = Path(tmp) / "export.html"
            link_path.symlink_to(target_file)
            with self.assertRaises(ValueError):
                DASH.atomic_private_html(link_path, "<html></html>")
            self.assertEqual(target_file.read_text(encoding="utf-8"), "do-not-overwrite")

    def test_role_overrides_preserve_worktree_and_home_classifications_after_normalize(self):
        now = "2026-01-01T12:00:00+00:00"
        snap = {
            "schema_version": 1,
            "observed_at": now,
            "health": {"report": None, "error": "absent"},
            "coverage": {"processes": "complete", "agent_discovery": "complete", "screens_read": 3, "sessions_seen": 3},
            "errors": [],
            "sessions": [
                {
                    "agent": "codex", "pid": 11, "started_at": now, "age_seconds": 60, "tty": "ttys001",
                    "cwd": "/workspace/Mechs/EdgeMech/w01", "title": "w01",
                    "screen": "• Working\n>\nGPT-Example high", "errors": [],
                    "target": {"transport": "terminal", "window_id": 1, "tab": 1, "selected": True, "minimized": False},
                },
                {
                    "agent": "codex", "pid": 12, "started_at": now, "age_seconds": 60, "tty": "ttys002",
                    "cwd": "", "repo_label": "(home)", "title": "XMechBay coordinator",
                    "screen": "• Idle\n>\nGPT-Example high", "errors": [],
                    "target": {"transport": "terminal", "window_id": 2, "tab": 1, "selected": True, "minimized": False},
                },
                {
                    "agent": "claude", "pid": 13, "started_at": now, "age_seconds": 60, "tty": "ttys003",
                    "cwd": "/workspace/custom-pilot", "title": "custom",
                    "screen": "Done.\n❯\nSonnet 1 · medium", "errors": [],
                    "target": {"transport": "terminal", "window_id": 3, "tab": 1, "selected": True, "minimized": False},
                },
            ],
        }
        overrides = {"custom-pilot": {"role": "mech", "project": "CustomMech"}}
        report = DASH.normalize_host("m1", "M1 Workstation", snap, source="local", role_overrides=overrides)
        report["snapshot_stale"] = False
        payload = DASH.build_dashboard_payload([report], role_overrides=overrides)
        by_pid = {row["pid"]: row for row in payload["sessions"]}
        self.assertEqual((by_pid[11]["role"], by_pid[11]["project_label"], by_pid[11]["stream_label"]), ("mech", "EdgeMech", "w01"))
        self.assertEqual((by_pid[12]["role"], by_pid[12]["project_label"]), ("mechbay", "XMechBay"))
        self.assertEqual((by_pid[13]["role"], by_pid[13]["project_label"]), ("mech", "CustomMech"))

    def test_template_replacement_does_not_expand_csrf_token_inside_payload_and_preserves_errors(self):
        reports, _ = DASH.collect_dashboard_reports(snapshot_path=ROOT / "examples/fleet-snapshot.json")
        reports[0]["sessions"][0]["activity"] = "Inspecting __CSRF_TOKEN__ and __STATIC_EXPORT_BOOL__ placeholders"
        payload = DASH.build_dashboard_payload(reports)
        secret_tok = "SecretTokenValue1234567890"
        rendered = DASH.render_dashboard_html(payload, csrf_token=secret_tok, static_export=False)
        prefix = '<script id="initial-payload" type="application/json">'
        start = rendered.index(prefix) + len(prefix)
        end = rendered.index("</script>", start)
        parsed = json.loads(rendered[start:end])
        activities = [row["activity"] for row in parsed["sessions"] if row["activity"]]
        self.assertTrue(any("__CSRF_TOKEN__" in act and secret_tok not in act for act in activities))
        detached = [row for row in parsed["sessions"] if row["project_label"] == "DetachedMech"][0]
        self.assertIn("screen_unavailable", detached["errors"])

    def test_attention_sorts_critical_alerts_ahead_of_warnings(self):
        reports, _ = DASH.collect_dashboard_reports(snapshot_path=ROOT / "examples/fleet-snapshot.json")
        # Add 25 yellow QUESTION sessions on the first host and one unreachable critical host at the end.
        base_row = dict(reports[0]["sessions"][0], status="QUESTION")
        reports[0]["sessions"] = [dict(base_row, pid=500 + idx) for idx in range(25)]
        reports.append({
            "key": "down", "label": "Down Laptop", "source": "ssh", "reachable": False,
            "observed_at": "2026-01-01T12:00:00+00:00", "sessions": [], "errors": ["ssh_probe_timed_out"],
            "coverage": {}, "health": {"status": "unknown"}, "system": {},
        })
        payload = DASH.build_dashboard_payload(reports)
        self.assertEqual(len(payload["stats"]["attention"]), 24)
        self.assertEqual([item["severity"] for item in payload["stats"]["attention"][:2]], ["critical", "critical"])
        self.assertIn("host-unreachable", [item["kind"] for item in payload["stats"]["attention"][:2]])
        self.assertIn("http://127.0.0.1", DASH._allowed_origins(80))

    def test_unreachable_host_uses_cached_health_and_excludes_cached_from_mechs_present(self):
        unreachable_report = {
            "key": "m5",
            "label": "M5 Laptop",
            "source": "ssh",
            "reachable": False,
            "observed_at": "2026-01-01T12:00:00+00:00",
            "sessions": [],
            "errors": ["ssh_probe_timed_out"],
            "coverage": {},
            "health": {"status": "unknown"},
            "system": {"arch": None, "loadavg_1m": None, "disk_free_gib": None, "disk_total_gib": None},
            "last_reachable": {
                "observed_at": "2026-01-01T11:30:00+00:00",
                "age_seconds": 1800,
                "health": {"status": "stale", "occupancy_ratio": 0.142, "age_seconds": 1800, "process_status": "stale"},
                "system": {"arch": "arm64", "loadavg_1m": 2.5, "disk_free_gib": 210.0, "disk_total_gib": 500.0},
                "coverage": {"processes": "complete"},
                "sessions": [
                    {
                        "role": "mech",
                        "project_label": "EdgeMech",
                        "stream_label": None,
                        "repo_label": "EdgeMech",
                        "pid": 77,
                        "agent": "codex",
                        "model_compact": "GPT-6 H",
                        "target": {"transport": "tmux", "pane": "%1", "window": "worker", "tmux_sessions": ["xmb-view-custom-claw"]},
                        "status": "Working",
                        "activity": "cached work",
                        "stale_cached": True,
                        "delta": {"kind": "stale_cached", "seconds": None},
                        "listeners": {"count": None, "status": "stale", "stale": True},
                        "errors": [],
                    },
                    {
                        "role": "other",
                        "project_label": "kg-microbe",
                        "stream_label": "custom-stream-mech",
                        "repo_label": "kg-microbe",
                        "pid": 78,
                        "agent": "claude",
                        "model_compact": "Opus 4.6",
                        "target": {"transport": "tmux", "pane": "%2", "window": "0"},
                        "status": "idle",
                        "activity": None,
                        "stale_cached": True,
                        "delta": {"kind": "stale_cached", "seconds": None},
                        "listeners": {"count": None, "status": "stale", "stale": True},
                        "errors": [],
                    },
                ],
            },
        }
        payload_plain = DASH.build_dashboard_payload([unreachable_report])
        self.assertEqual(payload_plain["hosts"][0]["health"]["status"], "stale")
        self.assertEqual(payload_plain["hosts"][0]["health"]["occupancy_pct"], 14.2)
        self.assertEqual(payload_plain["stats"]["mechs_present_count"], 0)
        self.assertEqual(payload_plain["stats"]["cached_sessions"], 2)

        overrides = {
            "custom-claw": {"role": "claw", "project": "culturebotai-claw"},
            "custom-stream-mech": {"role": "mech", "project": "AssayMech"},
        }
        payload_ov = DASH.build_dashboard_payload([unreachable_report], role_overrides=overrides)
        by_pid = {row["pid"]: row for row in payload_ov["sessions"]}
        self.assertEqual((by_pid[77]["role"], by_pid[77]["project_label"]), ("claw", "culturebotai-claw"))
        self.assertEqual((by_pid[78]["role"], by_pid[78]["project_label"]), ("mech", "AssayMech"))

    def test_safe_json_and_private_html_scrub_surrogates_and_secure_intermediate_dirs(self):
        reports, _ = DASH.collect_dashboard_reports(snapshot_path=ROOT / "examples/fleet-snapshot.json")
        payload = DASH.build_dashboard_payload(reports)
        payload["hosts"][0]["label"] = "M1\ud800Laptop"
        rendered = DASH.render_dashboard_html(payload, csrf_token="", static_export=True)
        encoded = rendered.encode("utf-8")
        self.assertIn(b'"label":"M1?Laptop"', encoded)
        self.assertNotIn(b"\\ud800", encoded)
        self.assertIn('b.setAttribute("aria-selected", String(b.getAttribute("data-view") === "role"))', DASH.DASHBOARD_TEMPLATE)
        self.assertIn("if (sess && !matchesFilters(sess))", DASH.DASHBOARD_TEMPLATE)
        self.assertNotRegex(
            DASH.DASHBOARD_TEMPLATE,
            r"\b(?:innerHTML|outerHTML|insertAdjacentHTML|document\.write)\b",
        )

        with tempfile.TemporaryDirectory() as tmp:
            out_file = Path(tmp) / ".local" / "fleet-reports" / "snap.html"
            DASH.atomic_private_html(out_file, rendered)
            self.assertEqual((Path(tmp) / ".local").lstat().st_mode & 0o777, 0o700)
            self.assertEqual((Path(tmp) / ".local" / "fleet-reports").lstat().st_mode & 0o777, 0o700)
            self.assertEqual(out_file.lstat().st_mode & 0o777, 0o600)

    def test_client_js_csrf_restart_reload_loop_guard_and_state_recovery(self):
        node_bin = shutil.which("node")
        if not node_bin:
            self.skipTest("node binary not available for client JS execution test")

        reports, _ = DASH.collect_dashboard_reports(snapshot_path=ROOT / "examples/fleet-snapshot.json")
        stale_payload = DASH.build_dashboard_payload(reports)
        stale_payload["generated_at"] = "2026-01-01T00:00:00+00:00"
        stale_html = DASH.render_dashboard_html(stale_payload, csrf_token="tok_old_1", static_export=False)
        script_body = re.findall(r"<script>(.*?)</script>", stale_html, re.S)[0]

        harness = r"""
const fs = require("fs");
const vm = require("vm");
const scriptSrc = fs.readFileSync(process.argv[2], "utf8");

function makeNode(tag, attrs = {}) {
  const listeners = {};
  const classes = new Set((attrs.className || "").split(/\s+/).filter(Boolean));
  const attrMap = Object.assign({}, attrs);
  const node = {
    tagName: String(tag || "DIV").toUpperCase(),
    id: attrs.id || "",
    className: attrs.className || "",
    textContent: attrs.textContent || "",
    value: attrs.value || "",
    checked: Boolean(attrs.checked),
    disabled: false,
    open: false,
    style: {},
    children: [],
    classList: {
      add: (...cs) => cs.forEach(c => classes.add(c)),
      remove: (...cs) => cs.forEach(c => classes.delete(c)),
      contains: c => classes.has(c),
    },
    setAttribute(k, v) {
      attrMap[k] = String(v);
      if (k === "id") this.id = String(v);
      if (k === "open") this.open = true;
    },
    getAttribute(k) { return Object.prototype.hasOwnProperty.call(attrMap, k) ? attrMap[k] : null; },
    removeAttribute(k) { delete attrMap[k]; if (k === "open") this.open = false; },
    addEventListener(ev, fn) { (listeners[ev] = listeners[ev] || []).push(fn); },
    click() { (listeners.click || []).forEach(fn => fn({ currentTarget: node, target: node, preventDefault() {} })); },
    dispatchInput(val) {
      node.value = val;
      (listeners.input || []).forEach(fn => fn({ currentTarget: node, target: node }));
    },
    appendChild(child) { this.children.push(child); return child; },
    replaceChildren(...kids) { this.children = kids.slice(); },
    focus() {},
    blur() {},
    scrollIntoView() {},
  };
  return node;
}

function bootPage({ payloadObj, csrfToken, storageMap, fetchImpl }) {
  const byId = {
    "initial-payload": makeNode("script", { textContent: JSON.stringify(payloadObj) }),
    "mode-badge": makeNode("span"),
    "stamp-line": makeNode("div"),
    "btn-update": makeNode("button"),
    "btn-update-label": makeNode("span", { textContent: "↻ Update" }),
    "select-autorefresh": makeNode("select", { value: "0" }),
    "autorefresh-countdown": makeNode("span"),
    "link-export": makeNode("a"),
    "btn-theme": makeNode("button", { textContent: "Theme: Auto" }),
    "error-toast": makeNode("div"),
    "host-cards": makeNode("section"),
    "fleet-rollup": makeNode("section"),
    "attention-banner": makeNode("section"),
    "mac-filters": makeNode("div"),
    "filter-search": makeNode("input", { value: "" }),
    "cached-toggle-wrap": makeNode("label"),
    "toggle-cached": makeNode("input", { checked: true }),
    "tables-container": makeNode("main"),
  };
  const viewBtns = ["role", "host", "matrix"].map(v => makeNode("button", { "data-view": v, "aria-selected": String(v === "role") }));
  const filterBtns = ["all", "input", "active", "alert", "changed", "idle", "unknown"].map(f => makeNode("button", { "data-filter": f, "aria-pressed": String(f === "all") }));
  const docEl = makeNode("html");
  let reloadCount = 0;

  const sessionStorage = {
    getItem: k => (storageMap.has(k) ? storageMap.get(k) : null),
    setItem: (k, v) => storageMap.set(k, String(v)),
    removeItem: k => storageMap.delete(k),
  };
  const win = {
    location: { protocol: "http:", reload() { reloadCount += 1; } },
    sessionStorage,
  };
  const doc = {
    documentElement: docEl,
    activeElement: null,
    getElementById: id => byId[id] || null,
    querySelectorAll: sel => (sel === "[data-view]" ? viewBtns : (sel === "[data-filter]" ? filterBtns : [])),
    createElement: tag => makeNode(tag),
    createTextNode: txt => ({ textContent: String(txt) }),
    addEventListener() {},
  };
  const patchedSrc = scriptSrc.replace(
    'const CSRF_TOKEN = "tok_old_1";',
    () => 'const CSRF_TOKEN = ' + JSON.stringify(csrfToken) + ';'
  );
  const ctx = vm.createContext({
    window: win,
    document: doc,
    sessionStorage,
    fetch: fetchImpl,
    setInterval: () => 1,
    setTimeout: () => 1,
    Date, Math, Number, String, Boolean, Array, Object, Set, JSON, RegExp, Error, parseInt, isNaN,
  });
  vm.runInContext(patchedSrc, ctx);
  return { byId, viewBtns, filterBtns, docEl, getReloadCount: () => reloadCount };
}

(async () => {
  const basePayload = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
  const storage = new Map();
  const seenTokens = [];

  // 1. User expands a session row (+00:00 in row_id), customizes view/filter/search/theme, clicks Update -> 403 -> auto-reloads once.
  function findRowById(root, targetId) {
    if (root.id === targetId) return root;
    for (const ch of root.children || []) {
      const hit = findRowById(ch, targetId);
      if (hit) return hit;
    }
    return null;
  }
  const activeMech = basePayload.sessions.find(
    s => s.symbols.state.tone === "green" && s.project_label.toLowerCase().includes("mech") && String(s.row_id).includes("+")
  );
  if (!activeMech) throw new Error("fixture missing active Mech session with + timezone offset in row_id");
  const targetRowId = "row-" + activeMech.row_id;
  const page1 = bootPage({
    payloadObj: Object.assign({}, basePayload, { generated_at: "2026-01-01T00:00:00+00:00" }),
    csrfToken: "stale_tok",
    storageMap: storage,
    fetchImpl: async (_url, opts) => {
      seenTokens.push(opts.headers["X-MechBay-Token"]);
      return { ok: false, status: 403, json: async () => ({ error: "invalid_csrf_token" }) };
    },
  });
  findRowById(page1.byId["tables-container"], targetRowId).click();
  page1.viewBtns[1].click(); // host view (renders session rows including expanded detail row)
  page1.filterBtns[2].click(); // active
  page1.byId["filter-search"].dispatchInput("Mech");
  page1.byId["btn-theme"].click(); // light
  page1.byId["btn-update"].click();
  await new Promise(r => setImmediate(r));
  await new Promise(r => setImmediate(r));

  if (page1.getReloadCount() !== 1) throw new Error("expected 1 auto-reload, got " + page1.getReloadCount());
  if (page1.byId["error-toast"].classList.contains("is-visible")) throw new Error("toast should stay hidden during auto-reload");

  // 2. Reloaded page serves pre-click cached GET / payload -> restores UI state + expanded row AND fires post-reload refresh.
  const page2 = bootPage({
    payloadObj: Object.assign({}, basePayload, { generated_at: "2026-01-01T00:00:00+00:00", refresh_seq: 1 }),
    csrfToken: "fresh_tok",
    storageMap: storage,
    fetchImpl: async (_url, opts) => {
      seenTokens.push(opts.headers["X-MechBay-Token"]);
      return { ok: true, status: 200, json: async () => Object.assign({}, basePayload, { refresh_seq: 2 }) };
    },
  });
  await new Promise(r => setImmediate(r));
  await new Promise(r => setImmediate(r));

  if (seenTokens.join(",") !== "stale_tok,fresh_tok") throw new Error("unexpected token sequence: " + seenTokens);
  if (page2.viewBtns[1].getAttribute("aria-selected") !== "true") throw new Error("host view not restored");
  if (page2.filterBtns[2].getAttribute("aria-pressed") !== "true") throw new Error("active filter not restored");
  if (page2.byId["filter-search"].value !== "Mech") throw new Error("search input casing not restored");
  if (page2.docEl.getAttribute("data-theme") !== "light") throw new Error("theme not restored");
  const restoredRow = findRowById(page2.byId["tables-container"], targetRowId);
  if (!restoredRow || restoredRow.getAttribute("aria-expanded") !== "true") throw new Error("expanded row with +00:00 row_id not restored");
  if (storage.has("xmechbay_csrf_recovery")) throw new Error("recovery key not cleared after boot");

  // 3. Cooldown guard: another 403 invalid_csrf_token within 30s must NOT reload again; shows toast + Reload page button.
  const page3 = bootPage({
    payloadObj: basePayload,
    csrfToken: "expired_again",
    storageMap: storage,
    fetchImpl: async () => ({ ok: false, status: 403, json: async () => ({ error: "invalid_csrf_token" }) }),
  });
  page3.byId["btn-update"].click();
  await new Promise(r => setImmediate(r));
  await new Promise(r => setImmediate(r));

  if (page3.getReloadCount() !== 0) throw new Error("cooldown guard failed: reloaded again");
  if (!page3.byId["error-toast"].classList.contains("is-visible")) throw new Error("expected visible error toast");
  const toastText = page3.byId["error-toast"].children.map(c => c.textContent).join(" | ");
  if (!toastText.includes("invalid_csrf_token") || !toastText.includes("Reload page")) {
    throw new Error("unexpected toast content: " + toastText);
  }

  // 4. Backward clock step (prevAt in the future) must not suppress auto-reload.
  const storageSkew = new Map([["xmechbay_csrf_reload_at", String(Date.now() + 3600000)]]);
  const pageSkew = bootPage({
    payloadObj: basePayload,
    csrfToken: "tok$dollar",
    storageMap: storageSkew,
    fetchImpl: async () => ({ ok: false, status: 403, json: async () => ({ error: "invalid_csrf_token" }) }),
  });
  pageSkew.byId["btn-update"].click();
  await new Promise(r => setImmediate(r));
  await new Promise(r => setImmediate(r));
  if (pageSkew.getReloadCount() !== 1) throw new Error("future prevAt clock skew should still allow 1 auto-reload");

  // 5. Cold GET / after restart (generated_at >= rec.at) restores UI without firing a duplicate POST /api/refresh.
  const storage4 = new Map();
  let postCalls4 = 0;
  storage4.set("xmechbay_csrf_recovery", JSON.stringify({
    at: Date.now() - 500,
    pendingRefresh: true,
    ui: { viewMode: "host", stateFilter: "all", searchInputVal: "", themeMode: "dark" },
  }));
  const freshIso = new Date(Date.now() + 1000).toISOString();
  const page4 = bootPage({
    payloadObj: Object.assign({}, basePayload, { generated_at: freshIso }),
    csrfToken: "cold_boot_tok",
    storageMap: storage4,
    fetchImpl: async () => { postCalls4 += 1; return { ok: true, status: 200, json: async () => basePayload }; },
  });
  await new Promise(r => setImmediate(r));
  if (postCalls4 !== 0) throw new Error("expected 0 duplicate POSTs when GET / was already fresh, got " + postCalls4);
  if (page4.viewBtns[1].getAttribute("aria-selected") !== "true") throw new Error("host view not restored on cold boot");

  console.log("CLIENT_JS_RECOVERY_OK");
})().catch(err => { console.error(err); process.exit(1); });
"""
        with tempfile.TemporaryDirectory() as tmp:
            js_path = Path(tmp) / "dashboard_client.js"
            payload_path = Path(tmp) / "payload.json"
            harness_path = Path(tmp) / "harness.js"
            js_path.write_text(script_body, encoding="utf-8")
            payload_path.write_text(json.dumps(stale_payload), encoding="utf-8")
            harness_path.write_text(harness, encoding="utf-8")
            proc = subprocess.run(
                [node_bin, str(harness_path), str(js_path), str(payload_path)],
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stderr or proc.stdout)
            self.assertIn("CLIENT_JS_RECOVERY_OK", proc.stdout)

    def test_human_input_payload_enrichment_sorting_and_starvation_immunity(self):
        stalled_rows = [
            {
                "agent": "codex",
                "pid": 1000 + idx,
                "tty": "ttys100",
                "started_at": "2026-01-01T08:00:00+00:00",
                "age_seconds": 3600,
                "role": "mech",
                "project_label": "CultureMech",
                "repo_label": "CultureMech",
                "status": "Goal stalled",
                "activity": "Stalled worker " + str(idx),
                "target": {"transport": "tmux", "pane": "%" + str(idx)},
                "flags": [],
                "errors": [],
            }
            for idx in range(30)
        ]
        human_rows = [
            {
                "agent": "agy",
                "pid": 2001,
                "tty": "ttys201",
                "started_at": "2026-01-01T10:00:00+00:00",
                "age_seconds": 5400,
                "role": "mech",
                "project_label": "AssayMech",
                "repo_label": "AssayMech",
                "status": "UNKNOWN",
                "activity": "Drafting follow-up instruction",
                "target": {"transport": "terminal", "window_id": 203, "tab": 1},
                "delta": {"kind": "unchanged", "seconds": 900},
                "flags": ["draft"],
                "errors": [],
            },
            {
                "agent": "claude",
                "pid": 2002,
                "tty": "ttys202",
                "started_at": "2026-01-01T10:05:00+00:00",
                "age_seconds": 1800,
                "role": "other",
                "project_label": "dismech",
                "repo_label": "dismech",
                "status": "QUESTION",
                "activity": "Which storage engine should this fixture use?",
                "target": {"transport": "terminal", "window_id": 310, "tab": 1},
                "delta": {"kind": "unchanged", "seconds": 240},
                "flags": [],
                "errors": [],
            },
            {
                "agent": "codex",
                "pid": 2003,
                "tty": "ttys203",
                "started_at": "2026-01-01T10:10:00+00:00",
                "age_seconds": 1200,
                "role": "mech",
                "project_label": "EdgeMech",
                "repo_label": "EdgeMech",
                "status": "APPROVAL",
                "activity": "Reviewing approval request",
                "timer": {"kind": "prompt", "text": "14m 20s"},
                "target": {"transport": "terminal", "window_id": 5335, "tab": 1},
                "delta": {"kind": "unchanged", "seconds": 60},
                "flags": ["approval-review"],
                "errors": [],
            },
            {
                "agent": "agy",
                "pid": 2004,
                "tty": "ttys204",
                "started_at": "2026-01-01T09:30:00+00:00",
                "age_seconds": 2400,
                "role": "claw",
                "project_label": "culturebotai-claw",
                "repo_label": "culturebotai-claw",
                "status": "APPROVAL",
                "activity": "subagent awaiting command approval",
                "target": {"transport": "tmux", "pane": "%9", "tmux_session": "claw-coord"},
                "delta": {"kind": "unchanged", "seconds": 1800},
                "flags": ["subagent-approval"],
                "errors": [],
            },
        ]
        live_host = {
            "key": "alpha",
            "label": "Workstation Alpha",
            "source": "local",
            "reachable": True,
            "observed_at": "2026-01-01T12:00:00+00:00",
            "sessions": stalled_rows + human_rows,
            "errors": [],
            "coverage": {"processes": "complete", "agent_discovery": "complete", "screens_read": 34, "sessions_seen": 34},
            "health": {"status": "ok", "process_status": "ok", "occupancy_ratio": 0.12, "age_seconds": 20},
            "system": {"arch": "arm64", "cpu_count": 12, "loadavg_1m": 3.5, "disk_free_gib": 200.0},
        }
        unreachable_host = {
            "key": "beta",
            "label": "Laptop Beta",
            "source": "ssh",
            "reachable": False,
            "observed_at": "2026-01-01T12:00:00+00:00",
            "sessions": [],
            "errors": ["ssh_probe_timed_out"],
            "coverage": {},
            "health": {"status": "unknown"},
            "system": {},
            "last_reachable": {
                "observed_at": "2026-01-01T09:00:00+00:00",
                "age_seconds": 10800,
                "sessions": [
                    {
                        "agent": "codex",
                        "pid": 9001,
                        "tty": "ttys901",
                        "started_at": "2026-01-01T08:00:00+00:00",
                        "age_seconds": 3600,
                        "role": "mech",
                        "project_label": "TraitMech",
                        "repo_label": "TraitMech",
                        "status": "APPROVAL",
                        "activity": "Cached approval from offline laptop",
                        "target": {"transport": "terminal", "window_id": 8, "tab": 1},
                        "stale_cached": True,
                    },
                    {
                        "agent": "agy",
                        "pid": 9002,
                        "tty": "ttys902",
                        "started_at": "2026-01-01T08:05:00+00:00",
                        "age_seconds": 3500,
                        "role": "mech",
                        "project_label": "AIMech",
                        "repo_label": "AIMech",
                        "status": "UNKNOWN",
                        "flags": ["draft"],
                        "target": {"transport": "terminal", "window_id": 9, "tab": 1},
                        "stale_cached": True,
                    },
                ],
            },
        }

        payload = DASH.build_dashboard_payload([live_host, unreachable_host])
        stats = payload["stats"]

        # Legacy attention remains capped at 24 critical items, while human_input retains all 4 live blockers.
        self.assertEqual(len(stats["attention"]), 24)
        self.assertEqual(stats["human_prompt_count"], 3)
        self.assertEqual(stats["human_draft_count"], 1)
        self.assertEqual(stats["human_total_count"], 4)
        self.assertEqual(len(stats["human_input"]), 4)

        host_by_key = {h["key"]: h for h in payload["hosts"]}
        self.assertEqual(
            (host_by_key["alpha"]["human_prompt_count"], host_by_key["alpha"]["human_draft_count"], host_by_key["alpha"]["human_total_count"]),
            (3, 1, 4),
        )
        self.assertEqual(host_by_key["beta"]["human_total_count"], 0)

        # Order: APPROVAL (1800s wait) -> APPROVAL (14m20s = 860s wait) -> QUESTION -> pasted_prompt
        ordered = [(item["project_label"], item["kind"], item["action_label"], item["wait_seconds"]) for item in stats["human_input"]]
        self.assertEqual(ordered, [
            ("culturebotai-claw", "keystroke", "Approve subagent command", 1800),
            ("EdgeMech", "keystroke", "Approve pending request", 860),
            ("dismech", "keystroke", "Answer agent question", 240),
            ("AssayMech", "pasted_prompt", "Unsent prompt — NOT running", 900),
        ])
        self.assertEqual(stats["human_keystroke_count"], 3)
        self.assertEqual(stats["human_pasted_count"], 1)
        self.assertEqual(stats["human_message_count"], 0)
        self.assertEqual(stats["human_agy_work_count"], 0)
        edge_item = stats["human_input"][1]
        self.assertEqual(edge_item["timer_text"], "⏳ 14m 20s")
        self.assertEqual(edge_item["keystroke_hint"], "Approve in TUI · esc interrupts")

        sess_by_pid = {s["pid"]: s for s in payload["sessions"]}
        self.assertIsNone(sess_by_pid[9001]["human_block_kind"])
        self.assertIsNone(sess_by_pid[9002]["human_block_kind"])

    def test_client_js_human_input_spotlight_drawer_filter_and_jump(self):
        node_bin = shutil.which("node")
        if not node_bin:
            self.skipTest("node binary not available for client JS execution test")

        live_host = {
            "key": "alpha",
            "label": "Workstation Alpha",
            "source": "local",
            "reachable": True,
            "observed_at": "2026-01-01T12:00:00+00:00",
            "sessions": [
                {
                    "agent": "claude",
                    "pid": 301,
                    "tty": "ttys001",
                    "started_at": "2026-01-01T10:00:00+00:00",
                    "age_seconds": 1200,
                    "role": "other",
                    "project_label": "dismech",
                    "repo_label": "dismech",
                    "status": "APPROVAL",
                    "activity": "Do you want to make this edit to schema.yaml?",
                    "target": {"transport": "terminal", "window_id": 41, "tab": 1},
                    "delta": {"kind": "unchanged", "seconds": 600},
                    "flags": [],
                    "errors": [],
                },
                {
                    "agent": "agy",
                    "pid": 302,
                    "tty": "ttys002",
                    "started_at": "2026-01-01T10:05:00+00:00",
                    "age_seconds": 900,
                    "role": "mech",
                    "project_label": "AssayMech",
                    "repo_label": "AssayMech",
                    "status": "UNKNOWN",
                    "activity": "Unsent follow-up draft",
                    "target": {"transport": "terminal", "window_id": 203, "tab": 1},
                    "delta": {"kind": "changed", "seconds": 0},
                    "flags": ["draft"],
                    "errors": [],
                },
                {
                    "agent": "codex",
                    "pid": 303,
                    "tty": "ttys003",
                    "started_at": "2026-01-01T09:00:00+00:00",
                    "age_seconds": 3600,
                    "role": "mech",
                    "project_label": "EdgeMech",
                    "repo_label": "EdgeMech",
                    "status": "Working",
                    "activity": "Running unit tests",
                    "target": {"transport": "tmux", "pane": "%3", "tmux_session": "edgemech-w01"},
                    "delta": {"kind": "changed", "seconds": 0},
                    "flags": [],
                    "errors": [],
                },
            ],
            "errors": [],
            "coverage": {"processes": "complete", "agent_discovery": "complete", "screens_read": 3, "sessions_seen": 3},
            "health": {"status": "ok", "process_status": "ok", "occupancy_ratio": 0.1, "age_seconds": 15},
            "system": {"arch": "arm64", "cpu_count": 10, "loadavg_1m": 2.4, "disk_free_gib": 150.0},
        }
        offline_host = {
            "key": "beta",
            "label": "Laptop Beta",
            "source": "ssh",
            "reachable": False,
            "observed_at": "2026-01-01T12:00:00+00:00",
            "sessions": [],
            "errors": ["ssh_probe_timed_out"],
            "coverage": {},
            "health": {"status": "unknown"},
            "system": {},
            "last_reachable": {
                "observed_at": "2026-01-01T08:00:00+00:00",
                "age_seconds": 14400,
                "sessions": [{
                    "agent": "codex",
                    "pid": 999,
                    "tty": "ttys009",
                    "started_at": "2026-01-01T07:00:00+00:00",
                    "age_seconds": 3600,
                    "role": "mech",
                    "project_label": "TraitMech",
                    "repo_label": "TraitMech",
                    "status": "APPROVAL",
                    "activity": "Cached offline prompt",
                    "target": {"transport": "terminal", "window_id": 9, "tab": 1},
                    "stale_cached": True,
                }],
            },
        }
        blocked_payload = DASH.build_dashboard_payload([live_host, offline_host])
        clear_host = dict(live_host, sessions=[live_host["sessions"][2]])
        clear_payload = DASH.build_dashboard_payload([clear_host])

        rendered = DASH.render_dashboard_html(blocked_payload, csrf_token="tok_spot_1", static_export=False)
        script_body = re.findall(r"<script>(.*?)</script>", rendered, re.S)[0]

        harness = r"""
const fs = require("fs");
const vm = require("vm");
const scriptSrc = fs.readFileSync(process.argv[2], "utf8");
const blockedPayload = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const clearPayload = JSON.parse(fs.readFileSync(process.argv[4], "utf8"));

function allText(node) {
  if (!node) return "";
  let out = node.textContent || "";
  for (const ch of node.children || []) out += " " + allText(ch);
  return out;
}

function findNodes(node, pred, acc = []) {
  if (!node) return acc;
  if (pred(node)) acc.push(node);
  for (const ch of node.children || []) findNodes(ch, pred, acc);
  return acc;
}

function makeNode(tag, attrs = {}) {
  const listeners = {};
  const classes = new Set((attrs.className || "").split(/\s+/).filter(Boolean));
  const attrMap = Object.assign({}, attrs);
  let scrolled = 0;
  const node = {
    tagName: String(tag || "DIV").toUpperCase(),
    id: attrs.id || "",
    textContent: attrs.textContent || "",
    value: attrs.value || "",
    checked: Boolean(attrs.checked),
    disabled: false,
    open: false,
    style: {},
    children: [],
    get className() { return Array.from(classes).join(" "); },
    set className(val) {
      classes.clear();
      String(val || "").split(/\s+/).filter(Boolean).forEach(c => classes.add(c));
    },
    classList: {
      add: (...cs) => cs.forEach(c => classes.add(c)),
      remove: (...cs) => cs.forEach(c => classes.delete(c)),
      contains: c => classes.has(c),
    },
    setAttribute(k, v) {
      attrMap[k] = String(v);
      if (k === "id") this.id = String(v);
      if (k === "open") this.open = true;
    },
    getAttribute(k) { return Object.prototype.hasOwnProperty.call(attrMap, k) ? attrMap[k] : null; },
    removeAttribute(k) { delete attrMap[k]; if (k === "open") this.open = false; },
    addEventListener(ev, fn) { (listeners[ev] = listeners[ev] || []).push(fn); },
    click() { (listeners.click || []).forEach(fn => fn({ currentTarget: node, target: node, preventDefault() {} })); },
    appendChild(child) { this.children.push(child); return child; },
    replaceChildren(...kids) { this.children = kids.filter(Boolean); },
    focus() {},
    blur() {},
    scrollIntoView() { scrolled += 1; },
    getScrollCount() { return scrolled; },
  };
  return node;
}

function boot(payloadObj) {
  const byId = {
    "initial-payload": makeNode("script", { textContent: JSON.stringify(payloadObj) }),
    "mode-badge": makeNode("span"),
    "stamp-line": makeNode("div"),
    "btn-update": makeNode("button"),
    "btn-update-label": makeNode("span", { textContent: "↻ Update" }),
    "select-autorefresh": makeNode("select", { value: "0" }),
    "autorefresh-countdown": makeNode("span"),
    "link-export": makeNode("a"),
    "btn-theme": makeNode("button", { textContent: "Theme: Auto" }),
    "error-toast": makeNode("div"),
    "human-input-banner": makeNode("section"),
    "host-cards": makeNode("section"),
    "fleet-rollup": makeNode("section"),
    "attention-banner": makeNode("section"),
    "mac-filters": makeNode("div"),
    "filter-search": makeNode("input", { value: "" }),
    "cached-toggle-wrap": makeNode("label"),
    "toggle-cached": makeNode("input", { checked: true }),
    "tables-container": makeNode("main"),
  };
  const viewBtns = ["role", "host", "matrix"].map(v => makeNode("button", { "data-view": v, "aria-selected": String(v === "role") }));
  const filterBtns = ["all", "input", "active", "alert", "changed", "idle", "unknown"].map(f => makeNode("button", { "data-filter": f, "aria-pressed": String(f === "all") }));
  const docEl = makeNode("html");
  const docListeners = {};
  const storageMap = new Map();
  const sessionStorage = {
    getItem: k => (storageMap.has(k) ? storageMap.get(k) : null),
    setItem: (k, v) => storageMap.set(k, String(v)),
    removeItem: k => storageMap.delete(k),
  };
  const doc = {
    title: "",
    documentElement: docEl,
    activeElement: null,
    getElementById(id) {
      if (byId[id]) return byId[id];
      for (const root of Object.values(byId)) {
        const hits = findNodes(root, n => n.id === id);
        if (hits.length) return hits[0];
      }
      return null;
    },
    querySelectorAll: sel => (sel === "[data-view]" ? viewBtns : (sel === "[data-filter]" ? filterBtns : [])),
    createElement: tag => makeNode(tag),
    createTextNode: txt => ({ textContent: String(txt) }),
    addEventListener(ev, fn) { (docListeners[ev] = docListeners[ev] || []).push(fn); },
    pressKey(key) {
      (docListeners.keydown || []).forEach(fn => fn({ key, ctrlKey: false, metaKey: false, altKey: false, preventDefault() {} }));
    },
  };
  const ctx = vm.createContext({
    window: { location: { protocol: "http:", reload() {} }, sessionStorage },
    document: doc,
    sessionStorage,
    fetch: async () => ({ ok: true, status: 200, json: async () => payloadObj }),
    setInterval: () => 1,
    setTimeout: () => 1,
    Date, Math, Number, String, Boolean, Array, Object, Set, JSON, RegExp, Error, parseInt, isNaN,
  });
  vm.runInContext(scriptSrc, ctx);
  return { byId, doc, viewBtns, filterBtns };
}

// 1. Blocked payload: banner cards, tab title, auto-open Other drawer, row classes, 'i' filter, jump-to-row
const p1 = boot(blockedPayload);
if (!p1.doc.title.startsWith("🟡 (2 need input)")) throw new Error("unexpected title: " + p1.doc.title);
if (p1.filterBtns[1].textContent !== "🟡 Needs Input (2)") throw new Error("unexpected input chip text: " + p1.filterBtns[1].textContent);

const cards = findNodes(p1.byId["human-input-banner"], n => n.classList && n.classList.contains("human-input-card"));
if (cards.length !== 2) throw new Error("expected 2 spotlight cards, got " + cards.length);
if (!cards[0].classList.contains("is-prompt") || !cards[1].classList.contains("is-draft")) {
  throw new Error("expected prompt card first and draft card second");
}

// Attention banner keeps unreachable host alert but deduplicates human-input items
const attText = allText(p1.byId["attention-banner"]);
if (!attText.includes("Laptop Beta is UNREACHABLE") || attText.includes("dismech · T41/1")) {
  throw new Error("attention banner did not deduplicate human-input item: " + attText);
}

// Other Observed Sessions drawer auto-expanded because dismech (role=other) needs input
const drawer1 = p1.doc.getElementById("other-sessions-drawer");
if (!drawer1 || !drawer1.open) throw new Error("expected other-sessions-drawer to auto-open when other session needs input");
if (!allText(drawer1).includes("1 need input")) throw new Error("expected '1 need input' badge on other drawer");

// Row highlight classes: live prompt, live draft, and cached unreachable approval excluded from human-prompt highlight
const promptRow = p1.doc.getElementById("row-alpha:301:2026-01-01T10:00:00+00:00");
const draftRow = p1.doc.getElementById("row-alpha:302:2026-01-01T10:05:00+00:00");
const cachedRow = p1.doc.getElementById("row-beta:999:2026-01-01T07:00:00+00:00");
if (!promptRow.classList.contains("is-human-prompt")) throw new Error("missing is-human-prompt class");
if (!draftRow.classList.contains("is-human-draft")) throw new Error("missing is-human-draft class");
if (cachedRow.classList.contains("is-human-prompt") || !cachedRow.classList.contains("is-stale-cached")) {
  throw new Error("cached unreachable approval row should not be highlighted as live human prompt");
}

// Keyboard shortcut 'i' filters tables to ONLY the 2 live human-blocked sessions (including draft, excluding cached)
p1.doc.pressKey("i");
const visibleRowsInput = findNodes(p1.byId["tables-container"], n => n.classList && n.classList.contains("session-row"));
if (visibleRowsInput.length !== 2) throw new Error("expected 2 visible rows under 'i' filter, got " + visibleRowsInput.length);
p1.doc.pressKey("i"); // toggle back to all
const visibleRowsAll = findNodes(p1.byId["tables-container"], n => n.classList && n.classList.contains("session-row"));
if (visibleRowsAll.length !== 4) throw new Error("expected 4 visible rows after toggling 'i' off, got " + visibleRowsAll.length);

// Switch to Matrix view, then click first spotlight card (dismech in 'other' role) -> switches to role view, expands & scrolls
p1.viewBtns[2].click();
cards[0].click();
if (p1.viewBtns[0].getAttribute("aria-selected") !== "true") throw new Error("clicking spotlight card from matrix should switch to role view");
const jumpedRow = p1.doc.getElementById("row-alpha:301:2026-01-01T10:00:00+00:00");
if (jumpedRow.getAttribute("aria-expanded") !== "true" || !jumpedRow.classList.contains("is-flash-target")) {
  throw new Error("jumped row was not expanded and flashed");
}
if (jumpedRow.getScrollCount() < 1) throw new Error("expected scrollIntoView on jumped row");
if (!allText(p1.byId["tables-container"]).includes("Human Action Needed")) {
  throw new Error("expected Human Action Needed in expanded detail row");
}

// 2. Clear payload: green 0-waiting banner, default tab title, other drawer stays closed
const p2 = boot(clearPayload);
if (p2.doc.title !== "XMechBay · Local Fleet Session Dashboard") throw new Error("unexpected clear title: " + p2.doc.title);
if (!p2.byId["human-input-banner"].classList.contains("is-clear")) throw new Error("expected is-clear banner when 0 sessions need input");
if (!allText(p2.byId["human-input-banner"]).includes("0 sessions waiting on human input")) {
  throw new Error("missing 0-waiting confirmation text");
}
const drawer2 = p2.doc.getElementById("other-sessions-drawer");
if (!drawer2 || drawer2.open) throw new Error("other-sessions-drawer should remain collapsed when 0 other sessions need input");

console.log("CLIENT_JS_HUMAN_INPUT_OK");
"""
        with tempfile.TemporaryDirectory() as tmp:
            js_path = Path(tmp) / "dashboard_client.js"
            blocked_path = Path(tmp) / "blocked.json"
            clear_path = Path(tmp) / "clear.json"
            harness_path = Path(tmp) / "harness_human.js"
            js_path.write_text(script_body, encoding="utf-8")
            blocked_path.write_text(json.dumps(blocked_payload), encoding="utf-8")
            clear_path.write_text(json.dumps(clear_payload), encoding="utf-8")
            harness_path.write_text(harness, encoding="utf-8")
            proc = subprocess.run(
                [node_bin, str(harness_path), str(js_path), str(blocked_path), str(clear_path)],
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stderr or proc.stdout)
            self.assertIn("CLIENT_JS_HUMAN_INPUT_OK", proc.stdout)


class DashboardHTTPServerTests(unittest.TestCase):
    def setUp(self):
        self.calls = 0
        self.call_lock = threading.Lock()
        self.barrier_delay = 0.0

        def fake_collect():
            with self.call_lock:
                self.calls += 1
            if self.barrier_delay > 0:
                time.sleep(self.barrier_delay)
            return DASH.collect_dashboard_reports(snapshot_path=ROOT / "examples/fleet-snapshot.json")

        self.state = DASH.DashboardState(fake_collect)
        try:
            self.server = DASH.create_server(self.state, host="127.0.0.1", port=0)
        except PermissionError as exc:
            self.skipTest("Loopback socket bind restricted in current test environment: " + str(exc))
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._shutdown_server)

    def _shutdown_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _request_inprocess(self, method, path, headers=None):
        hdrs = dict(headers or {})
        if not any(k.lower() == "host" for k in hdrs):
            hdrs["Host"] = "127.0.0.1:" + str(self.port)
        hdrs["Connection"] = "close"
        raw_lines = [method + " " + path + " HTTP/1.1"]
        for key, value in hdrs.items():
            raw_lines.append(key + ": " + str(value))
        raw_req = ("\r\n".join(raw_lines) + "\r\n\r\n").encode("utf-8")

        client_sock, server_sock = socket.socketpair()
        try:
            def serve_one():
                try:
                    DASH.DashboardHandler(server_sock, ("127.0.0.1", 54321), self.server)
                finally:
                    server_sock.close()

            worker = threading.Thread(target=serve_one, daemon=True)
            worker.start()
            client_sock.sendall(raw_req)
            resp = http.client.HTTPResponse(client_sock)
            resp.begin()
            body = resp.read()
            worker.join(timeout=5)
            return resp.status, dict(resp.getheaders()), body
        finally:
            client_sock.close()

    def _request(self, method, path, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, headers=headers or {})
            resp = conn.getresponse()
            body = resp.read()
            return resp.status, dict(resp.getheaders()), body
        except PermissionError:
            return self._request_inprocess(method, path, headers=headers)
        finally:
            conn.close()

    def test_get_index_and_status_set_csp_and_return_payload(self):
        status, headers, body = self._request("GET", "/")
        self.assertEqual(status, 200)
        csp = {k.lower(): v for k, v in headers.items()}.get("content-security-policy", "")
        self.assertIn("default-src 'none'", csp)
        self.assertIn(self.state.csrf_token, body.decode("utf-8"))

        status2, _, body2 = self._request("GET", "/api/status")
        self.assertEqual(status2, 200)
        data = json.loads(body2.decode("utf-8"))
        self.assertEqual(data["refresh_seq"], 1)
        self.assertEqual(self.calls, 1)

    def test_post_refresh_enforces_csrf_origin_and_host_headers(self):
        # Missing CSRF token -> 403 invalid_csrf_token
        st_no_tok, _, body_no_tok = self._request("POST", "/api/refresh")
        self.assertEqual(st_no_tok, 403)
        self.assertEqual(json.loads(body_no_tok.decode("utf-8"))["error"], "invalid_csrf_token")

        # Wrong CSRF token -> 403 invalid_csrf_token
        st_bad_tok, _, body_bad_tok = self._request("POST", "/api/refresh", {"X-MechBay-Token": "wrong"})
        self.assertEqual(st_bad_tok, 403)
        self.assertEqual(json.loads(body_bad_tok.decode("utf-8"))["error"], "invalid_csrf_token")

        # Foreign Origin -> 403 forbidden_origin
        st_bad_origin, _, body_bad_origin = self._request("POST", "/api/refresh", {
            "X-MechBay-Token": self.state.csrf_token,
            "Origin": "https://evil.example.invalid",
        })
        self.assertEqual(st_bad_origin, 403)
        self.assertEqual(json.loads(body_bad_origin.decode("utf-8"))["error"], "forbidden_origin")

        # Spoofed Host header (DNS rebinding attempt) -> 403 forbidden_host_header
        st_bad_host, _, body_bad_host = self._request("POST", "/api/refresh", {
            "Host": "evil.example.invalid",
            "X-MechBay-Token": self.state.csrf_token,
        })
        self.assertEqual(st_bad_host, 403)
        self.assertEqual(json.loads(body_bad_host.decode("utf-8"))["error"], "forbidden_host_header")

        # Valid token + matching localhost Origin -> 200 and increments refresh_seq
        st_ok, _, body = self._request("POST", "/api/refresh", {
            "X-MechBay-Token": self.state.csrf_token,
            "Origin": "http://127.0.0.1:" + str(self.port),
        })
        self.assertEqual(st_ok, 200)
        refreshed = json.loads(body.decode("utf-8"))
        self.assertGreaterEqual(refreshed["refresh_seq"], 1)

    def test_restarted_state_rotates_csrf_and_template_wires_bounded_reload_recovery(self):
        st_initial, _, html_before = self._request("GET", "/")
        self.assertEqual(st_initial, 200)
        old_token = self.state.csrf_token
        self.assertIn(old_token, html_before.decode("utf-8"))

        # Simulate restarting the local dashboard server with a fresh DashboardState/token.
        restarted_state = DASH.DashboardState(self.state._collect_fn)
        self.assertNotEqual(restarted_state.csrf_token, old_token)
        self.server.state = restarted_state

        # An already-open browser tab posting the pre-restart token receives 403 invalid_csrf_token.
        st_stale, _, stale_body = self._request("POST", "/api/refresh", {"X-MechBay-Token": old_token})
        self.assertEqual(st_stale, 403)
        self.assertEqual(json.loads(stale_body.decode("utf-8"))["error"], "invalid_csrf_token")

        # Reloading GET / delivers the rotated token and bounded client-side recovery hooks.
        st_reloaded, _, html_after = self._request("GET", "/")
        self.assertEqual(st_reloaded, 200)
        doc_after = html_after.decode("utf-8")
        self.assertIn(restarted_state.csrf_token, doc_after)
        self.assertNotIn(old_token, doc_after)
        self.assertIn('errCode === "invalid_csrf_token"', doc_after)
        self.assertIn("CSRF_RELOAD_COOLDOWN_MS = 30000", doc_after)
        self.assertIn("CSRF_RECOVERY_MAX_AGE_MS = 60000", doc_after)
        self.assertIn("attemptCsrfRestartReload()", doc_after)
        self.assertIn("restoreCsrfRecoveryState()", doc_after)
        self.assertIn("Dashboard server restarted or token expired (HTTP 403: invalid_csrf_token)", doc_after)
        self.assertIn('text: "Reload page"', doc_after)

        # Follow-up refresh with the reloaded token succeeds.
        st_recovered, _, ok_body = self._request("POST", "/api/refresh", {
            "X-MechBay-Token": restarted_state.csrf_token,
        })
        self.assertEqual(st_recovered, 200)
        self.assertGreaterEqual(json.loads(ok_body.decode("utf-8"))["refresh_seq"], 1)

    def test_concurrent_refreshes_coalesce_into_single_flight(self):
        self.state.get_payload()
        before = self.calls
        self.barrier_delay = 0.15
        results = []

        def worker():
            st, _, body = self._request("POST", "/api/refresh", {
                "X-MechBay-Token": self.state.csrf_token,
            })
            results.append((st, json.loads(body.decode("utf-8"))["refresh_seq"]))

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual(len(results), 4)
        self.assertTrue(all(st == 200 for st, _ in results))
        # Four simultaneous refresh requests coalesce rather than running 4 separate probes.
        self.assertLessEqual(self.calls - before, 2)

    def test_coalesced_waiters_propagate_leader_failure_instead_of_returning_stale_200(self):
        # Populate initial cached payload first.
        self.assertEqual(self.state.get_payload()["refresh_seq"], 1)
        fail_event = threading.Event()

        def failing_collect():
            fail_event.set()
            time.sleep(0.12)
            raise RuntimeError("simulated_refresh_failure")

        self.state._collect_fn = failing_collect
        outcomes = []

        def waiter():
            st, _, _ = self._request("POST", "/api/refresh", {
                "X-MechBay-Token": self.state.csrf_token,
            })
            outcomes.append(st)

        t1 = threading.Thread(target=waiter)
        t2 = threading.Thread(target=waiter)
        t1.start()
        fail_event.wait(timeout=2)
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)
        self.assertEqual(outcomes, [500, 500])

    def test_per_flight_waiter_survives_many_subsequent_refreshes_and_post_body_guards(self):
        started = threading.Event()
        release = threading.Event()
        first_call = [True]

        def controlled_collect():
            if first_call[0]:
                first_call[0] = False
                started.set()
                release.wait(timeout=3)
            return DASH.collect_dashboard_reports(snapshot_path=ROOT / "examples/fleet-snapshot.json")

        flight_state = DASH.DashboardState(controlled_collect, wait_timeout=1.5)
        results = []
        t_leader = threading.Thread(target=lambda: results.append(("leader", flight_state.refresh()["refresh_seq"])))
        t_leader.start()
        started.wait(timeout=2)
        t_waiter = threading.Thread(target=lambda: results.append(("waiter", flight_state.refresh()["refresh_seq"])))
        t_waiter.start()
        time.sleep(0.03)
        release.set()
        for _ in range(12):
            flight_state.refresh()
        t_leader.join(timeout=3)
        t_waiter.join(timeout=3)
        self.assertIn(("leader", 1), results)
        self.assertIn(("waiter", 1), results)

        # Truncated POST body (half-closed before Content-Length bytes arrive) -> 400
        c1, s1 = socket.socketpair()
        try:
            w1 = threading.Thread(target=lambda: (DASH.DashboardHandler(s1, ("127.0.0.1", 54321), self.server), s1.close()), daemon=True)
            w1.start()
            req1 = (
                "POST /api/refresh HTTP/1.1\r\n"
                "Host: 127.0.0.1:" + str(self.port) + "\r\n"
                "X-MechBay-Token: " + self.state.csrf_token + "\r\n"
                "Content-Length: 50\r\n\r\nshort"
            ).encode("utf-8")
            c1.sendall(req1)
            c1.shutdown(socket.SHUT_WR)
            resp1 = http.client.HTTPResponse(c1)
            resp1.begin()
            body1 = json.loads(resp1.read().decode("utf-8"))
            w1.join(timeout=3)
            self.assertEqual(resp1.status, 400)
            self.assertEqual(body1["error"], "incomplete_request_body")
        finally:
            c1.close()

        # Stalled POST body exceeding handler socket timeout -> 408
        class FastTimeoutHandler(DASH.DashboardHandler):
            timeout = 0.08

        c2, s2 = socket.socketpair()
        try:
            w2 = threading.Thread(target=lambda: (FastTimeoutHandler(s2, ("127.0.0.1", 54321), self.server), s2.close()), daemon=True)
            w2.start()
            req2 = (
                "POST /api/refresh HTTP/1.1\r\n"
                "Host: 127.0.0.1:" + str(self.port) + "\r\n"
                "X-MechBay-Token: " + self.state.csrf_token + "\r\n"
                "Content-Length: 50\r\n\r\n"
            ).encode("utf-8")
            c2.sendall(req2)
            resp2 = http.client.HTTPResponse(c2)
            resp2.begin()
            body2 = json.loads(resp2.read().decode("utf-8"))
            w2.join(timeout=3)
            self.assertEqual(resp2.status, 408)
            self.assertEqual(body2["error"], "request_timeout")
        finally:
            c2.close()

    def test_three_way_human_blocker_taxonomy_agy_work_subset_and_host_dot_alignment(self):
        node_bin = shutil.which("node")
        if not node_bin:
            self.skipTest("node binary not available for client JS execution test")

        host_alpha = {
            "key": "alpha",
            "label": "Workstation Alpha",
            "source": "local",
            "reachable": True,
            "observed_at": "2026-01-01T12:00:00+00:00",
            "sessions": [
                {
                    "agent": "agy",
                    "pid": 501,
                    "tty": "ttys001",
                    "started_at": "2026-01-01T10:00:00+00:00",
                    "age_seconds": 1800,
                    "skip_permissions": False,
                    "role": "mech",
                    "project_label": "TraitMech",
                    "repo_label": "TraitMech",
                    "status": "APPROVAL",
                    "keystroke_hint": "1 or Enter allow · 2 deny · f diff · ↑/↓",
                    "activity": "Allow execution of pytest -q?",
                    "target": {"transport": "terminal", "window_id": 11, "tab": 1},
                    "delta": {"kind": "unchanged", "seconds": 900},
                    "flags": ["agy-work-fixable"],
                    "errors": [],
                },
                {
                    "agent": "agy",
                    "pid": 502,
                    "tty": "ttys002",
                    "started_at": "2026-01-01T10:05:00+00:00",
                    "age_seconds": 1500,
                    "skip_permissions": True,
                    "role": "mech",
                    "project_label": "EdgeMech",
                    "repo_label": "EdgeMech",
                    "status": "APPROVAL",
                    "keystroke_hint": "1 or Enter allow · 2 deny · ↑/↓",
                    "activity": "Allow execution of rm scratch.tmp?",
                    "target": {"transport": "terminal", "window_id": 12, "tab": 1},
                    "delta": {"kind": "unchanged", "seconds": 300},
                    "flags": ["agy-work-active"],
                    "errors": [],
                },
                {
                    "agent": "agy",
                    "pid": 503,
                    "tty": "ttys003",
                    "started_at": "2026-01-01T10:10:00+00:00",
                    "age_seconds": 1200,
                    "skip_permissions": False,
                    "role": "claw",
                    "project_label": "culturebotai-claw",
                    "repo_label": "culturebotai-claw",
                    "status": "QUESTION",
                    "keystroke_hint": "1–3 or ↑/↓ + Enter",
                    "activity": "Which target branch should we sync?",
                    "target": {"transport": "tmux", "pane": "%5", "tmux_session": "claw-main"},
                    "delta": {"kind": "unchanged", "seconds": 400},
                    "flags": [],
                    "errors": [],
                },
                {
                    "agent": "claude",
                    "pid": 504,
                    "tty": "ttys004",
                    "started_at": "2026-01-01T10:15:00+00:00",
                    "age_seconds": 900,
                    "role": "mechbay",
                    "project_label": "XMechBay",
                    "repo_label": "XMechBay",
                    "status": "idle",
                    "activity": "Ranked 3 open backlog items.",
                    "agent_message": "Which of those 3 items would you like me to branch and build first?",
                    "target": {"transport": "terminal", "window_id": 14, "tab": 1},
                    "delta": {"kind": "unchanged", "seconds": 600},
                    "flags": ["agent-message"],
                    "errors": [],
                },
            ],
            "errors": [],
            "coverage": {"processes": "complete", "agent_discovery": "complete", "screens_read": 4, "sessions_seen": 4},
            "health": {"status": "ok", "process_status": "ok", "occupancy_ratio": 0.1, "age_seconds": 10},
            "system": {"arch": "arm64", "cpu_count": 12, "loadavg_1m": 2.0, "disk_free_gib": 180.0},
        }
        host_pasted_only = {
            "key": "gamma",
            "label": "Laptop Gamma",
            "source": "ssh",
            "reachable": True,
            "observed_at": "2026-01-01T12:00:00+00:00",
            "sessions": [
                {
                    "agent": "claude",
                    "pid": 601,
                    "tty": "ttys010",
                    "started_at": "2026-01-01T11:00:00+00:00",
                    "age_seconds": 700,
                    "role": "mech",
                    "project_label": "AssayMech",
                    "repo_label": "AssayMech",
                    "status": "UNKNOWN",
                    "activity": "Finished earlier turn.",
                    "composer_text": "add these to NEXT TASKS [Pasted text #2 +26 lines]",
                    "target": {"transport": "terminal", "window_id": 204, "tab": 1},
                    "delta": {"kind": "unchanged", "seconds": 500},
                    "flags": ["draft", "pasted-prompt"],
                    "errors": [],
                },
            ],
            "errors": [],
            "coverage": {"processes": "complete", "agent_discovery": "complete", "screens_read": 1, "sessions_seen": 1},
            "health": {"status": "ok", "process_status": "ok", "occupancy_ratio": 0.05, "age_seconds": 12},
            "system": {"arch": "arm64", "cpu_count": 8, "loadavg_1m": 0.8, "disk_free_gib": 95.0},
        }
        host_msg_only = {
            "key": "delta",
            "label": "Laptop Delta",
            "source": "ssh",
            "reachable": True,
            "observed_at": "2026-01-01T12:00:00+00:00",
            "sessions": [
                {
                    "agent": "agy",
                    "pid": 701,
                    "tty": "ttys020",
                    "started_at": "2026-01-01T11:10:00+00:00",
                    "age_seconds": 600,
                    "role": "mech",
                    "project_label": "HabitatMech",
                    "repo_label": "HabitatMech",
                    "status": "idle",
                    "activity": "Prepared PR diff.",
                    "agent_message": "Do you approve opening this PR in SampleOrg/HabitatMech?",
                    "target": {"transport": "terminal", "window_id": 305, "tab": 1},
                    "delta": {"kind": "unchanged", "seconds": 180},
                    "flags": ["agent-message"],
                    "errors": [],
                },
            ],
            "errors": [],
            "coverage": {"processes": "complete", "agent_discovery": "complete", "screens_read": 1, "sessions_seen": 1},
            "health": {"status": "ok", "process_status": "ok", "occupancy_ratio": 0.04, "age_seconds": 10},
            "system": {"arch": "arm64", "cpu_count": 8, "loadavg_1m": 0.4, "disk_free_gib": 110.0},
        }

        payload = DASH.build_dashboard_payload([host_alpha, host_pasted_only, host_msg_only])
        stats = payload["stats"]
        self.assertEqual(stats["human_keystroke_count"], 3)
        self.assertEqual(stats["human_agy_work_count"], 1)  # Only pid 501 (not 502 skip_perm=True, not 503 QUESTION)
        self.assertEqual(stats["human_message_count"], 2)
        self.assertEqual(stats["human_pasted_count"], 1)
        self.assertEqual(stats["human_total_count"], 6)

        # Issue #163 alignment: gamma (pasted-only) uses 🟠1 in dot_summary (not ⚫1), delta (msg-only) uses 🟣1
        hosts_by_key = {h["key"]: h for h in payload["hosts"]}
        self.assertEqual(hosts_by_key["gamma"]["dot_summary"], [{"dot": "🟠", "count": 1}])
        self.assertEqual(hosts_by_key["delta"]["dot_summary"], [{"dot": "🟣", "count": 1}])
        # Issue #234 alignment: agent_message and pasted_prompt sessions are excluded from idle/unknown tone_counts
        self.assertEqual(stats["tone_counts"]["idle"], 0)
        self.assertEqual(stats["tone_counts"]["unknown"], 0)

        rendered = DASH.render_dashboard_html(payload, csrf_token="tok_tax_1", static_export=False)
        script_body = re.findall(r"<script>(.*?)</script>", rendered, re.S)[0]

        harness = r"""
const fs = require("fs");
const vm = require("vm");
const scriptSrc = fs.readFileSync(process.argv[2], "utf8");
const payloadObj = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));

function allText(node) {
  if (!node) return "";
  let out = node.textContent || "";
  for (const ch of node.children || []) out += " " + allText(ch);
  return out;
}

function findNodes(node, pred, acc = []) {
  if (!node) return acc;
  if (pred(node)) acc.push(node);
  for (const ch of node.children || []) findNodes(ch, pred, acc);
  return acc;
}

function makeNode(tag, attrs = {}) {
  const listeners = {};
  const classes = new Set((attrs.className || "").split(/\s+/).filter(Boolean));
  const attrMap = Object.assign({}, attrs);
  const node = {
    tagName: String(tag || "DIV").toUpperCase(),
    id: attrs.id || "",
    textContent: attrs.textContent || "",
    value: attrs.value || "",
    checked: Boolean(attrs.checked),
    disabled: false,
    open: false,
    style: {},
    children: [],
    get className() { return Array.from(classes).join(" "); },
    set className(val) {
      classes.clear();
      String(val || "").split(/\s+/).filter(Boolean).forEach(c => classes.add(c));
    },
    classList: {
      add: (...cs) => cs.forEach(c => classes.add(c)),
      remove: (...cs) => cs.forEach(c => classes.delete(c)),
      contains: c => classes.has(c),
    },
    setAttribute(k, v) {
      attrMap[k] = String(v);
      if (k === "id") this.id = String(v);
      if (k === "open") this.open = true;
    },
    getAttribute(k) { return Object.prototype.hasOwnProperty.call(attrMap, k) ? attrMap[k] : null; },
    removeAttribute(k) { delete attrMap[k]; if (k === "open") this.open = false; },
    addEventListener(ev, fn) { (listeners[ev] = listeners[ev] || []).push(fn); },
    click() { (listeners.click || []).forEach(fn => fn({ currentTarget: node, target: node, preventDefault() {} })); },
    appendChild(child) { this.children.push(child); return child; },
    replaceChildren(...kids) { this.children = kids.filter(Boolean); },
    focus() {},
    blur() {},
    scrollIntoView() {},
  };
  return node;
}

const byId = {
  "initial-payload": makeNode("script", { textContent: JSON.stringify(payloadObj) }),
  "mode-badge": makeNode("span"),
  "stamp-line": makeNode("div"),
  "btn-update": makeNode("button"),
  "btn-update-label": makeNode("span", { textContent: "↻ Update" }),
  "select-autorefresh": makeNode("select", { value: "0" }),
  "autorefresh-countdown": makeNode("span"),
  "link-export": makeNode("a"),
  "btn-theme": makeNode("button", { textContent: "Theme: Auto" }),
  "error-toast": makeNode("div"),
  "human-input-banner": makeNode("section"),
  "host-cards": makeNode("section"),
  "fleet-rollup": makeNode("section"),
  "attention-banner": makeNode("section"),
  "mac-filters": makeNode("div"),
  "filter-search": makeNode("input", { value: "" }),
  "cached-toggle-wrap": makeNode("label"),
  "toggle-cached": makeNode("input", { checked: true }),
  "tables-container": makeNode("main"),
};
const viewBtns = ["role", "host", "matrix"].map(v => makeNode("button", { "data-view": v, "aria-selected": String(v === "role") }));
const filterBtns = ["all", "input", "active", "alert", "changed", "idle", "unknown"].map(f => makeNode("button", { "data-filter": f, "aria-pressed": String(f === "all") }));
const doc = {
  title: "",
  documentElement: makeNode("html"),
  activeElement: null,
  getElementById(id) {
    if (byId[id]) return byId[id];
    for (const root of Object.values(byId)) {
      const hits = findNodes(root, n => n.id === id);
      if (hits.length) return hits[0];
    }
    return null;
  },
  querySelectorAll: sel => (sel === "[data-view]" ? viewBtns : (sel === "[data-filter]" ? filterBtns : [])),
  createElement: tag => makeNode(tag),
  createTextNode: txt => ({ textContent: String(txt) }),
  addEventListener() {},
};
const storageMap = new Map();
const sessionStorage = {
  getItem: k => (storageMap.has(k) ? storageMap.get(k) : null),
  setItem: (k, v) => storageMap.set(k, String(v)),
  removeItem: k => storageMap.delete(k),
};
vm.runInContext(scriptSrc, vm.createContext({
  window: { location: { protocol: "http:", reload() {} }, sessionStorage },
  document: doc,
  sessionStorage,
  fetch: async () => ({ ok: true, status: 200, json: async () => payloadObj }),
  setInterval: () => 1,
  setTimeout: () => 1,
  Date, Math, Number, String, Boolean, Array, Object, Set, JSON, RegExp, Error, parseInt, isNaN,
}));

// Verify banner renders all 6 human blockers with distinct card classes and badges
const bannerText = allText(byId["human-input-banner"]);
if (!bannerText.includes("1/3 fixable by agy-work")) throw new Error("missing 1/3 agy-work ratio: " + bannerText);
if (!bannerText.includes("⚡ Fixable via agy-work") || !bannerText.includes("agy-work -c")) {
  throw new Error("missing agy-work fixable badge/cmd: " + bannerText);
}
if (!bannerText.includes("🛡 agy-work on (Ask gate)")) throw new Error("missing agy-work active Ask-gate badge: " + bannerText);
if (!bannerText.includes("⌨ 1 or Enter allow · 2 deny · f diff · ↑/↓")) throw new Error("missing keystroke hint: " + bannerText);
if (!bannerText.includes("💬 Which of those 3 items")) throw new Error("missing agent message preview: " + bannerText);
if (!bannerText.includes("❯ add these to NEXT TASKS [Pasted text #2 +26 lines]")) throw new Error("missing pasted composer text: " + bannerText);

// Verify Mac host card border classes align with blocker categories (Issue #163)
const hostCards = findNodes(byId["host-cards"], n => n.classList && n.classList.contains("host-card"));
if (hostCards.length !== 3) throw new Error("expected 3 host cards, got " + hostCards.length);
if (!hostCards[0].classList.contains("has-human-input")) throw new Error("alpha should have has-human-input");
if (!hostCards[1].classList.contains("has-drafts-only")) throw new Error("gamma (pasted-only) should have has-drafts-only");
if (!hostCards[2].classList.contains("has-agent-message")) throw new Error("delta (message-only) should have has-agent-message");

// Verify table row highlight classes distinguish prompt vs message vs pasted
const msgRow = doc.getElementById("row-alpha:504:2026-01-01T10:15:00+00:00");
const pasteRow = doc.getElementById("row-gamma:601:2026-01-01T11:00:00+00:00");
if (!msgRow.classList.contains("is-human-message")) throw new Error("expected is-human-message on agent_message row");
if (!pasteRow.classList.contains("is-human-draft")) throw new Error("expected is-human-draft on pasted_prompt row");
if (!allText(pasteRow).includes("❯ add these to NEXT TASKS")) throw new Error("expected composer_text in table row");

// Click the '⚡ Fixable by agy-work (1/3)' sub-filter chip -> narrows both spotlight cards and main tables to ONLY TraitMech (pid 501)
const agyChip = findNodes(byId["human-input-banner"], n => n.getAttribute && n.getAttribute("data-input-sub") === "agy_work")[0];
if (!agyChip) throw new Error("missing agy_work sub-filter chip");
agyChip.click();

const filteredCards = findNodes(byId["human-input-banner"], n => n.classList && n.classList.contains("human-input-card"));
if (filteredCards.length !== 1 || !allText(filteredCards[0]).includes("TraitMech")) {
  throw new Error("expected agy_work filter to isolate TraitMech card, got " + filteredCards.length);
}
const filteredRows = findNodes(byId["tables-container"], n => n.classList && n.classList.contains("session-row"));
if (filteredRows.length !== 1 || filteredRows[0].id !== "row-alpha:501:2026-01-01T10:00:00+00:00") {
  throw new Error("expected agy_work filter to isolate TraitMech table row, got " + filteredRows.length);
}

// Click '🟣 💬 Agent Messages (2)' sub-filter chip -> isolates the 2 coding-agent plain-text message rows
const msgChip = findNodes(byId["human-input-banner"], n => n.getAttribute && n.getAttribute("data-input-sub") === "agent_message")[0];
msgChip.click();
const msgRows = findNodes(byId["tables-container"], n => n.classList && n.classList.contains("session-row"));
if (msgRows.length !== 2) throw new Error("expected 2 agent_message rows, got " + msgRows.length);

// Click '🟠 📋 Pasted Not Running (1)' sub-filter chip -> isolates AssayMech pasted prompt row
const pasteChip = findNodes(byId["human-input-banner"], n => n.getAttribute && n.getAttribute("data-input-sub") === "pasted_prompt")[0];
pasteChip.click();
const pasteRows = findNodes(byId["tables-container"], n => n.classList && n.classList.contains("session-row"));
if (pasteRows.length !== 1 || !allText(pasteRows[0]).includes("AssayMech")) {
  throw new Error("expected 1 pasted_prompt row (AssayMech), got " + pasteRows.length);
}

// Issue #234: 'idle' and 'unknown' filter chips exclude agent_message and pasted_prompt rows
const idleBtn = filterBtns.find(b => b.getAttribute("data-filter") === "idle");
idleBtn.click();
const idleRows = findNodes(byId["tables-container"], n => n.classList && n.classList.contains("session-row"));
if (idleRows.length !== 0) {
  throw new Error("expected 0 unblocked idle rows when only agent_message sessions are idle, got " + idleRows.length);
}
const unknownBtn = filterBtns.find(b => b.getAttribute("data-filter") === "unknown");
unknownBtn.click();
const unknownRows = findNodes(byId["tables-container"], n => n.classList && n.classList.contains("session-row"));
if (unknownRows.length !== 0) {
  throw new Error("expected 0 unblocked unknown rows when only pasted_prompt session is UNKNOWN, got " + unknownRows.length);
}

// Issue #235: Clicking Laptop Delta host input pill scopes human-input-banner Spotlight to Delta only
const deltaInputPill = findNodes(hostCards[2], n => n.classList && n.classList.contains("host-input-pill"))[0];
if (!deltaInputPill) throw new Error("missing delta host-input-pill");
deltaInputPill.click();
const deltaBannerText = allText(byId["human-input-banner"]);
if (!deltaBannerText.includes("on Laptop Delta")) {
  throw new Error("expected Spotlight banner to show 'on Laptop Delta', got: " + deltaBannerText);
}
const deltaSpotlightCards = findNodes(byId["human-input-banner"], n => n.classList && n.classList.contains("human-input-card"));
if (deltaSpotlightCards.length !== 1 || !allText(deltaSpotlightCards[0]).includes("HabitatMech")) {
  throw new Error("expected Spotlight banner to scope to 1 HabitatMech card on Delta, got " + deltaSpotlightCards.length);
}
const inputBtn = filterBtns.find(b => b.getAttribute("data-filter") === "input");
if (inputBtn.textContent !== "🟡 Needs Input (1)") {
  throw new Error("expected Needs Input chip to show (1) for Delta, got: " + inputBtn.textContent);
}
// Clicking All Macs restores all 6 Spotlight cards
const allMacsBtn = findNodes(byId["mac-filters"], n => n.textContent === "All Macs")[0];
allMacsBtn.click();
const restoredSpotlightCards = findNodes(byId["human-input-banner"], n => n.classList && n.classList.contains("human-input-card"));
if (restoredSpotlightCards.length !== 6) {
  throw new Error("expected All Macs to restore 6 Spotlight cards, got " + restoredSpotlightCards.length);
}

console.log("CLIENT_JS_TAXONOMY_OK");
"""
        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "dashboard_client.js"
            payload_path = Path(tmp) / "payload.json"
            harness_path = Path(tmp) / "harness.js"
            script_path.write_text(script_body, encoding="utf-8")
            payload_path.write_text(json.dumps(payload), encoding="utf-8")
            harness_path.write_text(harness, encoding="utf-8")
            proc = subprocess.run(
                [node_bin, str(harness_path), str(script_path), str(payload_path)],
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stderr or proc.stdout)
            self.assertIn("CLIENT_JS_TAXONOMY_OK", proc.stdout)

    def _post_json(self, path, data_obj, headers=None):
        raw_body = data_obj if isinstance(data_obj, bytes) else json.dumps(data_obj).encode("utf-8")
        hdrs = dict(headers or {})
        hdrs.setdefault("Content-Type", "application/json")
        hdrs["Content-Length"] = str(len(raw_body))
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("POST", path, body=raw_body, headers=hdrs)
            resp = conn.getresponse()
            body = resp.read()
            return resp.status, dict(resp.getheaders()), body
        except PermissionError:
            if not any(k.lower() == "host" for k in hdrs):
                hdrs["Host"] = "127.0.0.1:" + str(self.port)
            hdrs["Connection"] = "close"
            raw_lines = ["POST " + path + " HTTP/1.1"]
            for key, value in hdrs.items():
                raw_lines.append(key + ": " + str(value))
            raw_req = ("\r\n".join(raw_lines) + "\r\n\r\n").encode("utf-8") + raw_body
            client_sock, server_sock = socket.socketpair()
            try:
                worker = threading.Thread(
                    target=lambda: (DASH.DashboardHandler(server_sock, ("127.0.0.1", 54321), self.server), server_sock.close()),
                    daemon=True,
                )
                worker.start()
                client_sock.sendall(raw_req)
                resp = http.client.HTTPResponse(client_sock)
                resp.begin()
                body = resp.read()
                worker.join(timeout=5)
                return resp.status, dict(resp.getheaders()), body
            finally:
                client_sock.close()
        finally:
            conn.close()

    def test_m1_terminal_focus_enrichment_and_http_guards(self):
        from unittest import mock

        m1_local = {
            "key": "m1",
            "label": "M1 Workstation",
            "source": "local",
            "reachable": True,
            "observed_at": "2026-01-01T12:00:00+00:00",
            "sessions": [
                {
                    "agent": "agy",
                    "pid": 801,
                    "tty": "ttys011",
                    "started_at": "2026-01-01T10:00:00+00:00",
                    "age_seconds": 1800,
                    "role": "mech",
                    "project_label": "CultureMech",
                    "repo_label": "CultureMech",
                    "status": "APPROVAL",
                    "activity": "Allow execution of pytest?",
                    "target": {"transport": "terminal", "window_id": 601, "tab": 2},
                    "flags": [],
                    "errors": [],
                },
                {
                    "agent": "codex",
                    "pid": 802,
                    "tty": "ttys012",
                    "started_at": "2026-01-01T10:05:00+00:00",
                    "age_seconds": 1500,
                    "role": "mechbay",
                    "project_label": "XMechBay",
                    "repo_label": "XMechBay",
                    "status": "Working",
                    "activity": "Monitoring fleet",
                    "target": {
                        "transport": "tmux",
                        "pane": "%3",
                        "tmux_session": "xmb-primary",
                        "tmux_sessions": ["xmb-primary", "xmb-view-primary"],
                    },
                    "flags": [],
                    "errors": [],
                },
                {
                    "agent": "claude",
                    "pid": 803,
                    "tty": "??",
                    "started_at": "2026-01-01T10:10:00+00:00",
                    "age_seconds": 900,
                    "role": "other",
                    "project_label": "HeadlessWorker",
                    "repo_label": "HeadlessWorker",
                    "status": "Working",
                    "activity": "Background task",
                    "target": {"transport": "process"},
                    "flags": [],
                    "errors": [],
                },
            ],
            "errors": [],
            "coverage": {"processes": "complete", "agent_discovery": "complete", "screens_read": 3, "sessions_seen": 3},
            "health": {"status": "ok", "process_status": "ok", "occupancy_ratio": 0.08, "age_seconds": 5},
            "system": {"arch": "arm64", "cpu_count": 10, "loadavg_1m": 1.5, "disk_free_gib": 200.0},
        }
        m5_ssh = {
            "key": "m5",
            "label": "M5 Laptop",
            "source": "ssh",
            "reachable": True,
            "observed_at": "2026-01-01T12:00:00+00:00",
            "sessions": [
                {
                    "agent": "codex",
                    "pid": 901,
                    "tty": "ttys004",
                    "started_at": "2026-01-01T09:00:00+00:00",
                    "age_seconds": 3600,
                    "role": "mech",
                    "project_label": "EdgeMech",
                    "repo_label": "EdgeMech",
                    "status": "Working",
                    "activity": "Remote work on M5",
                    "target": {"transport": "terminal", "window_id": 44, "tab": 1},
                    "flags": [],
                    "errors": [],
                },
            ],
            "errors": [],
            "coverage": {"processes": "complete", "agent_discovery": "complete", "screens_read": 1, "sessions_seen": 1},
            "health": {"status": "ok", "process_status": "ok", "occupancy_ratio": 0.06, "age_seconds": 8},
            "system": {"arch": "arm64", "cpu_count": 16, "loadavg_1m": 3.0, "disk_free_gib": 400.0},
        }
        m5_even_if_local = dict(m5_ssh, key="m5local", label="M5 Local Host", source="local", sessions=[
            dict(m5_ssh["sessions"][0], pid=902),
        ])
        m1_remote_ssh = dict(m1_local, key="m1ssh", label="M1 Remote SSH", source="ssh", sessions=[
            dict(m1_local["sessions"][0], pid=903),
        ])

        # Snapshot mode disables terminal focus even for M1 local rows.
        snap_payload = DASH.build_dashboard_payload([m1_local, m5_ssh], mode="snapshot")
        self.assertTrue(all(not r["can_focus_terminal"] for r in snap_payload["sessions"]))

        # Live mode enables terminal focus strictly for live local M1 terminal/tmux sessions.
        live_reports = [m1_local, m5_ssh, m5_even_if_local, m1_remote_ssh]
        live_payload = DASH.build_dashboard_payload(live_reports, mode="live")
        by_pid = {r["pid"]: r for r in live_payload["sessions"]}
        self.assertTrue(by_pid[801]["can_focus_terminal"])
        self.assertEqual((by_pid[801]["window_id"], by_pid[801]["tab_index"]), (601, 2))
        self.assertTrue(by_pid[802]["can_focus_terminal"])
        self.assertFalse(by_pid[803]["can_focus_terminal"])  # M1 headless process
        self.assertFalse(by_pid[901]["can_focus_terminal"])  # M5 ssh
        self.assertFalse(by_pid[902]["can_focus_terminal"])  # M5 local (non-M1 Mac)
        self.assertFalse(by_pid[903]["can_focus_terminal"])  # M1 probed over SSH
        self.assertTrue(live_payload["stats"]["human_input"][0]["can_focus_terminal"])

        # Snapshot server rejects POST /api/focus-terminal with 409 before touching OS processes.
        st_snap, _, b_snap = self._post_json(
            "/api/focus-terminal",
            {"row_id": "any:1:t"},
            {"X-MechBay-Token": self.state.csrf_token},
        )
        self.assertEqual(st_snap, 409)
        self.assertEqual(json.loads(b_snap.decode("utf-8"))["error"], "terminal_focus_unavailable_in_snapshot_mode")

        # Switch server to live payload for route/security/subprocess tests.
        self.server.state = DASH.DashboardState(lambda: (live_reports, {"mode": "live", "role_overrides": None}))
        tok = self.server.state.csrf_token

        # CSRF, Origin, Host, and JSON body validation on /api/focus-terminal
        st_csrf, _, _ = self._post_json("/api/focus-terminal", {"row_id": by_pid[801]["row_id"]})
        self.assertEqual(st_csrf, 403)
        st_orig, _, b_orig = self._post_json(
            "/api/focus-terminal",
            {"row_id": by_pid[801]["row_id"]},
            {"X-MechBay-Token": tok, "Origin": "https://attacker.example.invalid"},
        )
        self.assertEqual(st_orig, 403)
        self.assertEqual(json.loads(b_orig.decode("utf-8"))["error"], "forbidden_origin")
        st_bad_json, _, b_bad_json = self._post_json(
            "/api/focus-terminal",
            b"{not-json",
            {"X-MechBay-Token": tok},
        )
        self.assertEqual(st_bad_json, 400)
        self.assertEqual(json.loads(b_bad_json.decode("utf-8"))["error"], "invalid_json_body")
        st_bad_id, _, b_bad_id = self._post_json(
            "/api/focus-terminal",
            {"row_id": "bad\nrow"},
            {"X-MechBay-Token": tok},
        )
        self.assertEqual(st_bad_id, 400)
        self.assertEqual(json.loads(b_bad_id.decode("utf-8"))["error"], "invalid_row_id")

        # Non-M1 (M5) and M1-over-SSH sessions are rejected with 403 m1_local_sessions_only without running subprocesses.
        with mock.patch.object(DASH.subprocess, "run") as mock_run:
            for denied_pid in (901, 902, 903):
                st_m5, _, b_m5 = self._post_json(
                    "/api/focus-terminal",
                    {"row_id": by_pid[denied_pid]["row_id"]},
                    {"X-MechBay-Token": tok},
                )
                self.assertEqual(st_m5, 403)
                self.assertEqual(json.loads(b_m5.decode("utf-8"))["error"], "m1_local_sessions_only")
            mock_run.assert_not_called()

            # Headless M1 process rejected with 409 session_has_no_terminal_window.
            st_proc, _, b_proc = self._post_json(
                "/api/focus-terminal",
                {"row_id": by_pid[803]["row_id"]},
                {"X-MechBay-Token": tok},
            )
            self.assertEqual(st_proc, 409)
            self.assertEqual(json.loads(b_proc.decode("utf-8"))["error"], "session_has_no_terminal_window")
            mock_run.assert_not_called()

        # SSH environment blocks GUI Terminal switching.
        with mock.patch.object(DASH.sys, "platform", "darwin"), mock.patch.dict(DASH.os.environ, {"SSH_CONNECTION": "1 2 3 4"}, clear=False):
            st_ssh_env, _, b_ssh_env = self._post_json(
                "/api/focus-terminal",
                {"row_id": by_pid[801]["row_id"]},
                {"X-MechBay-Token": tok},
            )
            self.assertEqual(st_ssh_env, 409)
            self.assertEqual(json.loads(b_ssh_env.decode("utf-8"))["error"], "local_macos_gui_required")

        clean_env = {k: v for k, v in DASH.os.environ.items() if k not in ("SSH_CONNECTION", "SSH_TTY")}

        # Refuses to cold-launch Terminal.app when absent from ps output.
        def fake_run_no_term(cmd, **_kwargs):
            self.assertEqual(cmd[0], "/bin/ps")
            return subprocess.CompletedProcess(cmd, 0, stdout="/sbin/launchd\n", stderr="")

        with mock.patch.object(DASH.sys, "platform", "darwin"), \
             mock.patch.dict(DASH.os.environ, clean_env, clear=True), \
             mock.patch.object(DASH.subprocess, "run", side_effect=fake_run_no_term):
            st_noterm, _, b_noterm = self._post_json(
                "/api/focus-terminal",
                {"row_id": by_pid[801]["row_id"]},
                {"X-MechBay-Token": tok},
            )
            self.assertEqual(st_noterm, 409)
            self.assertEqual(json.loads(b_noterm.decode("utf-8"))["error"], "terminal_not_running")

        # Live M1 Terminal session succeeds and passes structured JSON argv to osascript.
        seen_osascript_args = []

        def fake_run_ok(cmd, **_kwargs):
            if cmd[0] == "/bin/ps":
                return subprocess.CompletedProcess(
                    cmd, 0, stdout="/System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal\n", stderr=""
                )
            if Path(cmd[0]).name == "tmux":
                if cmd[1] == "list-panes":
                    return subprocess.CompletedProcess(
                        cmd,
                        0,
                        stdout="%3\t/dev/ttys012\t@3\txmb-primary\n%3\t/dev/ttys012\t@3\txmb-view-primary\n",
                        stderr="",
                    )
                if cmd[1] == "list-clients":
                    return subprocess.CompletedProcess(
                        cmd,
                        0,
                        stdout="/dev/ttys040\txmb-primary\t@3\t%3\n/dev/ttys041\txmb-view-primary\t@3\t%3\n",
                        stderr="",
                    )
            if cmd[0] == "/usr/bin/osascript":
                req_obj = json.loads(cmd[-1])
                seen_osascript_args.append(req_obj)
                return subprocess.CompletedProcess(
                    cmd,
                    0,
                    stdout=json.dumps({
                        "ok": True,
                        "window_id": req_obj["window_id"] or 777,
                        "tab_index": req_obj["tab_index"] or 1,
                        "tty": req_obj["ttys"][0],
                        "front_window_id": req_obj["window_id"] or 777,
                    }),
                    stderr="",
                )
            raise AssertionError("Unexpected command: " + repr(cmd))

        with mock.patch.object(DASH.sys, "platform", "darwin"), \
             mock.patch.dict(DASH.os.environ, clean_env, clear=True), \
             mock.patch.object(DASH, "_find_local_tmux_bin", return_value="/opt/homebrew/bin/tmux"), \
             mock.patch.object(DASH.subprocess, "run", side_effect=fake_run_ok):
            st_ok1, _, b_ok1 = self._post_json(
                "/api/focus-terminal",
                {"row_id": by_pid[801]["row_id"]},
                {"X-MechBay-Token": tok},
            )
            self.assertEqual(st_ok1, 200)
            res1 = json.loads(b_ok1.decode("utf-8"))
            self.assertTrue(res1["ok"])
            self.assertEqual((res1["window_id"], res1["tab_index"], res1["tty"]), (601, 2, "ttys011"))
            self.assertEqual(seen_osascript_args[-1], {"ttys": ["ttys011"], "window_id": 601, "tab_index": 2})

            # M1 tmux session prioritizes attached xmb-view-* client tty first.
            st_ok2, _, b_ok2 = self._post_json(
                "/api/focus-terminal",
                {"row_id": by_pid[802]["row_id"]},
                {"X-MechBay-Token": tok},
            )
            self.assertEqual(st_ok2, 200)
            res2 = json.loads(b_ok2.decode("utf-8"))
            self.assertEqual(res2["tty"], "ttys041")
            self.assertEqual(seen_osascript_args[-1], {"ttys": ["ttys041", "ttys040"], "window_id": None, "tab_index": None})

    def test_review_regressions_tmux_group_host_identity_tcc_and_timeouts(self):
        from unittest import mock

        # F3 (#245): M1 host matcher avoids false positives (mac-1, mini-1, m4-1, m5_1) and false negatives (local_m1, m1max).
        for false_key, expected_glyph in (("mac-1", "MA1"), ("mini-1", "MI1"), ("m4-1", "M41"), ("m5_1", "M5"), ("in", "INH")):
            sym = DASH.host_symbol(false_key, "Workstation")
            self.assertEqual(sym["glyph"], expected_glyph)
            self.assertFalse(DASH.is_m1_host(false_key, "Workstation"))
            fake_report = {
                "key": false_key,
                "label": "Workstation",
                "source": "local",
                "reachable": True,
                "observed_at": "2026-01-01T12:00:00+00:00",
                "sessions": [{
                    "agent": "codex",
                    "pid": 301,
                    "tty": "ttys003",
                    "started_at": "2026-01-01T10:00:00+00:00",
                    "age_seconds": 60,
                    "role": "mech",
                    "project_label": "TraitMech",
                    "repo_label": "TraitMech",
                    "status": "Working",
                    "target": {"transport": "terminal", "window_id": 10, "tab": 1},
                    "flags": [],
                    "errors": [],
                }],
                "errors": [],
                "coverage": {"processes": "complete", "agent_discovery": "complete", "screens_read": 1, "sessions_seen": 1},
                "health": {"status": "ok", "process_status": "ok"},
                "system": {"arch": "arm64", "cpu_count": 8, "loadavg_1m": 1.0},
            }
            p_non_m1 = DASH.build_dashboard_payload([fake_report], mode="live")
            self.assertFalse(p_non_m1["sessions"][0]["can_focus_terminal"])

        for m1_key, m1_label in (("local_m1", "Local"), ("m1_max", "Studio"), ("box", "Apple M1 Pro"), ("m1max", "Host")):
            self.assertTrue(DASH.is_m1_host(m1_key, m1_label))
            self.assertEqual(DASH.host_symbol(m1_key, m1_label)["glyph"], "M1")

        # F4 (#246): terminal session without a valid tty is NOT focusable even if window_id is present.
        no_tty_m1 = {
            "key": "m1",
            "label": "M1 Workstation",
            "source": "local",
            "reachable": True,
            "observed_at": "2026-01-01T12:00:00+00:00",
            "sessions": [{
                "agent": "codex",
                "pid": 401,
                "tty": "??",
                "started_at": "2026-01-01T10:00:00+00:00",
                "age_seconds": 60,
                "role": "mech",
                "project_label": "TraitMech",
                "repo_label": "TraitMech",
                "status": "Working",
                "target": {"transport": "terminal", "window_id": 601, "tab": 1},
                "flags": [],
                "errors": [],
            }],
            "errors": [],
            "coverage": {"processes": "complete", "agent_discovery": "complete", "screens_read": 1, "sessions_seen": 1},
            "health": {"status": "ok", "process_status": "ok"},
            "system": {"arch": "arm64", "cpu_count": 10, "loadavg_1m": 1.0},
        }
        self.assertFalse(DASH.build_dashboard_payload([no_tty_m1], mode="live")["sessions"][0]["can_focus_terminal"])

        # F2 (#244): _find_local_tmux_bin checks /opt/homebrew/bin/tmux before falling back to PATH.
        with mock.patch.object(DASH.os.path, "isfile", side_effect=lambda p: p == "/opt/homebrew/bin/tmux"), \
             mock.patch.object(DASH.os, "access", side_effect=lambda p, _m: p == "/opt/homebrew/bin/tmux"), \
             mock.patch.object(DASH.shutil, "which", return_value=None):
            self.assertEqual(DASH._find_local_tmux_bin(), "/opt/homebrew/bin/tmux")

        # F1 (#243): Grouped xmb-view-* >8 sessions selects ONLY the client viewing target pane/window,
        # and returns 409 tmux_window_not_visible_in_terminal when attached viewers show a different window.
        many_viewers = ["fleet"] + [f"xmb-view-m{idx:02d}" for idx in range(12)]
        tmux_m1_report = {
            "key": "m1",
            "label": "M1 Workstation",
            "source": "local",
            "reachable": True,
            "observed_at": "2026-01-01T12:00:00+00:00",
            "sessions": [
                {
                    "agent": "claude",
                    "pid": 501,
                    "tty": "ttys061",
                    "started_at": "2026-01-01T10:00:00+00:00",
                    "age_seconds": 300,
                    "role": "claw",
                    "project_label": "sample-claw",
                    "repo_label": "sample-claw",
                    "status": "APPROVAL",
                    "target": {
                        "transport": "tmux",
                        "pane": "%11",
                        "window": "claw-win",
                        "tmux_session": "fleet",
                        "tmux_sessions": many_viewers,
                    },
                    "flags": ["interactive-prompt"],
                    "errors": [],
                },
                {
                    "agent": "codex",
                    "pid": 502,
                    "tty": "ttys062",
                    "started_at": "2026-01-01T10:01:00+00:00",
                    "age_seconds": 300,
                    "role": "mech",
                    "project_label": "AssayMech",
                    "repo_label": "AssayMech",
                    "status": "Working",
                    "target": {
                        "transport": "tmux",
                        "pane": "%12",
                        "window": "assay-win",
                        "tmux_session": "fleet",
                        "tmux_sessions": many_viewers,
                    },
                    "flags": [],
                    "errors": [],
                },
            ],
            "errors": [],
            "coverage": {"processes": "complete", "agent_discovery": "complete", "screens_read": 2, "sessions_seen": 2},
            "health": {"status": "ok", "process_status": "ok"},
            "system": {"arch": "arm64", "cpu_count": 10, "loadavg_1m": 1.0},
        }
        self.server.state = DASH.DashboardState(lambda: ([tmux_m1_report], {"mode": "live", "role_overrides": None}))
        tok = self.server.state.csrf_token
        rows_by_pid = {r["pid"]: r for r in self.server.state.get_payload()["sessions"]}
        self.assertEqual(len(rows_by_pid[501]["tmux_sessions"]), 8)  # UI display stays bounded
        self.assertEqual(len(rows_by_pid[501]["tmux_lookup_sessions"]), 13)  # Server lookup preserves all 13

        clean_env = {k: v for k, v in DASH.os.environ.items() if k not in ("SSH_CONNECTION", "SSH_TTY")}
        seen_ttys = []

        def fake_grouped_tmux(cmd, **_kwargs):
            if cmd[0] == "/bin/ps":
                return subprocess.CompletedProcess(
                    cmd, 0, stdout="/System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal\n", stderr=""
                )
            if Path(cmd[0]).name == "tmux":
                if cmd[1] == "list-panes":
                    return subprocess.CompletedProcess(
                        cmd,
                        0,
                        stdout=(
                            "%10\t/dev/ttys060\t@10\tfleet\n"
                            "%11\t/dev/ttys061\t@11\tfleet\n"
                            "%11\t/dev/ttys061\t@11\txmb-view-m11\n"
                            "%12\t/dev/ttys062\t@12\tfleet\n"
                        ),
                        stderr="",
                    )
                if cmd[1] == "list-clients":
                    # xmb-view-m00 sorts FIRST but is viewing @10 (%10); xmb-view-m11 (past [:8]) is viewing @11 (%11);
                    # NO client is viewing @12 (%12)!
                    return subprocess.CompletedProcess(
                        cmd,
                        0,
                        stdout=(
                            "/dev/ttys070\txmb-view-m00\t@10\t%10\n"
                            "/dev/ttys071\txmb-view-m11\t@11\t%11\n"
                        ),
                        stderr="",
                    )
            if cmd[0] == "/usr/bin/osascript":
                req_obj = json.loads(cmd[-1])
                seen_ttys.append(req_obj["ttys"])
                return subprocess.CompletedProcess(
                    cmd,
                    0,
                    stdout=json.dumps({
                        "ok": True,
                        "window_id": 901,
                        "tab_index": 1,
                        "tty": req_obj["ttys"][0],
                        "front_window_id": 901,
                    }),
                    stderr="",
                )
            raise AssertionError("Unexpected cmd: " + repr(cmd))

        with mock.patch.object(DASH.sys, "platform", "darwin"), \
             mock.patch.dict(DASH.os.environ, clean_env, clear=True), \
             mock.patch.object(DASH, "_find_local_tmux_bin", return_value="/opt/homebrew/bin/tmux"), \
             mock.patch.object(DASH.subprocess, "run", side_effect=fake_grouped_tmux):
            # Clicking pid 501 (%11) selects ttys071 (xmb-view-m11), NEVER ttys070 (xmb-view-m00)!
            st_501, _, b_501 = self._post_json(
                "/api/focus-terminal",
                {"row_id": rows_by_pid[501]["row_id"]},
                {"X-MechBay-Token": tok},
            )
            self.assertEqual(st_501, 200)
            self.assertEqual(json.loads(b_501.decode("utf-8"))["tty"], "ttys071")
            self.assertEqual(seen_ttys[-1], ["ttys071"])

            # Clicking pid 502 (%12, @12) refuses with 409 tmux_window_not_visible_in_terminal instead of focusing @10/@11!
            st_502, _, b_502 = self._post_json(
                "/api/focus-terminal",
                {"row_id": rows_by_pid[502]["row_id"]},
                {"X-MechBay-Token": tok},
            )
            self.assertEqual(st_502, 409)
            self.assertEqual(json.loads(b_502.decode("utf-8"))["error"], "tmux_window_not_visible_in_terminal")

        # F6 (#248) & F4 (#246): TCC -1743 -> 403, TimeoutExpired -> 504, un-raised window -> 502.
        term_m1_report = dict(no_tty_m1, sessions=[dict(no_tty_m1["sessions"][0], pid=601, tty="ttys019")])
        self.server.state = DASH.DashboardState(lambda: ([term_m1_report], {"mode": "live", "role_overrides": None}))
        tok2 = self.server.state.csrf_token
        row_601 = self.server.state.get_payload()["sessions"][0]

        def fake_tcc_denied(cmd, **_kwargs):
            if cmd[0] == "/bin/ps":
                return subprocess.CompletedProcess(
                    cmd, 0, stdout="/System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal\n", stderr=""
                )
            return subprocess.CompletedProcess(
                cmd, 1, stdout="", stderr="execution error: Not authorized to send Apple events to Terminal. (-1743)\n"
            )

        with mock.patch.object(DASH.sys, "platform", "darwin"), \
             mock.patch.dict(DASH.os.environ, clean_env, clear=True), \
             mock.patch.object(DASH.subprocess, "run", side_effect=fake_tcc_denied):
            st_tcc, _, b_tcc = self._post_json("/api/focus-terminal", {"row_id": row_601["row_id"]}, {"X-MechBay-Token": tok2})
            self.assertEqual(st_tcc, 403)
            self.assertEqual(json.loads(b_tcc.decode("utf-8"))["error"], "terminal_automation_not_authorized")

        def fake_timeout(cmd, **_kwargs):
            if cmd[0] == "/bin/ps":
                return subprocess.CompletedProcess(
                    cmd, 0, stdout="/System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal\n", stderr=""
                )
            raise subprocess.TimeoutExpired(cmd, 5)

        with mock.patch.object(DASH.sys, "platform", "darwin"), \
             mock.patch.dict(DASH.os.environ, clean_env, clear=True), \
             mock.patch.object(DASH.subprocess, "run", side_effect=fake_timeout):
            st_to, _, b_to = self._post_json("/api/focus-terminal", {"row_id": row_601["row_id"]}, {"X-MechBay-Token": tok2})
            self.assertEqual(st_to, 504)
            self.assertEqual(json.loads(b_to.decode("utf-8"))["error"], "terminal_focus_timed_out")

        def fake_not_raised(cmd, **_kwargs):
            if cmd[0] == "/bin/ps":
                return subprocess.CompletedProcess(
                    cmd, 0, stdout="/System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal\n", stderr=""
                )
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=json.dumps({"ok": True, "window_id": 601, "tab_index": 1, "tty": "ttys019", "front_window_id": 999}),
                stderr="",
            )

        with mock.patch.object(DASH.sys, "platform", "darwin"), \
             mock.patch.dict(DASH.os.environ, clean_env, clear=True), \
             mock.patch.object(DASH.subprocess, "run", side_effect=fake_not_raised):
            st_nr, _, b_nr = self._post_json("/api/focus-terminal", {"row_id": row_601["row_id"]}, {"X-MechBay-Token": tok2})
            self.assertEqual(st_nr, 502)
            self.assertEqual(json.loads(b_nr.decode("utf-8"))["error"], "terminal_window_not_raised")

    def test_terminal_focus_jxa_logic_and_no_input_primitives(self):
        # Static safety guard: dashboard never invokes AppleScript/JXA 'do script', 'doScript', or 'System Events'.
        dash_source = (SCRIPTS / "fleet_dashboard.py").read_text(encoding="utf-8")
        self.assertNotRegex(DASH.TERMINAL_FOCUS_JXA, r"\bdoScript\b|\bdo\s+script\b|\bkeystroke\b|\bSystem\s+Events\b")
        self.assertNotRegex(dash_source, r"\bdo\s+script\b|\bdoScript\b|\bSystem\s+Events\b")

        node_bin = shutil.which("node")
        if not node_bin:
            self.skipTest("node binary not available for JXA VM test")

        jxa_driver = r"""
const fs = require("fs");
const vm = require("vm");
const jxaSrc = fs.readFileSync(process.argv[2], "utf8");

function runScenario(windowsModel, running, reqObj) {
  let activated = 0;
  const winObjs = windowsModel.map(w => {
    const tabObjs = w.tabs.map(t => {
      const tabObj = { tty: () => t.tty };
      Object.defineProperty(tabObj, "selected", {
        get: () => () => Boolean(t.selected),
        set: v => { t.selected = Boolean(v); },
      });
      return tabObj;
    });
    const winObj = {
      id: () => w.id,
      tabs: () => tabObjs,
      set index(v) {
        w.index = v;
        if (v === 1) {
          const pos = winObjs.indexOf(winObj);
          if (pos > 0) {
            winObjs.splice(pos, 1);
            winObjs.unshift(winObj);
          }
        }
      },
      set frontmost(v) { w.frontmost = Boolean(v); },
    };
    Object.defineProperty(winObj, "miniaturized", {
      get: () => () => Boolean(w.minimized),
      set: v => { w.minimized = Boolean(v); },
    });
    return winObj;
  });
  const app = {
    running: () => running,
    windows: () => winObjs,
    activate() { activated += 1; },
  };
  const ctx = vm.createContext({
    Application: name => {
      if (name !== "Terminal") throw new Error("unexpected app: " + name);
      return app;
    },
    JSON, Array, Number, String, Math,
  });
  vm.runInContext(jxaSrc, ctx);
  const out = JSON.parse(ctx.run([JSON.stringify(reqObj)]));
  return { out, activated, windowsModel };
}

// 1. Multi-tab window selects exact tab 2, un-minimizes, raises window, activates Terminal.
const s1 = runScenario(
  [
    { id: 100, minimized: false, tabs: [{ tty: "/dev/ttys001", selected: true }] },
    { id: 400, minimized: true, tabs: [
      { tty: "/dev/ttys010", selected: true },
      { tty: "/dev/ttys011", selected: false },
      { tty: "/dev/ttys012", selected: false },
    ] },
  ],
  true,
  { ttys: ["ttys011"], window_id: 400, tab_index: 2 }
);
if (!s1.out.ok || s1.out.window_id !== 400 || s1.out.tab_index !== 2 || s1.out.tty !== "ttys011" || s1.out.front_window_id !== 400) {
  throw new Error("s1 unexpected output: " + JSON.stringify(s1.out));
}
if (s1.windowsModel[1].minimized !== false || !s1.windowsModel[1].tabs[1].selected || s1.activated !== 1) {
  throw new Error("s1 did not unminimize/select/activate");
}

// 2. Transient recycled window_id: if ttys042 closed and window 400 now hosts ttys099, MUST refuse rather than focusing wrong session!
const s2 = runScenario(
  [{ id: 400, minimized: false, tabs: [{ tty: "/dev/ttys099", selected: true }] }],
  true,
  { ttys: ["ttys042"], window_id: 400, tab_index: 1 }
);
if (s2.out.ok || s2.out.error !== "terminal_tab_not_found" || s2.activated !== 0) {
  throw new Error("s2 should refuse mismatched tty on recycled window_id: " + JSON.stringify(s2));
}

// 3. Tab dragged to a new window (window_id changed from 400 -> 888): matches live tty ttys011 in window 888!
const s3 = runScenario(
  [
    { id: 400, minimized: false, tabs: [{ tty: "/dev/ttys010", selected: true }] },
    { id: 888, minimized: false, tabs: [{ tty: "/dev/ttys011", selected: true }] },
  ],
  true,
  { ttys: ["ttys011"], window_id: 400, tab_index: 2 }
);
if (!s3.out.ok || s3.out.window_id !== 888 || s3.out.tty !== "ttys011" || s3.out.front_window_id !== 888) {
  throw new Error("s3 failed to follow dragged tty to window 888: " + JSON.stringify(s3.out));
}

// 4. Empty ttys list is rejected even if window_id is supplied (F4 #246: no tty-less window_id fallback).
const s4 = runScenario(
  [{ id: 400, minimized: false, tabs: [{ tty: "/dev/ttys010", selected: true }] }],
  true,
  { ttys: [], window_id: 400, tab_index: 1 }
);
if (s4.out.ok || s4.out.error !== "missing_focus_tty" || s4.activated !== 0) {
  throw new Error("s4 should reject empty ttys without touching window 400: " + JSON.stringify(s4));
}

console.log("JXA_FOCUS_LOGIC_OK");
"""
        with tempfile.TemporaryDirectory() as tmp:
            jxa_p = Path(tmp) / "focus.jxa.js"
            drv_p = Path(tmp) / "driver.js"
            jxa_p.write_text(DASH.TERMINAL_FOCUS_JXA, encoding="utf-8")
            drv_p.write_text(jxa_driver, encoding="utf-8")
            proc = subprocess.run([node_bin, str(drv_p), str(jxa_p)], capture_output=True, text=True, timeout=10)
            self.assertEqual(proc.returncode, 0, msg=proc.stderr or proc.stdout)
            self.assertIn("JXA_FOCUS_LOGIC_OK", proc.stdout)

    def test_client_js_m1_row_click_focuses_terminal_and_non_m1_toggles_only(self):
        node_bin = shutil.which("node")
        if not node_bin:
            self.skipTest("node binary not available for client JS execution test")

        m1_host = {
            "key": "m1",
            "label": "M1 Workstation",
            "source": "local",
            "reachable": True,
            "observed_at": "2026-01-01T12:00:00+00:00",
            "sessions": [
                {
                    "agent": "agy",
                    "pid": 101,
                    "tty": "ttys019",
                    "started_at": "2026-01-01T10:00:00+00:00",
                    "age_seconds": 1200,
                    "role": "mech",
                    "project_label": "CultureMech",
                    "repo_label": "CultureMech",
                    "status": "APPROVAL",
                    "activity": "Approve test run?",
                    "target": {"transport": "terminal", "window_id": 601, "tab": 2},
                    "flags": [],
                    "errors": [],
                },
                {
                    "agent": "claude",
                    "pid": 102,
                    "tty": "??",
                    "started_at": "2026-01-01T10:05:00+00:00",
                    "age_seconds": 600,
                    "role": "mechbay",
                    "project_label": "XMechBay",
                    "repo_label": "XMechBay",
                    "status": "Working",
                    "activity": "Headless worker",
                    "target": {"transport": "process"},
                    "flags": [],
                    "errors": [],
                },
            ],
            "errors": [],
            "coverage": {"processes": "complete", "agent_discovery": "complete", "screens_read": 2, "sessions_seen": 2},
            "health": {"status": "ok", "process_status": "ok", "occupancy_ratio": 0.05, "age_seconds": 5},
            "system": {"arch": "arm64", "cpu_count": 10, "loadavg_1m": 1.2, "disk_free_gib": 200.0},
        }
        m5_host = {
            "key": "m5",
            "label": "M5 Laptop",
            "source": "ssh",
            "reachable": True,
            "observed_at": "2026-01-01T12:00:00+00:00",
            "sessions": [
                {
                    "agent": "codex",
                    "pid": 201,
                    "tty": "ttys004",
                    "started_at": "2026-01-01T09:30:00+00:00",
                    "age_seconds": 2400,
                    "role": "mech",
                    "project_label": "EdgeMech",
                    "repo_label": "EdgeMech",
                    "status": "QUESTION",
                    "activity": "Select option on M5",
                    "target": {"transport": "terminal", "window_id": 55, "tab": 1},
                    "flags": [],
                    "errors": [],
                },
            ],
            "errors": [],
            "coverage": {"processes": "complete", "agent_discovery": "complete", "screens_read": 1, "sessions_seen": 1},
            "health": {"status": "ok", "process_status": "ok", "occupancy_ratio": 0.04, "age_seconds": 5},
            "system": {"arch": "arm64", "cpu_count": 16, "loadavg_1m": 2.1, "disk_free_gib": 300.0},
        }
        live_payload = DASH.build_dashboard_payload([m1_host, m5_host], mode="live")
        live_html = DASH.render_dashboard_html(live_payload, csrf_token="tok_m1_live", static_export=False)
        static_html = DASH.render_dashboard_html(live_payload, csrf_token="", static_export=True)
        live_js = re.findall(r"<script>(.*?)</script>", live_html, re.S)[0]
        static_js = re.findall(r"<script>(.*?)</script>", static_html, re.S)[0]

        harness = r"""
const fs = require("fs");
const vm = require("vm");
const liveSrc = fs.readFileSync(process.argv[2], "utf8");
const staticSrc = fs.readFileSync(process.argv[3], "utf8");
const payloadObj = JSON.parse(fs.readFileSync(process.argv[4], "utf8"));

function findNodes(node, pred, acc = []) {
  if (!node) return acc;
  if (pred(node)) acc.push(node);
  for (const ch of node.children || []) findNodes(ch, pred, acc);
  return acc;
}
function allText(node) {
  if (!node) return "";
  let out = node.textContent || "";
  for (const ch of node.children || []) out += " " + allText(ch);
  return out;
}
function makeNode(tag, attrs = {}) {
  const listeners = {};
  const classes = new Set((attrs.className || "").split(/\s+/).filter(Boolean));
  const attrMap = Object.assign({}, attrs);
  const node = {
    tagName: String(tag || "DIV").toUpperCase(),
    id: attrs.id || "",
    textContent: attrs.textContent || "",
    value: attrs.value || "",
    checked: Boolean(attrs.checked),
    disabled: false,
    open: false,
    style: {},
    children: [],
    get className() { return Array.from(classes).join(" "); },
    set className(val) {
      classes.clear();
      String(val || "").split(/\s+/).filter(Boolean).forEach(c => classes.add(c));
    },
    classList: {
      add: (...cs) => cs.forEach(c => classes.add(c)),
      remove: (...cs) => cs.forEach(c => classes.delete(c)),
      contains: c => classes.has(c),
    },
    setAttribute(k, v) {
      attrMap[k] = String(v);
      if (k === "id") this.id = String(v);
      if (k === "open") this.open = true;
    },
    getAttribute(k) { return Object.prototype.hasOwnProperty.call(attrMap, k) ? attrMap[k] : null; },
    removeAttribute(k) { delete attrMap[k]; if (k === "open") this.open = false; },
    addEventListener(ev, fn) { (listeners[ev] = listeners[ev] || []).push(fn); },
    click(evExtra = {}) {
      const ev = Object.assign({ currentTarget: node, target: node, preventDefault() {}, stopPropagation() {} }, evExtra);
      (listeners.click || []).forEach(fn => fn(ev));
    },
    keydown(key, evExtra = {}) {
      const ev = Object.assign({ key, currentTarget: node, target: node, preventDefault() {}, stopPropagation() {} }, evExtra);
      (listeners.keydown || []).forEach(fn => fn(ev));
    },
    contains(other) {
      if (!other) return false;
      if (other === this) return true;
      for (const ch of this.children || []) {
        if (ch === other || (typeof ch.contains === "function" && ch.contains(other))) return true;
      }
      return false;
    },
    appendChild(child) { this.children.push(child); return child; },
    replaceChildren(...kids) { this.children = kids.filter(Boolean); },
    focus() { if (this._ownerDoc) this._ownerDoc.activeElement = this; },
    blur() { if (this._ownerDoc && this._ownerDoc.activeElement === this) this._ownerDoc.activeElement = null; },
    scrollIntoView() {},
  };
  return node;
}

function boot(src, storageMap, fetchImpl, selectionFactory = null) {
  const byId = {
    "initial-payload": makeNode("script", { textContent: JSON.stringify(payloadObj) }),
    "mode-badge": makeNode("span"), "stamp-line": makeNode("div"),
    "btn-update": makeNode("button"), "btn-update-label": makeNode("span", { textContent: "↻ Update" }),
    "select-autorefresh": makeNode("select", { value: "0" }), "autorefresh-countdown": makeNode("span"),
    "link-export": makeNode("a"), "btn-theme": makeNode("button", { textContent: "Theme: Auto" }),
    "error-toast": makeNode("div"), "human-input-banner": makeNode("section"),
    "host-cards": makeNode("section"), "fleet-rollup": makeNode("section"),
    "attention-banner": makeNode("section"), "mac-filters": makeNode("div"),
    "filter-search": makeNode("input", { value: "" }),
    "cached-toggle-wrap": makeNode("label"), "toggle-cached": makeNode("input", { checked: true }),
    "tables-container": makeNode("main"),
  };
  const viewBtns = ["role", "host", "matrix"].map(v => makeNode("button", { "data-view": v, "aria-selected": String(v === "role") }));
  const filterBtns = ["all", "input", "active", "alert", "changed", "idle", "unknown"].map(f => makeNode("button", { "data-filter": f, "aria-pressed": String(f === "all") }));
  let reloadCount = 0;
  const sessionStorage = {
    getItem: k => (storageMap.has(k) ? storageMap.get(k) : null),
    setItem: (k, v) => storageMap.set(k, String(v)),
    removeItem: k => storageMap.delete(k),
  };
  const doc = {
    title: "", documentElement: makeNode("html"), activeElement: null,
    getElementById(id) {
      if (byId[id]) return byId[id];
      for (const root of Object.values(byId)) {
        const hits = findNodes(root, n => n.id === id);
        if (hits.length) return hits[0];
      }
      return null;
    },
    querySelectorAll: sel => (sel === "[data-view]" ? viewBtns : (sel === "[data-filter]" ? filterBtns : [])),
    createElement: t => { const n = makeNode(t); n._ownerDoc = doc; return n; },
    createTextNode: s => ({ textContent: String(s) }),
    addEventListener() {},
  };
  Object.values(byId).forEach(n => { n._ownerDoc = doc; });
  viewBtns.forEach(n => { n._ownerDoc = doc; });
  filterBtns.forEach(n => { n._ownerDoc = doc; });
  const getSel = selectionFactory || (() => ({ isCollapsed: true, anchorNode: null, focusNode: null, toString: () => "" }));
  vm.runInContext(src, vm.createContext({
    window: { location: { protocol: "http:", reload() { reloadCount += 1; } }, sessionStorage, getSelection: getSel },
    document: doc, sessionStorage, fetch: fetchImpl,
    setInterval: () => 1, setTimeout: () => 1,
    Date, Math, Number, String, Boolean, Array, Object, Set, JSON, RegExp, Error, parseInt, isNaN,
  }));
  return { byId, doc, viewBtns, getReloadCount: () => reloadCount };
}

(async () => {
  const focusCalls = [];
  const storage = new Map();
  const page = boot(liveSrc, storage, async (url, opts) => {
    focusCalls.push({ url, token: opts.headers["X-MechBay-Token"], body: JSON.parse(opts.body) });
    return { ok: true, status: 200, json: async () => ({ ok: true, window_id: 601, tab_index: 2, tty: "ttys019" }) };
  });

  const m1RowId = "row-m1:101:2026-01-01T10:00:00+00:00";
  const m1ProcRowId = "row-m1:102:2026-01-01T10:05:00+00:00";
  const m5RowId = "row-m5:201:2026-01-01T09:30:00+00:00";

  const m1Row = page.doc.getElementById(m1RowId);
  const m1ProcRow = page.doc.getElementById(m1ProcRowId);
  const m5Row = page.doc.getElementById(m5RowId);
  if (!m1Row.classList.contains("is-m1-focusable")) throw new Error("m1 terminal row missing is-m1-focusable");
  if (m1ProcRow.classList.contains("is-m1-focusable")) throw new Error("m1 headless process row should not be focusable");
  if (m5Row.classList.contains("is-m1-focusable")) throw new Error("m5 row must NOT be is-m1-focusable");
  if (!allText(m1Row).includes("T601/2 ↗")) throw new Error("m1 row missing ↗ link badge");
  if (allText(m5Row).includes("↗")) throw new Error("m5 row must not show ↗ link badge");

  // 1. Clicking M1 row calls POST /api/focus-terminal without expanding detail drawer.
  m1Row.click();
  await new Promise(r => setImmediate(r));
  if (focusCalls.length !== 1 || focusCalls[0].url !== "/api/focus-terminal" || focusCalls[0].body.row_id !== "m1:101:2026-01-01T10:00:00+00:00") {
    throw new Error("unexpected focusCalls after M1 row click: " + JSON.stringify(focusCalls));
  }
  const m1RowAfter = page.doc.getElementById(m1RowId);
  if (m1RowAfter.getAttribute("aria-expanded") !== "false") throw new Error("M1 row click should switch terminal without forcing detail expansion");

  // 2. Clicking M1 row's ▸ chevron button toggles details WITHOUT calling /api/focus-terminal.
  const expandBtn = findNodes(m1RowAfter, n => n.classList && n.classList.contains("row-expand-btn"))[0];
  if (!expandBtn) throw new Error("missing row-expand-btn on M1 row");
  expandBtn.click();
  await new Promise(r => setImmediate(r));
  if (focusCalls.length !== 1) throw new Error("clicking row-expand-btn must not call /api/focus-terminal");
  const m1RowExpanded = page.doc.getElementById(m1RowId);
  if (m1RowExpanded.getAttribute("aria-expanded") !== "true") throw new Error("row-expand-btn failed to expand M1 row");

  // 3. Clicking '↗ Switch to Terminal' button inside expanded detail drawer calls /api/focus-terminal.
  const detailFocusBtn = findNodes(page.byId["tables-container"], n => n.classList && n.classList.contains("detail-focus-btn"))[0];
  if (!detailFocusBtn) throw new Error("missing detail-focus-btn inside expanded M1 detail row");
  detailFocusBtn.click();
  await new Promise(r => setImmediate(r));
  if (focusCalls.length !== 2) throw new Error("detail-focus-btn did not call /api/focus-terminal");

  // 4. Clicking M5 row, M1 headless process row, and M5 spotlight card NEVER calls /api/focus-terminal.
  page.doc.getElementById(m5RowId).click();
  page.doc.getElementById(m1ProcRowId).click();
  const cards = findNodes(page.byId["human-input-banner"], n => n.classList && n.classList.contains("human-input-card"));
  const m5Card = cards.find(c => allText(c).includes("EdgeMech"));
  const m1Card = cards.find(c => allText(c).includes("CultureMech"));
  m5Card.click();
  await new Promise(r => setImmediate(r));
  if (focusCalls.length !== 2) throw new Error("non-M1 or headless clicks must never call /api/focus-terminal");

  // Clicking M1 spotlight card highlights row AND calls /api/focus-terminal.
  m1Card.click();
  await new Promise(r => setImmediate(r));
  if (focusCalls.length !== 3) throw new Error("M1 spotlight card should also focus M1 terminal");

  // 5. Static HTML export disables M1 terminal focusing completely.
  const staticCalls = [];
  const staticPage = boot(staticSrc, new Map(), async url => { staticCalls.push(url); return { ok: true, status: 200, json: async () => ({}) }; });
  staticPage.doc.getElementById(m1RowId).click();
  await new Promise(r => setImmediate(r));
  if (staticCalls.length !== 0) throw new Error("STATIC_EXPORT must never call /api/focus-terminal");

  // 6. Stale CSRF token on M1 row click auto-reloads once and completes the M1 focus on the reloaded page.
  const restartStorage = new Map();
  const seenRestartTokens = [];
  const stalePage = boot(liveSrc, restartStorage, async (_url, opts) => {
    seenRestartTokens.push(opts.headers["X-MechBay-Token"]);
    return { ok: false, status: 403, json: async () => ({ error: "invalid_csrf_token" }) };
  });
  stalePage.doc.getElementById(m1RowId).click();
  await new Promise(r => setImmediate(r));
  if (stalePage.getReloadCount() !== 1) throw new Error("expected 1 auto-reload on stale CSRF focus click");

  const freshSrc = liveSrc.replace('const CSRF_TOKEN = "tok_m1_live";', 'const CSRF_TOKEN = "tok_m1_fresh";');
  boot(freshSrc, restartStorage, async (_url, opts) => {
    seenRestartTokens.push(opts.headers["X-MechBay-Token"]);
    return { ok: true, status: 200, json: async () => ({ ok: true, window_id: 601, tab_index: 2, tty: "ttys019" }) };
  });
  await new Promise(r => setImmediate(r));
  if (seenRestartTokens.join(",") !== "tok_m1_live,tok_m1_fresh") {
    throw new Error("expected post-reload M1 focus recovery, got: " + seenRestartTokens);
  }

  // 7. F5 (#247): Text selection outside the clicked row does NOT block M1 focus, while selection inside the row suppresses click.
  let activeSel = { isCollapsed: false, anchorNode: null, focusNode: null, toString: () => "copied text" };
  const selCalls = [];
  const selPage = boot(liveSrc, new Map(), async url => {
    selCalls.push(url);
    return { ok: true, status: 200, json: async () => ({ ok: true, window_id: 601, tab_index: 2, tty: "ttys019" }) };
  }, () => activeSel);
  activeSel.anchorNode = selPage.byId["human-input-banner"];
  selPage.doc.getElementById(m1RowId).click();
  await new Promise(r => setImmediate(r));
  if (selCalls.length !== 1) throw new Error("selection outside row should NOT suppress row click");

  const selM1Row = selPage.doc.getElementById(m1RowId);
  activeSel.anchorNode = selM1Row.children[0];
  selM1Row.click();
  await new Promise(r => setImmediate(r));
  if (selCalls.length !== 1) throw new Error("selection inside row MUST suppress row click while copying text");

  // 8. F7 (#249): Keyboard Space on focused M1 row toggles details and preserves document.activeElement across renderMain;
  // Matrix M1 pill tooltip includes Terminal switch cue while M5 pill does not.
  const kbRow = page.doc.getElementById(m1RowId);
  kbRow.focus();
  if (page.doc.activeElement !== kbRow) throw new Error("expected kbRow to be activeElement");
  kbRow.keydown(" ");
  if (!page.doc.activeElement || page.doc.activeElement.id !== m1RowId) {
    throw new Error("renderMain lost keyboard focus on M1 row after Space toggle");
  }
  const matrixBtn = page.viewBtns.find(b => b.getAttribute("data-view") === "matrix");
  matrixBtn.click();
  const matrixPills = findNodes(page.byId["tables-container"], n => n.classList && n.classList.contains("matrix-pill-btn"));
  const m1Pill = matrixPills.find(p => (p.getAttribute("title") || "").includes("CultureMech"));
  const m5Pill = matrixPills.find(p => (p.getAttribute("title") || "").includes("EdgeMech"));
  if (!m1Pill || !(m1Pill.getAttribute("title") || "").includes("Click to switch to M1 Terminal")) {
    throw new Error("M1 matrix pill missing switch tooltip cue");
  }
  if (!m5Pill || (m5Pill.getAttribute("title") || "").includes("Click to switch to M1 Terminal")) {
    throw new Error("M5 matrix pill must NOT show M1 switch tooltip cue");
  }

  // 9. F6 (#248): 403 terminal_automation_not_authorized shows Automation toast WITHOUT reload button.
  const tccPage = boot(liveSrc, new Map(), async () => ({
    ok: false, status: 403, json: async () => ({ error: "terminal_automation_not_authorized" })
  }));
  tccPage.doc.getElementById(m1RowId).click();
  await new Promise(r => setImmediate(r));
  const tccToastText = allText(tccPage.byId["error-toast"]);
  if (!tccToastText.includes("Privacy & Security") || tccToastText.includes("Reload page")) {
    throw new Error("expected TCC guidance without Reload button, got: " + tccToastText);
  }

  // 10. F7 (#249): Post-reload pendingFocusRowId for a session that exited during restart surfaces session_not_found toast.
  const goneStorage = new Map();
  const goneStale = boot(liveSrc, goneStorage, async () => ({
    ok: false, status: 403, json: async () => ({ error: "invalid_csrf_token" })
  }));
  goneStale.doc.getElementById(m1RowId).click();
  await new Promise(r => setImmediate(r));
  const emptyPayload = Object.assign({}, payloadObj, { sessions: [] });
  const goneFreshSrc = freshSrc.replace(
    'document.getElementById("initial-payload").textContent',
    JSON.stringify(JSON.stringify(emptyPayload))
  );
  const goneRecovered = boot(goneFreshSrc, goneStorage, async () => ({ ok: true, status: 200, json: async () => ({}) }));
  if (!allText(goneRecovered.byId["error-toast"]).includes("no longer in the server snapshot")) {
    throw new Error("expected session_not_found toast when pending focus row exited before restart completed");
  }

  console.log("CLIENT_JS_M1_FOCUS_OK");
})();
"""
        with tempfile.TemporaryDirectory() as tmp:
            live_p = Path(tmp) / "live.js"
            stat_p = Path(tmp) / "static.js"
            pay_p = Path(tmp) / "payload.json"
            har_p = Path(tmp) / "harness.js"
            live_p.write_text(live_js, encoding="utf-8")
            stat_p.write_text(static_js, encoding="utf-8")
            pay_p.write_text(json.dumps(live_payload), encoding="utf-8")
            har_p.write_text(harness, encoding="utf-8")
            proc = subprocess.run(
                [node_bin, str(har_p), str(live_p), str(stat_p), str(pay_p)],
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stderr or proc.stdout)
            self.assertIn("CLIENT_JS_M1_FOCUS_OK", proc.stdout)

    def test_tailscale_route_badges_bind_validation_and_loopback_only_focus(self):
        ts_self_ip = ".".join(["100", "74", "9", "11"])
        ts_other_ip = ".".join(["100", "74", "9", "99"])
        lan_ip = ".".join(["192", "168", "1", "20"])
        ts_dns = "m1.sample-net." + "ts.net"

        # Default bind validation still rejects Tailscale IPs; allow_tailscale accepts only verified local Tailscale IPs
        with self.assertRaises(ValueError):
            DASH.validate_bind_host(ts_self_ip)
        self.assertEqual(
            DASH.validate_bind_host(ts_self_ip, allow_tailscale=True, local_tailscale_ips=[ts_self_ip]),
            ts_self_ip,
        )
        for bad in ("0.0.0.0", "::", lan_ip, ts_other_ip):
            with self.assertRaises(ValueError):
                DASH.validate_bind_host(bad, allow_tailscale=True, local_tailscale_ips=[ts_self_ip])

        # Build payload with one local M1 host, one live Tailscale M5 host, and one offline Tailscale Intel host
        reports = [
            {
                "key": "m1",
                "label": "M1 MacBook Pro",
                "source": "local",
                "route": "local",
                "reachable": True,
                "observed_at": "2026-11-22T12:00:00+00:00",
                "snapshot_stale": False,
                "health": {"status": "ok", "occupancy_ratio": 0.02, "age_seconds": 5, "process_status": "ok"},
                "system": {"arch": "arm64", "cpu_count": 10, "loadavg_1m": 1.2, "disk_free_gib": 120.0, "disk_total_gib": 500.0},
                "coverage": {"processes": "complete", "agent_discovery": "complete", "screens_read": 1, "sessions_seen": 1},
                "errors": [],
                "sessions": [{
                    "agent": "agy",
                    "pid": 201,
                    "tty": "ttys002",
                    "started_at": "2026-11-22T11:00:00+00:00",
                    "age_seconds": 3600,
                    "status": "idle",
                    "repo_label": "XMechBay",
                    "role": "mechbay",
                    "project_label": "XMechBay",
                    "target": {"transport": "terminal", "window_id": 10, "tab": 1},
                    "flags": [],
                    "errors": [],
                }],
            },
            {
                "key": "m5",
                "label": "M5 MacBook Pro",
                "source": "ssh",
                "route": "tailscale",
                "reachable": True,
                "observed_at": "2026-11-22T12:00:01+00:00",
                "snapshot_stale": False,
                "health": {"status": "ok", "occupancy_ratio": 0.01, "age_seconds": 8, "process_status": "ok"},
                "system": {"arch": "arm64", "cpu_count": 18, "loadavg_1m": 2.0, "disk_free_gib": 200.0, "disk_total_gib": 1000.0},
                "coverage": {"processes": "complete", "agent_discovery": "complete", "screens_read": 1, "sessions_seen": 1},
                "errors": [],
                "sessions": [{
                    "agent": "codex",
                    "pid": 305,
                    "tty": "ttys010",
                    "started_at": "2026-11-22T10:00:00+00:00",
                    "age_seconds": 7200,
                    "status": "Working",
                    "repo_label": "CultureMech",
                    "role": "mech",
                    "project_label": "CultureMech",
                    "target": {"transport": "tmux", "pane": "%2", "tmux_session": "fleet", "window": "CultureMech"},
                    "flags": [],
                    "errors": [],
                }],
            },
            {
                "key": "intelmbpro",
                "label": "Intel MacBook Pro",
                "source": "ssh",
                "route": "ssh",
                "reachable": False,
                "observed_at": "2026-11-22T12:00:02+00:00",
                "snapshot_stale": False,
                "health": {"status": "unknown"},
                "system": {},
                "coverage": {},
                "errors": ["ssh_probe_failed", "tailscale_peer_offline"],
                "sessions": [],
            },
        ]
        state = DASH.DashboardState(lambda: (reports, {"mode": "live", "role_overrides": None}))
        payload = state.get_payload()
        by_key = {h["key"]: h for h in payload["hosts"]}
        self.assertEqual(by_key["m5"]["route"], "tailscale")
        self.assertIn("tailscale_peer_offline", by_key["intelmbpro"]["errors"])

        # HTTP handler with extra_host_names accepts Tailscale Host/Origin for GET and POST /api/refresh,
        # while POST /api/focus-terminal rejects non-loopback client_address (tested in-process via socketpair)
        port = 8765

        class FakeTailscaleServer:
            server_address = (ts_self_ip, port)
            extra_host_names = (ts_self_ip, ts_dns, "m1")

        FakeTailscaleServer.state = state

        def send_inprocess(method, path, headers=None, body=b"", client_ip=ts_other_ip):
            hdrs = dict(headers or {})
            hdrs["Connection"] = "close"
            if body:
                hdrs["Content-Length"] = str(len(body))
            raw_lines = [method + " " + path + " HTTP/1.1"]
            for k, v in hdrs.items():
                raw_lines.append(k + ": " + str(v))
            raw_req = ("\r\n".join(raw_lines) + "\r\n\r\n").encode("utf-8") + body
            c_sock, s_sock = socket.socketpair()
            try:
                def serve_one():
                    try:
                        DASH.DashboardHandler(s_sock, (client_ip, 54321), FakeTailscaleServer)
                    finally:
                        s_sock.close()

                worker = threading.Thread(target=serve_one, daemon=True)
                worker.start()
                c_sock.sendall(raw_req)
                resp = http.client.HTTPResponse(c_sock)
                resp.begin()
                resp_body = resp.read()
                worker.join(timeout=5)
                return resp.status, resp_body
            finally:
                c_sock.close()

        st_get, body_get = send_inprocess("GET", "/", headers={"Host": f"{ts_dns}:{port}"})
        html_body = body_get.decode("utf-8")
        self.assertEqual(st_get, 200)
        self.assertIn("🔗 Tailscale", html_body)
        self.assertIn("Tailscale offline", html_body)

        # Unlisted Host header is still rejected even when Tailscale extra_host_names are active
        st_bad_host, _ = send_inprocess("GET", "/", headers={"Host": f"evil.example.invalid:{port}"})
        self.assertEqual(st_bad_host, 403)

        st_ref, _ = send_inprocess(
            "POST",
            "/api/refresh",
            body=b"{}",
            headers={
                "Host": f"{ts_dns}:{port}",
                "Origin": f"http://{ts_dns}:{port}",
                "X-MechBay-Token": state.csrf_token,
                "Content-Type": "application/json",
            },
        )
        self.assertEqual(st_ref, 200)

        # Non-loopback Tailscale client_address hitting /api/focus-terminal is rejected with 403 loopback_client_required
        m1_row_id = payload["sessions"][0]["row_id"]
        st_foc, body_foc = send_inprocess(
            "POST",
            "/api/focus-terminal",
            body=json.dumps({"row_id": m1_row_id}).encode("utf-8"),
            headers={
                "Host": f"{ts_dns}:{port}",
                "Origin": f"http://{ts_dns}:{port}",
                "X-MechBay-Token": state.csrf_token,
                "Content-Type": "application/json",
            },
            client_ip=ts_other_ip,
        )
        self.assertEqual(st_foc, 403)
        self.assertEqual(json.loads(body_foc.decode("utf-8"))["error"], "loopback_client_required")


if __name__ == "__main__":
    unittest.main()
