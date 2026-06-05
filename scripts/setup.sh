#!/usr/bin/env sh
set -eu

REPO_ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$REPO_ROOT"

if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi

. ".venv/bin/activate"
python -m pip install --upgrade pip

if [ "${1:-}" = "--dev" ]; then
  python -m pip install -e ".[dev]"
else
  python -m pip install -e .
fi

if [ ! -f ".env" ] && [ -f ".env.example" ]; then
  cp ".env.example" ".env"
fi

printf '%s\n' "Setup complete."
printf '%s\n' "Run the UI with: ./scripts/run_local.sh"
