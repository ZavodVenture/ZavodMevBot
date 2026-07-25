#!/usr/bin/env bash
set -euo pipefail
umask 077

root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd -- "$root"

usage() {
  echo 'Usage: ./scripts/mint-run.sh <MINT> [--timeout 30..300] | ./scripts/mint-run.sh <MINT> --diagnostic d0 [--timeout 30..300]' >&2
  exit 64
}

[[ $# -ge 1 ]] || usage
mint="$1"
shift
[[ -n "$mint" && "$mint" != -* ]] || usage
timeout_seconds=300
diagnostic_mode=""
timeout_seen=0
diagnostic_seen=0
while (( $# > 0 )); do
  case "$1" in
    --timeout)
      (( $# >= 2 && timeout_seen == 0 )) || usage
      timeout_seconds="$2"
      timeout_seen=1
      shift 2
      ;;
    --diagnostic)
      (( $# >= 2 && diagnostic_seen == 0 )) || usage
      diagnostic_mode="$2"
      diagnostic_seen=1
      shift 2
      ;;
    *)
      usage
      ;;
  esac
done
[[ "$timeout_seconds" =~ ^[0-9]+$ ]] || usage
(( timeout_seconds >= 30 && timeout_seconds <= 300 )) || usage
(( diagnostic_seen == 0 )) || [[ "$diagnostic_mode" == "d0" ]] || usage

run_id=""
finalized=0
guard_pid=""
prepare_pid=""
prepare_output_path=""
result_fd=""
pending_signal=""
pending_signal_status=0
live_lock_fd=""
lock_acquired=0
prepare_started=0
live_lock_path="$root/state/.zavod-live.lock"

lock_failure() {
  echo 'Refusing live run: workspace live lock is unavailable.' >&2
  exit 75
}

extract_unique_field() {
  local payload="$1"
  local key="$2"
  printf '%s\n' "$payload" |
    awk -v key="$key" '
      index($0, key "=") == 1 {
        count += 1
        value = substr($0, length(key) + 2)
      }
      END {
        if (count != 1) {
          exit 1
        }
        print value
      }
    '
}

acquire_live_lock() {
  [[ -d "$root/state" && ! -L "$root/state" ]] || lock_failure
  local state_owner
  state_owner="$(stat -Lc '%u' "$root/state" 2>/dev/null)" || lock_failure
  [[ "$state_owner" == "$EUID" ]] || lock_failure

  if [[ -e "$live_lock_path" || -L "$live_lock_path" ]]; then
    [[ -f "$live_lock_path" && ! -L "$live_lock_path" ]] || lock_failure
  else
    (
      set -o noclobber
      : >"$live_lock_path"
    ) 2>/dev/null || lock_failure
    chmod 600 "$live_lock_path" 2>/dev/null || lock_failure
  fi

  if ! { exec {live_lock_fd}<>"$live_lock_path"; } 2>/dev/null; then
    lock_failure
  fi
  local descriptor_identity path_identity
  descriptor_identity="$(
    stat -Lc '%d:%i:%u:%a' "/proc/$$/fd/$live_lock_fd" 2>/dev/null
  )" || lock_failure
  path_identity="$(
    stat -Lc '%d:%i:%u:%a' "$live_lock_path" 2>/dev/null
  )" || lock_failure
  [[ "$descriptor_identity" == "$path_identity" ]] || lock_failure

  local device inode owner mode
  IFS=: read -r device inode owner mode <<<"$descriptor_identity"
  [[
    -n "$device" &&
    -n "$inode" &&
    "$owner" == "$EUID" &&
    "$mode" == "600"
  ]] || lock_failure
  flock -n "$live_lock_fd" 2>/dev/null || lock_failure
  lock_acquired=1
}

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
  if [[ -n "$prepare_pid" ]] && kill -0 "$prepare_pid" 2>/dev/null; then
    kill -TERM "$prepare_pid" 2>/dev/null || true
    wait "$prepare_pid" 2>/dev/null || true
  fi
  if [[ -n "$prepare_output_path" ]]; then
    rm -f -- "$prepare_output_path"
    prepare_output_path=""
  fi
  close_result_fd || true
  if [[
    "$lock_acquired" -eq 1 &&
    "$prepare_started" -eq 1 &&
    "$finalized" -eq 0 &&
    -n "$run_id"
  ]]; then
    python3 scripts/mint_runner.py --root "$root" restore --run-id "$run_id" \
      >/dev/null 2>&1 || true
  elif [[
    "$lock_acquired" -eq 1 &&
    "$prepare_started" -eq 1 &&
    "$finalized" -eq 0
  ]]; then
    python3 scripts/mint_runner.py --root "$root" restore-active \
      >/dev/null 2>&1 || true
  fi
  exit "$status"
}

forward_pending_signal() {
  [[ -n "$pending_signal" ]] || return 0
  local child_pid
  for child_pid in "$prepare_pid" "$guard_pid"; do
    if [[ -n "$child_pid" ]] && kill -0 "$child_pid" 2>/dev/null; then
      kill "-$pending_signal" "$child_pid" 2>/dev/null || true
    fi
  done
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

acquire_live_lock
trap cleanup EXIT
trap latch_int INT
trap latch_term TERM

prepare_output_path="$(
  mktemp "${TMPDIR:-/tmp}/zavod-mint-prepare.XXXXXX"
)"
if (( pending_signal_status != 0 )); then
  exit "$pending_signal_status"
fi
set +e
prepare_started=1
if [[ -n "$diagnostic_mode" ]]; then
  python3 scripts/mint_runner.py --root "$root" prepare \
    --mint "$mint" \
    --timeout "$timeout_seconds" \
    --diagnostic "$diagnostic_mode" \
    >"$prepare_output_path" &
else
  python3 scripts/mint_runner.py --root "$root" prepare \
    --mint "$mint" \
    --timeout "$timeout_seconds" \
    >"$prepare_output_path" &
fi
prepare_pid=$!
forward_pending_signal
while true; do
  wait "$prepare_pid"
  prepare_wait_status=$?
  if kill -0 "$prepare_pid" 2>/dev/null; then
    forward_pending_signal
    continue
  fi
  prepare_status=$prepare_wait_status
  break
done
prepare_pid=""
set -e
if (( pending_signal_status != 0 )); then
  exit "$pending_signal_status"
fi
if (( prepare_status != 0 )); then
  exit "$prepare_status"
fi
prepare_output="$(<"$prepare_output_path")"
rm -f -- "$prepare_output_path"
prepare_output_path=""
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
diagnostic_config=""
prepared_diagnostic_mode=""
prepared_diagnostic_target=""
prepared_diagnostic_config_sha256=""
prepared_diagnostic_tokens_sha256=""
if [[ -n "$diagnostic_mode" ]]; then
  if ! prepared_diagnostic_mode="$(
    extract_unique_field "$prepare_output" "diagnostic_mode"
  )" ||
    ! prepared_diagnostic_target="$(
      extract_unique_field "$prepare_output" "diagnostic_target"
    )" ||
    ! prepared_diagnostic_config_sha256="$(
      extract_unique_field "$prepare_output" "diagnostic_config_sha256"
    )" ||
    ! prepared_diagnostic_tokens_sha256="$(
      extract_unique_field "$prepare_output" "diagnostic_tokens_sha256"
    )"; then
    echo 'Diagnostic preparation validation failed.' >&2
    exit 1
  fi
  if [[
    "$prepared_diagnostic_mode" != "$diagnostic_mode" ||
    "$prepared_diagnostic_target" != "$mint" ||
    ! "$prepared_diagnostic_config_sha256" =~ ^[0-9a-f]{64}$ ||
    ! "$prepared_diagnostic_tokens_sha256" =~ ^[0-9a-f]{64}$
  ]]; then
    echo 'Diagnostic preparation validation failed.' >&2
    exit 1
  fi
  diagnostic_config="state/mint-runs/$run_id/selector-diagnostic.toml"
  printf 'diagnostic_mode=%s\n' "$prepared_diagnostic_mode"
  printf 'diagnostic_target=%s\n' "$prepared_diagnostic_target"
  printf 'diagnostic_config_sha256=%s\n' \
    "$prepared_diagnostic_config_sha256"
  printf 'diagnostic_tokens_sha256=%s\n' \
    "$prepared_diagnostic_tokens_sha256"
