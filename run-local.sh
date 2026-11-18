#!/usr/bin/env bash
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${PYTHON:-$REPO/.venv/bin/python}"
cd "$REPO"
umask 077

if [ ! -x "$PYTHON" ]; then
  echo "no virtual environment yet: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  exit 1
fi

"$PYTHON" -m calendarapp.store &
STORE_PID=$!
trap 'kill "$STORE_PID" 2>/dev/null || true' EXIT INT TERM
"$PYTHON" -m calendarapp
