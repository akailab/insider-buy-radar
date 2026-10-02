#!/bin/bash
# Double-click to start Insider Buy Radar on a Mac.
cd "$(dirname "$0")"
if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 isn't installed. Get it from https://www.python.org/downloads/ then double-click this file again."
  read -p "Press Enter to close..."; exit 1
fi
if [ ! -x .venv/bin/python ]; then
  echo "First run: setting up (takes a few seconds)..."
  python3 -m venv .venv >/dev/null 2>&1 && .venv/bin/python -m pip install --quiet --disable-pip-version-check certifi pypdf >/dev/null 2>&1
fi
if [ -x .venv/bin/python ] && ! .venv/bin/python -c "import pypdf" >/dev/null 2>&1; then
  .venv/bin/python -m pip install --quiet --disable-pip-version-check pypdf >/dev/null 2>&1
fi
PY=.venv/bin/python; [ -x "$PY" ] || PY=python3
"$PY" insider_radar.py
