#!/bin/bash
# 2026-11-07: Add three CBORG MCP servers; config in, backed-up config out.
# Provenance: XMechBay LBNL Tavily MCP setup PR. No credentials are persisted.
set -euo pipefail
config_file="$HOME/.codex/config.toml"
backup_file="${config_file}.bak-tavily-20261107"
for server in tavily context7 valency; do
  if codex mcp get "$server" >/dev/null 2>&1; then
    echo "$server already configured; refusing to overwrite." >&2
    exit 1
  fi
done
if test -e "$backup_file"; then
  echo 'Backup already exists; refusing to overwrite.' >&2
  exit 1
fi
cp -p "$config_file" "$backup_file"
cat >> "$config_file" <<'TOML'

[mcp_servers.tavily]
url = "https://api.cborg.lbl.gov/mcp/tavily"
bearer_token_env_var = "CBORG_API_KEY"
enabled = false
default_tools_approval_mode = "prompt"
disabled_tools = ["tavily-tavily_crawl", "tavily-tavily_map", "tavily_crawl", "tavily_map"]

[mcp_servers.context7]
url = "https://api.cborg.lbl.gov/mcp/context7"
bearer_token_env_var = "CBORG_API_KEY"
enabled = false
default_tools_approval_mode = "prompt"

[mcp_servers.valency]
url = "https://api.cborg.lbl.gov/mcp/valency"
bearer_token_env_var = "CBORG_API_KEY"
enabled = false
default_tools_approval_mode = "prompt"
TOML
for server in tavily context7 valency; do
  codex mcp get "$server"
done
echo "Undo: restore $backup_file to $config_file"
