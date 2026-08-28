#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ -n "${VIRTUAL_ENV:-}" && -x "$VIRTUAL_ENV/bin/python" ]]; then
  exec "$VIRTUAL_ENV/bin/python" -m backend.launcher "$@"
fi

if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
  exec "$ROOT_DIR/.venv/bin/python" -m backend.launcher "$@"
fi

exec uv run --frozen python -m backend.launcher "$@"
