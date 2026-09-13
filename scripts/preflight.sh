#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
if [[ -n "${1:-}" ]]; then
  exec python3 scripts/zavod_guard.py preflight --config config.toml --profile "$1"
fi
exec python3 scripts/zavod_guard.py preflight --config config.toml
