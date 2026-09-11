#!/usr/bin/env bash
set -euo pipefail
cd "$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)"
exec uv run python integrations/mcp/install.py "$@"
