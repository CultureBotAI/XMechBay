#!/usr/bin/env bash
# Launch guarded terminal control using Python's argument-based transport.
# Inputs: explicit host, command and arguments. Outputs: screens/status metadata.
# Public infrastructure and shell-quoting cleanup, 2026-10-25.
# Compatible with stock macOS Bash 3.2; layout calculations are done in Python.
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
SCRIPT_DIR="$(unset CDPATH && cd -P -- "$(dirname -- "$SOURCE")" && pwd)"
exec "${XMECHBAY_PYTHON:-python3}" -B "$SCRIPT_DIR/mac_term_control.py" "$@"
