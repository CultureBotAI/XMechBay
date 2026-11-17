#!/usr/bin/env python3
"""Fleet operations: observe the whole Mech fleet and delegate work to its claws.

`observe` gathers three read-only views and ranks what needs attention:
  * sessions  - every agent session on every configured Mac, reusing the
                fleet-session-status collector (or a running dashboard, or a file);
  * resources - disk, swap, load, temporary-root growth, tmp-state guard health,
                Time Machine local snapshots and transcript retention per host
                (host_resources_probe.py, run locally or piped over SSH);
  * backlog   - open issues and pull requests of every Mech repository (GitHub
                GraphQL in batches of three repositories, newest 100 PRs each; the
                repository list comes from the claw fleet manifest and/or the config).
Each finding gets a severity, a category and an owner: the user (merge
decisions, human prompts, usage), the claw responsible for the Mech, or
XMechBay maintenance (host resources and durability).

`plan` turns a run's findings into one brief per claw, a short goal (under 1000
characters) that points the claw at its brief, a list of user decisions, a
maintenance list, and the exact guarded commands to stage and paste each goal.
It sends nothing: dispatch is a separate, approved step (see SKILL.md).

`recommend` synthesizes a run's sessions, resources, backlog and findings into
systemic infrastructure, supervision, workflow and capacity conclusions, checks
open GitHub issues across XMechBay, claw and affected Mech repositories to avoid
duplicates, and writes RECOMMENDATIONS.md, ISSUE_DRAFTS.md and
recommendations.json under <run>/recommendations/ without creating issues.

Inputs: a private config (default .local/fleet-ops.json; see
examples/fleet-ops.json). Outputs: a run directory (default
.local/fleet-ops/runs/<UTC>/) holding sessions.json, resources.json,
issues.json, findings.json and REPORT.md, plus plan/ and recommendations/.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]  # <root>/.agents/skills/fleet-ops/scripts
STATUS_SCRIPTS = ROOT / ".agents/skills/fleet-session-status/scripts"
PROBE = HERE / "host_resources_probe.py"
GOAL_LIMIT = 1000

DEFAULT_THRESHOLDS = {
    "disk_critical_gib": 50,      # near the tmp-state guard's 50 GB (46.6 GiB) backup floor
    "disk_warn_gib": 100,
    "swap_warn_ratio": 0.8,
    "swap_warn_min_gib": 4,
    "tmp_root_warn_gib": 10,
    "watch_warn_gib": 100,
    "codex_sessions_warn_gib": 40,
    "guard_stale_hours": 36,
    "idle_sessions_warn": 12,
    "non_working_sessions_warn": 20,
    "blind_sessions_warn": 2,
    "achieved_goal_age_hours": 12,
    "pr_stale_days": 14,
    "pr_backlog_warn": 25,
    "issue_steward_warn": 100,
    "pr_ci_cohort_min": 5,
    "pr_ci_cohort_ratio": 0.5,
}
KNOWN_CLAW_ROLES = frozenset({
    "cycle_supervisor",
    "discovery_supervisor",
    "stewardship_supervisor",
    "infrastructure_supervisor",
    "closure_steward",
    "fleet_maintainer",
})
SUPERVISOR_ROLES = frozenset({
    "cycle_supervisor",
    "discovery_supervisor",
    "stewardship_supervisor",
    "infrastructure_supervisor",
})
DEFAULT_INFRA_REPOS = {
    "xmechbay": "CultureBotAI/XMechBay",
    "claw": "CultureBotAI/culturebotai-claw",
}
SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}
PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2}
HUMAN_PROMPTS = ("APPROVAL", "QUESTION", "SIGN-IN", "TRUST-PROMPT", "UPDATE-PROMPT")
ACTIVE_STATES = ("Pursuing goal", "Working", "Goal stalled", "Goal paused", "Goal hit usage limits") + HUMAN_PROMPTS
REPO_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$")
TMUX_SESSION_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]*$")
MACOS_ROOT_SYMLINKS = {Path("/tmp"), Path("/var"), Path("/etc")}
GENERIC_HOST_TOKENS = {
    "local", "localhost", "fleet", "main", "host", "hosts", "mac", "work", "ssh", "tmux", "claw", "mech", "mechs",
    "worker", "workers", "server", "servers", "session", "sessions", "status", "report", "agent", "agents",
    "python", "github", "default", "example", "invalid",
}
CREDENTIAL_TOKEN_RE = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|AKIA[A-Z0-9]{16}|xox[baprs]-[A-Za-z0-9-]{16,})\b"
)
PRIVATE_KEY_HEADER_RE = re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----")
CREDENTIAL_ASSIGNMENT_RE = re.compile(
    r"(?im)((?:^|(?<=[\s,;|{(\[]))[\"']?(?:api_key|access_token|refresh_token|client_secret|password)[\"']?\s*[:=]\s*[\"']?)([A-Za-z0-9_+/=-]{16,})"
)
EMAIL_OR_MENTION_RE = re.compile(
    r"(?<![A-Za-z0-9_])@*(?P<email>[A-Za-z0-9_][\w.+-]*@(?:[\w-]+\.)+[A-Za-z]{2,})(?![.-]?[A-Za-z0-9_])"
)
LOCAL_HOSTNAME_RE = re.compile(r"(?i)\b[A-Za-z0-9][A-Za-z0-9.-]*\.local\b(?![.-]?[A-Za-z0-9])")
PRIVATE_IP_RE = re.compile(
    r"(?<!\d)(?<!\d\.)(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})(?!(?:\.\d|\d))"
)
UNIX_LOCAL_PATH_RE = re.compile(
    r"(?i)(?:~[A-Za-z0-9_.-]*/|/(?:Users|home|Volumes)/|/private/var/(?:folders|tmp)/|/var/(?:folders|tmp)/|/private/tmp/|/tmp/)[^\s)\]>\"'`,;]*[^\s)\]>\"'`,;.:]"
)
WIN_LOCAL_PATH_RE = re.compile(
    r"(?i)\b[A-Z]:[\\/](?:Users|Documents and Settings)[\\/][^\s)\]>\"'`,;]*[^\s)\]>\"'`,;.:]"
)


def is_valid_repo_slug(repo: object) -> bool:
    return isinstance(repo, str) and bool(REPO_SLUG_RE.fullmatch(repo)) and ".." not in repo


# --------------------------------------------------------------------------- config

def load_config(path: Path) -> dict:
    config = json.loads(Path(path).read_text())
    if not isinstance(config, dict) or config.get("schema_version") != 1:
        raise SystemExit("fleet-ops config must be a JSON object with schema_version 1")
    config["_dir"] = str(Path(path).resolve().parent)
    sections = {}
    for key in ("thresholds", "infra_repos", "claws", "hosts", "mech_owner", "repos"):
        raw = config.get(key)
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise SystemExit(f"fleet-ops config {key!r} must be a JSON object")
        sections[key] = raw
    for key, val in sections["infra_repos"].items():
        if key not in DEFAULT_INFRA_REPOS:
            raise SystemExit(f"infra_repos key must be one of {sorted(DEFAULT_INFRA_REPOS)}, got {key!r}")
        if not is_valid_repo_slug(val):
            raise SystemExit(f"infra_repos[{key!r}] must be OWNER/REPO, got {val!r}")
    for section_name in ("claws", "hosts"):
        cleaned_section = {}
        for subkey, subval in sections[section_name].items():
            if subval is None:
                subval = {}
            if not isinstance(subval, dict):
                raise SystemExit(f"fleet-ops config {section_name}[{subkey!r}] must be a JSON object")
            cleaned_section[subkey] = dict(subval)
        sections[section_name] = cleaned_section
    config["thresholds"] = {**DEFAULT_THRESHOLDS, **sections["thresholds"]}
    config["infra_repos"] = {**DEFAULT_INFRA_REPOS, **sections["infra_repos"]}
    config["claws"] = sections["claws"]
    config["hosts"] = sections["hosts"]
    for key in ("mech_owner", "repos"):
        config[key] = dict(sections[key])
    return config


def resolve_path(config: dict, value: str | None) -> Path | None:
    if not value:
        return None
    path = Path(os.path.expanduser(value))
    return path if path.is_absolute() else Path(config["_dir"]) / path


def manifest_repos(text: str) -> dict:
    """Map display_name -> owner/repo from the claw fleet manifest's `mechs:` section (no YAML dependency)."""
    repos, name, in_mechs = {}, None, False
    for line in text.splitlines():
        if re.match(r"^\S", line):
            in_mechs = line.startswith("mechs:")
            continue
        if not in_mechs:
            continue
        if re.match(r"^  [A-Za-z0-9_-]+:\s*$", line):
            name = None
        match = re.match(r"^    (display_name|github):\s*(\S+)\s*$", line)
        if match and match.group(1) == "display_name":
            name = match.group(2)
        elif match and name:
            repos[name] = match.group(2)
    return repos


def fleet_repos(config: dict) -> dict:
    repos = {}
    manifest = resolve_path(config, config.get("fleet_manifest"))
    if manifest and manifest.exists():
        repos.update(manifest_repos(manifest.read_text()))
    repos.update(config.get("repos", {}))
    return repos


# --------------------------------------------------------------------------- collection

INITIAL_PAYLOAD_RE = re.compile(
    r"<script\b[^>]*\bid=[\"']initial-payload[\"'][^>]*>(.*?)</script>",
    re.I | re.S,
)


def extract_dashboard_html_payload(html_text: str) -> dict:
    """Extract the embedded `/api/status` JSON payload from a rendered dashboard HTML file."""
    match = INITIAL_PAYLOAD_RE.search(str(html_text or ""))
    if not match:
        raise SystemExit("dashboard HTML missing <script id='initial-payload'> JSON payload")
    try:
        payload = json.loads(match.group(1))
    except ValueError as exc:
        raise SystemExit(f"dashboard HTML initial-payload is not valid JSON: {exc}")
    if not isinstance(payload, dict) or not isinstance(payload.get("sessions"), list):
        raise SystemExit("dashboard HTML initial-payload must be a JSON object with a sessions list")
    return payload


def collect_sessions(args, config: dict) -> dict:
    if getattr(args, "status_json", None):
        return json.loads(Path(args.status_json).read_text())
    if getattr(args, "dashboard_html", None):
        return extract_dashboard_html_payload(Path(args.dashboard_html).read_text())
    if getattr(args, "dashboard_url", None):
        with urllib.request.urlopen(args.dashboard_url.rstrip("/") + "/api/status", timeout=120) as response:
            return json.loads(response.read().decode())
    sys.path.insert(0, str(STATUS_SCRIPTS))
    import fleet_dashboard  # noqa: E402  (sibling skill; shares its collector)
    status_config = None if getattr(args, "snapshot", None) else resolve_path(config, config.get("fleet_status_config"))
    reports, meta = fleet_dashboard.collect_dashboard_reports(
        config_path=status_config, snapshot_path=getattr(args, "snapshot", None))
    return fleet_dashboard.build_dashboard_payload(reports, mode=meta["mode"], role_overrides=meta["role_overrides"])


def status_hosts(config: dict) -> dict:
    path = resolve_path(config, config.get("fleet_status_config"))
    if not path or not path.exists():
        return {}
    raw = json.loads(path.read_text())
    hosts = raw.get("hosts") if isinstance(raw, dict) else None
    return hosts if isinstance(hosts, dict) else {}


def probe_host(key: str, host_cfg: dict, watch: list, timeout: int = 1800) -> dict:
    args = []
    for item in watch:
        args += ["--watch", item]
    python = host_cfg.get("python", "/usr/bin/python3")
    if host_cfg.get("transport") == "local":
        cmd = [python, str(PROBE)] + args
        stdin = None
    else:
        destination = host_cfg.get("host")
        if not destination:
            return {"error": "no ssh host configured"}
        # The remote command is parsed by the remote shell: quote every word (no expansion, UTF-8 kept).
        remote = " ".join(shlex.quote(word) for word in [python, "-"] + args)
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "--", destination, remote]
        stdin = PROBE.open()
    try:
        proc = subprocess.run(cmd, stdin=stdin, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"error": "probe timeout"}
    finally:
        if stdin:
            stdin.close()
    if proc.returncode != 0:
        return {"error": f"probe exit {proc.returncode}: {proc.stderr.strip()[-300:]}"}
    try:
        return json.loads(proc.stdout)
    except ValueError:
        return {"error": "probe output is not JSON: " + proc.stdout.strip()[:120]}


def collect_resources(config: dict, host_keys: list) -> dict:
    hosts = status_hosts(config)
    out = {}
    for key in host_keys:
        if key not in hosts:
            out[key] = {"error": "host missing from fleet-status config"}
            continue
        out[key] = probe_host(key, hosts[key], config["hosts"].get(key, {}).get("watch", []))
    return out


ISSUES_QUERY_PART = """
  r{i}: repository(owner: "{owner}", name: "{name}") {{
    nameWithOwner
    issues(states: OPEN) {{ totalCount }}
    pullRequests(states: OPEN, first: 100, orderBy: {{field: UPDATED_AT, direction: DESC}}) {{
      totalCount
      nodes {{ number title isDraft updatedAt mergeable reviewDecision
               commits(last: 1) {{ nodes {{ commit {{ statusCheckRollup {{ state }} }} }} }} }}
    }}
  }}"""


