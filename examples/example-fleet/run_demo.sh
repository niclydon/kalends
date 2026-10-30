#!/usr/bin/env bash
# Run the Kalends demo end to end against the example fleet.
set -euo pipefail
cd "$(dirname "$0")/../.."
if [ -x .venv/bin/python ]; then
  PY=.venv/bin/python
else
  PY=python3
fi
PYTHONPATH=src "$PY" -m kalends.cli demo "$@"
