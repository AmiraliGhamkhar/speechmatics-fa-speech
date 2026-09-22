#!/usr/bin/env bash
# Compatibility wrapper -> scripts/swiftmedics_tools.py
# The implementation lives in ONE place (the Python tool).
set -euo pipefail
cd "$(dirname "$0")/.."
python3 scripts/swiftmedics_tools.py install
.venv/bin/python scripts/swiftmedics_tools.py run-fa "$@"