def graphql(chunk: list) -> tuple:
    parts = []
    for i, (_, full) in enumerate(chunk):
        owner_name, _, name = full.partition("/")
        parts.append(ISSUES_QUERY_PART.format(i=i, owner=owner_name, name=name))
    query = "query {" + "".join(parts) + "\n}"
    try:
        proc = subprocess.run(["gh", "api", "graphql", "-f", f"query={query}"], capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return {}, "gh timeout"
    try:
        return json.loads(proc.stdout or "{}").get("data") or {}, (proc.stderr or "no data").strip()[-200:]
    except ValueError:
        return {}, (proc.stderr or proc.stdout or "unparseable response").strip()[-200:]


def collect_issues(repos: dict, batch: int = 3) -> dict:
    """Open issue counts and open PRs per repository via `gh api graphql`; a failed batch is retried repo by repo."""
    out, items = {}, sorted(repos.items())
    for start in range(0, len(items), batch):
        chunk = items[start:start + batch]
        data, error = graphql(chunk)
        errors = {i: error for i in range(len(chunk))}
        if not data and len(chunk) > 1:
            for i, single in enumerate(chunk):
                single_data, single_error = graphql([single])
                if single_data.get("r0"):
                    data[f"r{i}"] = single_data["r0"]
                else:
                    errors[i] = single_error  # each repository keeps its own error text
        for i, (mech, full) in enumerate(chunk):
            repo = data.get(f"r{i}")
            if not repo:
                out[mech] = {"repo": full, "error": errors[i]}
                continue
            prs = []
            for node in repo["pullRequests"]["nodes"]:
                commits = node.get("commits", {}).get("nodes") or [{}]
                rollup = ((commits[-1].get("commit") or {}).get("statusCheckRollup") or {}).get("state")
                prs.append({"number": node["number"], "title": node["title"], "draft": node["isDraft"],
                            "updated_at": node["updatedAt"], "mergeable": node["mergeable"],
                            "review": node["reviewDecision"], "checks": rollup})
            out[mech] = {"repo": full, "open_issues": repo["issues"]["totalCount"],
                         "open_prs": repo["pullRequests"]["totalCount"], "prs": prs}
    return out


# --------------------------------------------------------------------------- ownership

def mech_sessions(sessions: list | None) -> dict:
    by_mech = defaultdict(list)
    for row in (sessions or []):
        if not isinstance(row, dict):
            continue
        proj = row.get("project_label")
        if row.get("role") == "mech" and not row.get("stale_cached") and isinstance(proj, str) and proj:
            by_mech[proj].append(row)
    return by_mech


def valid_tmux_session_name(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned if TMUX_SESSION_RE.fullmatch(cleaned) else None


def claw_roles(claw: object) -> tuple:
    if not isinstance(claw, dict):
        return ()
    raw = claw.get("roles")
    if not isinstance(raw, list):
        return ()
    out = []
    for item in raw:
        if isinstance(item, str):
            role = item.strip()
            if role and role not in out:
                out.append(role)
    return tuple(out)


def is_writer_claw(claw: object) -> bool:
    if not isinstance(claw, dict) or not claw.get("host"):
        return False
    roles = set(claw_roles(claw))
    return not roles or bool(roles & SUPERVISOR_ROLES)


def claw_mechs(claw: object) -> list:
    """Configured Mech scope for a claw.

    An explicit list (`["AlphaMech"]`, `["*"]`, or `[]`) is respected verbatim so `[]` never
    becomes a catch-all. When `mechs` is omitted (`None`), roleless claws and supervisor claws
    (`cycle_supervisor`, `discovery_supervisor`, `stewardship_supervisor`) default to `["*"]`,
    while pure `closure_steward`, `fleet_maintainer` and `infrastructure_supervisor` claws default to `[]`."""
    if not isinstance(claw, dict):
        return []
    raw = claw.get("mechs")
    if raw is None:
        roles = set(claw_roles(claw))
        if not roles or (roles & {"cycle_supervisor", "discovery_supervisor", "stewardship_supervisor"}):
            return ["*"]
        return []
    if not isinstance(raw, list):
        return []
    return [m.strip() for m in raw if isinstance(m, str) and m.strip()]


def accepts_infra_cohorts(claw: object) -> bool:
    if not isinstance(claw, dict) or "infrastructure_supervisor" not in claw_roles(claw):
        return False
    raw = claw.get("accept_infra_cohorts")
    if isinstance(raw, bool):
        return raw
    if raw is not None:
        return False
    raw_mechs = claw.get("mechs")
    return raw_mechs is None or (isinstance(raw_mechs, list) and len(claw_mechs(claw)) > 0)


def has_role_routing(config: dict) -> bool:
    return any(bool(claw_roles(claw)) for claw in (config.get("claws") or {}).values())


def preferred_hosts_for_mech(rows: list) -> list:
    ranked = sorted(rows, key=lambda r: (not str(r.get("status", "")).startswith(ACTIVE_STATES), str(r.get("host_key") or "")))
    out = []
    for row in ranked:
        row_host = row.get("host_key")
        if isinstance(row_host, str) and row_host and row_host not in out:
            out.append(row_host)
    return out


def pick_claw(candidates: list, live: set | None = None, preferred_hosts: list | None = None,
              claws: dict | None = None, require_live: bool = False) -> str | None:
    if not candidates:
        return None
    if live is not None:
        alive = [key for key in candidates if key in live]
        if require_live:
            if not alive:
                return None
            pool = alive
        else:
            pool = alive or candidates
    else:
        pool = candidates
    if preferred_hosts and claws:
        for host in preferred_hosts:
            for key in pool:
                if (claws.get(key) or {}).get("host") == host:
                    return key
    return pool[0]


def mech_mode(mech: str, repo_data: dict | None, thresholds: dict) -> str:
    """Classify a Mech's operational lane from observed GitHub backlog.

    Returns 'infra' (systemic CI check-failure cohort), 'steward' (open PRs/issues above
    thresholds, failing checks, or truncated PR sample), 'discover' (healthy queue below
    thresholds with all open PRs observed), or 'unknown'."""
    if not isinstance(repo_data, dict) or repo_data.get("error"):
        return "unknown"
    prs = [p for p in (repo_data.get("prs") or []) if isinstance(p, dict)]
    failing = [p for p in prs if p.get("checks") in ("FAILURE", "ERROR")]
    if len(failing) >= thresholds["pr_ci_cohort_min"] and len(failing) / max(len(prs), 1) >= thresholds["pr_ci_cohort_ratio"]:
        return "infra"
    open_prs = repo_data.get("open_prs") or 0
    open_issues = repo_data.get("open_issues") or 0
    if (open_prs >= thresholds["pr_backlog_warn"] or failing
            or open_issues >= thresholds["issue_steward_warn"] or open_prs > len(prs)):
        return "steward"
    return "discover"


def claw_for_mech(mech: str, by_mech: dict, config: dict, live: set | None = None, issues: dict | None = None) -> str | None:
    """Explicit writer owner, then a writer claw listing the Mech, then role-based lane (`discover`/`steward`/`infra`),
    then a catch-all supervisor claw on the host of its busiest loop.

    Only configured writer claws are returned (unknown or non-writer `mech_owner` values are ignored; config_findings
    reports them). An explicit `mechs: []` or a claw holding only non-writer roles (`closure_steward`, `fleet_maintainer`)
    never acts as a primary Mech writer."""
    claws = config["claws"]
    explicit = config["mech_owner"].get(mech)
    if explicit in claws and is_writer_claw(claws[explicit]):
        return explicit
    preferred_hosts = preferred_hosts_for_mech(by_mech.get(mech, []))

    listed = [
        key for key, claw in sorted(claws.items())
        if is_writer_claw(claw) and mech in claw_mechs(claw)
    ]
    if listed:
        return pick_claw(listed, live, preferred_hosts, claws)

    role_mode = has_role_routing(config)
    mode = mech_mode(mech, (issues or {}).get(mech), config["thresholds"]) if role_mode else "unknown"
    if role_mode:
        def _role_pool(role_name: str, allow_infra_cohorts: bool = False) -> list:
            out = []
            for key, claw in sorted(claws.items()):
                if not isinstance(claw, dict) or not claw.get("host") or role_name not in claw_roles(claw):
                    continue
                scope = claw_mechs(claw)
                if "*" in scope or mech in scope or (allow_infra_cohorts and accepts_infra_cohorts(claw)):
                    out.append(key)
            return out

        if mode == "infra":
            pools = [_role_pool("infrastructure_supervisor", allow_infra_cohorts=True),
                     _role_pool("stewardship_supervisor"), _role_pool("cycle_supervisor")]
        elif mode == "discover":
            pools = [_role_pool("discovery_supervisor"), _role_pool("cycle_supervisor")]
        else:
            pools = [_role_pool("stewardship_supervisor"), _role_pool("cycle_supervisor")]

        if live is not None:
            for pool in pools:
                chosen = pick_claw(pool, live, preferred_hosts, claws, require_live=True)
                if chosen:
                    return chosen
        for pool in pools:
            chosen = pick_claw(pool, live, preferred_hosts, claws, require_live=False)
            if chosen:
                return chosen

    allowed_catchall_roles = {"cycle_supervisor", "discovery_supervisor"} if mode == "discover" else {"cycle_supervisor", "stewardship_supervisor"}
    candidates = []
    for row_host in preferred_hosts:
        for key, claw in sorted(claws.items()):
            if not isinstance(claw, dict) or not isinstance(claw.get("host"), str) or claw["host"] != row_host:
                continue
            roles = set(claw_roles(claw))
            if roles and not (roles & (allowed_catchall_roles if role_mode else SUPERVISOR_ROLES)):
                continue
            if "*" in claw_mechs(claw) and key not in candidates:
                candidates.append(key)
    return pick_claw(candidates, live)


def closure_claw_for_mech(mech: str, config: dict, by_mech: dict | None = None, live: set | None = None) -> str | None:
    claws = config["claws"]
    candidates = []
    for key, claw in sorted(claws.items()):
        if not isinstance(claw, dict) or not claw.get("host") or "closure_steward" not in claw_roles(claw):
            continue
        raw_closure = claw.get("closure_mechs")
        if raw_closure is not None:
            if not isinstance(raw_closure, list) or any(not isinstance(m, str) or not m.strip() for m in raw_closure):
                continue
            scope = [m.strip() for m in raw_closure]
            if "*" not in scope and mech not in scope:
                continue
        elif not is_writer_claw(claw):
            raw_mechs = claw.get("mechs")
            if raw_mechs is not None and not isinstance(raw_mechs, list):
                continue
            scope = claw_mechs(claw)
            if scope and "*" not in scope and mech not in scope:
                continue
        candidates.append(key)
    preferred_hosts = preferred_hosts_for_mech((by_mech or {}).get(mech, []))
    return pick_claw(candidates, live, preferred_hosts, claws, require_live=(live is not None))


def maintenance_owner(config: dict, host: str | None = None, live: set | None = None) -> dict:
    """Route reachable host-local maintenance to a live `fleet_maintainer` on that host;
    cross-host or coordinator-level maintenance stays on XMechBay."""
    if not host:
        return owner("xmechbay", "maintenance")
    claws = config["claws"]
    candidates = [
        key for key, claw in sorted(claws.items())
        if isinstance(claw, dict) and claw.get("host") == host and "fleet_maintainer" in claw_roles(claw)
    ]
    if live is not None:
        candidates = [key for key in candidates if key in live]
    return owner("claw", candidates[0]) if candidates else owner("xmechbay", "maintenance")


def live_claws(sessions: list | None, config: dict) -> set:
    return {key for key, claw in config["claws"].items() if claw_matching_targets({"sessions": sessions or []}, claw)}


def owner(kind: str, key: str | None = None) -> dict:
    return {"kind": kind, "key": key}


def mech_owner(mech: str, by_mech: dict, config: dict, live: set | None = None, issues: dict | None = None) -> dict:
    key = claw_for_mech(mech, by_mech, config, live, issues=issues)
    return owner("claw", key) if key else owner("user", "unassigned-mech")


def config_findings(config: dict) -> list:
    """Configuration errors that would misroute work."""
    out = []
    for mech, key in sorted(config["mech_owner"].items()):
        if key not in config["claws"]:
            out.append(finding("warning", "observation", f"mech_owner names unknown claw {key!r} for {mech}",
                               "Fix the config; the Mech falls back to the default ownership rules.", owner("user", "config"), mech=mech))
        elif not is_writer_claw(config["claws"][key]):
            out.append(finding("warning", "observation", f"mech_owner assigns {mech} to non-writer claw {key!r}",
                               "Assign a supervisor claw or add a supervisor role; the Mech falls back to default writer routing.",
                               owner("user", "config"), mech=mech))
    roles_by_host = defaultdict(lambda: defaultdict(list))
    for key, claw in sorted(config["claws"].items()):
        if not isinstance(claw, dict) or not claw.get("host"):
            out.append(finding("warning", "observation", f"claw {key!r} has no host", "Set its host in the config.", owner("user", "config")))
            continue
        raw_mechs = claw.get("mechs")
        if raw_mechs is not None and (not isinstance(raw_mechs, list) or any(not isinstance(m, str) or not m.strip() for m in raw_mechs)):
            out.append(finding("warning", "observation", f"claw {key!r} mechs must be a list of non-empty strings",
                               "Set mechs to a list of Mech names, ['*'], or [].", owner("user", "config")))
        raw_closure = claw.get("closure_mechs")
        if raw_closure is not None and (not isinstance(raw_closure, list) or any(not isinstance(m, str) or not m.strip() for m in raw_closure)):
            out.append(finding("warning", "observation", f"claw {key!r} closure_mechs must be a list of non-empty strings",
                               "Set closure_mechs to a list of Mech names, ['*'], or [].", owner("user", "config")))
        raw_cohorts = claw.get("accept_infra_cohorts")
        if raw_cohorts is not None and not isinstance(raw_cohorts, bool):
            out.append(finding("warning", "observation", f"claw {key!r} accept_infra_cohorts must be a boolean",
                               "Set accept_infra_cohorts to true or false, or omit it.", owner("user", "config")))
        raw_tmux = claw.get("tmux_session")
        if raw_tmux is not None and not valid_tmux_session_name(raw_tmux):
            out.append(finding("warning", "observation", f"claw {key!r} tmux_session must be a valid session name",
                               "Set tmux_session to match ^[A-Za-z0-9_][A-Za-z0-9_-]*$ or omit it.", owner("user", "config")))
        raw_roles = claw.get("roles")
        if raw_roles is not None:
            if not isinstance(raw_roles, list) or any(not isinstance(r, str) or not r.strip() for r in raw_roles):
                out.append(finding("warning", "observation", f"claw {key!r} roles must be a list of non-empty strings",
                                   f"Use roles from {sorted(KNOWN_CLAW_ROLES)}.", owner("user", "config")))
            for role in claw_roles(claw):
                if role not in KNOWN_CLAW_ROLES:
                    out.append(finding("warning", "observation", f"claw {key!r} names unknown role {role!r}",
                                       f"Use roles from {sorted(KNOWN_CLAW_ROLES)}.", owner("user", "config")))
                else:
                    roles_by_host[claw["host"]][role].append(key)
    for host, role_map in sorted(roles_by_host.items()):
        for role, keys in sorted(role_map.items()):
            if len(keys) >= 2:
                out.append(finding("warning", "observation", f"host {host!r} assigns role {role!r} to multiple claws ({', '.join(keys)})",
                                   "Give at most one claw per host each role so delegation is unambiguous.", owner("user", "config"), host=host))
    return out


# --------------------------------------------------------------------------- rules

def finding(severity, category, title, action, owner_, host=None, mech=None, evidence=None):
    return {"severity": severity, "category": category, "host": host, "mech": mech, "owner": owner_,
            "title": title, "evidence": evidence or {}, "action": action}


def parse_time(value: str) -> datetime:
    """ISO timestamp as an aware datetime; a timestamp without a zone is taken as UTC."""
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def mech_in_path(path: str, mechs) -> str | None:
    """The Mech named by a component of the path (case-insensitive); the longest name wins."""
    parts = {part.lower() for part in Path(path).parts}
    hits = [m for m in mechs if m and m.lower() in parts]
    return max(hits, key=len) if hits else None


def resource_findings(resources: dict, config: dict, by_mech: dict, mechs=(), issues: dict | None = None, live: set | None = None) -> list:
    t, out = config["thresholds"], []
    coord_maint = owner("xmechbay", "maintenance")
    for host, r in sorted(resources.items()):
        if r.get("error"):
            out.append(finding("warning", "observation", f"{host}: resource probe failed", "Fix SSH/probe access; treat the host as unobserved.", coord_maint, host, evidence={"error": r["error"]}))
            continue
        maint = maintenance_owner(config, host=host, live=live)
        free = (r.get("disk") or {}).get("free_gib")
        tmp = {root["path"]: root for root in r.get("tmp_roots", [])}
        if free is None:
            out.append(finding("warning", "observation", f"{host}: free space unknown", "Check df on the host; treat disk as unobserved.", maint, host))
        elif free < t["disk_warn_gib"]:
            sev = "critical" if free < t["disk_critical_gib"] else "warning"
            out.append(finding(sev, "resources", f"{host}: {free} GiB free on the data volume",
                               "Run a read-only space review (fleet-repo-crud-cleanup / disk-usage-report), then delete only approved, verified-regenerable items; pause disk-heavy jobs (KG builds, full-corpus validations) on this host.",
                               maint, host, evidence={"disk": r["disk"], "tmp_roots": [{"path": p, "gib": v.get("gib"), "top": v.get("children", [])[:3]} for p, v in tmp.items()],
                                                       "watched": r.get("watched", []), "local_snapshots": r.get("local_snapshots")}))
            if r.get("local_snapshots"):
                out.append(finding("info", "resources", f"{host}: {r['local_snapshots']} Time Machine local snapshots",
                                   "Deleting files frees space only as these snapshots expire (about 24 h); verify recovery the next day, not right after deletion.", maint, host))
        swap = r.get("swap") or {}
        if swap.get("total_gib") and swap.get("used_gib") is not None:
            if swap["used_gib"] >= t["swap_warn_min_gib"] and swap["used_gib"] / swap["total_gib"] >= t["swap_warn_ratio"]:
                out.append(finding("warning", "resources", f"{host}: swap {swap['used_gib']} of {swap['total_gib']} GiB in use",
                                   "Swap files live on the same disk. Find the memory-heavy job; restart long-lived idle sessions only at an owned idle checkpoint (graceful exit protocol).", maint, host, evidence={"swap": swap, "memory_gib": r.get("memory_gib")}))
        sized = [(p, root, "tmp_root_warn_gib") for p, root in tmp.items()] + [(w.get("path"), w, "watch_warn_gib") for w in r.get("watched", [])]
        codex = r.get("codex_sessions") or {}
        if codex.get("exists", True):
            sized.append((codex.get("path", "~/.codex/sessions"), codex, "codex_sessions_warn_gib"))
        for path, item, limit in sized:
            gib = item.get("gib")
            if gib is None:
                if item.get("exists", True):  # a du timeout is unknown, never zero
                    out.append(finding("warning", "observation", f"{host}: size of {path} unknown ({item.get('error', 'not measured')})",
                                       "Measure it with a longer timeout; a directory too big to size in time is itself suspect.", maint, host, evidence={"path": path}))
                continue
            if gib < t[limit]:
                continue
            if limit == "tmp_root_warn_gib":
                out.append(finding("warning", "hygiene", f"{host}: {gib} GiB in {path}",
                                   "Temporary roots are purged by macOS and hold only scratch: point test basetemps and work dirs at a repository .temp/ or ~/agent-work, then remove verified scratch.", maint, host, evidence={"top": item.get("children", [])}))
            elif limit == "watch_warn_gib":
                mech = mech_in_path(path, set(by_mech) | set(mechs))
                mo = mech_owner(mech, by_mech, config, live=live, issues=issues) if mech else None
                watch_owner = (
                    mo if (mo and mo["kind"] == "claw" and (config["claws"].get(mo["key"]) or {}).get("host") == host)
                    else maint
                )
                out.append(finding("warning", "resources", f"{host}: {gib} GiB in a watched path",
                                   "Ask the owning session for a retention inventory (what it still needs), then archive or delete with approval.",
                                   watch_owner, host, mech, evidence={"path": path}))
            else:
                out.append(finding("info", "resources", f"{host}: {gib} GiB of Codex session logs",
                                   "Archive closed rollouts older than 14 days into verified per-day tar.zst files, then remove the originals with approval.", maint, host))
        guard = r.get("guard") or {}
        if not guard.get("installed"):
            out.append(finding("warning", "durability", f"{host}: tmp-state guard not installed",
                               "Install it (scripts/install_tmp_state_guard.py --install --notify) so agent work left in temporary roots is copied before macOS purges it.", maint, host))
        elif not guard.get("status"):
            out.append(finding("warning", "durability", f"{host}: tmp-state guard installed but has no report",
                               "Check the LaunchAgent (launchctl print) and launchd-stderr.log; the guard may never have run.", maint, host))
        else:
            skipped, failed = guard.get("skipped") or {}, guard.get("failed") or 0
            if skipped or failed or guard.get("exit_code") == 20 or guard.get("status") == "unrescued":
                out.append(finding("critical", "durability", f"{host}: tmp-state guard left at-risk work unrescued (skipped {sum(skipped.values())}, failed {failed})",
                                   "Unrescued work in temporary roots can be purged at the next cleaner run; free space above the guard floor or rescue the entries by hand.", maint, host, evidence={"skipped": skipped, "failed": failed}))
            elif guard.get("exit_code") == 30 or guard.get("status") == "incomplete":
                out.append(finding("info", "durability", f"{host}: tmp-state guard run incomplete ({guard.get('unknown', 0)} unknown entries)",
                                   "Read latest.json for the unknown entries and check whether their source directories still exist.", maint, host))
            finished = guard.get("finished_at")
            if finished:
                age_h = (datetime.now(timezone.utc) - parse_time(finished)).total_seconds() / 3600
                if age_h > t["guard_stale_hours"]:
                    out.append(finding("warning", "durability", f"{host}: tmp-state guard report is {age_h:.0f} h old",
                                       "Check the LaunchAgent (launchctl print) and launchd-stderr.log.", maint, host))
        retention = r.get("claude_cleanup_period_days") or {}
        short = {k: (v if isinstance(v, int) else "default 30") for k, v in retention.items()
                 if v in (None, "absent") or (isinstance(v, int) and v < 365)}
        if short:
            days = ", ".join(f"{k}: {v} days" for k, v in sorted(short.items()))
            out.append(finding("warning", "durability", f"{host}: Claude Code deletes old transcripts ({days})",
                               "Archive transcripts, then raise cleanupPeriodDays (back up settings.json first).", maint, host, evidence=retention))
    return out


def has_composer_draft(row: dict) -> bool:
    flags = set(row.get("flags") or ())
    return (
        row.get("human_block_kind") in ("draft", "pasted_prompt")
        or bool(row.get("is_pasted_prompt"))
        or bool(flags & {"draft", "pasted-prompt"})
    )


def is_pasted_prompt_row(row: dict) -> bool:
    return bool(row.get("is_pasted_prompt")) or "pasted-prompt" in (row.get("flags") or ())


def is_agent_message_row(row: dict) -> bool:
    return (
        row.get("human_block_kind") == "agent_message"
        or "agent-message" in (row.get("flags") or ())
        or bool(str(row.get("agent_message") or "").strip())
    )


def is_keystroke_prompt_row(row: dict) -> bool:
    status = str(row.get("status") or "")
    return (
        status.startswith(HUMAN_PROMPTS)
        or row.get("human_block_kind") in ("keystroke", "prompt")
        or "subagent-approval" in (row.get("flags") or ())
    )


def is_agy_work_fixable_row(row: dict) -> bool:
    status = str(row.get("status") or "")
    return (
        bool(row.get("agy_work_fixable"))
        or "agy-work-fixable" in (row.get("flags") or ())
        or (row.get("agent") == "agy" and status.startswith("APPROVAL") and row.get("skip_permissions") is False)
    )


def human_block_category(row: dict) -> str | None:
    if is_keystroke_prompt_row(row):
        return "keystroke"
    if is_agent_message_row(row):
        return "agent_message"
    if has_composer_draft(row):
        return "pasted_prompt"
    return None


def compact_snippet(value: object, limit: int = 140) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = re.sub(r"\s+", " ", value).strip()
    if not cleaned:
        return ""
    return cleaned if len(cleaned) <= limit else cleaned[:limit - 1].rstrip() + "…"


def target_display(row: dict) -> str:
    target = str(row.get("target_short") or "?")
    t_sess = row.get("tmux_session")
    t_win = row.get("tmux_window")
    if isinstance(t_sess, str) and t_sess:
        win_part = f":{t_win}" if isinstance(t_win, str) and t_win else ""
        return f"{target} (tmux {t_sess}{win_part})"
    return target


def blocker_evidence(row: dict, base_where: dict, category: str) -> dict:
    ev = dict(base_where)
    ev["human_block_kind"] = category
    if row.get("tmux_session"):
        ev["tmux_session"] = row["tmux_session"]
    if row.get("tmux_window"):
        ev["tmux_window"] = row["tmux_window"]
    wait_sec = row.get("wait_seconds")
    if isinstance(wait_sec, (int, float)) and not isinstance(wait_sec, bool) and wait_sec >= 0:
        ev["wait_seconds"] = int(wait_sec)
    if category == "keystroke":
        ks = compact_snippet(row.get("keystroke_hint"), 80)
        if ks:
            ev["keystroke_hint"] = ks
        if is_agy_work_fixable_row(row):
            ev["agy_work_fixable"] = True
            ev["agy_work_status"] = "fixable"
        elif row.get("agy_work_status") and row.get("agy_work_status") != "not_applicable":
            ev["agy_work_status"] = row["agy_work_status"]
        elif row.get("agent") == "agy" and row.get("skip_permissions") is True:
            ev["agy_work_status"] = "already_enabled"
        preview = compact_snippet(row.get("activity"), 140)
        if preview:
            ev["prompt_preview"] = preview
    elif category == "agent_message":
        msg = compact_snippet(row.get("agent_message") or row.get("activity"), 160)
        if msg:
            ev["agent_message"] = msg
    elif category == "pasted_prompt":
        ev["is_pasted_prompt"] = is_pasted_prompt_row(row)
        comp = compact_snippet(row.get("composer_text"), 140)
        if comp:
            ev["composer_text"] = comp
    return ev


def build_human_blocker_finding(row: dict, subject: str, role: str | None, host: str | None,
                                mech: str | None, base_where: dict, category: str) -> dict:
    status = str(row.get("status") or "")
    loc = target_display(row)
    ev = blocker_evidence(row, base_where, category)
    if category == "keystroke":
        flags = set(row.get("flags") or ())
        prompt_label = status if status.startswith(HUMAN_PROMPTS) else ("subagent-approval" if "subagent-approval" in flags else (status or "PROMPT"))
        ks = ev.get("keystroke_hint")
        preview = ev.get("prompt_preview")
        suffix = f" Prompt: {preview}" if preview else ""
        if ev.get("agy_work_fixable"):
            action = (
                f"Plain agy without skip-permissions at {loc}: answer in TUI ({ks or '1 / Enter'}) "
                f"or exit cleanly and relaunch with `agy-work -c` so routine approvals stop blocking.{suffix}"
            )
        elif ev.get("agy_work_status") == "already_enabled":
            ks_part = f"; keys: {ks}" if ks else ""
            action = (
                f"Only the user answers approval, question, sign-in, trust or update prompts "
                f"({loc}{ks_part}; already running agy-work, stopped on a standing Ask rule).{suffix}"
            )
        else:
            ks_part = f"; keys: {ks}" if ks else ""
            loc_part = f" ({loc}{ks_part})" if loc and loc != "?" else ""
            action = f"Only the user answers approval, question, sign-in, trust or update prompts{loc_part}.{suffix}"
        return finding("warning", "activity", f"{subject} on {host} waits for a human ({prompt_label})",
                       action, owner("user", "prompt"), host, mech, ev)
    if category == "agent_message":
        msg = ev.get("agent_message")
        suffix = f" Message: {msg}" if msg else ""
        action = f"Agent finished its turn at {loc} with a plain-text question or approval request; read the message and type your reply + Enter.{suffix}"
        return finding("warning", "activity", f"{subject} on {host} awaits a user reply ({row.get('target_short')})",
                       action, owner("user", "agent-message"), host, mech, ev)
    is_pasted = bool(ev.get("is_pasted_prompt"))
    comp = ev.get("composer_text")
    suffix = f" Composer: {comp}" if comp else ""
    title_kind = "has a pasted prompt not running" if is_pasted else "has an unsent composer draft"
    if role in ("claw", "mechbay"):
        action = f"Send or clear the unsent text in the composer at {loc} (press Enter to run or clear to discard) before pasting a fleet-ops goal.{suffix}"
        cat_name = "delegation"
    else:
        action = f"Prompt text sits unsubmitted in the composer at {loc} and is NOT running: inspect the prompt and press Enter to submit or clear to discard.{suffix}"
        cat_name = "activity"
    return finding("warning", cat_name, f"{subject} on {host} {title_kind} ({row.get('target_short')})",
                   action, owner("user", "draft"), host, mech, ev)


def is_blind_process_row(row: dict) -> bool:
    target = str(row.get("target_short") or "")
    return (row.get("transport") == "process" or target.startswith("P")) and not row.get("tmux_session")


def is_live_host_observation(payload: dict, host: dict) -> bool:
    return payload.get("mode") != "snapshot" and not bool(host.get("snapshot_stale"))


def parked_achieved_seconds(row: dict) -> float:
    """Seconds a session screen has remained unchanged at Goal achieved (-1 when unknown or freshly changed)."""
    wait = row.get("wait_seconds")
    if isinstance(wait, (int, float)) and not isinstance(wait, bool):
        return float(wait)
    delta = row.get("delta")
    if isinstance(delta, dict) and delta.get("kind") == "unchanged":
        sec = delta.get("seconds")
        if isinstance(sec, (int, float)) and not isinstance(sec, bool):
            return float(sec)
    return -1.0


def host_observation_findings(payload: dict, config: dict, live: set | None = None) -> list:
    t, out = config["thresholds"], []
    if live is None and "sessions" in payload:
        live = live_claws(payload.get("sessions"), config)
    coord_maint = owner("xmechbay", "maintenance")
    raw_hosts = [h for h in (payload.get("hosts") or []) if isinstance(h, dict)]
    unreachable_hosts = {h.get("key") for h in raw_hosts if isinstance(h.get("key"), str) and h.get("reachable") is False}
    for host in sorted(raw_hosts, key=lambda h: str(h.get("key") or "")):
        key = host.get("key")
        if not isinstance(key, str) or not key:
            continue
        if host.get("reachable") is False:
            out.append(finding("critical", "observation", f"{key}: session collection unreachable",
                               "Restore SSH or local probe access; cached rows are not live state.", coord_maint, key,
                               evidence={"errors": host.get("errors") or [], "last_reachable": host.get("last_reachable")}))
            continue
        maint = maintenance_owner(config, host=key, live=live)
        errors = list(host.get("errors") or [])
        inv = host.get("inventory_state")
        if errors or inv == "partial":
            codes = ", ".join(str(e) for e in (errors or [inv]))
            out.append(finding("warning", "observation", f"{key}: session collection partial ({codes})",
                               "Inspect collector errors (tmux server/named sockets, Terminal window readability, process argv discovery); blind sessions cannot be supervised safely.",
                               maint, key, evidence={"errors": errors, "inventory_state": inv, "screens": host.get("screens_text")}))
        health = host.get("health")
        if isinstance(health, dict) and health:
            h_status, p_status = health.get("status"), health.get("process_status")
            if h_status in ("critical", "warning") or p_status in ("critical", "warning"):
                sev = "critical" if "critical" in (h_status, p_status) else "warning"
                occ = f"{health.get('occupancy_pct')}%" if health.get("occupancy_pct") is not None else "?"
                out.append(finding(sev, "resources", f"{key}: port-watch health {sev} (TCP {occ}, process {p_status})",
                                   "Inspect high-listener processes from the port-watch report and restart runaway listeners at an owned idle checkpoint.",
                                   maint, key, evidence=health))
            elif h_status in (None, "unknown"):
                out.append(finding("warning", "observation", f"{key}: port-watch health report missing or unknown",
                                   "Install or repair the ephemeral-port watch LaunchAgent on this host so TCP port pressure is observed.",
                                   maint, key, evidence=health))
            elif h_status == "stale" and is_live_host_observation(payload, host):
                out.append(finding("warning", "observation", f"{key}: port-watch health report is stale",
                                   "Check the ephemeral-port watch LaunchAgent on this host; its health report has stopped updating.",
                                   maint, key, evidence=health))
    by_blind = defaultdict(list)
    for row in (payload.get("sessions") or []):
        if not isinstance(row, dict):
            continue
        hk = row.get("host_key")
        if isinstance(hk, str) and hk and not row.get("stale_cached") and is_blind_process_row(row):
            by_blind[hk].append(row)
    for key, rows in sorted(by_blind.items()):
        if len(rows) >= t["blind_sessions_warn"]:
            maint = coord_maint if key in unreachable_hosts else maintenance_owner(config, host=key, live=live)
            out.append(finding("warning", "observation", f"{key}: {len(rows)} agent sessions have no readable screen",
                               "Launch worker loops inside an observable tmux server instead of detached processes, and verify collector socket coverage.",
                               maint, key, evidence={"count": len(rows), "projects": sorted({str(r.get("project_label") or "?") for r in rows})[:20],
                                                     "targets": [r.get("target_short") for r in rows[:15]]}))
    return out


def activity_findings(payload: dict, config: dict, by_mech: dict, issues: dict | None = None, live: set | None = None) -> list:
    t, out = config["thresholds"], []
    if live is None:
        live = live_claws(payload.get("sessions"), config)
    sessions = [r for r in (payload.get("sessions") or []) if isinstance(r, dict) and not r.get("stale_cached")]
    for row in sessions:  # prompts, stalls, usage stops, agent messages and composer drafts on supervisor or non-Mech sessions need direct attention
        status, role, host = str(row.get("status", "")), row.get("role"), row.get("host_key")
        if role not in ("claw", "mechbay", "other"):
            continue
        where = {"target": row.get("target_short"), "agent": row.get("agent")}
        role_subj = f"{role} session {row.get('project_label')}"
        if is_keystroke_prompt_row(row):
            out.append(build_human_blocker_finding(row, role_subj, role, host, None, where, "keystroke"))
        elif status.startswith("Goal stalled"):
            label = f"non-Mech session {row.get('project_label')}" if role == "other" else role_subj
            action = ("Inspect the session directly or register the repository in the fleet manifest if it is a fleet Mech."
                      if role == "other" else "Inspect the supervisor session directly and clear or restart the stalled goal before delegating.")
            out.append(finding("warning", "activity", f"{label} on {host}: goal stalled",
                               action, owner("user", "stalled-other" if role == "other" else "stalled-supervisor"), host, evidence=where))
        elif status.startswith("Goal hit usage limits"):
            label = f"non-Mech session {row.get('project_label')}" if role == "other" else role_subj
            out.append(finding("warning", "activity", f"{label} on {host} hit usage limits",
                               "Decide whether and when to resume; resumes spend usage.", owner("user", "usage"), host, evidence=where))
        if not is_keystroke_prompt_row(row):
            if is_agent_message_row(row):
                out.append(build_human_blocker_finding(row, role_subj, role, host, None, where, "agent_message"))
            elif has_composer_draft(row):
                out.append(build_human_blocker_finding(row, role_subj, role, host, None, where, "pasted_prompt"))
    for mech, rows in sorted(by_mech.items()):
        for row in rows:
            status, host = str(row.get("status", "")), row.get("host_key")
            where = {"host": host, "target": row.get("target_short"), "agent": row.get("agent")}
            if is_keystroke_prompt_row(row):
                out.append(build_human_blocker_finding(row, mech, "mech", host, mech, where, "keystroke"))
            elif status.startswith("Goal hit usage limits"):
                out.append(finding("warning", "activity", f"{mech} on {host} hit usage limits", "Decide whether and when to resume; resumes spend usage.", owner("user", "usage"), host, mech, where))
            elif status.startswith("Goal stalled"):
                out.append(finding("warning", "activity", f"{mech} on {host}: goal stalled", "Diagnose the stall from authoritative state (ledger, receipts, GitHub); repair with verified facts or report the blocker.", mech_owner(mech, by_mech, config, live=live, issues=issues), host, mech, where))
            elif status.startswith("Goal paused"):
                out.append(finding("info", "activity", f"{mech} on {host}: goal paused", "Propose resume or close with the reason; do not resume without approval.", mech_owner(mech, by_mech, config, live=live, issues=issues), host, mech, where))
            if not is_keystroke_prompt_row(row):
                if is_agent_message_row(row):
                    out.append(build_human_blocker_finding(row, mech, "mech", host, mech, where, "agent_message"))
                elif has_composer_draft(row):
                    out.append(build_human_blocker_finding(row, mech, "mech", host, mech, where, "pasted_prompt"))
        states = [str(r.get("status", "")) for r in rows]
        flagged = any(
            s.startswith(("Goal stalled", "Goal paused", "Goal hit usage limits") + HUMAN_PROMPTS)
            or human_block_category(r) is not None
            for r, s in zip(rows, states)
        )
        unknown = any(
            s.upper().startswith("UNKNOWN") and human_block_category(r) is None
            for r, s in zip(rows, states)
        )  # an unreadable screen is not "not working"
        if rows and not flagged and not unknown and not any(s.startswith(("Pursuing goal", "Working")) for s in states):
            m_owner = mech_owner(mech, by_mech, config, live=live, issues=issues)
            lane = mech_lane_label(mech, config, issues=issues) if has_role_routing(config) else "unknown"
            owner_claw = config["claws"].get(m_owner["key"]) if m_owner["kind"] == "claw" else None
            is_discovery_lane = lane == "discover" and "discovery_supervisor" in claw_roles(owner_claw)
            action = ("Run bounded source discovery and add_records up to the open-PR WIP cap, recording the discovery saturation ledger."
                      if is_discovery_lane else "Check the backlog and propose the next goal for this Mech.")
            evidence = {"states": dict(Counter(states))}
            if lane != "unknown":
                evidence["mode"] = lane
            out.append(finding("info", "activity", f"{mech}: no session is working", action, m_owner, None, mech, evidence))
    absent = [mech for mech in (payload.get("canonical_mechs") or []) if isinstance(mech, str) and mech and mech not in by_mech]
    if absent:
        out.append(finding("info", "activity", f"{len(absent)} fleet Mechs have no session on any observed host",
                           "Confirm each is intentionally dormant, or assign it to a claw (mech_owner) and start a supervised loop.",
                           owner("user", "dormant-mechs"), evidence={"mechs": absent}))
    for key, claw in sorted(config["claws"].items()):
        c_host = claw.get("host") if isinstance(claw, dict) else None
        if not claw_matching_targets(payload, claw):
            host_candidates = claw_targets(payload, claw)
            action = ("No live claw session on that host matches this claw's tmux_session/target_hint; start the missing claw or update its binding."
                      if host_candidates else "Delegation to this claw is impossible until it runs; start it on that host (user-authorized).")
            out.append(finding("critical", "delegation", f"claw {key} has no live session on {c_host}",
                               action, owner("user", "claws"), c_host))
    host_sessions = [r for r in sessions if isinstance(r.get("host_key"), str) and r.get("host_key")]
    idle = Counter(r["host_key"] for r in host_sessions if str(r.get("status")) == "idle" and human_block_category(r) is None and r.get("role") in ("mech", "claw"))
    for host, count in sorted(idle.items()):
        if count >= t["idle_sessions_warn"]:
            out.append(finding("info", "hygiene", f"{host}: {count} idle Mech/claw sessions",
                               "Idle agent processes keep memory, swap and ports; close duplicates at an owned idle checkpoint (graceful exit).", maintenance_owner(config, host=host, live=live), host))
    non_working = Counter(r["host_key"] for r in host_sessions if not str(r.get("status", "")).startswith(("Pursuing goal", "Working")))
    totals = Counter(r["host_key"] for r in host_sessions)
    for host, count in sorted(non_working.items()):
        if count >= t["non_working_sessions_warn"]:
            out.append(finding("warning", "hygiene", f"{host}: {count} non-working agent sessions across all roles",
                               "Retire finished or duplicate sessions at an owned idle checkpoint to reclaim memory, swap and Terminal windows.",
                               maintenance_owner(config, host=host, live=live), host, evidence={"non_working": count, "total": totals[host]}))
    by_achieved = defaultdict(list)
    for r in host_sessions:
        if str(r.get("status", "")).startswith("Goal achieved") and parked_achieved_seconds(r) >= t["achieved_goal_age_hours"] * 3600:
            by_achieved[r["host_key"]].append(r)
    for host, rows in sorted(by_achieved.items()):
        if len(rows) >= 2:
            out.append(finding("info", "hygiene", f"{host}: {len(rows)} sessions parked at Goal achieved for {t['achieved_goal_age_hours']}h+",
                               "Record their outputs and close or assign the next goal so finished loops stop holding memory and swap.",
                               maintenance_owner(config, host=host, live=live), host,
                               evidence={"projects": [r.get("project_label") for r in rows[:15]], "targets": [r.get("target_short") for r in rows[:15]]}))
    multi = {mech: sorted({r["host_key"] for r in rows if isinstance(r.get("host_key"), str) and r.get("host_key")}) for mech, rows in sorted(by_mech.items())}
    multi = {m: hs for m, hs in multi.items() if len(hs) >= 2}
    if multi:
        out.append(finding("info", "hygiene", f"{len(multi)} Mechs have live sessions on multiple hosts",
                           "Keep one primary loop per Mech and retire duplicate sessions at an owned idle checkpoint.",
                           owner("xmechbay", "maintenance"), evidence={"mechs": multi}))
    return out


def backlog_findings(issues: dict, config: dict, by_mech: dict, live: set | None = None) -> list:
    t, out = config["thresholds"], []
    now = datetime.now(timezone.utc)
    coord_maint = owner("xmechbay", "maintenance")
    for mech, repo in sorted(issues.items()):
        if repo.get("error"):
            out.append(finding("warning", "observation", f"{mech}: GitHub query failed", "Check gh authentication and the repository name.", coord_maint, mech=mech, evidence={"error": repo["error"]}))
            continue
        prs = repo.get("prs", [])
        open_ready = [p for p in prs if not p["draft"] and p["mergeable"] == "MERGEABLE" and p.get("review") != "CHANGES_REQUESTED"]
        ready = [p["number"] for p in open_ready if p["checks"] == "SUCCESS"]
        unreviewed_ready = [p["number"] for p in open_ready if p["checks"] == "SUCCESS" and p.get("review") != "APPROVED"]
        unchecked = [p["number"] for p in open_ready if p["checks"] is None]  # no checks ran: not "green"
        failing = [p["number"] for p in prs if p["checks"] in ("FAILURE", "ERROR")]
        stale = [p["number"] for p in prs if p["draft"] and (now - parse_time(p["updated_at"])).days >= t["pr_stale_days"]]
        closure_key = closure_claw_for_mech(mech, config, by_mech=by_mech, live=live)
        if ready:
            out.append(finding("info", "backlog", f"{mech}: {len(ready)} PRs mergeable with green checks",
                               "Review each PR adversarially before any merge proposal; merging stays the user's decision.", owner("user", "merges"), mech=mech,
                               evidence={"repo": repo["repo"], "prs": ready[:30], "mergeable_without_checks": unchecked[:30]}))
        if closure_key and unreviewed_ready:
            out.append(finding("info", "backlog", f"{mech}: {len(unreviewed_ready)} green PRs awaiting closure-steward diff review",
                               "Review each exact PR head read-only via gh, file issues for any defects, and record verified merge candidates without merging or editing branches.",
                               owner("claw", closure_key), mech=mech,
                               evidence={"repo": repo["repo"], "prs": unreviewed_ready[:30], "mergeable_without_checks": unchecked[:30]}))
        if failing:
            out.append(finding("warning", "backlog", f"{mech}: {len(failing)} PRs with failing checks",
                               "Fix or close each failing PR; file an issue for any infrastructure cause.", mech_owner(mech, by_mech, config, live=live, issues=issues), mech=mech, evidence={"repo": repo["repo"], "prs": failing[:30]}))
        if stale:
            stale_owner = owner("claw", closure_key) if closure_key else mech_owner(mech, by_mech, config, live=live, issues=issues)
            stale_action = ("Inspect stale drafts read-only via gh, classify obsolete vs salvageable drafts, and route closure or handoff actions without pushing to another writer's branch."
                            if closure_key else "Advance, split or close each stale draft with a reason.")
            out.append(finding("info", "backlog", f"{mech}: {len(stale)} draft PRs untouched for {t['pr_stale_days']}+ days",
                               stale_action, stale_owner, mech=mech, evidence={"repo": repo["repo"], "prs": stale[:30]}))
        open_prs = repo.get("open_prs") or 0
        if open_prs >= t["pr_backlog_warn"]:
            out.append(finding("warning", "backlog", f"{mech}: {open_prs} open PRs, {repo.get('open_issues')} open issues",
                               "Batch the review queue by theme (fleet-issue-review) and stop opening new PRs until it shrinks.", mech_owner(mech, by_mech, config, live=live, issues=issues), mech=mech, evidence={"repo": repo["repo"]}))
        elif (repo.get("open_issues") or 0) >= t["issue_steward_warn"]:
            out.append(finding("info", "backlog", f"{mech}: {repo.get('open_issues')} open issues, {open_prs} open PRs",
                               "Triage and resolve open review/curation issues by theme (fleet-issue-review) before starting new record discovery.", mech_owner(mech, by_mech, config, live=live, issues=issues), mech=mech, evidence={"repo": repo["repo"]}))
    return out


def rank(findings: list) -> list:
    return sorted(findings, key=lambda f: (SEVERITY_ORDER[f["severity"]], f["category"], f.get("host") or "", f.get("mech") or "", f["title"]))


def analyze(payload: dict, resources: dict, issues: dict, config: dict) -> list:
    sessions = payload.get("sessions") or []
    by_mech = mech_sessions(sessions)
    live = live_claws(sessions, config)
    mechs = {m for m in (payload.get("canonical_mechs") or []) if isinstance(m, str) and m} | set(issues or {})
    findings = (config_findings(config) + host_observation_findings(payload, config, live=live)
                + resource_findings(resources or {}, config, by_mech, mechs, issues=issues, live=live)
                + activity_findings(payload, config, by_mech, issues=issues, live=live)
                + backlog_findings(issues or {}, config, by_mech, live=live))
    ranked = rank(findings)
    for number, item in enumerate(ranked, 1):
        item["id"] = f"F{number:03d}"
    return ranked


# --------------------------------------------------------------------------- report

def owner_label(o: dict) -> str:
    return {"user": "user", "claw": "claw", "xmechbay": "XMechBay"}[o["kind"]] + (f":{o['key']}" if o.get("key") else "")


def mech_lane_label(mech: str, config: dict, issues: dict | None = None) -> str:
    explicit = config["mech_owner"].get(mech)
    if explicit in config["claws"] and is_writer_claw(config["claws"][explicit]):
        return "pinned"
    if any(is_writer_claw(claw) and mech in claw_mechs(claw) for claw in config["claws"].values()):
        return "pinned"
    return mech_mode(mech, (issues or {}).get(mech), config["thresholds"])


def session_state_cell(row: dict) -> str:
    host = row.get("host_key")
    status = row.get("status")
    cat = human_block_category(row)
    if cat == "keystroke" and is_agy_work_fixable_row(row):
        return f"{host}: {status} [agy-work fixable]"
    if cat == "agent_message":
        return f"{host}: {status} [awaiting reply]"
    if cat == "pasted_prompt":
        tag = "pasted not running" if is_pasted_prompt_row(row) else "draft"
        return f"{host}: {status} [{tag}]"
    return f"{host}: {status}"


def render_report(run_id: str, payload: dict, resources: dict, issues: dict, findings: list, config: dict) -> str:
    lines = [f"# Fleet operations report {run_id}", "",
             f"Sessions observed {payload.get('generated_at')} ({payload.get('mode')}); resources and backlog collected for this run. "
             "Screen-derived states are observations, not verified completion.", "", "## Hosts", "",
             "| Host | Free GiB | Swap used/total GiB | Load (1m) | /tmp GiB | user temp GiB | Codex logs GiB | Guard | Sessions |",
             "|---|---:|---|---:|---:|---:|---:|---|---:|"]
    live_rows = [r for r in (payload.get("sessions") or []) if isinstance(r, dict) and not r.get("stale_cached")]
    counts = Counter(r["host_key"] for r in live_rows if isinstance(r.get("host_key"), str) and r["host_key"])
    payload_hosts = {h["key"] for h in (payload.get("hosts") or []) if isinstance(h, dict) and isinstance(h.get("key"), str) and h["key"]}
    for host in sorted({k for k in (resources or {}) if isinstance(k, str) and k} | payload_hosts | set(counts)):
        r = (resources or {}).get(host) or {}
        if r.get("error"):
            lines.append(f"| {host} | probe failed: {r['error'][:60]} | | | | | | | {counts.get(host, 0)} |")
            continue
        tmp = [t.get("gib") for t in (r.get("tmp_roots") or []) if isinstance(t, dict)] + [None, None]
        g = r.get("guard") or {}
        guard = "not installed" if not g.get("installed") else f"{g.get('status')} (exit {g.get('exit_code')}, skipped {sum((g.get('skipped') or {}).values())})"
        swap = r.get("swap") or {}
        lines.append(f"| {host} | {(r.get('disk') or {}).get('free_gib')} | {swap.get('used_gib')}/{swap.get('total_gib')} | {round((r.get('loadavg') or [0])[0], 1)} | {tmp[0]} | {tmp[1]} | {(r.get('codex_sessions') or {}).get('gib')} | {guard} | {counts.get(host, 0)} |")
    tones = ((payload.get("stats") or {}).get("tone_counts")) or {}
    by_mech = mech_sessions(live_rows)
    live = live_claws(live_rows, config)
    role_mode = has_role_routing(config)
    lines += ["", "## Activity", "", f"Session tones: {', '.join(f'{k} {v}' for k, v in sorted(tones.items()))}."]
    hb_counts = Counter(human_block_category(r) for r in live_rows if human_block_category(r) is not None)
    agy_fixable_total = sum(1 for r in live_rows if human_block_category(r) == "keystroke" and is_agy_work_fixable_row(r))
    hb_total = sum(hb_counts.values())
    if hb_total:
        agy_note = f" [{agy_fixable_total} fixable via agy-work -c]" if agy_fixable_total else ""
        lines.append(
            f"Human blockers: {hb_total} ({hb_counts['keystroke']} keystroke/TUI prompts{agy_note}, "
            f"{hb_counts['agent_message']} agent messages awaiting reply, "
            f"{hb_counts['pasted_prompt']} pasted/unsent composer prompts)."
        )
    if role_mode:
        lines += ["", "### Claw roles", ""]
        for key, claw in sorted(config["claws"].items()):
            if isinstance(claw, dict):
                roles_txt = ", ".join(claw_roles(claw)) or "untyped"
                lines.append(f"- `{key}` ({claw.get('host')}): {roles_txt}")
        lines += ["", "| Mech | Lane | Sessions (host: state) | Responsible claw |", "|---|---|---|---|"]
        for mech in sorted(set(by_mech) | {m for m in (payload.get("canonical_mechs") or []) if isinstance(m, str) and m}):
            states = ", ".join(session_state_cell(r) for r in by_mech.get(mech, [])) or "none"
            lane = mech_lane_label(mech, config, issues=issues)
            lines.append(f"| {mech} | {lane} | {states} | {claw_for_mech(mech, by_mech, config, live, issues=issues) or 'unassigned'} |")
    else:
        lines += ["", "| Mech | Sessions (host: state) | Responsible claw |", "|---|---|---|"]
        for mech in sorted(set(by_mech) | {m for m in (payload.get("canonical_mechs") or []) if isinstance(m, str) and m}):
            states = ", ".join(session_state_cell(r) for r in by_mech.get(mech, [])) or "none"
            lines.append(f"| {mech} | {states} | {claw_for_mech(mech, by_mech, config, live, issues=issues) or 'unassigned'} |")
    if issues:
        lines += ["", "## Backlog", "", "| Mech | Repository | Open issues | Open PRs | Drafts | Failing checks |", "|---|---|---:|---:|---:|---:|"]
        for mech, repo in sorted(issues.items()):
            prs = repo.get("prs", [])
            lines.append(f"| {mech} | {repo.get('repo')} | {repo.get('open_issues', '?')} | {repo.get('open_prs', '?')} | "
                         f"{sum(p['draft'] for p in prs)} | {sum(p['checks'] in ('FAILURE', 'ERROR') for p in prs)} |")
    lines += ["", "## Findings", "", "| ID | Severity | Category | Owner | Finding | Action |", "|---|---|---|---|---|---|"]
    for f in findings:
        lines.append(f"| {f['id']} | {f['severity']} | {f['category']} | {owner_label(f['owner'])} | {f['title']} | {f['action']} |")
    by_owner = Counter(owner_label(f["owner"]) for f in findings)
    lines += ["", "## Delegation summary", ""] + [f"- {k}: {v} findings" for k, v in sorted(by_owner.items())]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- plan

ROLE_BRIEF_RULES = {
    "discovery_supervisor": (
        "- Discovery lane: create net-new record files and source saturation ledgers only (`add/...` branches under "
        "`~/agent-work/<Mech>-discovery/`). Do not edit existing records, schemas or workflows, and stop opening new PRs "
        "on any Mech that reaches {pr_cap} open PRs or a failing check."
    ),
    "stewardship_supervisor": (
        "- Stewardship lane: run `record_review`, `resolve_review_findings`, `open_issue_triage` and failing-PR repairs "
        "under `~/agent-work/<Mech>-steward/`. Pause new record additions on these Mechs until open PRs drop below {pr_cap} "
        "and status checks pass."
    ),
    "infrastructure_supervisor": (
        "- Infrastructure lane: fix shared CI/workflow/schema causes once on `main`, advance `INFRA_TASKS.md` and shared "
        "exporters, and bootstrap explicitly assigned repositories."
    ),
    "closure_steward": (
        "- Closure lane: inspect PR diffs and checks read-only via `gh`, file issues for defects, and prepare per-Mech "
        "verified merge lists for the user. Never merge, push to another session's branch, or edit Mech checkouts."
    ),
    "fleet_maintainer": (
        "- Maintainer lane: run read-only inventories first (`fleet-repo-crud-cleanup`, `disk-usage-report`), clean only "
        "approved regenerable scratch on this host, refresh checkouts at idle checkpoints, and retire duplicate/completed "
        "loops only via the graceful exit protocol."
    ),
    "cycle_supervisor": (
        "- Cycle supervisor lane: verify closure receipts before advancing stages and keep at most one primary loop per Mech."
    ),
}

BRIEF_RULES = """## Rules for this brief
- Re-read live state first; this brief was prepared from a read-only snapshot and can be stale.
- Assess, then act on one Mech end to end (the canary) before fanning out to the rest.
- No merges, no answering approval, question, sign-in, trust or update prompts, no paid provider credits, no mentions of people or accounts.
- Never reset or rewrite ledgers, receipts or status; repair only with verified facts.
- Keep all work in ~/agent-work/ or a repository's .temp/, never in /tmp or $TMPDIR; point test basetemps there too.
- Resuming goals spends usage: propose resumes in the report instead of resuming many sessions.
- Do not start disk-heavy work (KG builds, full-corpus validations, large fixtures) while the host has less than {disk_floor} GiB free; delete only regenerable scratch you created.
"""


def render_brief(run_id: str, claw_key: str, claw: dict, items: list, report_dir: str, thresholds: dict) -> str:
    lines = [f"# XMechBay fleet-ops brief {run_id} for {claw.get('label', claw_key)}", "",
             "Prepared by the XMechBay fleet-ops skill from read-only observation of sessions, host resources and GitHub.", "",
             "## Your Mechs needing attention", ""]
    for f in items:
        evidence = json.dumps(f["evidence"], sort_keys=True)[:400] if f["evidence"] else ""
        lines.append(f"- [{f['severity']}] {f['id']} {f['title']}. Action: {f['action']}" + (f" Evidence: {evidence}" if evidence else ""))
    role_lines = [ROLE_BRIEF_RULES[r].format(pr_cap=thresholds["pr_backlog_warn"]) for r in claw_roles(claw) if r in ROLE_BRIEF_RULES]
    if role_lines:
        lines += ["", "## Role boundaries", ""] + role_lines
    lines += ["", BRIEF_RULES.format(disk_floor=thresholds["disk_critical_gib"]).rstrip(), "", "## Report", "",
              f"Write {report_dir}/report.md and summary.json: one row per Mech (state, action taken, PRs, remaining blockers, decision needed from the user), then stop."]
    return "\n".join(lines) + "\n"


def render_goal(run_id: str, brief_path: str, report_dir: str) -> str:
    text = (f"XMechBay fleet-ops {run_id}: read {brief_path} and carry it out for the Mechs it lists. "
            "Follow its rules: re-read live state, one canary Mech end to end before the rest, no merges, "
            "no answering prompts, no paid credits, nothing in /tmp. "
            f"Write the report it names under {report_dir}, then stop.")
    if len(text) > GOAL_LIMIT:
        raise ValueError("goal text over the 1000-character limit")
    return text


def claw_targets(payload: dict, claw: dict) -> list:
    """Live, controllable claw sessions for a claw config (cached rows and process-only rows excluded)."""
    if not isinstance(claw, dict):
        return []
    claw_host = claw.get("host")
    if not isinstance(claw_host, str) or not claw_host:
        return []
    out = []
    for r in (payload.get("sessions") or []):
        if not isinstance(r, dict) or r.get("role") != "claw" or r.get("host_key") != claw_host or r.get("stale_cached"):
            continue
        if claw.get("repo") and r.get("repo_label") != claw["repo"]:
            continue
        target = str(r.get("target_short") or "")
        match = re.fullmatch(r"T(\d+)/(\d+)", target)
        if r.get("tmux_session"):
            transport = "tmux"
        elif match:
            transport = "terminal"
        else:
            continue  # process-only rows (P123) cannot receive guarded input
        status_str = str(r.get("status") or "")
        flags_set = set(r.get("flags") or ())
        prompt_label = (
            status_str
            if status_str.startswith(HUMAN_PROMPTS)
            else ("subagent-approval" if "subagent-approval" in flags_set else (status_str or "PROMPT"))
        )
        out.append({"target": target, "transport": transport,
                    "window": int(match.group(1)) if match else None, "tab": int(match.group(2)) if match else None,
                    "tmux_session": r.get("tmux_session"), "tmux_window": r.get("tmux_window"),
                    "tty": r.get("tty"), "pid": r.get("pid"), "status": r.get("status"),
                    "has_draft": has_composer_draft(r),
                    "is_pasted_prompt": is_pasted_prompt_row(r),
                    "has_agent_message": is_agent_message_row(r),
                    "is_keystroke_prompt": is_keystroke_prompt_row(r),
                    "keystroke_prompt_label": prompt_label})
    return out


def hint_matches(hint: str, t: dict) -> bool:
    hint = str(hint).strip()
    forms = {t["target"], t.get("tty") or ""}
    if t.get("tty"):
        forms.add("/dev/" + t["tty"])
    if t["window"] is not None:
        forms |= {str(t["window"]), f"T{t['window']}"}
    if t.get("tmux_session"):
        forms |= {t["tmux_session"], t["tmux_window"] or "", f"{t['tmux_session']}:{t['tmux_window']}"}
    return hint in forms - {""}


def claw_matching_targets(payload: dict, claw: dict) -> list:
    """Controllable claw sessions matching this claw's host, repo, tmux_session and target_hint."""
    targets = claw_targets(payload, claw)
    if not targets or not isinstance(claw, dict):
        return []
    if claw.get("tmux_session") is not None:
        sess = valid_tmux_session_name(claw.get("tmux_session"))
        if not sess:
            return []
        targets = [t for t in targets if t["transport"] == "tmux" and t["tmux_session"] == sess]
    raw_hint = claw.get("target_hint")
    if raw_hint is not None:
        hint = str(raw_hint).strip()
        if not hint:
            return []
        targets = [t for t in targets if hint_matches(hint, t)]
    return targets


def controller_tmux_for_claw(claw: dict, config: dict) -> str:
    sess = valid_tmux_session_name(claw.get("tmux_session") if isinstance(claw, dict) else None)
    if sess:
        return sess
    host_cfg = (config.get("hosts") or {}).get(claw.get("host") if isinstance(claw, dict) else None) or {}
    host_tmux = valid_tmux_session_name(host_cfg.get("tmux_session"))
    if host_tmux:
        return host_tmux
    default_tmux = valid_tmux_session_name(config.get("controller_tmux_session"))
    return default_tmux or "fleet"


def choose_target(targets: list, claw: dict, controller_session: str):
    """Return (target or None, reason). Never guesses: a hint must match exactly one candidate."""
    if not targets:
        return None, "no live controllable claw session on this host"
    pool = targets
    if isinstance(claw, dict) and claw.get("tmux_session") is not None:
        claw_tmux = valid_tmux_session_name(claw.get("tmux_session"))
        if not claw_tmux:
            return None, f"configured tmux_session {claw.get('tmux_session')!r} is invalid"
        pool = [t for t in targets if t["transport"] == "tmux" and t["tmux_session"] == claw_tmux]
        if not pool:
            return None, f"no live claw session in configured tmux_session {claw_tmux!r} ({len(targets)} other session(s) on this host)"
    hint = claw.get("target_hint") if isinstance(claw, dict) else None
    if hint is not None:
        hint_str = str(hint).strip()
        if not hint_str:
            return None, "configured target_hint is empty"
        matches = [t for t in pool if hint_matches(hint_str, t)]
        if len(matches) != 1:
            return None, f"target_hint {hint!r} matches {len(matches)} of {len(pool)} live claw sessions"
        chosen = matches[0]
    elif len(pool) == 1:
        chosen = pool[0]
    else:
        return None, f"{len(pool)} live claw sessions on this host; set target_hint after confirming which claw owns these Mechs"
    if chosen["transport"] == "tmux" and chosen["tmux_session"] != controller_session:
        return None, (f"claw runs in tmux session {chosen['tmux_session']!r}, not the controller's owned session "
                      f"{controller_session!r}; that session's owner must deliver the goal")
    if chosen["transport"] == "tmux" and not chosen.get("tmux_window"):
        return None, f"claw {chosen['target']} has no tmux window name; inspect tmux windows before pasting"
    if chosen["transport"] == "terminal" and chosen["tab"] != 1:
        return None, f"claw is in tab {chosen['tab']} of window {chosen['window']}; mac-term pastes into the selected tab, so deliver by hand"
    if chosen.get("has_draft"):
        if chosen.get("is_pasted_prompt"):
            return None, f"claw {chosen['target']} has a pasted prompt not running; send or clear it before pasting"
        return None, f"claw {chosen['target']} has an unsent composer draft; send or clear it before pasting"
    c_status = str(chosen.get("status") or "")
    if chosen.get("is_keystroke_prompt") or c_status.startswith(HUMAN_PROMPTS):
        prompt_name = chosen.get("keystroke_prompt_label") or (
            c_status if c_status.startswith(HUMAN_PROMPTS) else (c_status or "PROMPT")
        )
        return None, f"claw {chosen['target']} waits for a human ({prompt_name}); answer or clear the prompt before pasting"
    if chosen.get("has_agent_message"):
        return None, f"claw {chosen['target']} awaits a user reply to its last message; reply before pasting a new goal"
    if c_status.startswith("Goal hit usage limits"):
        return None, f"claw {chosen['target']} hit usage limits; decide whether to resume before pasting"
    return chosen, "resolved"


def render_decisions_md(decisions: list, claw_summary: dict) -> str:
    lines = ["# Decisions for the user", ""]
    if not decisions:
        lines.append("No user decisions required in this run.")
        return "\n".join(lines) + "\n"
    lines += [
        "Recommended continuation order: (1) submit or clear pasted prompts that are not running, "
        "(2) relaunch plain `agy` approval stalls with `agy-work -c`, (3) answer remaining TUI prompts and agent messages, "
        "(4) restore any missing/stalled claws, then (5) canary-dispatch prepared claw briefs in `DISPATCH.md` and review merge candidates.",
        "",
    ]
    buckets = [
        ("Pasted / unsent prompts not running (press Enter or clear)",
         lambda f: f["owner"].get("key") == "draft"),
        ("Antigravity approvals fixable via agy-work -c",
         lambda f: f["owner"].get("key") == "prompt" and bool((f.get("evidence") or {}).get("agy_work_fixable"))),
        ("Interactive TUI prompts & keystrokes",
         lambda f: f["owner"].get("key") == "prompt" and not bool((f.get("evidence") or {}).get("agy_work_fixable"))),
        ("Agent messages awaiting your plain-text reply",
         lambda f: f["owner"].get("key") == "agent-message"),
        ("Supervisor stalls, missing claws & usage limits",
         lambda f: f["owner"].get("key") in ("claws", "stalled-supervisor", "stalled-other", "usage")),
        ("PR merges & fleet scope",
         lambda _: True),
    ]
    remaining = list(decisions)
    section_num = 0
    for heading, predicate in buckets:
        matched = [f for f in remaining if predicate(f)]
        if not matched:
            continue
        matched_ids = {f["id"] for f in matched}
        remaining = [f for f in remaining if f["id"] not in matched_ids]
        section_num += 1
        lines.append(f"## {section_num}. {heading}")
        lines.append("")
        for f in matched:
            lines.append(f"- [{f['severity']}] {f['id']} {f['title']}. {f['action']}")
        lines.append("")
    if claw_summary:
        ready = [k for k, v in sorted(claw_summary.items()) if v.get("target_resolved")]
        blocked = [k for k, v in sorted(claw_summary.items()) if not v.get("target_resolved")]
        lines.append("## Prepared claw briefs (`DISPATCH.md`)")
        lines.append("")
        if ready:
            ready_text = ", ".join(
                f"`{k}` ({claw_summary[k]['findings']} findings)" for k in ready
            )
            lines.append(f"- Ready for canary-first dispatch after approval: {ready_text}.")
        if blocked:
            blocked_text = ", ".join(
                f"`{k}` ({claw_summary[k]['reason']})" for k in blocked
            )
            lines.append(f"- Waiting on target resolution or blocker clearance before dispatch: {blocked_text}.")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def plan(run_dir: Path, config: dict) -> dict:
    run_dir = ensure_private_dir(run_dir, enforce_leaf_mode=False, create=False)
    findings = json.loads((run_dir / "findings.json").read_text())
    payload = json.loads((run_dir / "sessions.json").read_text())
    run_id = run_dir.name
    out = ensure_private_dir(run_dir / "plan", enforce_leaf_mode=True)
    ensure_private_dir(out / "briefs", enforce_leaf_mode=True)
    ensure_private_dir(out / "goals", enforce_leaf_mode=True)
    hosts = status_hosts(config)
    groups = defaultdict(list)
    for f in findings:
        groups[(f["owner"]["kind"], f["owner"]["key"])].append(f)
    dispatch = [f"# Dispatch plan {run_id}", "",
                "Nothing here has been sent. Dispatch needs the user's approval for this batch; send to ONE claw first (the canary), "
                "verify it accepted the goal and is working, then the rest. Each command is guarded by mac-term.sh: it refuses "
                "unreadable screens and screens showing a prompt. Re-check the target's tty and screen first; window ids are transient.", ""]
    summary = {"run": run_id, "claws": {}, "user_decisions": 0, "maintenance": 0}
    shared_brief_dirs = Counter(
        (c.get("host"), (c.get("brief_dir") or "~/agent-work/claw/fleet-ops").rstrip("/"))
        for c in config["claws"].values() if isinstance(c, dict) and c.get("host")
    )
    for (kind, key), items in sorted(groups.items(), key=lambda kv: (kv[0][0], str(kv[0][1]))):
        if kind != "claw":
            continue
        claw = config["claws"].get(key) or {}
        brief_dir = (claw.get("brief_dir") or "~/agent-work/claw/fleet-ops").rstrip("/")
        safe_key = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(key))
        remote_dir = f"{brief_dir}/{safe_key}/{run_id}" if shared_brief_dirs[(claw.get("host"), brief_dir)] >= 2 else f"{brief_dir}/{run_id}"
        brief = render_brief(run_id, key, claw, items, remote_dir, config["thresholds"])
        goal = render_goal(run_id, f"{remote_dir}/brief.md", remote_dir)
        write_private_file(out / "briefs" / f"{key}.md", brief)
        write_private_file(out / "goals" / f"{key}.txt", goal)
        host_cfg = hosts.get(claw.get("host")) or {}
        dest = "local" if host_cfg.get("transport") == "local" else host_cfg.get("host", "<ssh-host>")
        targets = claw_targets(payload, claw)
        controller = controller_tmux_for_claw(claw, config)
        chosen, reason = choose_target(targets, claw, controller)
        brief_file, goal_file = (out / "briefs" / f"{key}.md").resolve(), (out / "goals" / f"{key}.txt").resolve()
        mt = (ROOT / ".agents/skills/mac-orchestration/scripts/mac-term.sh").resolve()
        dispatch += [f"## {key} ({len(items)} findings, host {claw.get('host')})", "",
                     f"Live claw candidates: {json.dumps(targets)}",
                     ("Chosen target: " + json.dumps(chosen)) if chosen else f"**Not resolved: {reason}. Confirm the claw session before sending.**",
                     "", "```bash"]
        dispatch.append(f"mkdir -p {remote_dir} && cp {shlex.quote(str(brief_file))} {remote_dir}/brief.md" if dest == "local"
                        else f"ssh {dest} 'mkdir -p {remote_dir}' && scp {shlex.quote(str(brief_file))} {dest}:{remote_dir.replace('~/', '')}/brief.md")
        if chosen and chosen["transport"] == "tmux" and chosen.get("tmux_window"):
            dispatch.append(f"XMECHBAY_TMUX_SESSION={shlex.quote(controller)} bash {shlex.quote(str(mt))} -H {dest} tmux-paste "
                            f"{shlex.quote(str(chosen['tmux_window']))} {shlex.quote(str(goal_file))}")
        else:
            window = chosen["window"] if chosen else "<window-id>"
            tty = chosen["tty"] if chosen else "<claw tty>"
            dispatch += [f"# Proceed only if this line shows tty {tty} (the selected tab must be the claw):",
                         f"bash {shlex.quote(str(mt))} -H {dest} windows | grep '|{window}|'",
                         f"bash {shlex.quote(str(mt))} -H {dest} screen {window} 40",
                         f"bash {shlex.quote(str(mt))} -H {dest} paste {window} {shlex.quote(str(goal_file))}",
                         f"bash {shlex.quote(str(mt))} -H {dest} screen {window} 40"]
        dispatch += ["```", ""]
        summary["claws"][key] = {"findings": len(items), "targets": len(targets), "target_resolved": chosen is not None, "reason": reason}
    decisions = [f for f in findings if f["owner"]["kind"] == "user"]
    maintenance = [f for f in findings if f["owner"]["kind"] == "xmechbay"]
    write_private_file(out / "DECISIONS.md", render_decisions_md(decisions, summary["claws"]))
    write_private_file(out / "MAINTENANCE.md", "# XMechBay maintenance\n\n" + "".join(f"- [{f['severity']}] {f['id']} {f['title']}. {f['action']}\n" for f in maintenance))
    write_private_file(out / "DISPATCH.md", "\n".join(dispatch) + "\n")
    summary.update(user_decisions=len(decisions), maintenance=len(maintenance))
    write_private_file(out / "summary.json", json.dumps(summary, indent=1) + "\n")
    return summary


# --------------------------------------------------------------------------- recommend

def ensure_private_dir(directory: Path, enforce_leaf_mode: bool = True, create: bool = True) -> Path:
    target = Path(directory).expanduser()
    if not target.is_absolute():
        target = Path.cwd() / target
    if target.is_symlink() and target not in MACOS_ROOT_SYMLINKS:
        raise ValueError(f"refusing symlink directory: {target}")
    current = Path(target.anchor)
    for part in target.parts[1:]:
        if part in ("", "."):
            continue
        if part == "..":
            current = current.parent
            continue
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            if not create:
                raise SystemExit(f"run directory not found: {target}")
            os.mkdir(current, 0o700)
            os.chmod(current, 0o700)
            info = current.lstat()
        if stat.S_ISLNK(info.st_mode):
            if current in MACOS_ROOT_SYMLINKS and info.st_uid == 0:
                current = current.resolve(strict=True)
                res_info = current.lstat()
                if not stat.S_ISDIR(res_info.st_mode):
                    raise ValueError(f"not a directory: {current}")
                continue
            raise ValueError(f"refusing symlink directory: {current}")
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"not a directory: {current}")
    if enforce_leaf_mode:
        os.chmod(current, 0o700)
    return current


