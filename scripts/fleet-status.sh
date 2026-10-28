#!/usr/bin/env bash
# Purpose: on-demand fleet tables. Inputs: configured host keys/options; output: Markdown/JSON.
# Fleet-session-status skill/PR, 2026-10-27. Runs only the repository's read-only reporter.
set -euo pipefail
XMECHBAY_SOURCE_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
exec python3 -B "$XMECHBAY_SOURCE_ROOT/.agents/skills/fleet-session-status/scripts/fleet_status.py" "$@"
