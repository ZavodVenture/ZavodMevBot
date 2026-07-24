#!/usr/bin/env bash
set -euo pipefail
umask 077

root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd -- "$root"

usage() {
  echo 'Usage: ./scripts/mint-run.sh <MINT> [--timeout 30..300]' >&2
  exit 64
}

[[ $# -ge 1 ]] || usage
mint="$1"
shift
[[ -n "$mint" && "$mint" != -* ]] || usage
timeout_seconds=300
if [[ "${1:-}" == "--timeout" ]]; then
  [[ $# -eq 2 ]] || usage
  timeout_seconds="$2"
  shift 2
fi
[[ $# -eq 0 ]] || usage
[[ "$timeout_seconds" =~ ^[0-9]+$ ]] || usage
(( timeout_seconds >= 30 && timeout_seconds <= 300 )) || usage

run_id=""
finalized=0
guard_pid=""

cleanup() {
  status=$?
  trap - EXIT
  if [[ -n "$run_id" && "$finalized" -eq 0 ]]; then
    python3 scripts/mint_runner.py --root "$root" restore --run-id "$run_id" \
      >/dev/null 2>&1 || true
  elif [[ "$finalized" -eq 0 ]]; then
    python3 scripts/mint_runner.py --root "$root" restore-active \
      >/dev/null 2>&1 || true
  fi
  exit "$status"
}
trap cleanup EXIT

forward_signal() {
  signal_name="$1"
  if [[ -n "$guard_pid" ]] && kill -0 "$guard_pid" 2>/dev/null; then
    kill "-$signal_name" "$guard_pid" 2>/dev/null || true
  fi
}
trap 'forward_signal INT' INT
trap 'forward_signal TERM' TERM

prepare_output="$(
  python3 scripts/mint_runner.py --root "$root" prepare \
    --mint "$mint" \
    --timeout "$timeout_seconds"
)"
prepared_run_id="$(
  printf '%s\n' "$prepare_output" |
    awk -F= '$1 == "run_id" {print $2; exit}'
)"
printf '%s\n' "$prepare_output" |
  awk -F= '
    $1 == "run_id" ||
    $1 == "mint" ||
    $1 == "timeout_seconds" ||
    $1 == "cli_version" ||
    $1 == "auto_mode" ||
    $1 == "preflight" ||
    $1 == "loss_limit_lamports" ||
    $1 == "early_stop_lamports" {
      print
    }
  '
[[ "$prepared_run_id" =~ ^[0-9]{8}T[0-9]{6}Z$ ]] || {
  echo 'Preparation returned an invalid run identifier.' >&2
  exit 1
}
run_id="$prepared_run_id"

confirmation="RUN $mint FOR $timeout_seconds"
printf 'Type exactly: %s\n> ' "$confirmation"
answer=""
IFS= read -r answer || true
if [[ "$answer" != "$confirmation" ]]; then
  echo 'Live run declined; restoring workspace.'
  exit 0
fi

result_path="$(
  python3 scripts/mint_runner.py --root "$root" result-path --run-id "$run_id"
)"
mkdir -p -- "$(dirname -- "$result_path")"
started_at="$(date -u +%s)"
set +e
./scripts/run-guarded.sh --live-confirmed --timeout "$timeout_seconds" \
  >"$result_path" 2>&1 &
guard_pid=$!
while true; do
  wait "$guard_pid"
  wait_status=$?
  if ! kill -0 "$guard_pid" 2>/dev/null; then
    guard_status=$wait_status
    break
  fi
done
guard_pid=""
set -e
ended_at="$(date -u +%s)"
chmod 600 "$result_path"
sed -n '1,200p' "$result_path"

python3 scripts/mint_runner.py --root "$root" finalize \
  --run-id "$run_id" \
  --guard-exit "$guard_status" \
  --started-at "$started_at" \
  --ended-at "$ended_at"
finalized=1
exit "$guard_status"
