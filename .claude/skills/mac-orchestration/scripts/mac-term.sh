#!/usr/bin/env bash
# Launch guarded terminal control using Python's argument-based transport.
# Inputs: explicit host, command and arguments. Outputs: screens/status metadata.
# Public infrastructure and shell-quoting cleanup, 2026-10-25.
# Compatible with stock macOS Bash 3.2; layout calculations are done in Python.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec python3 "$SCRIPT_DIR/mac_term_control.py" "$@"
