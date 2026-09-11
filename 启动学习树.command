#!/usr/bin/env bash
set -euo pipefail
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
if ! command -v uv >/dev/null 2>&1; then
  echo 'Install uv, then run: uv run python scripts/manage.py setup'
  exit 1
fi
exec uv run python scripts/manage.py start --open