def write_private_file(path: Path, content: str) -> Path:
    expanded = Path(path).expanduser()
    if not expanded.is_absolute():
        expanded = Path.cwd() / expanded
    if expanded.is_symlink():
        raise ValueError(f"refusing symlink output path: {path}")
    parent = ensure_private_dir(expanded.parent, enforce_leaf_mode=False)
    dest = parent / expanded.name
    if dest.is_symlink():
        raise ValueError(f"refusing symlink output path: {path}")
    fd, tmp_name = tempfile.mkstemp(prefix=f".{dest.name}.", dir=str(parent))
    fd_open = True
    try:
        os.fchmod(fd, 0o600)
        stream = os.fdopen(fd, "w", encoding="utf-8")
        fd_open = False
        with stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if dest.is_symlink():
            raise ValueError(f"refusing symlink output path: {path}")
        os.replace(tmp_name, dest)
    finally:
        if fd_open:
            try:
                os.close(fd)
            except OSError:
                pass
        if os.path.lexists(tmp_name):
            os.unlink(tmp_name)
    return dest


def collect_private_host_terms(config: dict, payload: dict | None = None, resources: dict | None = None) -> set:
    out = set()

    def _add(raw_val: object) -> None:
        if not isinstance(raw_val, str):
            return
        term = raw_val.strip()
        if "@" in term:
            term = term.rpartition("@")[2].strip()
        low = term.lower()
        if len(term) < 2 or low in GENERIC_HOST_TOKENS or low in ("127.0.0.1", "::1") or low.endswith(".invalid"):
            return
        out.add(term)
        if "." in term and not re.fullmatch(r"[\d.]+", term):
            short = term.split(".", 1)[0]
            if len(short) >= 3 and short.lower() not in GENERIC_HOST_TOKENS:
                out.add(short)

    for key in (config.get("hosts") or {}):
        _add(key)
    for claw in (config.get("claws") or {}).values():
        if isinstance(claw, dict):
            _add(claw.get("host"))
    for host in ((payload or {}).get("hosts") or []):
        if isinstance(host, dict):
            _add(host.get("key"))
    for row in ((payload or {}).get("sessions") or []):
        if isinstance(row, dict):
            _add(row.get("host_key"))
    for key, rdata in (resources or {}).items():
        _add(key)
        if isinstance(rdata, dict):
            _add(rdata.get("hostname"))
    for key, cfg in status_hosts(config).items():
        _add(key)
        if isinstance(cfg, dict):
            _add(cfg.get("host"))
    return out


