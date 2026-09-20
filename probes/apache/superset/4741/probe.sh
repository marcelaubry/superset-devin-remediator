#!/usr/bin/env bash
# Immutable probe: exits 1 while the defect is present, 0 once it is fixed.
# The fake probe runner never executes this file; the local runner runs it from an
# isolated checkout of the exact commit under test (cwd = checkout root).
set -euo pipefail
python3 -m pytest tests/unit_tests/views/test_core.py -q -k "issue_4741"
