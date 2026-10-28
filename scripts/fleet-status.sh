#!/usr/bin/env bash
# Purpose: fleet tables and local Excel snapshots at 150%. Inputs: host keys/options.
# Output: Markdown/JSON and optional local workbook. Fleet status PR, 2026-10-28.
# Remote collection remains read-only; --no-excel disables the local GUI export.
set -euo pipefail
XMECHBAY_SOURCE_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
exec python3 -B "$XMECHBAY_SOURCE_ROOT/.agents/skills/fleet-session-status/scripts/fleet_status.py" "$@"
