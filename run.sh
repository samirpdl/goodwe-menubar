#!/usr/bin/env bash
# Launch the GoodWe menubar app using the project virtualenv.
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -x ".venv/bin/python" ]]; then
  echo "No .venv found. Create it first:" >&2
  echo "  /opt/homebrew/bin/python3 -m venv .venv" >&2
  echo "  ./.venv/bin/python -m pip install -r requirements.txt" >&2
  exit 1
fi

exec ./.venv/bin/python -m goodwe_menubar
