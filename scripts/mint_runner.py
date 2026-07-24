#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import tomllib
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM_ID = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
MIN_TIMEOUT_SECONDS = 30
MAX_TIMEOUT_SECONDS = 300
EXPECTED_CLI_VERSION = "0.2.2"
LOSS_LIMIT_LAMPORTS = 30_000_000
EARLY_STOP_LAMPORTS = 25_000_000
REQUIRED_FILES = ("config.toml", "tokens.toml")
OPTIONAL_FILES = ("hot_tokens.json", "routing.json")
RUN_ID_PATTERN = re.compile(r"[0-9]{8}T[0-9]{6}Z")
LOG_PATTERNS = {
    "wsol_exists": re.compile(r"WSOL account exists", re.I),
    "wsol_missing": re.compile(r"WSOL account does not exist", re.I),
    "wsol_created": re.compile(r"WSOL account created successfully", re.I),
    "mint_refresh": re.compile(r"Fetched [0-9]+ mint list", re.I),
    "pool_events": re.compile(
        r"found [0-9]+ pools?|selected pool|pool selected",
        re.I,
    ),
    "lut_events": re.compile(r"Resolved LUTs|Finding proper luts", re.I),
    "sent_events": re.compile(r"Transaction sent successfully", re.I),
    "error_events": re.compile(r"error|failed", re.I),
}
CHAIN_SUMMARY_KEYS = (
    "landed",
    "successful",
    "failed",
    "fees_lamports",
    "rent_lamports",
    "transfers_lamports",
    "sol_delta_lamports",
    "wsol_delta_raw",
)
STOP_REASONS = frozenset(
    {
        "child_exit",
        "cleanup_failed",
        "loss_threshold",
        "operator_signal",
        "output_error",
        "rpc_error",
        "timeout",
    }
)


class RunnerError(RuntimeError):
    pass


class _CliArgumentError(RunnerError):
    pass


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        del message
        raise _CliArgumentError("invalid arguments")


@dataclass(frozen=True)
class PreparedRun:
    run_id: str
    mint: str
    timeout: int
    backup_dir: Path
    result_dir: Path
    cli_version: str
    loss_limit_lamports: int
    early_stop_lamports: int

    def safe_summary(self):
        return {
            "run_id": self.run_id,
            "mint": self.mint,
            "timeout_seconds": self.timeout,
            "cli_version": self.cli_version,
            "auto_mode": "enabled",
            "preflight": "ok",
            "loss_limit_lamports": self.loss_limit_lamports,
            "early_stop_lamports": self.early_stop_lamports,
        }


def validate_timeout(value):
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise RunnerError("timeout must be an integer from 30 through 300") from exc
    if (
        isinstance(value, bool)
        or not MIN_TIMEOUT_SECONDS <= parsed <= MAX_TIMEOUT_SECONDS
    ):
        raise RunnerError("timeout must be an integer from 30 through 300")
    return parsed


def _validate_run_id(value):
    if not isinstance(value, str) or RUN_ID_PATTERN.fullmatch(value) is None:
        raise _CliArgumentError("invalid arguments")
    return value


def decode_pubkey(value):
    if not isinstance(value, str) or not value:
        raise RunnerError("mint is invalid")
    number = 0
    try:
        for char in value:
            number = number * 58 + ALPHABET.index(char)
    except ValueError as exc:
        raise RunnerError("mint is invalid") from exc
    decoded = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    decoded = b"\0" * (len(value) - len(value.lstrip("1"))) + decoded
    if len(decoded) != 32:
        raise RunnerError("mint is invalid")
    return decoded


def rpc_call(url, payload, timeout=10):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def validate_mint_account(rpc_url, mint, transport=None):
    decode_pubkey(mint)
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getAccountInfo",
        "params": [mint, {"encoding": "jsonParsed", "commitment": "confirmed"}],
    }
    try:
        body = (transport or rpc_call)(rpc_url, payload, 10)
        value = body["result"]["value"]
        parsed_type = value["data"]["parsed"]["type"] if value else None
        if (
            value is None
            or value.get("executable") is not False
            or value.get("owner") not in (TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID)
            or parsed_type != "mint"
        ):
            raise ValueError("not a token mint")
    except Exception as exc:
        raise RunnerError("mint account validation failed") from exc


def _atomic_write(path, data, mode=0o600):
    path = Path(path)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _safe_copy(source, destination):
    shutil.copy2(source, destination)
    destination.chmod(0o600)


