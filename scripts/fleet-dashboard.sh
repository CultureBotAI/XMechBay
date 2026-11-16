#!/usr/bin/env bash
# Purpose: launch or export the local 127.0.0.1 MechBay HTML session dashboard.
# Inputs: host keys/options (--all, --config, --port, --open, --snapshot, --export-html).
# Output: localhost HTTP server or private static HTML snapshot. Fleet dashboard PR, 2026-11-14.
set -euo pipefail
SOURCE="${BASH_SOURCE[0]}"
HOP=0
while [ -L "$SOURCE" ]; do
  HOP=$((HOP + 1))
  if [ "$HOP" -gt 40 ]; then
    echo '{"error": "wrapper_symlink_loop"}' >&2
    exit 2
  fi
  DIR="$(unset CDPATH && cd -P -- "$(dirname -- "$SOURCE")" && pwd)"
  SOURCE="$(readlink -- "$SOURCE")"
  case "$SOURCE" in
    /*) ;;
    *) SOURCE="$DIR/$SOURCE" ;;
  esac
done
XMECHBAY_SOURCE_ROOT="$(unset CDPATH && cd -P -- "$(dirname -- "$SOURCE")/.." && pwd)"
exec "${XMECHBAY_PYTHON:-python3}" -B "$XMECHBAY_SOURCE_ROOT/.agents/skills/fleet-session-status/scripts/fleet_dashboard.py" "$@"
