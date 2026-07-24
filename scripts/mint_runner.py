#!/usr/bin/env python3
import argparse
import json
import os
import re
import shutil
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
REQUIRED_FILES = ("config.toml", "tokens.toml")
OPTIONAL_FILES = ("hot_tokens.json", "routing.json")


class RunnerError(RuntimeError):
    pass


@dataclass(frozen=True)
class PreparedRun:
    run_id: str
    mint: str
    timeout: int
    backup_dir: Path
    result_dir: Path
    cli_version: str

    def safe_summary(self):
        return {
            "run_id": self.run_id,
            "mint": self.mint,
            "timeout_seconds": self.timeout,
            "cli_version": self.cli_version,
            "auto_mode": "enabled",
            "preflight": "ok",
            "loss_limit_lamports": 30_000_000,
            "early_stop_lamports": 25_000_000,
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
    if (
        safe.get("preflight") != "ok"
        or safe.get("cli_version") != EXPECTED_CLI_VERSION
    ):
        raise RunnerError("guarded preflight failed")
    return safe


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
        if (
            preflight.get("preflight") != "ok"
            or preflight.get("cli_version") != EXPECTED_CLI_VERSION
        ):
            raise RunnerError("guarded preflight failed")
        return PreparedRun(
            run_id=run_id,
            mint=mint,
            timeout=timeout,
            backup_dir=backup_dir,
            result_dir=result_dir,
            cli_version=preflight["cli_version"],
        )
    except Exception:
        if (backup_dir / "metadata.json").exists():
            restore_run(root, run_id)
        raise


def restore_run(root, run_id):
    root = Path(root).resolve()
    backup_dir = root / "state" / "backups" / f"mint-run-{run_id}"
    metadata_path = backup_dir / "metadata.json"
    if not metadata_path.exists():
        return
    metadata = json.loads(metadata_path.read_text())
    for name in REQUIRED_FILES:
        backup = backup_dir / name
        if backup.exists():
            _atomic_copy(backup, root / name)
    for name in OPTIONAL_FILES:
        existed = metadata.get("optional_files", {}).get(name, False)
        current = root / name
        backup = backup_dir / name
        if existed and backup.exists():
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
    if not re.fullmatch(r"[0-9]{8}T[0-9]{6}Z", run_id):
        raise RunnerError("active mint-run marker is invalid")
    restore_run(root, run_id)


def _record_state(root, heading, bullets):
    root = Path(root).resolve()
    backup_root = root / "state" / "backups"
    backup_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = Path(tempfile.mkdtemp(prefix=f"state-{stamp}-", dir=backup_root))
    backup_dir.chmod(0o700)
    for name in ("CURRENT.md", "EXPERIMENTS.md"):
        path = root / "state" / name
        if path.exists():
            _safe_copy(path, backup_dir / name)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"\n## {heading}\n\n")
            for bullet in bullets:
                handle.write(f"- {bullet}\n")
        path.chmod(0o600)


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


def _record_failure_safely(root, command):
    try:
        if command == "prepare":
            record_preparation_failure(root)
        elif command == "finalize":
            record_finalization_failure(root)
    except Exception:
        pass


def _print_safe_summary(prepared):
    for key, value in prepared.safe_summary().items():
        print(f"{key}={value}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Single-mint Zavod run state manager")
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
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    try:
        if args.command == "prepare":
            prepared = prepare_run(root, args.mint, args.timeout)
            _print_safe_summary(prepared)
            return 0
        if args.command == "restore":
            restore_run(root, args.run_id)
            return 0
        if args.command == "restore-active":
            restore_active(root)
            return 0
        if args.command == "result-path":
            print(root / "state" / "mint-runs" / args.run_id / "guard-result.txt")
            return 0
    except RunnerError:
        _record_failure_safely(root, args.command)
        print("status=failed\nerror=operation failed", file=os.sys.stderr)
        return 1
    except Exception:
        _record_failure_safely(root, args.command)
        print("status=failed\nerror=operation failed", file=os.sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
