#!/usr/bin/env python3
"""Read-only inventory of LBNL library-service configuration on one Mac (2026-11-15).

Purpose: show what a host has configured for optional library services
(CBORG MCP servers, publisher TDM keys and library-access skills). Output is
private deployment evidence, not a description of any particular fleet.
This reports what is *configured*; only a provider canary shows
that a service works, and only for the scope it exercised.

Inputs: none. Output: JSON of variable names, server and skill names, booleans
and file modes. No credential value, hostname or path is printed; no shell
profile is run; Codex auth files are never read; nothing is written.
Credential detection follows `lbnl_runtime.py` (the launcher behind
`with_lbnl_access.sh`): it starts from this process's environment, loads every
allowlisted key from `~/.config/lbnl-tdm.env` and only `CBORG_API_KEY` from
`~/.bashrc`, `~/.secrets` and `~/.config/cborg.env`, accepts one shell word per
value, resolves a bare `$NAME` from the environment, and skips other
expansions. Lines the launcher could not parse are listed as unparseable. Over
SSH this process's environment is not the agent's, so names inherited only from
an agent's environment are not seen.
Run over SSH with the system Python: ssh HOST /usr/bin/python3 - < this_file
"""
import glob
import json
import os
import re
import shlex

HOME = os.path.expanduser("~")
KEYS = ("CBORG_API_KEY", "WILEY_TDM_TOKEN", "SPRINGER_OPENACCESS_API_KEY", "SPRINGER_META_API_KEY",
            "SPRINGER_LIFESCIENCE_API_KEY", "SPRINGER_API_KEY", "CORE_API_KEY",
            "CAS_COMMON_CHEMISTRY_API_KEY", "ELSEVIER_API_KEY", "ZOTERO_DATA_DIR")
SERVERS = ("context7", "tavily", "valency")
SKILLS = ("lbnl-literature-and-db-access", "lbnl-mech-record-review")
BIN_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin", "/usr/bin",
            os.path.join(HOME, "miniforge3/bin"), os.path.join(HOME, "miniconda3/bin"),
            os.path.join(HOME, "anaconda3/bin"), "/opt/homebrew/Caskroom/miniforge/base/bin")


def load_assignments(path, allowed, env, unparseable):
    """Same rules as lbnl_runtime.load_assignments; values stay in memory only."""
    try:
        lines = open(path).read().splitlines()
    except OSError:
        return
    for line in lines:
        m = re.fullmatch(r"\s*(?:export\s+)?([A-Z_]+)=(.*)", line)
        if not m or m.group(1) not in allowed:
            continue
        try:
            parts = shlex.split(m.group(2), comments=True)
        except ValueError:
            unparseable.append("%s:%s" % (os.path.basename(path), m.group(1)))
            continue
        if len(parts) != 1:
            continue
        value = parts[0]
        if re.fullmatch(r"\$[A-Z_]+", value):
            value = env.get(value[1:], "")
        elif "$" in value or "`" in value:
            continue
        if value:
            env[m.group(1)] = value


def toml_servers(path):
    """Enabled [mcp_servers.<name>] tables (quoted or indented headers; `enabled = false` excluded)."""
    try:
        text = open(path).read()
    except OSError:
        return None
    found = []
    for name in SERVERS:
        m = re.search(r'^\s*\[\s*mcp_servers\.(?:"%s"|%s)\s*\]\s*$' % (name, name), text, re.M)
        if not m:
            continue
        body = text[m.end():]
        nxt = re.search(r"^\s*\[", body, re.M)
        body = body[:nxt.start()] if nxt else body
        if not re.search(r"^\s*enabled\s*=\s*false\b", body, re.M):
            found.append(name)
    return found


def json_servers(path):
    """Configured, not disabled, servers in a JSON MCP config."""
    try:
        data = json.load(open(path))
    except (OSError, ValueError):
        return None
    servers = data.get("mcpServers") or data.get("mcp_servers") or {}
    return sorted(k for k, v in servers.items()
                  if k in SERVERS and not (isinstance(v, dict) and v.get("disabled")))


def find_binary(name):
    for d in BIN_DIRS:
        if os.access(os.path.join(d, name), os.X_OK):
            return True
    return False


def main():
    tdm_path = os.path.join(HOME, ".config/lbnl-tdm.env")
    env = dict(os.environ)
    unparseable = []
    load_assignments(tdm_path, KEYS, env, unparseable)
    for rel in (".bashrc", ".secrets", ".config/cborg.env"):
        load_assignments(os.path.join(HOME, rel), ("CBORG_API_KEY",), env, unparseable)
    user_skills = [s for s in SKILLS if os.path.exists(os.path.join(HOME, ".agents/skills", s))]
    codex = {}
    for home in sorted(glob.glob(os.path.join(HOME, ".codex*"))):
        if os.path.isdir(home):
            codex[os.path.basename(home)] = {
                "mcp_servers": toml_servers(os.path.join(home, "config.toml")),
                "skills": [s for s in SKILLS if os.path.exists(os.path.join(home, "skills", s))],
            }
    claude = {}
    for name, cfg in ((".claude", os.path.join(HOME, ".claude.json")),
                      (".claude-work", os.path.join(HOME, ".claude-work", ".claude.json"))):
        if os.path.isdir(os.path.join(HOME, name)):
            claude[name] = {"mcp_servers": json_servers(cfg),
                            "skills": [s for s in SKILLS
                                       if os.path.exists(os.path.join(HOME, name, "skills", s))]}
    agy_servers = json_servers(os.path.join(HOME, ".gemini/config/mcp_config.json")) or []
    try:
        agy_text = open(os.path.join(HOME, ".gemini/antigravity-cli/settings.json")).read()
        agy_rules = sorted(s for s in SERVERS if "mcp(%s/" % s in agy_text)
    except OSError:
        agy_rules = None
    zotero_dir = env.get("ZOTERO_DATA_DIR")
    report = {
        "schema_version": 3,
        "tdm_env": {"present": os.path.exists(tdm_path),
                    "mode": oct(os.stat(tdm_path).st_mode & 0o777) if os.path.exists(tdm_path) else None},
        "configured": sorted(k for k in KEYS if k != "ZOTERO_DATA_DIR" and env.get(k)),
        "unparseable": sorted(set(unparseable)),
        "user_agents_skills": user_skills,
        "codex_homes": codex,
        "claude_homes": claude,
        "agy": {"mcp_servers": agy_servers, "permission_rules": agy_rules},
        "python311_plus": sorted(v for v in ("3.11", "3.12", "3.13", "3.14")
                                 if find_binary("python" + v)
                                 or os.path.exists(os.path.join(HOME, ".pyenv/shims/python" + v))),
        "pdfinfo": find_binary("pdfinfo"),
        "pdftotext": find_binary("pdftotext"),
        "zotero_sqlite": bool(zotero_dir) and os.path.isfile(os.path.join(zotero_dir, "zotero.sqlite")),
        "codex_found": bool(glob.glob(os.path.join(HOME, ".nvm/versions/node/*/bin/codex")))
        or find_binary("codex"),
    }
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
