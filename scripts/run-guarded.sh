#!/usr/bin/env bash
set -euo pipefail
umask 077

root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd -- "$root"

usage() {
  echo 'Usage: run-guarded.sh --live-confirmed [--timeout 30..1200] [--profile default] | run-guarded.sh --live-confirmed [--timeout 30..300] --profile single-mint-auto | run-guarded.sh --live-confirmed --timeout 30..300 --profile selector-diagnostic --config state/mint-runs/RUN_ID/selector-diagnostic.toml --test-mode --diagnostic-mode d0 --diagnostic-target MINT --config-sha256 SHA256 --tokens-sha256 SHA256' >&2
  exit 64
}

lock_failure() {
  echo 'Refusing live run: workspace live lock is unavailable.' >&2
  exit 75
}

validate_lock_state_directory() {
  [[ -d "$root/state" && ! -L "$root/state" ]] || lock_failure
  local owner
  owner="$(stat -Lc '%u' "$root/state" 2>/dev/null)" || lock_failure
  [[ "$owner" == "$EUID" ]] || lock_failure
}

validate_lock_identity() {
  local descriptor="$1"
  [[ "$descriptor" =~ ^[0-9]+$ ]] || lock_failure
  [[ -e "/proc/$$/fd/$descriptor" ]] || lock_failure
  [[ -f "$live_lock_path" && ! -L "$live_lock_path" ]] || lock_failure

  local descriptor_identity path_identity
  descriptor_identity="$(
    stat -Lc '%d:%i:%u:%a' "/proc/$$/fd/$descriptor" 2>/dev/null
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
}

validate_inherited_lock() {
  local descriptor="$1"
  validate_lock_identity "$descriptor"

  local probe_fd probe_status inherited_status
  if ! { exec {probe_fd}<>"$live_lock_path"; } 2>/dev/null; then
    lock_failure
  fi
  set +e
  flock -n -E 73 "$probe_fd" 2>/dev/null
  probe_status=$?
  set -e
  if (( probe_status != 73 )); then
    if (( probe_status == 0 )); then
      flock -u "$probe_fd" 2>/dev/null || true
    fi
    exec {probe_fd}>&-
    lock_failure
  fi
  exec {probe_fd}>&-

  set +e
  flock -n -E 73 "$descriptor" 2>/dev/null
  inherited_status=$?
  set -e
  (( inherited_status == 0 )) || lock_failure
}

acquire_live_lock() {
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
  validate_lock_identity "$live_lock_fd"
  flock -n "$live_lock_fd" 2>/dev/null || lock_failure
}

validate_diagnostic_config_path() {
  [[
    "$config_path" =~ ^state/mint-runs/[0-9]{8}T[0-9]{6}Z/selector-diagnostic\.toml$
  ]] || usage
}

[[ "${1:-}" == "--live-confirmed" ]] || {
  echo 'Refusing live run: explicit confirmation is required.' >&2
  exit 64
}
shift

timeout_seconds=300
profile=default
config_path=config.toml
diagnostic_mode=""
diagnostic_target=""
config_sha256=""
tokens_sha256=""
timeout_option_count=0
profile_option_count=0
config_option_count=0
test_mode_count=0
diagnostic_mode_count=0
diagnostic_target_count=0
config_sha256_count=0
tokens_sha256_count=0
while (( $# > 0 )); do
  case "$1" in
    --timeout)
      (( $# >= 2 )) || usage
      timeout_seconds="$2"
      (( timeout_option_count += 1 ))
      shift 2
      ;;
    --profile)
      (( $# >= 2 )) || usage
      profile="$2"
      (( profile_option_count += 1 ))
      shift 2
      ;;
    --config)
      (( $# >= 2 )) || usage
      config_path="$2"
      (( config_option_count += 1 ))
      shift 2
      ;;
    --test-mode)
      (( test_mode_count += 1 ))
      shift
      ;;
    --diagnostic-mode)
      (( $# >= 2 )) || usage
      diagnostic_mode="$2"
      (( diagnostic_mode_count += 1 ))
      shift 2
      ;;
    --diagnostic-target)
      (( $# >= 2 )) || usage
      diagnostic_target="$2"
      (( diagnostic_target_count += 1 ))
      shift 2
      ;;
    --config-sha256)
      (( $# >= 2 )) || usage
      config_sha256="$2"
      (( config_sha256_count += 1 ))
      shift 2
      ;;
    --tokens-sha256)
      (( $# >= 2 )) || usage
      tokens_sha256="$2"
      (( tokens_sha256_count += 1 ))
      shift 2
      ;;
    *)
      usage
      ;;
  esac
done

[[ "$profile" == "default" || "$profile" == "single-mint-auto" || "$profile" == "selector-diagnostic" ]] || usage
max_timeout_seconds=300
if [[ "$profile" == "default" ]]; then
  max_timeout_seconds=1200
fi

[[ "$timeout_seconds" =~ ^[0-9]+$ ]] || {
  echo "Timeout must be an integer from 30 through $max_timeout_seconds." >&2
  exit 64
}
(( timeout_seconds >= 30 && timeout_seconds <= max_timeout_seconds )) || {
  echo "Timeout must be an integer from 30 through $max_timeout_seconds." >&2
  exit 64
}

if [[ "$profile" == "selector-diagnostic" ]]; then
  ((
    timeout_option_count == 1 &&
    profile_option_count == 1 &&
    config_option_count == 1 &&
    test_mode_count == 1 &&
    diagnostic_mode_count == 1 &&
    diagnostic_target_count == 1 &&
    config_sha256_count == 1 &&
    tokens_sha256_count == 1
  )) || usage
  [[ "$diagnostic_mode" == "d0" ]] || usage
  [[ "$diagnostic_target" =~ ^[1-9A-HJ-NP-Za-km-z]{32,44}$ ]] || usage
  [[ "$config_sha256" =~ ^[0-9a-f]{64}$ ]] || usage
  [[ "$tokens_sha256" =~ ^[0-9a-f]{64}$ ]] || usage
  [[ "${ZAVOD_LIVE_LOCK_FD+x}" == "x" ]] || lock_failure
  validate_diagnostic_config_path
else
  ((
    config_option_count == 0 &&
    test_mode_count == 0 &&
    diagnostic_mode_count == 0 &&
    diagnostic_target_count == 0 &&
    config_sha256_count == 0 &&
    tokens_sha256_count == 0
  )) || usage
fi

validate_lock_state_directory
live_lock_path="$root/state/.zavod-live.lock"
live_lock_fd=""
if [[ "${ZAVOD_LIVE_LOCK_FD+x}" == "x" ]]; then
  validate_inherited_lock "$ZAVOD_LIVE_LOCK_FD"
  live_lock_fd="$ZAVOD_LIVE_LOCK_FD"
else
  acquire_live_lock
fi
unset ZAVOD_LIVE_LOCK_FD

if [[ "$profile" == "selector-diagnostic" ]]; then
  exec python3 scripts/zavod_guard.py run \
    --live-confirmed \
    --config "$config_path" \
    --timeout-seconds "$timeout_seconds" \
    --profile selector-diagnostic \
    --test-mode \
    --diagnostic-mode "$diagnostic_mode" \
    --diagnostic-target "$diagnostic_target" \
    --config-sha256 "$config_sha256" \
    --tokens-sha256 "$tokens_sha256"
fi

exec python3 scripts/zavod_guard.py run \
  --live-confirmed \
  --config config.toml \
  --timeout-seconds "$timeout_seconds" \
  --profile "$profile"