def _atomic_copy(source, destination):
    _atomic_write(destination, Path(source).read_bytes())


def _integrity_record(path):
    data = Path(path).read_bytes()
    return {
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _load_workspace_config(root):
    config_path = root / "config.toml"
    try:
        if stat.S_IMODE(config_path.stat().st_mode) != 0o600:
            raise RunnerError("config.toml must have mode 600")
        with config_path.open("rb") as handle:
            config = tomllib.load(handle)
    except RunnerError:
        raise
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise RunnerError("config.toml is invalid or unreadable") from exc
    if config.get("auto", {}).get("enabled") is not True:
        raise RunnerError("auto mode must be enabled")
    rpc_url = config.get("rpc", {}).get("url")
    if not isinstance(rpc_url, str) or not rpc_url:
        raise RunnerError("RPC configuration is missing")
    return config, rpc_url


def _run_preflight(root):
    result = subprocess.run(
        ["python3", "scripts/zavod_guard.py", "preflight", "--config", "config.toml"],
        cwd=root,
        text=True,
        capture_output=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RunnerError("guarded preflight failed")
    safe = {}
    allowed = {
        "preflight",
        "cli_version",
        "loss_limit_lamports",
        "early_stop_lamports",
        "timeout_seconds",
    }
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator and key in allowed:
            safe[key] = value
    return safe


def _parse_preflight_integer(value):
    if isinstance(value, bool):
        raise RunnerError("guarded preflight failed")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
        return int(value)
    raise RunnerError("guarded preflight failed")


def _validate_preflight(preflight):
    if not isinstance(preflight, dict):
        raise RunnerError("guarded preflight failed")
    if (
        preflight.get("preflight") != "ok"
        or preflight.get("cli_version") != EXPECTED_CLI_VERSION
    ):
        raise RunnerError("guarded preflight failed")
    loss_limit = _parse_preflight_integer(preflight.get("loss_limit_lamports"))
    early_stop = _parse_preflight_integer(preflight.get("early_stop_lamports"))
    if (
        loss_limit != LOSS_LIMIT_LAMPORTS
        or early_stop != EARLY_STOP_LAMPORTS
    ):
        raise RunnerError("guarded preflight failed")
    return EXPECTED_CLI_VERSION, loss_limit, early_stop


def _active_process_exists():
    result = subprocess.run(
        ["ps", "-eo", "comm="],
        text=True,
        capture_output=True,
        check=True,
        timeout=5,
    )
    return any(
        line.strip().startswith("zavod-mev-bot")
        for line in result.stdout.splitlines()
    )


def prepare_run(
    root,
    mint,
    timeout,
    transport=None,
    preflight_runner=None,
    now=None,
    process_checker=None,
):
    root = Path(root).resolve()
    timeout = validate_timeout(timeout)
    _config, rpc_url = _load_workspace_config(root)
    if (process_checker or _active_process_exists)():
        raise RunnerError("another ZavodMevBot process is active")
    validate_mint_account(rpc_url, mint, transport)
    instant = (now or (lambda: datetime.now(timezone.utc)))()
    run_id = instant.strftime("%Y%m%dT%H%M%SZ")
    backup_dir = root / "state" / "backups" / f"mint-run-{run_id}"
    result_dir = root / "state" / "mint-runs" / run_id
    active_marker = root / "state" / ".mint-run-active"
    if active_marker.exists() or backup_dir.exists() or result_dir.exists():
        raise RunnerError("a mint run is already prepared")
    backup_dir.mkdir(parents=True, mode=0o700)
    result_dir.mkdir(parents=True, mode=0o700)
    backup_dir.chmod(0o700)
    result_dir.chmod(0o700)
    metadata = {
        "run_id": run_id,
        "mint": mint,
        "timeout_seconds": timeout,
        "optional_files": {},
    }
    try:
        for name in REQUIRED_FILES:
            source = root / name
            if not source.is_file():
                raise RunnerError(f"{name} is missing")
            _safe_copy(source, backup_dir / name)
        for name in OPTIONAL_FILES:
            source = root / name
            existed = source.is_file()
            metadata["optional_files"][name] = existed
            if existed:
                _safe_copy(source, backup_dir / name)
        snapshot_names = list(REQUIRED_FILES)
        snapshot_names.extend(
            name for name in OPTIONAL_FILES if metadata["optional_files"][name]
        )
        metadata["backup_files"] = {
            name: _integrity_record(backup_dir / name)
            for name in snapshot_names
        }

        _atomic_write(
            backup_dir / "metadata.json",
            (json.dumps(metadata, sort_keys=True) + "\n").encode(),
        )
        _atomic_write(active_marker, f"{run_id}\n".encode())

        for name in OPTIONAL_FILES:
            source = root / name
            if metadata["optional_files"][name]:
                source.unlink()
        _atomic_write(root / "tokens.toml", f'tokens = ["{mint}"]\n'.encode())
        with (root / "tokens.toml").open("rb") as handle:
            tokens = tomllib.load(handle)
        if tokens != {"tokens": [mint]}:
            raise RunnerError("temporary tokens.toml validation failed")

        preflight = (preflight_runner or _run_preflight)(root)
        cli_version, loss_limit, early_stop = _validate_preflight(preflight)
        return PreparedRun(
            run_id=run_id,
            mint=mint,
            timeout=timeout,
            backup_dir=backup_dir,
            result_dir=result_dir,
            cli_version=cli_version,
            loss_limit_lamports=loss_limit,
            early_stop_lamports=early_stop,
        )
    except BaseException:
        if (backup_dir / "metadata.json").exists():
            restore_run(root, run_id)
        raise


def _validate_recovery_data(backup_dir, run_id):
    metadata_path = backup_dir / "metadata.json"
    try:
        if (
            not backup_dir.is_dir()
            or backup_dir.is_symlink()
            or stat.S_IMODE(backup_dir.stat().st_mode) != 0o700
            or not metadata_path.is_file()
            or metadata_path.is_symlink()
            or stat.S_IMODE(metadata_path.stat().st_mode) != 0o600
        ):
            raise ValueError("invalid private recovery paths")
        metadata = json.loads(metadata_path.read_bytes())
        if not isinstance(metadata, dict) or metadata.get("run_id") != run_id:
            raise ValueError("inconsistent recovery metadata")
        decode_pubkey(metadata.get("mint"))
        validate_timeout(metadata.get("timeout_seconds"))
        optional_files = metadata.get("optional_files")
        if (
            not isinstance(optional_files, dict)
            or set(optional_files) != set(OPTIONAL_FILES)
            or any(type(optional_files[name]) is not bool for name in OPTIONAL_FILES)
        ):
            raise ValueError("inconsistent optional-file metadata")
        expected_backups = set(REQUIRED_FILES)
        expected_backups.update(
            name for name in OPTIONAL_FILES if optional_files[name]
        )
        backup_files = metadata.get("backup_files")
        if not isinstance(backup_files, dict) or set(backup_files) != expected_backups:
            raise ValueError("inconsistent backup-file metadata")
        for name in REQUIRED_FILES:
            backup = backup_dir / name
            if (
                not backup.is_file()
                or backup.is_symlink()
                or stat.S_IMODE(backup.stat().st_mode) != 0o600
            ):
                raise ValueError("required recovery file is missing")
        for name in OPTIONAL_FILES:
            backup = backup_dir / name
            if optional_files[name]:
                if (
                    not backup.is_file()
                    or backup.is_symlink()
                    or stat.S_IMODE(backup.stat().st_mode) != 0o600
                ):
                    raise ValueError("optional recovery file is missing")
            elif backup.exists() or backup.is_symlink():
                raise ValueError("optional recovery metadata is inconsistent")
        for name in expected_backups:
            integrity = backup_files[name]
            if (
                not isinstance(integrity, dict)
                or set(integrity) != {"size", "sha256"}
                or isinstance(integrity["size"], bool)
                or not isinstance(integrity["size"], int)
                or integrity["size"] < 0
                or not isinstance(integrity["sha256"], str)
                or re.fullmatch(r"[0-9a-f]{64}", integrity["sha256"]) is None
                or _integrity_record(backup_dir / name) != integrity
            ):
                raise ValueError("recovery file integrity check failed")
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise RunnerError("private recovery data is invalid") from exc
    return optional_files


def restore_run(root, run_id):
    root = Path(root).resolve()
    run_id = _validate_run_id(run_id)
    backup_dir = root / "state" / "backups" / f"mint-run-{run_id}"
    optional_files = _validate_recovery_data(backup_dir, run_id)
    for name in REQUIRED_FILES:
        _atomic_copy(backup_dir / name, root / name)
    for name in OPTIONAL_FILES:
        existed = optional_files[name]
        current = root / name
        backup = backup_dir / name
        if existed:
            _atomic_copy(backup, current)
        elif not existed and current.exists():
            current.unlink()
    active_marker = root / "state" / ".mint-run-active"
    if active_marker.exists() and active_marker.read_text().strip() == run_id:
        active_marker.unlink()
    _atomic_write(backup_dir / "restored", b"restored\n")


def restore_active(root):
    root = Path(root).resolve()
    marker = root / "state" / ".mint-run-active"
    if not marker.exists():
        return
    run_id = marker.read_text().strip()
    _validate_run_id(run_id)
    restore_run(root, run_id)


def aggregate_log(log_path):
    text = Path(log_path).read_text(errors="replace")
    return {
        name: len(pattern.findall(text))
        for name, pattern in LOG_PATTERNS.items()
    }


def _parse_guard_result(path):
    allowed = {
        "reason",
        "start_balance",
        "end_balance",
        "observed_loss",
        "duration_seconds",
        "child_exit_code",
        "loss_limit_lamports",
        "early_stop_lamports",
        "log_path",
    }
    result = {}
    for line in Path(path).read_text(errors="replace").splitlines():
        key, separator, value = line.partition("=")
        if separator and key in allowed:
            result[key] = value
    return result


def _empty_chain_summary():
    return {name: 0 for name in CHAIN_SUMMARY_KEYS}


def _sanitize_chain_summary(summary):
    if not isinstance(summary, dict):
        raise ValueError("invalid chain summary")
    sanitized = {}
    for name in CHAIN_SUMMARY_KEYS:
        value = summary.get(name)
        if type(value) is not int:
            raise ValueError("invalid chain summary")
        sanitized[name] = value
    return sanitized


def aggregate_chain(
    config,
    mint,
    started_at,
    ended_at,
    transport=None,
    pubkey_resolver=None,
):
    from scripts import zavod_guard

    caller = transport or rpc_call
    rpc_url = config["rpc"]["url"]
    wallet = (
        pubkey_resolver(config)
        if pubkey_resolver
        else zavod_guard.wallet_pubkey(config["wallet"]["private_key"])
    )
    signatures_body = caller(
        rpc_url,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getSignaturesForAddress",
            "params": [wallet, {"limit": 200, "commitment": "finalized"}],
        },
        10,
    )
    entries = [
        entry
        for entry in signatures_body.get("result", [])
        if isinstance(entry.get("blockTime"), int)
        and started_at <= entry["blockTime"] <= ended_at
        and entry.get("confirmationStatus") in (None, "finalized")
    ]
    summary = _empty_chain_summary()
    wsol_mint = "So11111111111111111111111111111111111111112"
    for entry in entries:
        body = caller(
            rpc_url,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getTransaction",
                "params": [
                    entry["signature"],
                    {
                        "encoding": "jsonParsed",
                        "commitment": "finalized",
                        "maxSupportedTransactionVersion": 0,
                    },
                ],
            },
            10,
        )
        transaction = body.get("result")
        if not transaction:
            continue
        meta = transaction.get("meta") or {}
        token_balances = (
            list(meta.get("preTokenBalances", []) or [])
            + list(meta.get("postTokenBalances", []) or [])
        )
        account_keys = (
            transaction.get("transaction", {})
            .get("message", {})
            .get("accountKeys", [])
        )
        account_pubkeys = [
            item.get("pubkey") if isinstance(item, dict) else item
            for item in account_keys
        ]
        if mint not in account_pubkeys and not any(
            balance.get("mint") == mint for balance in token_balances
        ):
            continue
        summary["landed"] += 1
        if meta.get("err") is None:
            summary["successful"] += 1
            message = transaction.get("transaction", {}).get("message", {})
            instructions = list(message.get("instructions", []) or [])
            for group in meta.get("innerInstructions", []) or []:
                instructions.extend(group.get("instructions", []) or [])
            for instruction in instructions:
                if instruction.get("program") != "system":
                    continue
                parsed = instruction.get("parsed") or {}
                info = parsed.get("info") or {}
                if info.get("source") != wallet:
                    continue
                lamports = info.get("lamports")
                if not isinstance(lamports, int) or lamports < 0:
                    continue
                if parsed.get("type") in ("createAccount", "createAccountWithSeed"):
                    summary["rent_lamports"] += lamports
                elif parsed.get("type") in ("transfer", "transferWithSeed"):
                    summary["transfers_lamports"] += lamports
        else:
            summary["failed"] += 1
        summary["fees_lamports"] += int(meta.get("fee") or 0)
        if wallet in account_pubkeys:
            index = account_pubkeys.index(wallet)
            pre = meta.get("preBalances", [])
            post = meta.get("postBalances", [])
            if index < len(pre) and index < len(post):
                summary["sol_delta_lamports"] += int(post[index]) - int(pre[index])
        for field, sign in (("preTokenBalances", -1), ("postTokenBalances", 1)):
            for balance in meta.get(field, []) or []:
                if balance.get("owner") == wallet and balance.get("mint") == wsol_mint:
                    amount = balance.get("uiTokenAmount", {}).get("amount", "0")
                    if isinstance(amount, str) and amount.isdigit():
                        summary["wsol_delta_raw"] += sign * int(amount)
    return summary