def sanitize_public_text(text: str, private_terms=()) -> str:
    """Scrub mentions, emails, local paths, private network names/IPs, tokens and host keys from public drafts."""
    cleaned = CREDENTIAL_TOKEN_RE.sub("<redacted-token>", str(text))
    cleaned = PRIVATE_KEY_HEADER_RE.sub("<redacted-private-key>", cleaned)
    cleaned = CREDENTIAL_ASSIGNMENT_RE.sub(r"\1<redacted-secret>", cleaned)

    def _scrub_email(match: re.Match) -> str:
        addr = match.group("email")
        return addr if addr.lower().endswith(".invalid") else "<redacted-email>"

    cleaned = EMAIL_OR_MENTION_RE.sub(_scrub_email, cleaned)
    cleaned = re.sub(r"(?<!\w)@+(?=[A-Za-z0-9_])", "", cleaned)
    cleaned = UNIX_LOCAL_PATH_RE.sub("<local-path>", cleaned)
    cleaned = WIN_LOCAL_PATH_RE.sub("<local-path>", cleaned)
    cleaned = LOCAL_HOSTNAME_RE.sub("<redacted-host>", cleaned)
    cleaned = PRIVATE_IP_RE.sub("<redacted-ip>", cleaned)
    for term in sorted({str(t) for t in (private_terms or ()) if t}, key=len, reverse=True):
        if len(term) < 2 or term.lower() in GENERIC_HOST_TOKENS:
            continue
        cleaned = re.sub(rf"(?<![A-Za-z0-9_]){re.escape(term)}(?![A-Za-z0-9_])", "<host>", cleaned, flags=re.IGNORECASE)
    return cleaned


