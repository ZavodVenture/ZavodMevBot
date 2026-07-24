#!/usr/bin/env bash
set -euo pipefail
umask 077
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."

if [[ "${1:-}" != "--live-confirmed" ]]; then
  echo 'Refusing live run: explicit confirmation is required.' >&2
  exit 64
fi
shift

timeout_seconds=300
if [[ "${1:-}" == "--timeout" ]]; then
  [[ $# -eq 2 ]] || {
    echo 'Usage: run-guarded.sh --live-confirmed [--timeout 30..300]' >&2
    exit 64
  }
  timeout_seconds="$2"
  shift 2
fi
[[ $# -eq 0 ]] || {
  echo 'Usage: run-guarded.sh --live-confirmed [--timeout 30..300]' >&2
  exit 64
}
[[ "$timeout_seconds" =~ ^[0-9]+$ ]] || {
  echo 'Timeout must be an integer from 30 through 300.' >&2
  exit 64
}
(( timeout_seconds >= 30 && timeout_seconds <= 300 )) || {
  echo 'Timeout must be an integer from 30 through 300.' >&2
  exit 64
}

exec python3 scripts/zavod_guard.py run \
  --live-confirmed \
  --config config.toml \
  --timeout-seconds "$timeout_seconds" \
  --profile default