def _record_state(root, heading, bullets):
    root = Path(root).resolve()
    backup_root = root / "state" / "backups"
    backup_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = Path(tempfile.mkdtemp(prefix=f"state-{stamp}-", dir=backup_root))
    backup_dir.chmod(0o700)
    names = ("CURRENT.md", "EXPERIMENTS.md")
    originals = {}
    for name in names:
        path = root / "state" / name
        try:
            if not path.is_file() or path.is_symlink():
                raise OSError("state file is unavailable")
            originals[name] = path.read_bytes()
            _safe_copy(path, backup_dir / name)
        except OSError as exc:
            raise RunnerError("state files are unavailable") from exc
    addition = f"\n## {heading}\n\n"
    for bullet in bullets:
        addition += f"- {bullet}\n"
    addition_bytes = addition.encode()
    staged = {name: originals[name] + addition_bytes for name in names}
    try:
        for name in names:
            _atomic_write(root / "state" / name, staged[name])
    except BaseException:
        rollback_error = None
        for name in names:
            try:
                _atomic_copy(
                    backup_dir / name,
                    root / "state" / name,
                )
            except Exception as exc:
                rollback_error = exc
        if rollback_error is not None:
            raise RunnerError("state update rollback failed") from rollback_error
        raise


def record_preparation_failure(root):
    _record_state(
        root,
        "single-mint preparation failed",
        [
            "Preparation stopped before live execution.",
            "No transaction-capable command was invoked.",
            "Workspace restoration was attempted; inspect the private recovery directory before retrying.",
        ],
    )


