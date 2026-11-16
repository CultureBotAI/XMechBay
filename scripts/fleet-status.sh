#!/usr/bin/env bash
# Purpose: fleet tables and local Excel snapshots at 150%. Inputs: host keys/options.
# Output: Markdown/JSON and optional local workbook. Fleet status PR, 2026-10-28.
# Remote collection remains read-only; --no-excel disables the local GUI export.
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
exec "${XMECHBAY_PYTHON:-python3}" -B "$XMECHBAY_SOURCE_ROOT/.agents/skills/fleet-session-status/scripts/fleet_status.py" "$@"
