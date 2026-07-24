#!/usr/bin/env bash
set -euo pipefail
umask 077

root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd -- "$root"

usage() {
  echo 'Usage: run-guarded.sh --live-confirmed [--timeout 30..300] [--profile default|single-mint-auto]' >&2
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

[[ "${1:-}" == "--live-confirmed" ]] || {
  echo 'Refusing live run: explicit confirmation is required.' >&2
  exit 64
}
shift

timeout_seconds=300
profile=default
while (( $# > 0 )); do
  case "$1" in
    --timeout)
      (( $# >= 2 )) || usage
      timeout_seconds="$2"
      shift 2
      ;;
    --profile)
      (( $# >= 2 )) || usage
      profile="$2"
      shift 2
      ;;
    *)
      usage
      ;;
  esac
done

[[ "$timeout_seconds" =~ ^[0-9]+$ ]] || {
  echo 'Timeout must be an integer from 30 through 300.' >&2
  exit 64
}
(( timeout_seconds >= 30 && timeout_seconds <= 300 )) || {
  echo 'Timeout must be an integer from 30 through 300.' >&2
  exit 64
}
[[ "$profile" == "default" || "$profile" == "single-mint-auto" ]] || usage

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

exec python3 scripts/zavod_guard.py run \
  --live-confirmed \
  --config config.toml \
  --timeout-seconds "$timeout_seconds" \
  --profile "$profile"