def record_finalization_failure(root):
    _record_state(
        root,
        "single-mint finalization failed",
        [
            "A guarded live attempt ended, but result finalization did not complete.",
            "Workspace restoration was attempted; inspect the private recovery directory and guarded log.",
            "No automatic retry was started.",
        ],
    )


def finalize_run(
    root,
    run_id,
    guard_exit,
    started_at,
    ended_at,
    transport=None,
    pubkey_resolver=None,
    chain_aggregator=None,
):
    root = Path(root).resolve()
    run_id = _validate_run_id(run_id)
    backup_dir = root / "state" / "backups" / f"mint-run-{run_id}"
    result_dir = root / "state" / "mint-runs" / run_id
    _validate_recovery_data(backup_dir, run_id)
    metadata = json.loads((backup_dir / "metadata.json").read_bytes())
    guard = _parse_guard_result(result_dir / "guard-result.txt")
    log_relative = guard.get("log_path")
    log_path = None
    if log_relative:
        candidate = (root / log_relative).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            pass
        else:
            log_path = candidate
    log_summary = (
        aggregate_log(log_path)
        if log_path is not None and log_path.is_file()
        else {name: 0 for name in LOG_PATTERNS}
    )
    started_at = int(started_at)
    ended_at = int(ended_at)
    if started_at <= 0 or ended_at < started_at:
        raise RunnerError("run window is invalid")
    duration = float(guard.get("duration_seconds", "0"))
    try:
        with (backup_dir / "config.toml").open("rb") as handle:
            config = tomllib.load(handle)
        chain = _sanitize_chain_summary(
            (chain_aggregator or aggregate_chain)(
                config,
                metadata["mint"],
                started_at,
                ended_at,
                transport=transport,
                pubkey_resolver=pubkey_resolver,
            )
        )
        aggregation_status = "ok"
    except Exception:
        chain = _empty_chain_summary()
        aggregation_status = "failed"
    for name in OPTIONAL_FILES:
        generated = root / name
        if generated.exists():
            _safe_copy(generated, result_dir / f"generated-{name}")
    manifest = {
        "run_id": run_id,
        "mint": metadata["mint"],
        "timeout_seconds": metadata["timeout_seconds"],
        "guard_exit": int(guard_exit),
        "stop_reason": (
            guard["reason"]
            if guard.get("reason") in STOP_REASONS
            else "unknown"
        ),
        "duration_seconds": duration,
        "started_at": started_at,
        "ended_at": ended_at,
        "aggregation_status": aggregation_status,
        "log_events": log_summary,
        "chain": chain,
    }
    _atomic_write(
        result_dir / "manifest.json",
        (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode(),
    )
    heading = f"{run_id} — single-mint guarded run"
    bullets = [
        f"Target mint recorded in private run manifest; timeout `{metadata['timeout_seconds']} s`.",
        f"Stop reason `{manifest['stop_reason']}`; landed `{chain['landed']}`, successful `{chain['successful']}`, failed `{chain['failed']}`.",
        f"Fees `{chain['fees_lamports']}`, rent `{chain['rent_lamports']}`, transfers `{chain['transfers_lamports']}` lamports.",
        f"SOL delta `{chain['sol_delta_lamports']}` lamports; wSOL delta `{chain['wsol_delta_raw']}` raw units.",
        "Original config, token list, and runtime artifacts restored; no automatic retry.",
    ]
    restore_run(root, run_id)
    _record_state(root, heading, bullets)
    return manifest


def _record_failure_safely(root, command):
    try:
        if command == "prepare":
            record_preparation_failure(root)
        elif command == "finalize":
            record_finalization_failure(root)
    except Exception:
        return False
    return True


def _print_safe_summary(prepared):
    for key, value in prepared.safe_summary().items():
        print(f"{key}={value}")


def _sigterm_as_keyboard_interrupt(signum, frame):
    del signum, frame
    raise KeyboardInterrupt()


def _prepare_with_sigterm_handler(root, mint, timeout):
    previous_handler = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, _sigterm_as_keyboard_interrupt)
    try:
        return prepare_run(root, mint, timeout)
    finally:
        signal.signal(signal.SIGTERM, previous_handler)


