#!/usr/bin/env bash
set -euo pipefail
umask 077

root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd -- "$root"

usage() {
  echo 'Usage: mint-auto-diagnose.sh MINT' >&2
  exit 64
}

[[ $# -eq 1 ]] || usage
mint="$1"
[[ "$mint" =~ ^[1-9A-HJ-NP-Za-km-z]{32,44}$ ]] || usage

expected_confirmation="AUTODIAGNOSE $mint WITH 0.03 SOL"
echo "Type exactly: $expected_confirmation" >&2
IFS= read -r confirmation || exit 64
[[ "$confirmation" == "$expected_confirmation" ]] || {
  echo 'Confirmation declined.' >&2
  exit 64
}

[[ -d state && ! -L state ]] || {
  echo 'Auto diagnosis state is unavailable.' >&2
  exit 75
}
[[ "$(stat -Lc '%u:%a' state 2>/dev/null)" == "$EUID:700" ]] || {
  echo 'Auto diagnosis state is unavailable.' >&2
  exit 75
}

live_lock_path="state/.zavod-live.lock"
if [[ ! -e "$live_lock_path" ]]; then
  (
    set -o noclobber
    : >"$live_lock_path"
  ) 2>/dev/null || {
    echo 'Auto diagnosis live lock is unavailable.' >&2
    exit 75
  }
  chmod 600 "$live_lock_path"
fi
[[ -f "$live_lock_path" && ! -L "$live_lock_path" ]] || {
  echo 'Auto diagnosis live lock is unavailable.' >&2
  exit 75
}
exec {live_lock_fd}<>"$live_lock_path"
[[ "$(stat -Lc '%u:%a' "/proc/$$/fd/$live_lock_fd")" == "$EUID:600" ]] || {
  echo 'Auto diagnosis live lock is unavailable.' >&2
  exit 75
}
flock -n "$live_lock_fd" || {
  echo 'Auto diagnosis is already running.' >&2
  exit 75
}

batch_id=""
prepare_attempted=0
guard_pid=""
cleanup() {
  local status=$?
  trap - EXIT INT TERM
  if [[ -n "$batch_id" ]]; then
    python3 scripts/mint_auto_diagnoser.py \
      restore "$root" "$batch_id" >/dev/null 2>&1 || status=2
  elif (( prepare_attempted )); then
    python3 scripts/mint_auto_diagnoser.py \
      restore-active "$root" >/dev/null 2>&1 || status=2
  fi
  exit "$status"
}
handle_signal() {
  local status="$1"
  if [[ -n "$guard_pid" ]] && kill -0 "$guard_pid" 2>/dev/null; then
    kill -TERM "$guard_pid" 2>/dev/null || true
    wait "$guard_pid" 2>/dev/null || true
    guard_pid=""
  fi
  exit "$status"
}
trap cleanup EXIT
trap 'handle_signal 130' INT
trap 'handle_signal 143' TERM

prepare_attempted=1
prepare_json="$(
  python3 scripts/mint_auto_diagnoser.py prepare "$root" "$mint"
)" || {
  echo 'Auto diagnosis preparation failed.' >&2
  exit 2
}

parsed_prepare="$(
  python3 -c '
import json
import re
import sys

try:
    requested_mint = sys.argv[1]
    value = json.load(sys.stdin)
    required = {
        "batch_id", "target_mint", "timeout_seconds",
        "early_stop_lamports", "loss_limit_lamports", "stages",
    }
    names = (
        "baseline", "offchain", "activity", "aggregate_profit",
        "per_arb_profit", "roi", "volume", "pool_liquidity",
    )
    if set(value) != required:
        raise ValueError
    if re.fullmatch(r"[0-9]{8}T[0-9]{6}Z", value["batch_id"]) is None:
        raise ValueError
    if value["target_mint"] != requested_mint:
        raise ValueError
    if value["timeout_seconds"] != 300:
        raise ValueError
    if value["early_stop_lamports"] != 25_000_000:
        raise ValueError
    if value["loss_limit_lamports"] != 30_000_000:
        raise ValueError
    stages = value["stages"]
    if not isinstance(stages, list) or len(stages) != len(names):
        raise ValueError
    print(value["batch_id"])
    for index, (stage, expected) in enumerate(zip(stages, names)):
        if (
            not isinstance(stage, dict)
            or set(stage) != {"name", "skipped", "skip_reason"}
            or stage["name"] != expected
            or type(stage["skipped"]) is not bool
            or (
                stage["skip_reason"] is not None
                and stage["skip_reason"] != "already_permissive"
            )
        ):
            raise ValueError
        print(f"{index}\t{expected}\t{int(stage['"'"'skipped'"'"'])}")
except Exception:
    raise SystemExit(2)