def _parse_json_pages(stdout: str) -> list:
    if not stdout or not stdout.strip():
        raise ValueError("empty gh api response")
    decoder = json.JSONDecoder()
    idx, length, items = 0, len(stdout), []
    while idx < length:
        while idx < length and stdout[idx].isspace():
            idx += 1
        if idx >= length:
            break
        page, idx = decoder.raw_decode(stdout, idx)
        if not isinstance(page, list):
            raise ValueError("expected JSON array page from gh api")
        for elem in page:
            if isinstance(elem, list):
                items.extend(elem)
            else:
                items.append(elem)
    return items


def fetch_open_issues_for_repo(repo: str) -> tuple:
    if not is_valid_repo_slug(repo):
        return [], f"invalid repository slug: {repo!r}"
    owner_name, _, repo_name = repo.partition("/")
    try:
        proc = subprocess.run(
            ["gh", "api", "--paginate", f"repos/{owner_name}/{repo_name}/issues?state=open&per_page=100"],
            capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [], str(exc)
    if proc.returncode != 0:
        return [], (proc.stderr or proc.stdout or f"gh exit {proc.returncode}").strip()[-200:]
    try:
        raw_items = _parse_json_pages(proc.stdout)
    except ValueError as exc:
        return [], f"unparseable gh api JSON: {exc}"
    rows = []
    for item in raw_items:
        if not isinstance(item, dict) or item.get("pull_request") is not None:
            continue
        num = item.get("number")
        if isinstance(num, bool) or not isinstance(num, int):
            continue
        rows.append({
            "number": num,
            "title": str(item.get("title") or ""),
            "url": str(item.get("html_url") or item.get("url") or f"https://github.com/{repo}/issues/{num}"),
        })
    return rows, None


def load_open_issue_index(repos_needed: set, open_issues_json: Path | None = None, no_github: bool = False) -> tuple:
    if open_issues_json and no_github:
        raise ValueError("--open-issues-json and --no-github are mutually exclusive")
    needed = sorted({str(r) for r in repos_needed if isinstance(r, str) and r})
    if open_issues_json:
        try:
            raw = json.loads(Path(open_issues_json).read_text())
        except (OSError, ValueError) as exc:
            err = f"failed to read --open-issues-json: {exc}"
            return {}, {r: err for r in needed}, "file"
        if not isinstance(raw, dict):
            err = "--open-issues-json must be a JSON object mapping OWNER/REPO to issue lists"
            return {}, {r: err for r in needed}, "file"
        index, errors = {}, {}
        for repo, rows in raw.items():
            key = str(repo)
            if isinstance(rows, list):
                index[key] = rows
            else:
                errors[key] = "entry in --open-issues-json is not a list"
        for repo in needed:
            if not is_valid_repo_slug(repo):
                errors[repo] = f"invalid repository slug: {repo!r}"
            elif repo not in index and repo not in errors:
                errors[repo] = "repository missing from --open-issues-json"
        return index, errors, "file"
    if no_github:
        return {}, {}, "disabled"
    index, errors = {}, {}
    for repo in needed:
        rows, err = fetch_open_issues_for_repo(repo)
        if err:
            errors[repo] = err
        else:
            index[repo] = rows
    return index, errors, "github"


def _keyword_matches(kw: str, title_l: str) -> bool:
    token = str(kw).strip().lower()
    if not token:
        return False
    prefix_mode = token.endswith("*")
    if prefix_mode:
        token = token[:-1]
    if not token:
        return False
    pattern = re.escape(token)
    if re.match(r"[a-z0-9_]", token[0]):
        pattern = r"(?<![a-z0-9_])" + pattern
    if prefix_mode:
        pattern = pattern + r"[a-z0-9_]*"
    elif re.match(r"[a-z0-9_]", token[-1]):
        pattern = pattern + r"(?![a-z0-9_])"
    return re.search(pattern, title_l) is not None


def match_open_issues(index: dict, repos: list, keyword_groups: list) -> list:
    hits, seen = [], set()
    if not keyword_groups:
        return []
    for repo in repos:
        rows = index.get(repo, [])
        if not isinstance(rows, list):
            continue
        for item in rows:
            if not isinstance(item, dict) or item.get("pull_request") is not None:
                continue
            raw_num = item.get("number")
            if isinstance(raw_num, bool):
                continue
            try:
                number = int(raw_num)
            except (TypeError, ValueError):
                continue
            title = str(item.get("title") or "")
            title_l = title.lower()
            if (repo, number) in seen:
                continue
            if any(all(_keyword_matches(kw, title_l) for kw in group) for group in keyword_groups if group):
                seen.add((repo, number))
                hits.append({"repo": repo, "number": number, "title": title,
                             "url": str(item.get("url") or f"https://github.com/{repo}/issues/{number}")})
    return sorted(hits, key=lambda h: (h["repo"], h["number"]))


def build_recommendation(rule_key: str, priority: str, theme: str, title: str, conclusion: str,
                         recommendation: str, evidence: dict, check_repos: list, match_groups: list,
                         draft_repo: str, draft_title: str, draft_body: str,
                         open_index: dict, open_errors: dict, source_mode: str,
                         related_groups: list | None = None, private_terms=()) -> dict:
    matched = match_open_issues(open_index, check_repos, match_groups)
    related = [h for h in match_open_issues(open_index, check_repos, related_groups or [])
               if (h["repo"], h["number"]) not in {(m["repo"], m["number"]) for m in matched}]
    repo_errs = {r: open_errors[r] for r in check_repos if r in open_errors}
    if not is_valid_repo_slug(draft_repo):
        repo_errs.setdefault(str(draft_repo), f"invalid repository slug: {draft_repo!r}")
    if matched:
        status = "tracked"
    elif source_mode == "disabled":
        status = "unverified_offline"
    elif repo_errs:
        status = "lookup_failed"
    else:
        status = "new_issue"
    draft = None
    if status != "tracked":
        body = sanitize_public_text(draft_body.rstrip(), private_terms=private_terms)
        if related:
            rel_lines = ", ".join(f"{r['repo']}#{r['number']}" for r in related)
            body += f"\n\n### Related issues\n- {sanitize_public_text(rel_lines, private_terms=private_terms)}"
        draft = {
            "repo": sanitize_public_text(draft_repo, private_terms=private_terms),
            "title": sanitize_public_text(draft_title, private_terms=private_terms),
            "body": body + "\n",
        }
    return {
        "rule": rule_key,
        "priority": priority,
        "theme": theme,
        "title": title,
        "conclusion": conclusion,
        "recommendation": recommendation,
        "evidence": evidence,
        "tracking": {
            "status": status,
            "checked_repos": check_repos,
            "matched_issues": matched,
            "related_issues": related,
            "lookup_errors": repo_errs,
            "issue_draft": draft,
        },
    }


def synthesize_recommendations(payload: dict, resources: dict, issues: dict, findings: list, config: dict,
                               open_index: dict | None = None, open_errors: dict | None = None,
                               source_mode: str = "disabled", private_terms=None) -> list:
    t = config["thresholds"]
    infra = config["infra_repos"]
    xmb, claw_repo = infra["xmechbay"], infra["claw"]
    open_index, open_errors = open_index or {}, open_errors or {}
    if private_terms is None:
        private_terms = collect_private_host_terms(config, payload, resources)
    hosts = [h for h in (payload.get("hosts") or []) if isinstance(h, dict) and isinstance(h.get("key"), str) and h.get("key")]
    reachable_hosts = [h for h in hosts if h.get("reachable") is not False]
    live_sessions = [r for r in (payload.get("sessions") or []) if isinstance(r, dict) and not r.get("stale_cached")]
    by_mech = mech_sessions(live_sessions)
    recs = []

    # 1. Tmux collector false probe failure & named-socket blindness
    tmux_err_hosts = [h["key"] for h in reachable_hosts if "tmux_probe_failed" in (h.get("errors") or ())]
    blind_by_host = defaultdict(list)
    tty_blind_rows = []
    headless_blind_rows = []
    for row in live_sessions:
        hk = row.get("host_key")
        if isinstance(hk, str) and hk and is_blind_process_row(row):
            blind_by_host[hk].append(row)
            tty_val = str(row.get("tty") or "").strip()
            if tty_val and tty_val not in ("?", "??"):
                tty_blind_rows.append(row)
            else:
                headless_blind_rows.append(row)
    total_blind = sum(len(v) for v in blind_by_host.values())
    if tmux_err_hosts or len(tty_blind_rows) >= t["blind_sessions_warn"]:
        parts = []
        if tmux_err_hosts:
            parts.append(f"`tmux_probe_failed` fired on {len(tmux_err_hosts)} of {max(len(reachable_hosts), 1)} reachable host(s)")
        if tty_blind_rows:
            parts.append(f"{len(tty_blind_rows)} PTY-backed session(s) resolved only as process-only `P*` (`screen_unavailable`) rows")
        signal_summary = " and ".join(parts)
        recs.append(build_recommendation(
            "R-TMUX-COLLECT", "P1", "observability",
            "Tmux probe false errors on absent default server and named-socket blindness",
            (f"{signal_summary}. `fleet_collect.py` runs `tmux list-panes -a` solely against the default socket with `check=True`, "
             "conflating an absent default server with a collector error and missing panes hosted on named `-L`/`-S` tmux sockets."),
            "Update `fleet_collect.py` to classify an absent default tmux server as `not_running` (zero errors) and enumerate accessible sockets in the user tmux socket directory or host config.",
            {"tmux_probe_failed_hosts": tmux_err_hosts, "tty_blind_sessions": len(tty_blind_rows),
             "blind_counts": {k: len(v) for k, v in sorted(blind_by_host.items())}},
            [xmb], [("tmux_probe_failed",), ("fleet_collect", "tmux"), ("named", "socket*", "tmux"), ("absent", "default", "tmux")],
            xmb,
            "fleet-session-status: distinguish absent default tmux server from probe failure and discover named sockets",
            (f"### Problem\n"
             f"In live fleet status collection, {signal_summary}.\n\n"
             f"### Root cause\n"
             f"1. `fleet_collect.py` invokes `tmux list-panes -a` with `stderr=DEVNULL` and `check=True` against only the default socket, "
             f"turning normal `no server running` exit codes into `tmux_probe_failed` (`collection partial`).\n"
             f"2. Sessions hosted on explicit named tmux servers (`tmux -L <name>` or `-S <path>`) are never enumerated by the collector.\n\n"
             f"### Proposed fix\n"
             f"- Distinguish `no server running` / missing default socket (`coverage.tmux = \"not_running\"`, no error) from real `tmux` failures.\n"
             f"- Discover accessible sockets under the user tmux socket directory (plus optional configured socket names) and record socket identity on pane targets.\n"
             f"- Add offline unit tests in `tests/test_fleet_collect.py` covering absent default server, named sockets, and permission/error isolation."),
            open_index, open_errors, source_mode, related_groups=[("tmux", "standard session transport")], private_terms=private_terms,
        ))

    # 2. Detached / unreadable worker batch spawns & partial host discovery
    max_blind_host, max_blind = max(((k, len(v)) for k, v in blind_by_host.items()), key=lambda kv: kv[1], default=(None, 0))
    partial_hosts = [h["key"] for h in reachable_hosts
                     if h.get("inventory_state") == "partial" or any(e in ("agent_discovery_partial", "terminal_window_unreadable", "terminal_probe_failed") for e in (h.get("errors") or ()))]
    if max_blind >= 5 or (partial_hosts and total_blind >= t["blind_sessions_warn"]) or len(headless_blind_rows) >= t["blind_sessions_warn"]:
        host_total = sum(1 for r in live_sessions if r.get("host_key") == max_blind_host) or max_blind
        parts = []
        if max_blind:
            parts.append(f"up to {max_blind} of {host_total} live sessions on one worker host lack a readable Terminal or tmux screen")
        if partial_hosts:
            parts.append(f"{len(partial_hosts)} host(s) reported partial process discovery or unreadable Terminal windows")
        signal_summary = "; ".join(parts)
        recs.append(build_recommendation(
            "R-BLIND-SPAWN", "P1", "observability",
            "Detached worker processes and partial host discovery prevent safe supervision",
            (f"Observed blind worker state: {signal_summary}. Process-only (`P*`) sessions "
             "cannot be inspected or given guarded input (`mac-term.sh` rightly refuses them), and partial inventory suppresses departure tracking."),
            "Audit worker-launch helpers and supervisor briefs so every supervised Mech loop starts inside an observable tmux window rather than a detached background process, and diagnose `agent_discovery_partial` / `terminal_window_unreadable` on degraded hosts.",
            {"max_blind_host": max_blind_host, "max_blind_sessions": max_blind, "headless_blind_sessions": len(headless_blind_rows),
             "partial_hosts": partial_hosts,
             "blind_projects": sorted({str(r.get("project_label") or "?") for rows in blind_by_host.values() for r in rows})[:20]},
            [xmb, claw_repo],
            [("no readable screen",), ("detached", "worker*"), ("agent_discovery_partial",), ("terminal_window_unreadable",)],
            xmb,
            "fleet-ops / session contract: guard against detached worker spawns without readable Terminal or tmux panes",
            (f"### Problem\n"
             f"Fleet observation recorded blind worker state ({signal_summary}).\n\n"
             f"### Impact\n"
             f"- Process-only agent loops have no readable screen evidence and cannot receive guarded `mac-term.sh` control.\n"
             f"- `agent_discovery_partial` suppresses host departure counting so silent crashes go unnoticed.\n\n"
             f"### Proposed fix\n"
             f"- Require worker launch helpers and supervisor briefs to spawn loops inside named tmux windows rather than detached background processes, and refuse headless worker launches in preflight checks.\n"
             f"- Document remediation steps for `terminal_window_unreadable` and `agent_discovery_partial` in the fleet operations playbook."),
            open_index, open_errors, source_mode, related_groups=[("tmux", "standard session transport"), ("role and status file",)],
            private_terms=private_terms,
        ))

    # 3. Missing, stale, or degraded ephemeral-port health watch
    missing_health = [h["key"] for h in reachable_hosts if (h.get("health") or {}).get("status") in (None, "unknown")]
    stale_health = [h["key"] for h in reachable_hosts
                    if (h.get("health") or {}).get("status") == "stale" and is_live_host_observation(payload, h)]
    degraded_health = [h["key"] for h in reachable_hosts
                       if (h.get("health") or {}).get("status") in ("warning", "critical")
                       or (h.get("health") or {}).get("process_status") in ("warning", "critical")]
    if missing_health or stale_health or degraded_health:
        prio = "P1" if degraded_health else "P2"
        parts = []
        if missing_health:
            parts.append(f"{len(missing_health)} of {max(len(reachable_hosts), 1)} reachable host(s) have `health.status = unknown` (port-watch LaunchAgent not reporting)")
        if stale_health:
            parts.append(f"{len(stale_health)} host(s) report stale `port-watch` telemetry")
        if degraded_health:
            parts.append(f"{len(degraded_health)} host(s) report warning/critical listener socket pressure")
        signal_summary = "; ".join(parts)
        recs.append(build_recommendation(
            "R-PORT-WATCH", prio, "observability",
            "Ephemeral TCP port-watch telemetry is missing, stale, or degraded on observed hosts",
            f"{signal_summary}. High-session hosts without fresh `port-watch` reports cannot detect runaway localhost listeners before macOS ephemeral port exhaustion.",
            "Install or repair the `port-watch` LaunchAgent on every configured host and add a fleet-wide readiness/install check in `XMechBay`.",
            {"missing_health_hosts": missing_health, "stale_health_hosts": stale_health, "degraded_health_hosts": degraded_health},
            [xmb], [("port-watch", "missing"), ("port-watch", "install*"), ("port-watch", "launchagent"), ("port-watch", "fleet"), ("port-watch", "stale")],
            xmb,
            "port-watch: audit and enforce LaunchAgent health reporting across all configured fleet hosts",
            (f"### Problem\n"
             f"In multi-host fleet observation, {signal_summary}.\n\n"
             f"### Proposed fix\n"
             f"- Add a read-only verification / install helper across all hosts in `fleet-status` config confirming that the `port-watch` LaunchAgent is loaded and writing fresh health reports.\n"
             f"- Remediate runaway localhost listeners at owned idle checkpoints on hosts reporting warning/critical socket occupancy."),
            open_index, open_errors, source_mode, private_terms=private_terms,
        ))

    # 4. Claw dispatch readiness: composer drafts, awaiting replies, multi-claw ambiguity, missing claws
    draft_claws = [f"{r.get('host_key')}:{r.get('target_short')}" for r in live_sessions
                   if r.get("role") == "claw" and isinstance(r.get("host_key"), str) and r.get("host_key") and has_composer_draft(r)]
    reply_claws = [f"{r.get('host_key')}:{r.get('target_short')}" for r in live_sessions
                   if r.get("role") == "claw" and isinstance(r.get("host_key"), str) and r.get("host_key") and is_agent_message_row(r)]
    claws_per_host = Counter(r["host_key"] for r in live_sessions
                             if r.get("role") == "claw" and isinstance(r.get("host_key"), str) and r.get("host_key"))
    multi_claw_hosts = {h: c for h, c in sorted(claws_per_host.items()) if c >= 2}
    dead_claws = [k for k, c in sorted(config["claws"].items()) if not claw_matching_targets(payload, c)]
    if draft_claws or reply_claws or multi_claw_hosts or dead_claws:
        parts = []
        if draft_claws:
            parts.append(f"{len(draft_claws)} live claw session(s) hold unsent composer drafts")
        if reply_claws:
            parts.append(f"{len(reply_claws)} live claw session(s) await a user reply")
        if multi_claw_hosts:
            parts.append(f"{len(multi_claw_hosts)} host(s) run multiple concurrent claw sessions")
        if dead_claws:
            parts.append(f"{len(dead_claws)} configured claw(s) have no live controllable session")
        signal_summary = "; ".join(parts)
        recs.append(build_recommendation(
            "R-CLAW-DISPATCH", "P1", "supervision",
            "Claw dispatch requires role-status identity and live controllable supervisor sessions",
            (f"{signal_summary}. Static window `target_hint` IDs drift when Terminal or tmux restarts, "
             "and missing, blocked or ambiguous supervisor sessions block automated delegation."),
            "Publish machine-readable claw role/status files so `fleet-ops` can route findings and resolve targets by role rather than static window hints, retire duplicate coordinator claws, and ensure configured claws run in named tmux sessions.",
            {"draft_claws": draft_claws, "reply_claws": reply_claws, "multi_claw_hosts": multi_claw_hosts, "dead_claws": dead_claws},
            [xmb, claw_repo],
            [("label claw sessions by role",), ("route findings by claw role",), ("role and status file",), ("claw", "status file*")],
            xmb,
            "Delegation: resolve claw roles and dispatch targets from machine-readable claw status files",
            (f"### Problem\n"
             f"Fleet delegation check observed: {signal_summary}.\n\n"
             f"### Proposed fix\n"
             f"- Publish machine-readable claw role/status files per supervisor checkout and route `fleet-ops` findings by role instead of host catch-alls.\n"
             f"- Ensure every configured claw runs inside a persistent named tmux session and retire leftover duplicate coordinator claws."),
            open_index, open_errors, source_mode, private_terms=private_terms,
        ))

    # 5. Manifest vs CANONICAL_MECHS drift
    untracked_mechs = sorted(set(by_mech) - set(issues)) if issues else []
    if untracked_mechs:
        recs.append(build_recommendation(
            "R-MANIFEST-CLASSIFICATION", "P2", "observability",
            "Canonical Mech list drift between live sessions and fleet manifest backlog",
            f"{len(untracked_mechs)} active Mech loop(s) ({', '.join(untracked_mechs)}) are absent from the backlog manifest query.",
            "Derive `fleet-session-status` Mech membership directly from the claw fleet manifest and complete manifest onboarding for active Mechs.",
            {"mechs_missing_from_manifest_backlog": untracked_mechs},
            [xmb, claw_repo],
            [("derive the mech list", "manifest"), ("canonical_mechs",), ("mech membership", "manifest"), ("not a declared fleet member",)],
            xmb,
            "fleet-session-status: derive canonical Mech membership from the claw fleet manifest",
            (f"### Problem\n"
             f"`CANONICAL_MECHS` in `fleet_view.py` and the claw fleet manifest can drift, leaving {len(untracked_mechs)} active Mech repository loop(s) omitted from backlog queries or misclassified.\n\n"
             f"### Proposed fix\n"
             f"- Read canonical Mech membership from the fleet manifest with a static fallback for standalone use."),
            open_index, open_errors, source_mode, private_terms=private_terms,
        ))

    # 6. Per-repository systemic CI check-failure cohorts
    for mech, repo_data in sorted(issues.items()):
        if not isinstance(repo_data, dict) or repo_data.get("error"):
            continue
        prs = repo_data.get("prs", [])
        failing = [p["number"] for p in prs if isinstance(p, dict) and p.get("checks") in ("FAILURE", "ERROR")]
        if len(failing) >= t["pr_ci_cohort_min"] and len(failing) / max(len(prs), 1) >= t["pr_ci_cohort_ratio"]:
            repo_slug = repo_data.get("repo") or xmb
            drafts = sum(1 for p in prs if isinstance(p, dict) and p.get("draft"))
            recs.append(build_recommendation(
                f"R-CI-COHORT-{mech.upper()}", "P1", "workflow",
                f"{mech}: systemic CI failure across {len(failing)} of {len(prs)} sampled open PRs",
                (f"{len(failing)} of {len(prs)} sampled open PRs ({round(100 * len(failing) / len(prs))}%) in `{repo_slug}` fail status checks "
                 f"({drafts} drafts). Per the fleet operations playbook, mass check failures indicate one shared base-branch, pin, or workflow cause rather than independent PR defects."),
                f"Pause new PR creation in `{repo_slug}`, isolate the shared failing check from 2-3 representative runs, fix it once on `main`, and batch-revalidate.",
                {"mech": mech, "repo": repo_slug, "failing_prs": len(failing), "sampled_prs": len(prs), "sample_numbers": failing[:15]},
                [repo_slug],
                [("open prs", "fail*"), ("prs", "failing checks"), ("prs", "fail*", "check*"), ("shared", "ci", "fail*")],
                repo_slug,
                f"CI: {len(failing)} of {len(prs)} sampled open PRs fail checks (shared root-cause fix)",
                (f"### Problem\n"
                 f"`fleet-ops` observed {len(failing)} of {len(prs)} sampled open PRs ({drafts} drafts) failing checks in `{repo_slug}` "
                 f"(sample PRs: {', '.join(f'#{n}' for n in failing[:10])}).\n\n"
                 f"### Proposed fix\n"
                 f"- Inspect 2-3 representative failing workflow runs to identify the shared root cause (e.g., pinned source/site check, schema/dependency drift).\n"
                 f"- Fix the shared cause once on `main` before rebasing or rerunning open PRs."),
                open_index, open_errors, source_mode, private_terms=private_terms,
            ))

    # 7. Fleet-wide PR backlog accumulation, >100 PR truncation, and publication stalls
    stalled_mechs = sorted({f["mech"] for f in findings if f.get("mech") and "goal stalled" in f["title"]})
    over_wip = {m: d["open_prs"] for m, d in sorted(issues.items()) if isinstance(d, dict) and not d.get("error") and d.get("open_prs", 0) >= t["pr_backlog_warn"]}
    truncated_100 = {m: d["open_prs"] for m, d in sorted(issues.items()) if isinstance(d, dict) and not d.get("error") and d.get("open_prs", 0) > 100}
    if len(stalled_mechs) >= 3 or len(over_wip) >= 3 or truncated_100:
        parts = []
        if stalled_mechs:
            parts.append(f"{len(stalled_mechs)} Mech(s) are in `Goal stalled`")
        if over_wip:
            parts.append(f"{len(over_wip)} Mech(s) exceed the {t['pr_backlog_warn']}-PR backlog warning ({sum(over_wip.values())} open PRs across those queues)")
        if truncated_100:
            parts.append(f"{len(truncated_100)} repository queue(s) exceed the 100-PR single-page GraphQL window")
        signal_summary = "; ".join(parts)
        recs.append(build_recommendation(
            "R-CLOSURE-THROUGHPUT", "P1", "workflow",
            "PR generation outpaces closure, stalling Mech loops and truncating >100-PR queries",
            f"{signal_summary}.",
            "Enforce per-Mech open-PR WIP caps before new discovery stages, route green PR cohorts to a dedicated closure steward, paginate `collect_issues` past 100 PRs, and align automatic-approval scope for routine PR publication.",
            {"stalled_mechs": stalled_mechs, "over_wip_open_prs": over_wip, "over_100_open_prs": truncated_100},
            [claw_repo, xmb],
            [("pr production outpaces review",), ("open-pr", "limit*"), ("open-pr", "wip"), ("automatic approval review refuses publication",), ("first 100 open prs",)],
            claw_repo,
            "Fleet workflow: enforce per-Mech open-PR WIP limits and unblock publication closure stalls",
            (f"### Problem\n"
             f"Fleet backlog analysis observed: {signal_summary}.\n\n"
             f"### Proposed fix\n"
             f"- Gate new record-production cycles when a Mech exceeds its open-PR WIP cap.\n"
             f"- Batch adversarial review and stale-draft triage via the closure steward role, and paginate backlog queries past 100 PRs."),
            open_index, open_errors, source_mode, private_terms=private_terms,
        ))

    # 8. Swap saturation, multi-host duplicate Mech loops, and session sprawl
    swap_hosts = [h for h, r in sorted(resources.items())
                  if isinstance(h, str) and h and isinstance(r, dict) and not r.get("error") and (r.get("swap") or {}).get("total_gib")
                  and ((r["swap"].get("used_gib") or 0) >= t["swap_warn_min_gib"])
                  and ((r["swap"].get("used_gib") or 0) / r["swap"]["total_gib"] >= t["swap_warn_ratio"])]
    multi_mechs = {m: sorted({r["host_key"] for r in rows if isinstance(r.get("host_key"), str) and r.get("host_key")}) for m, rows in sorted(by_mech.items())}
    multi_mechs = {m: hs for m, hs in multi_mechs.items() if len(hs) >= 2}
    sprawl_hosts = {f["host"]: f["evidence"].get("non_working") for f in findings if f.get("host") and "non-working agent sessions across all roles" in f["title"]}
    achieved_hosts = {f["host"]: len(f["evidence"].get("projects", [])) for f in findings if f.get("host") and "parked at Goal achieved" in f["title"]}
    if len(swap_hosts) >= 2 or len(multi_mechs) >= 2 or sprawl_hosts or achieved_hosts:
        parts = []
        if swap_hosts:
            parts.append(f"{len(swap_hosts)} host(s) exceed {int(t['swap_warn_ratio'] * 100)}% swap usage")
        if multi_mechs:
            parts.append(f"{len(multi_mechs)} Mech(s) have concurrent sessions across multiple hosts")
        if sprawl_hosts:
            parts.append(f"{len(sprawl_hosts)} host(s) exceed {t['non_working_sessions_warn']} non-working sessions across all roles")
        if achieved_hosts:
            parts.append(f"{len(achieved_hosts)} host(s) keep long-parked `Goal achieved` loops open")
        signal_summary = "; ".join(parts)
        sprawl_title = ("Fleet-wide swap saturation and idle/duplicate session sprawl"
                        if swap_hosts else "Duplicate multi-host Mech loops and idle/completed session sprawl")
        recs.append(build_recommendation(
            "R-SWAP-SESSION-SPRAWL", "P1", "capacity",
            sprawl_title,
            f"{signal_summary}.",
            "Assign each active Mech to a single primary host/loop, gracefully exit duplicate idle and parked `Goal achieved` sessions at owned checkpoints, and cap concurrent sessions per host.",
            {"swap_hosts": swap_hosts, "multi_host_mechs": multi_mechs, "sprawl_hosts": sprawl_hosts, "achieved_hosts": achieved_hosts},
            [xmb],
            [("one primary loop per mech",), ("retire duplicate idle sessions",), ("swap near full",), ("session sprawl",)],
            xmb,
            "Fleet memory: enforce one primary loop per Mech and retire idle/completed session sprawl",
            (f"### Problem\n"
             f"Fleet session and memory audit observed: {signal_summary}.\n\n"
             f"### Proposed fix\n"
             f"- Enforce one primary host/session per Mech and retire duplicate idle or long-achieved sessions via the graceful exit protocol."),
            open_index, open_errors, source_mode, private_terms=private_terms,
        ))

    # 9a. Codex rollout log growth
    codex_hosts = {h: (r.get("codex_sessions") or {}).get("gib") for h, r in sorted(resources.items())
                   if isinstance(h, str) and h and isinstance(r, dict) and not r.get("error")
                   and ((r.get("codex_sessions") or {}).get("gib") or 0) >= t["codex_sessions_warn_gib"]}
    low_disk = {h: (r.get("disk") or {}).get("free_gib") for h, r in sorted(resources.items())
                if isinstance(h, str) and h and isinstance(r, dict) and not r.get("error")
                and (r.get("disk") or {}).get("free_gib") is not None and r["disk"]["free_gib"] < t["disk_warn_gib"]}
    if codex_hosts:
        total_codex_gib = round(sum(codex_hosts.values()), 1)
        disk_note = f" while {len(low_disk)} host(s) also have < {t['disk_warn_gib']} GiB free disk" if low_disk else ""
        recs.append(build_recommendation(
            "R-CODEX-LOG-ARCHIVE", "P1", "capacity",
            "Unarchived Codex session rollouts accumulate significant host disk usage",
            f"{len(codex_hosts)} host(s) hold >= {t['codex_sessions_warn_gib']} GiB in `.codex/sessions` ({total_codex_gib} GiB combined){disk_note}.",
            "Deploy verified archiving of closed Codex rollouts older than 14 days into checksummed `tar.zst` bundles before removing originals.",
            {"codex_sessions_gib": codex_hosts, "low_disk_free_gib": low_disk},
            [xmb],
            [("archiving of closed codex session logs",), ("codex session logs", "archiv*")],
            xmb,
            "Maintained, verified archiving of closed Codex session logs",
            (f"### Problem\n"
             f"{len(codex_hosts)} host(s) hold >= {t['codex_sessions_warn_gib']} GiB in `.codex/sessions` ({total_codex_gib} GiB combined){disk_note}.\n\n"
             f"### Proposed fix\n"
             f"- Archive closed Codex session rollouts older than 14 days into verified per-day `tar.zst` archives before removing originals."),
            open_index, open_errors, source_mode, private_terms=private_terms,
        ))

    # 9b. Low free disk headroom
    if low_disk:
        crit_disk = {h: v for h, v in low_disk.items() if v < t["disk_critical_gib"]}
        prio = "P0" if crit_disk else "P1"
        crit_note = f" ({len(crit_disk)} below critical {t['disk_critical_gib']} GiB floor)" if crit_disk else ""
        recs.append(build_recommendation(
            "R-DISK-HEADROOM", prio, "capacity",
            "Low free disk headroom threatens builds and tmp-state guard rescues",
            f"{len(low_disk)} host(s) have less than {t['disk_warn_gib']} GiB free disk{crit_note} (minimum observed: {min(low_disk.values())} GiB).",
            "Run a read-only space inventory (`disk-usage-report` / `fleet-repo-crud-cleanup`), pause disk-heavy KG/corpus builds below the critical floor, and remove only approved, verified-regenerable artifacts.",
            {"low_disk_free_gib": low_disk, "critical_disk_hosts": crit_disk},
            [xmb],
            [("free disk", "headroom"), ("free-disk", "gates"), ("low-disk", "hosts"), ("low free disk", "fleet"), ("disk-heavy", "floor")],
            xmb,
            "Fleet capacity: enforce preflight free-disk gates and reclaim regenerable build scratch on low-disk hosts",
            (f"### Problem\n"
             f"{len(low_disk)} observed host(s) have < {t['disk_warn_gib']} GiB free disk{crit_note}, risking failed builds and skipped `tmp-state` rescues.\n\n"
             f"### Proposed fix\n"
             f"- Gate disk-heavy workflows when host free space is below {t['disk_critical_gib']} GiB and run reviewed `fleet-repo-crud-cleanup` passes."),
            open_index, open_errors, source_mode,
            related_groups=[("nearly full disk",), ("disk-proposal",), ("disk-audit",)],
            private_terms=private_terms,
        ))

    # 10. tmp-state guard incomplete / unrescued / missing entries
    guard_hosts = {f["host"]: f["title"] for f in findings if "tmp-state guard" in f["title"] and f.get("host")}
    if guard_hosts:
        has_unrescued = any("unrescued" in v for v in guard_hosts.values())
        has_incomplete = any("incomplete" in v for v in guard_hosts.values())
        prio = "P0" if has_unrescued else "P2"
        if has_unrescued:
            guard_match = [("tmp-state guard", "unrescued"), ("tmp-state guard", "skip*"), ("tmp-state guard", "entry cap")]
            guard_draft_title = "tmp-state guard: resolve unrescued temporary-root work across fleet hosts"
        elif has_incomplete:
            guard_match = [("tmp-state guard", "incomplete"), ("tmp-state guard", "unknown")]
            guard_draft_title = "tmp-state guard: resolve incomplete durability status and unknown entries"
        else:
            guard_match = [("tmp-state guard", "not installed"), ("tmp-state guard", "no report"), ("tmp-state guard", "stale")]
            guard_draft_title = "tmp-state guard: install or repair stale LaunchAgent reporting across fleet hosts"
        recs.append(build_recommendation(
            "R-TMP-GUARD", prio, "capacity",
            "tmp-state guard runs report non-clean durability status",
            f"`tmp-state` guard flagged {len(guard_hosts)} host(s), preventing clean exit-0 durability verification across `/tmp` and `$TMPDIR`.",
            "Install/verify the `tmp-state` LaunchAgent, classify gutted empty clones cleanly, exclude regenerable ignored build outputs from rescue sizing, and rescue small high-risk entries before hitting disk-floor caps.",
            {"guard_findings": guard_hosts},
            [xmb],
            guard_match,
            xmb,
            guard_draft_title,
            (f"### Problem\n"
             f"`tmp-state` guard reported non-clean status across {len(guard_hosts)} observed host(s).\n\n"
             f"### Proposed fix\n"
             f"- Ensure the LaunchAgent runs cleanly on every host, handle already-cleaned clones without failing as unknown, and prioritize small real-work rescues near the free-space floor."),
            open_index, open_errors, source_mode, private_terms=private_terms,
        ))

    # 11. Claude Code 30-day transcript auto-deletion
    claude_hosts = {f["host"]: f["evidence"] for f in findings if "Claude Code deletes old transcripts" in f["title"] and f.get("host")}
    if claude_hosts:
        recs.append(build_recommendation(
            "R-CLAUDE-RETENTION", "P1", "capacity",
            "Claude Code config directories retain the 30-day transcript auto-deletion default",
            (f"{len(claude_hosts)} host(s) have Claude Code configuration directories (`.claude*`) with unset or <365-day `cleanupPeriodDays`, "
             "causing Claude Code to silently delete local transcripts older than 30 days and lose session provenance."),
            "Add an idempotent XMechBay helper/check that archives existing Claude transcripts, backs up each `settings.json`, and sets `cleanupPeriodDays` >= 3650 across all `.claude*` profiles.",
            {"hosts": claude_hosts},
            [xmb],
            [("cleanupperioddays",), ("claude", "transcript*", "delet*"), ("claude", "transcript*", "archiv*"), ("claude", "retention")],
            xmb,
            "durability: archive Claude Code transcripts and enforce cleanupPeriodDays across fleet config dirs",
            (f"### Problem\n"
             f"`host_resources_probe.py` detected {len(claude_hosts)} host(s) where Claude Code config directories (`.claude`, `.claude-work`, etc.) "
             f"leave `cleanupPeriodDays` unset (default 30 days) or below 365 days, risking silent deletion of session transcripts and research provenance.\n\n"
             f"### Proposed fix\n"
             f"- Provide an idempotent durability script (with `--check` and `--apply`) that archives existing transcripts, backs up each `<dir>/settings.json` as `settings.json.bak-cleanupPeriodDays-<YYYYMMDD>`, and sets `cleanupPeriodDays` to `3650`.\n"
             f"- Verify the setting across all `.claude*` config directories on every fleet host."),
            open_index, open_errors, source_mode, private_terms=private_terms,
        ))

    # 12. Antigravity sessions launched without agy-work stalling on routine approvals
    agy_fixable_rows = [r for r in live_sessions if is_agy_work_fixable_row(r)]
    if agy_fixable_rows:
        agy_targets = [f"{r.get('host_key')}:{r.get('target_short')}" for r in agy_fixable_rows]
        agy_hosts = sorted({str(r.get("host_key") or "") for r in agy_fixable_rows if r.get("host_key")})
        agy_projects = sorted({str(r.get("project_label") or "?") for r in agy_fixable_rows})[:20]
        recs.append(build_recommendation(
            "R-AGY-WORK-PERMISSIONS", "P1", "supervision",
            "Antigravity sessions launched without agy-work stall on routine tool approvals",
            (f"{len(agy_fixable_rows)} live Antigravity session(s) across {max(len(agy_hosts), 1)} host(s) are blocked on `APPROVAL` "
             "with `skip_permissions = false` (`agy_work_fixable`). Plain `agy` launches stop on routine approved tool calls that "
             "`agy-work` (`--dangerously-skip-permissions` paired with standing Ask rules) handles cleanly."),
            "Launch and resume supervised Antigravity working loops via `agy-work` / `agy-work -c` while keeping destructive and outbound operations on standing Ask rules.",
            {"count": len(agy_fixable_rows), "hosts": agy_hosts, "targets": agy_targets[:20], "projects": agy_projects},
            [xmb],
            [("agy-work", "approval*"), ("agy-work", "permission*"), ("skip-permissions", "agy"), ("plain agy", "approval*")],
            xmb,
            "agy-permissions / fleet-ops: launch working Antigravity loops via agy-work to avoid routine approval stalls",
            (f"### Problem\n"
             f"`fleet-ops` observed {len(agy_fixable_rows)} live Antigravity session(s) across {max(len(agy_hosts), 1)} host(s) "
             f"blocked on `APPROVAL` prompts with `skip_permissions = false` (`agy_work_fixable`).\n\n"
             f"### Proposed fix\n"
             f"- Ensure launcher helpers and operator runbooks start or resume supervised Antigravity working loops via `agy-work` / `agy-work -c`.\n"
             f"- Preserve standing Ask rules for destructive filesystem/Git operations and outbound communications while eliminating routine read/test approval stalls."),
            open_index, open_errors, source_mode, private_terms=private_terms,
        ))

    recs.sort(key=lambda r: (PRIORITY_ORDER.get(r["priority"], 9), r["theme"], r["rule"]))
    for idx, rec in enumerate(recs, 1):
        rec["id"] = f"R{idx:02d}"
    return recs


def render_recommendations_md(run_id: str, recs: list, source_mode: str, open_errors: dict) -> str:
    lines = [
        f"# Fleet systemic conclusions & recommendations ({run_id})", "",
        f"Open-issue reconciliation mode: `{source_mode}`."
        + (f" Lookup warnings: `{json.dumps(open_errors, sort_keys=True)}`." if open_errors else "")
        + " Nothing has been filed on GitHub; creating any issue requires explicit user approval.", "",
        "## Summary table", "",
        "| ID | Priority | Theme | Status | Tracked / Target | Title |",
        "|---|---|---|---|---|---|",
    ]
    for r in recs:
        tr = r["tracking"]
        rid = r.get("id") or r.get("rule", "R00")
        if tr["matched_issues"]:
            ref = ", ".join(f"{m['repo']}#{m['number']}" for m in tr["matched_issues"][:4])
        else:
            ref = (tr["issue_draft"] or {}).get("repo", "—")
        lines.append(f"| {rid} | {r['priority']} | {r['theme']} | `{tr['status']}` | {ref} | {r['title']} |")
    lines += ["", "## Detailed conclusions & recommendations", ""]
    for r in recs:
        tr = r["tracking"]
        rid = r.get("id") or r.get("rule", "R00")
        lines += [
            f"### {rid} [{r['priority']} · {r['theme']}] {r['title']}", "",
            f"- **Conclusion:** {r['conclusion']}",
            f"- **Recommendation:** {r['recommendation']}",
            f"- **Tracking (`{tr['status']}`):** " + (
                ", ".join(f"[{m['repo']}#{m['number']}]({m['url']}) ({m['title']})" for m in tr["matched_issues"])
                if tr["matched_issues"] else f"Candidate issue for `{(tr.get('issue_draft') or {}).get('repo', '—')}` — `{(tr.get('issue_draft') or {}).get('title', '—')}`"
            ),
            f"- **Evidence:** `{json.dumps(r['evidence'], sort_keys=True)}`", "",
        ]
    return "\n".join(lines) + "\n"


def render_issue_drafts_md(run_id: str, recs: list, private_terms=()) -> str:
    safe_run_id = sanitize_public_text(str(run_id), private_terms=private_terms)
    drafts = [r for r in recs if r["tracking"].get("issue_draft")]
    lines = [
        f"# Candidate GitHub issue drafts ({safe_run_id})", "",
        "Prepared read-only by `fleet_ops.py recommend`. **Do not run `gh issue create` without explicit per-issue human permission** "
        "naming the destination repository and title. Draft bodies omit hostnames, local paths and account mentions.", "",
    ]
    if not recs:
        lines.append("No systemic recommendations were triggered by this run.")
        return "\n".join(lines) + "\n"
    if not drafts:
        lines.append("All synthesized systemic recommendations are already tracked by open GitHub issues.")
        return "\n".join(lines) + "\n"
    for r in drafts:
        tr = r["tracking"]
        d = tr["issue_draft"]
        rid = r.get("id") or r.get("rule", "R00")
        safe_repo = sanitize_public_text(str(d.get("repo") or ""), private_terms=private_terms)
        safe_title = sanitize_public_text(str(d.get("title") or ""), private_terms=private_terms)
        safe_body = sanitize_public_text(str(d.get("body") or "").rstrip(), private_terms=private_terms)
        badge = "" if tr["status"] == "new_issue" else f" ({tr['status']} — verify open issues before filing)"
        lines += [
            f"## {rid} [{r['priority']}] `{safe_repo}` — {safe_title}{badge}", "",
            "```markdown",
            safe_body,
            "```", "",
        ]
    return "\n".join(lines) + "\n"


def recommend(run_dir: Path, config: dict, open_issues_json: Path | None = None,
              no_github: bool = False, infra_overrides: list | None = None) -> dict:
    if open_issues_json and no_github:
        raise SystemExit("--open-issues-json and --no-github are mutually exclusive")
    for item in infra_overrides or []:
        key, sep, val = str(item).partition("=")
        if not sep or not key:
            raise SystemExit(f"--infra-repo must be KEY=OWNER/REPO, got {item!r}")
        if key not in DEFAULT_INFRA_REPOS:
            raise SystemExit(f"--infra-repo key must be one of {sorted(DEFAULT_INFRA_REPOS)}, got {key!r}")
        if not is_valid_repo_slug(val):
            raise SystemExit(f"--infra-repo value must be OWNER/REPO, got {val!r}")
        config["infra_repos"][key] = val
    run_dir = ensure_private_dir(run_dir, enforce_leaf_mode=False, create=False)
    payload = json.loads((run_dir / "sessions.json").read_text())
    resources = json.loads((run_dir / "resources.json").read_text()) if (run_dir / "resources.json").exists() else {}
    issues = json.loads((run_dir / "issues.json").read_text()) if (run_dir / "issues.json").exists() else {}
    findings = analyze(payload, resources, issues, config)

    t = config["thresholds"]
    repos_needed = {config["infra_repos"]["xmechbay"], config["infra_repos"]["claw"]}
    for repo_data in issues.values():
        if not isinstance(repo_data, dict) or repo_data.get("error"):
            continue
        prs = repo_data.get("prs", [])
        failing = [p for p in prs if isinstance(p, dict) and p.get("checks") in ("FAILURE", "ERROR")]
        if len(failing) >= t["pr_ci_cohort_min"] and len(failing) / max(len(prs), 1) >= t["pr_ci_cohort_ratio"] and repo_data.get("repo"):
            repos_needed.add(repo_data["repo"])

    private_terms = collect_private_host_terms(config, payload, resources)
    open_index, open_errors, source_mode = load_open_issue_index(repos_needed, open_issues_json=open_issues_json, no_github=no_github)
    recs = synthesize_recommendations(payload, resources, issues, findings, config,
                                      open_index=open_index, open_errors=open_errors,
                                      source_mode=source_mode, private_terms=private_terms)
    out = ensure_private_dir(run_dir / "recommendations", enforce_leaf_mode=True)
    if source_mode == "github":
        write_private_file(out / "open_issues_snapshot.json", json.dumps(open_index, indent=1) + "\n")
    summary = {
        "run": run_dir.name,
        "source_mode": source_mode,
        "total": len(recs),
        "by_status": dict(Counter(r["tracking"]["status"] for r in recs)),
        "new_issue_drafts": [
            {"id": r["id"], "priority": r["priority"], "repo": r["tracking"]["issue_draft"]["repo"], "title": r["tracking"]["issue_draft"]["title"]}
            for r in recs if r["tracking"]["status"] == "new_issue" and r["tracking"].get("issue_draft")
        ],
        "tracked": [
            {"id": r["id"], "issues": [f"{m['repo']}#{m['number']}" for m in r["tracking"]["matched_issues"]]}
            for r in recs if r["tracking"]["status"] == "tracked"
        ],
        "lookup_errors": open_errors,
        "recommendations": recs,
    }
    write_private_file(out / "recommendations.json", json.dumps(summary, indent=1) + "\n")
    write_private_file(out / "RECOMMENDATIONS.md", render_recommendations_md(run_dir.name, recs, source_mode, open_errors))
    write_private_file(out / "ISSUE_DRAFTS.md", render_issue_drafts_md(run_dir.name, recs, private_terms=private_terms))
    return {k: v for k, v in summary.items() if k != "recommendations"}


# --------------------------------------------------------------------------- CLI

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, default=Path(os.environ.get("XMECHBAY_FLEET_OPS_CONFIG", ROOT / ".local/fleet-ops.json")))
    sub = parser.add_subparsers(dest="command", required=True)
    obs = sub.add_parser("observe", help="collect sessions, resources and backlog; write findings and REPORT.md")
    obs.add_argument("--out", type=Path, help="run directory (default .local/fleet-ops/runs/<UTC>)")
    obs_src = obs.add_mutually_exclusive_group()
    obs_src.add_argument("--snapshot", type=Path, help="offline fleet-session-status fixture instead of live collection")
    obs_src.add_argument("--status-json", type=Path, help="use a saved /api/status payload")
    obs_src.add_argument("--dashboard-html", type=Path, help="extract sessions from a rendered dashboard HTML file (e.g. dashboard-latest.html)")
    obs_src.add_argument("--dashboard-url", help="read sessions from a running dashboard, e.g. http://127.0.0.1:8765")
    obs.add_argument("--resources-json", type=Path, help="use saved probe results {host: probe} instead of probing")
    obs.add_argument("--issues-json", type=Path, help="use saved backlog data instead of querying GitHub")
    obs.add_argument("--no-issues", action="store_true", help="skip the GitHub backlog")
    pl = sub.add_parser("plan", help="write claw briefs, goals, decisions and dispatch commands for a run")
    pl.add_argument("run_dir", type=Path)
    rc = sub.add_parser("recommend", help="synthesize systemic conclusions, reconcile open issues and draft non-duplicate GitHub issues")
    rc.add_argument("run_dir", type=Path)
    rc_src = rc.add_mutually_exclusive_group()
    rc_src.add_argument("--open-issues-json", type=Path, help="offline {owner/repo: [{number, title, url}]} snapshot instead of querying gh")
    rc_src.add_argument("--no-github", action="store_true", help="skip gh open-issue queries (marks drafts unverified_offline)")
    rc.add_argument("--infra-repo", action="append", default=[], metavar="KEY=OWNER/REPO",
                    help="override infra_repos entry (xmechbay or claw)")
    args = parser.parse_args(argv)
    config = load_config(args.config)
    if args.command == "plan":
        summary = plan(args.run_dir, config)
        print(json.dumps(summary, indent=1))
        return 0
    if args.command == "recommend":
        summary = recommend(args.run_dir, config, open_issues_json=args.open_issues_json,
                            no_github=args.no_github, infra_overrides=args.infra_repo)
        print(json.dumps(summary, indent=1))
        return 0
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = ensure_private_dir(args.out or (ROOT / ".local/fleet-ops/runs" / run_id), enforce_leaf_mode=True)
    started = time.time()

    def save(name, data):  # each part is kept as soon as it exists, so a later failure loses nothing
        write_private_file(run_dir / name, json.dumps(data, indent=1) + "\n")
        return data

    payload = save("sessions.json", collect_sessions(args, config))
    host_keys = [h["key"] for h in (payload.get("hosts") or []) if isinstance(h, dict) and isinstance(h.get("key"), str) and h["key"]]
    resources = save("resources.json", json.loads(args.resources_json.read_text()) if args.resources_json
                     else collect_resources(config, host_keys))
    if args.issues_json:
        issues = json.loads(args.issues_json.read_text())
    elif args.no_issues:
        issues = {}
    else:
        issues = collect_issues(fleet_repos(config))
    save("issues.json", issues)
    findings = save("findings.json", analyze(payload, resources, issues, config))
    report = render_report(run_dir.name, payload, resources, issues, findings, config)
    write_private_file(run_dir / "REPORT.md", report)
    print(report)
    print(f"run: {run_dir} ({time.time() - started:.0f} s); next: fleet_ops.py plan {run_dir}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