def _emit_command_failure(root, command, error, status):
    state_recorded = _record_failure_safely(root, command)
    print(f"status=failed\nerror={error}", file=os.sys.stderr)
    if not state_recorded:
        print("state_recording=failed", file=os.sys.stderr)
    return status


def main(argv=None):
    parser = _SafeArgumentParser(
        description="Single-mint Zavod run state manager"
    )
    parser.add_argument("--root", default=".")
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("--mint", required=True)
    prepare_parser.add_argument("--timeout", default="300")
    restore_parser = subparsers.add_parser("restore")
    restore_parser.add_argument("--run-id", required=True)
    subparsers.add_parser("restore-active")
    result_parser = subparsers.add_parser("result-path")
    result_parser.add_argument("--run-id", required=True)
    finalize_parser = subparsers.add_parser("finalize")
    finalize_parser.add_argument("--run-id", required=True)
    finalize_parser.add_argument("--guard-exit", required=True, type=int)
    finalize_parser.add_argument("--started-at", required=True, type=int)
    finalize_parser.add_argument("--ended-at", required=True, type=int)
    try:
        args = parser.parse_args(argv)
    except _CliArgumentError:
        print("status=failed\nerror=invalid arguments", file=os.sys.stderr)
        return 2
    root = Path(args.root).resolve()
    try:
        if args.command == "prepare":
            prepared = _prepare_with_sigterm_handler(
                root, args.mint, args.timeout
            )
            _print_safe_summary(prepared)
            return 0
        if args.command == "restore":
            restore_run(root, args.run_id)
            return 0
        if args.command == "restore-active":
            restore_active(root)
            return 0
        if args.command == "result-path":
            run_id = _validate_run_id(args.run_id)
            print(root / "state" / "mint-runs" / run_id / "guard-result.txt")
            return 0
        if args.command == "finalize":
            manifest = finalize_run(
                root,
                args.run_id,
                args.guard_exit,
                args.started_at,
                args.ended_at,
            )
            print(f"stop_reason={manifest['stop_reason']}")
            print(f"manifest=state/mint-runs/{args.run_id}/manifest.json")
            return 0
    except _CliArgumentError:
        return _emit_command_failure(
            root, args.command, "invalid arguments", 2
        )
    except RunnerError:
        return _emit_command_failure(
            root, args.command, "operation failed", 1
        )
    except KeyboardInterrupt:
        return _emit_command_failure(
            root, args.command, "operation interrupted", 130
        )
    except Exception:
        return _emit_command_failure(
            root, args.command, "operation failed", 1
        )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
