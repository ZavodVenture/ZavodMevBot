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
result_fd=""
pending_signal=""
pending_signal_status=0

close_result_fd() {
  if [[ -z "$result_fd" ]]; then
    return 0
  fi
  closing_fd="$result_fd"
  result_fd=""
  exec {closing_fd}>&-
}

cleanup() {
  status=$?
  trap - EXIT
  close_result_fd || true
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

forward_pending_signal() {
  if (
    [[ -n "$pending_signal" && -n "$guard_pid" ]] &&
    kill -0 "$guard_pid" 2>/dev/null
  ); then
    kill "-$pending_signal" "$guard_pid" 2>/dev/null || true
  fi
}

latch_int() {
  pending_signal="INT"
  pending_signal_status=130
  forward_pending_signal
}

latch_term() {
  pending_signal="TERM"
  pending_signal_status=143
  forward_pending_signal
}

trap latch_int INT
trap latch_term TERM

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
if (( pending_signal_status != 0 )); then
  exit "$pending_signal_status"
fi
if [[ "$answer" != "$confirmation" ]]; then
  echo 'Live run declined; restoring workspace.'
  exit 0
fi

result_path="$(
  python3 scripts/mint_runner.py --root "$root" result-path --run-id "$run_id"
)"
if (( pending_signal_status != 0 )); then
  exit "$pending_signal_status"
fi
expected_result_path="$root/state/mint-runs/$run_id/guard-result.txt"
if [[ "$result_path" != "$expected_result_path" ]]; then
  echo 'Result path validation failed.' >&2
  exit 1
fi
if [[ -e "$result_path" || -L "$result_path" ]]; then
  echo 'Result file already exists.' >&2
  exit 1
fi
set -o noclobber
if ! exec {result_fd}> "$result_path"; then
  echo 'Result file creation failed.' >&2
  exit 1
fi

started_at=""
set +e
started_at="$(date -u +%s)"
started_status=$?
set -e
if (
  (( started_status != 0 )) ||
  [[ ! "$started_at" =~ ^[0-9]+$ ]] ||
  (( started_at <= 0 ))
); then
  echo 'Run start time capture failed.' >&2
  if (( started_status != 0 )); then
    exit "$started_status"
  fi
  exit 1
fi
if (( pending_signal_status != 0 )); then
  exit "$pending_signal_status"
fi

set +e
./scripts/run-guarded.sh --live-confirmed --timeout "$timeout_seconds" \
  >&"$result_fd" &
guard_pid=$!
forward_pending_signal
while true; do
  wait "$guard_pid"
  wait_status=$?
  if kill -0 "$guard_pid" 2>/dev/null; then
    forward_pending_signal
    continue
  fi
  guard_status=$wait_status
  break
done
guard_pid=""
post_run_status=0
close_result_fd
close_status=$?
if (( close_status != 0 )); then
  post_run_status=$close_status
fi

ended_at="$(date -u +%s)"
ended_status=$?
if (
  (( ended_status != 0 )) ||
  [[ ! "$ended_at" =~ ^[0-9]+$ ]] ||
  (( ended_at < started_at ))
); then
  ended_at=0
  if (( post_run_status == 0 )); then
    if (( ended_status != 0 )); then
      post_run_status=$ended_status
    else
      post_run_status=1
    fi
  fi
fi

python3 scripts/mint_runner.py --root "$root" finalize \
  --run-id "$run_id" \
  --guard-exit "$guard_status" \
  --started-at "$started_at" \
  --ended-at "$ended_at"
finalize_status=$?
if (( finalize_status == 0 )); then
  finalized=1
fi

exit_status=0
if (( pending_signal_status != 0 )); then
  exit_status=$pending_signal_status
elif (( guard_status != 0 )); then
  exit_status=$guard_status
elif (( finalize_status != 0 )); then
  exit_status=$finalize_status
elif (( post_run_status != 0 )); then
  exit_status=$post_run_status
fi
exit "$exit_status"
