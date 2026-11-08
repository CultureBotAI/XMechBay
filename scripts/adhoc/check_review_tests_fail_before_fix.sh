#!/usr/bin/env bash
# Purpose: show that the review regression tests (issues #55-#66, PR #54) fail against the
#   guard as it was before the review fixes, i.e. that they test something.
# Inputs: argv[1] git revision of the pre-fix guard (default HEAD); run from the checkout root
#   with the fixed tests in the working tree.
# Outputs: per-test result lines from unittest on stdout, from a scratch copy under
#   ~/agent-work/XMechBay/ (removed afterwards). Changes nothing in the checkout.
# Date: 2026-11-08.
set -euo pipefail
REV="${1:-HEAD}"
SCRATCH="$HOME/agent-work/XMechBay/review-regress-$$"
trap 'rm -rf "$SCRATCH"' EXIT
mkdir -p "$SCRATCH/scripts" "$SCRATCH/tests"
git show "$REV:scripts/tmp_state_guard.py" > "$SCRATCH/scripts/tmp_state_guard.py"
cp tests/test_tmp_state_guard.py "$SCRATCH/tests/"
cd "$SCRATCH"
/usr/bin/python3 -B -m unittest -v tests.test_tmp_state_guard.ReviewRegressionTests 2>&1 \
  | grep -E '^test_.* \.\.\. |^Ran |^OK|^FAILED' || true