else
  diagnostic_contract_fields="$(
    printf '%s\n' "$prepare_output" |
      awk '
        /^(diagnostic_mode|diagnostic_target|diagnostic_config_sha256|diagnostic_tokens_sha256)=/ {
          count += 1
        }
        END { print count + 0 }
      '
  )"
  if (( diagnostic_contract_fields != 0 )); then
    echo 'Preparation returned unexpected diagnostic state.' >&2
    exit 1
  fi
fi
if (( pending_signal_status != 0 )); then
  exit "$pending_signal_status"
fi

if [[ -n "$diagnostic_mode" ]]; then
  confirmation="DIAGNOSE $mint FOR $timeout_seconds"
else
  confirmation="RUN $mint FOR $timeout_seconds"
fi
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

if [[ -n "$diagnostic_mode" ]]; then
  set +e
  validated_diagnostic_contract="$(
    python3 scripts/mint_runner.py --root "$root" validate-live \
      --run-id "$run_id"
  )"
  validation_status=$?
  set -e
  if (( validation_status != 0 )); then
    exit "$validation_status"
  fi
  expected_diagnostic_contract="$(
    printf 'diagnostic_mode=%s\n' "$prepared_diagnostic_mode"
    printf 'diagnostic_target=%s\n' "$prepared_diagnostic_target"
    printf 'diagnostic_config_sha256=%s\n' \
      "$prepared_diagnostic_config_sha256"
    printf 'diagnostic_tokens_sha256=%s\n' \
      "$prepared_diagnostic_tokens_sha256"
  )"
  if [[ "$validated_diagnostic_contract" != "$expected_diagnostic_contract" ]]; then
    echo 'Diagnostic live validation failed.' >&2
    exit 1
  fi
else
  python3 scripts/mint_runner.py --root "$root" validate-live --run-id "$run_id"
fi
if (( pending_signal_status != 0 )); then
  exit "$pending_signal_status"
fi

set +e
if [[ -n "$diagnostic_mode" ]]; then
  ZAVOD_LIVE_LOCK_FD="$live_lock_fd" \
    ./scripts/run-guarded.sh \
    --live-confirmed \
    --timeout "$timeout_seconds" \
    --profile selector-diagnostic \
    --config "$diagnostic_config" \
    --test-mode \
    --diagnostic-mode "$prepared_diagnostic_mode" \
    --diagnostic-target "$prepared_diagnostic_target" \
    --config-sha256 "$prepared_diagnostic_config_sha256" \
    --tokens-sha256 "$prepared_diagnostic_tokens_sha256" \
    >&"$result_fd" &
else
  ZAVOD_LIVE_LOCK_FD="$live_lock_fd" \
    ./scripts/run-guarded.sh \
    --live-confirmed \
    --timeout "$timeout_seconds" \
    --profile single-mint-auto \
    >&"$result_fd" &
fi
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