' "$mint" <<<"$prepare_json"
)" || {
  echo 'Auto diagnosis preparation output is invalid.' >&2
  exit 2
}

mapfile -t prepared_lines <<<"$parsed_prepare"
batch_id="${prepared_lines[0]}"
executed=()
terminal_status="exhausted"
stop_reason="stages_exhausted"
target_status="absent"
three_hop_status="unproven"
shell_status=0

for line in "${prepared_lines[@]:1}"; do
  IFS=$'\t' read -r stage_index stage_name skipped <<<"$line"
  [[ "$skipped" == "0" ]] || continue
  workspace="state/auto-diagnose-runs/$batch_id/stages/$stage_index-$stage_name"
  contract_path="$workspace/stage-contract.json"
  [[ -f "$contract_path" && ! -L "$contract_path" ]] || {
    terminal_status="failed"
    stop_reason="contract_error"
    shell_status=2
    break
  }
  exec {contract_fd}<"$contract_path"
  guard_result_path="$workspace/guard-result.txt"
  set +e
  set -o noclobber
  exec {guard_result_fd}>"$guard_result_path"
  guard_result_status=$?
  set +o noclobber
  set -e
  if (( guard_result_status != 0 )); then
    exec {contract_fd}<&-
    terminal_status="failed"
    stop_reason="evaluation_error"
    shell_status=2
    break
  fi
  chmod 600 "/proc/$$/fd/$guard_result_fd"
  started_at="$(date +%s)"
  set +e
  ZAVOD_LIVE_LOCK_FD="$live_lock_fd" \
  ZAVOD_BATCH_CONTRACT_FD="$contract_fd" \
    scripts/run-guarded.sh \
      --live-confirmed \
      --timeout 300 \
      --profile auto-filter-live \
      --workspace "$workspace" >&"$guard_result_fd" &
  guard_pid=$!
  wait "$guard_pid"
  guard_exit=$?
  guard_pid=""
  set -e
  exec {guard_result_fd}>&-
  ended_at="$(date +%s)"
  exec {contract_fd}<&-
  executed+=("$stage_name")

  evaluation="$(
    python3 scripts/mint_auto_diagnoser.py \
      evaluate-stage "$root" "$batch_id" "$stage_name" \
      --guard-exit "$guard_exit" \
      --started-at "$started_at" \
      --ended-at "$ended_at"
  )" || {
    terminal_status="failed"
    stop_reason="evaluation_error"
    shell_status=2
    break
  }
  parsed_evaluation="$(
    python3 -c '
import json
import sys

try:
    value = json.load(sys.stdin)
    required = {
        "stage_name", "decision", "stop_reason", "target_status",
        "three_hop_status", "sender_accepted", "sender_rejected",
        "target_landed", "cumulative_loss_lamports",
    }
    if set(value) != required:
        raise ValueError
    if value["decision"] not in {"target_positive", "continue", "failed"}:
        raise ValueError
    if value["target_status"] not in {"positive", "absent", "unproven"}:
        raise ValueError
    if value["three_hop_status"] not in {"observed", "unproven"}:
        raise ValueError
    for name in (
        "sender_accepted", "sender_rejected", "target_landed",
        "cumulative_loss_lamports",
    ):
        if type(value[name]) is not int or value[name] < 0:
            raise ValueError
    print(value["decision"])
    print(value["stop_reason"])
    print(value["target_status"])
    print(value["three_hop_status"])
except Exception:
    raise SystemExit(2)
' <<<"$evaluation"
  )" || {
    terminal_status="failed"
    stop_reason="evaluation_error"
    shell_status=2
    break
  }
  mapfile -t evaluation_lines <<<"$parsed_evaluation"
  decision="${evaluation_lines[0]}"
  stop_reason="${evaluation_lines[1]}"
  target_status="${evaluation_lines[2]}"
  if [[ "${evaluation_lines[3]}" == "observed" ]]; then
    three_hop_status="observed"
  fi
  case "$decision" in
    target_positive)
      terminal_status="target_positive"
      break
      ;;
    failed)
      terminal_status="failed"
      shell_status=2
      break
      ;;
    continue)
      terminal_status="exhausted"
      stop_reason="stages_exhausted"
      ;;
  esac
done

result_args=(
  write-batch-result "$root" "$batch_id"
  --target-mint "$mint"
  --terminal-status "$terminal_status"
  --stop-reason "$stop_reason"
  --target-status "$target_status"
  --three-hop-status "$three_hop_status"
)
for stage_name in "${executed[@]}"; do
  result_args+=(--executed-stage "$stage_name")
done
python3 scripts/mint_auto_diagnoser.py "${result_args[@]}" >/dev/null || {
  echo 'Auto diagnosis result publication failed.' >&2
  exit 2
}

echo "Auto diagnosis finished: $terminal_status"
exit "$shell_status"
