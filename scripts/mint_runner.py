#!/usr/bin/env python3
import argparse
import hashlib
import json
import math
import os
import re
import secrets
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

try:
    from scripts import zavod_guard
except ModuleNotFoundError:
    import zavod_guard


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


class GeneratedArtifactContentError(RunnerError):
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


@dataclass
class _FinalizationDirectories:
    root_path: Path
    root_fd: int
    state_fd: int
    mint_runs_fd: int
    result_fd: int

    def close(self):
        for descriptor in (
            self.result_fd,
            self.mint_runs_fd,
            self.state_fd,
            self.root_fd,
        ):
            try:
                os.close(descriptor)
            except OSError:
                pass


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
        initialized = (
            value["data"]["parsed"]["info"]["isInitialized"]
            if value
            else None
        )
        if (
            value is None
            or value.get("executable") is not False
            or value.get("owner") not in (TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID)
            or parsed_type != "mint"
            or initialized is not True
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


def _lstat_owned_path(path, expected_type, mode=None):
    path = Path(path)
    try:
        info = path.lstat()
    except OSError as exc:
        raise RunnerError("private run paths are invalid") from exc
    type_matches = (
        stat.S_ISDIR(info.st_mode)
        if expected_type == "directory"
        else stat.S_ISREG(info.st_mode)
    )
    if (
        not type_matches
        or info.st_uid != os.getuid()
        or (mode is not None and stat.S_IMODE(info.st_mode) != mode)
    ):
        raise RunnerError("private run paths are invalid")
    return info


def _path_exists_no_follow(path):
    try:
        Path(path).lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise RunnerError("private run paths are invalid") from exc
    return True


def _read_owned_file_no_follow(path, mode=None):
    path = Path(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise RunnerError("private run paths are invalid") from exc
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or (mode is not None and stat.S_IMODE(info.st_mode) != mode)
        ):
            raise RunnerError("private run paths are invalid")
        chunks = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    except OSError as exc:
        raise RunnerError("private run paths are invalid") from exc
    finally:
        os.close(descriptor)


def _validate_directory_fd(descriptor, mode=None):
    try:
        info = os.fstat(descriptor)
    except OSError as exc:
        raise RunnerError("private run paths are invalid") from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or (mode is not None and stat.S_IMODE(info.st_mode) != mode)
    ):
        raise RunnerError("private run paths are invalid")
    return info


def _open_owned_directory_at(parent_fd, name, mode=None):
    if not isinstance(name, str) or not name or Path(name).name != name:
        raise RunnerError("private run paths are invalid")
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise RunnerError("private run paths are invalid") from exc
    try:
        _validate_directory_fd(descriptor, mode=mode)
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def _open_finalization_directories(root, run_id):
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    descriptors = []
    try:
        root_fd = os.open(root, flags)
        descriptors.append(root_fd)
        _validate_directory_fd(root_fd)
        state_fd = _open_owned_directory_at(root_fd, "state")
        descriptors.append(state_fd)
        mint_runs_fd = _open_owned_directory_at(state_fd, "mint-runs")
        descriptors.append(mint_runs_fd)
        result_fd = _open_owned_directory_at(
            mint_runs_fd,
            run_id,
            mode=0o700,
        )
        descriptors.append(result_fd)
        return _FinalizationDirectories(
            root_path=Path(root),
            root_fd=root_fd,
            state_fd=state_fd,
            mint_runs_fd=mint_runs_fd,
            result_fd=result_fd,
        )
    except OSError as exc:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        raise RunnerError("private run paths are invalid") from exc
    except Exception:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        raise


def _read_owned_file_at(directory_fd, name, mode=None, missing_ok=False):
    if not isinstance(name, str) or not name or Path(name).name != name:
        raise RunnerError("private run paths are invalid")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=directory_fd)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise RunnerError("private run paths are invalid") from None
    except OSError as exc:
        raise RunnerError("private run paths are invalid") from exc
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or (mode is not None and stat.S_IMODE(info.st_mode) != mode)
        ):
            raise RunnerError("private run paths are invalid")
        chunks = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    except OSError as exc:
        raise RunnerError("private run paths are invalid") from exc
    finally:
        os.close(descriptor)


def _existing_owned_file_at(directory_fd, name, mode=0o600):
    try:
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise RunnerError("private run paths are invalid") from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != mode
    ):
        raise RunnerError("private run paths are invalid")
    return True


def _atomic_write_at(directory_fd, name, data, mode=0o600):
    _validate_directory_fd(directory_fd, mode=0o700)
    _existing_owned_file_at(directory_fd, name, mode=mode)
    temporary = f".{name}.{secrets.token_hex(12)}"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = None
    try:
        descriptor = os.open(
            temporary,
            flags,
            mode,
            dir_fd=directory_fd,
        )
        os.fchmod(descriptor, mode)
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(
            temporary,
            name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        _read_owned_file_at(directory_fd, name, mode=mode)
    except Exception:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        except OSError:
            pass
        raise


def _directory_identity(descriptor):
    info = _validate_directory_fd(descriptor)
    return info.st_dev, info.st_ino


def _require_same_directory_at(parent_fd, name, expected_fd, mode=None):
    current_fd = _open_owned_directory_at(parent_fd, name, mode=mode)
    try:
        if _directory_identity(current_fd) != _directory_identity(expected_fd):
            raise RunnerError("private run paths are invalid")
    finally:
        os.close(current_fd)


def _validate_held_state(directories):
    try:
        root_info = directories.root_path.lstat()
    except OSError as exc:
        raise RunnerError("private run paths are invalid") from exc
    held_root = _validate_directory_fd(directories.root_fd)
    if (
        not stat.S_ISDIR(root_info.st_mode)
        or root_info.st_uid != os.getuid()
        or (root_info.st_dev, root_info.st_ino)
        != (held_root.st_dev, held_root.st_ino)
    ):
        raise RunnerError("private run paths are invalid")
    _require_same_directory_at(
        directories.root_fd,
        "state",
        directories.state_fd,
    )


def _validate_held_result(directories, run_id):
    _validate_held_state(directories)
    _require_same_directory_at(
        directories.state_fd,
        "mint-runs",
        directories.mint_runs_fd,
    )
    _require_same_directory_at(
        directories.mint_runs_fd,
        run_id,
        directories.result_fd,
        mode=0o700,
    )


def _integrity_record(path):
    data = Path(path).read_bytes()
    return {
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _load_workspace_config(root):
    config_path = root / "config.toml"
    try:
        config = zavod_guard.load_config_bytes(
            _read_owned_file_no_follow(config_path, mode=0o600)
        )
    except RunnerError:
        raise
    except zavod_guard.GuardError as exc:
        raise RunnerError("config.toml is invalid or unreadable") from exc
    if zavod_guard.validate_single_mint_auto(config):
        raise RunnerError("single-mint configuration is invalid")
    rpc_url = config.get("rpc", {}).get("url")
    if not isinstance(rpc_url, str) or not rpc_url:
        raise RunnerError("RPC configuration is missing")
    return config, rpc_url


def _run_preflight(root):
    result = subprocess.run(
        [
            "python3",
            "scripts/zavod_guard.py",
            "preflight",
            "--config",
            "config.toml",
            "--profile",
            "single-mint-auto",
        ],
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
    return metadata


def validate_live_state(root, run_id):
    root = Path(root).resolve()
    run_id = _validate_run_id(run_id)
    backup_dir = root / "state" / "backups" / f"mint-run-{run_id}"
    metadata = _validate_recovery_data(backup_dir, run_id)
    if _path_exists_no_follow(backup_dir / "restored"):
        raise RunnerError("live run state validation failed")
    expected_marker = f"{run_id}\n".encode()
    expected_tokens = f'tokens = ["{metadata["mint"]}"]\n'.encode()
    try:
        marker = _read_owned_file_no_follow(
            root / "state" / ".mint-run-active",
            mode=0o600,
        )
        tokens = _read_owned_file_no_follow(
            root / "tokens.toml",
            mode=0o600,
        )
    except RunnerError:
        raise
    except (OSError, ValueError, TypeError) as exc:
        raise RunnerError("live run state validation failed") from exc
    if marker != expected_marker or tokens != expected_tokens:
        raise RunnerError("live run state validation failed")


def restore_run(root, run_id):
    root = Path(root).resolve()
    run_id = _validate_run_id(run_id)
    backup_dir = root / "state" / "backups" / f"mint-run-{run_id}"
    metadata = _validate_recovery_data(backup_dir, run_id)
    optional_files = metadata["optional_files"]
    for name in REQUIRED_FILES:
        _atomic_copy(backup_dir / name, root / name)
    for name in OPTIONAL_FILES:
        existed = optional_files[name]
        current = root / name
        backup = backup_dir / name
        if existed:
            _atomic_copy(backup, current)
        elif not existed and _path_exists_no_follow(current):
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
    text = _read_owned_file_no_follow(log_path).decode(errors="replace")
    return {
        name: len(pattern.findall(text))
        for name, pattern in LOG_PATTERNS.items()
    }


def _parse_guard_result(path):
    return _parse_guard_result_bytes(
        _read_owned_file_no_follow(path, mode=0o600)
    )


def _parse_guard_result_bytes(data):
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
    text = data.decode(errors="replace")
    for line in text.splitlines():
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
        if name not in ("sol_delta_lamports", "wsol_delta_raw") and value < 0:
            raise ValueError("invalid chain summary")
        sanitized[name] = value
    return sanitized


def _rpc_result(body, expected_type):
    if (
        not isinstance(body, dict)
        or body.get("error") is not None
        or "result" not in body
        or not isinstance(body["result"], expected_type)
    ):
        raise RunnerError("chain aggregation failed")
    return body["result"]


def _require_nonnegative_integer(value):
    if type(value) is not int or value < 0:
        raise RunnerError("chain aggregation failed")
    return value


def _parse_token_balances(meta, field):
    balances = meta.get(field)
    if not isinstance(balances, list):
        raise RunnerError("chain aggregation failed")
    parsed = []
    for balance in balances:
        if not isinstance(balance, dict):
            raise RunnerError("chain aggregation failed")
        owner = balance.get("owner")
        mint = balance.get("mint")
        ui_amount = balance.get("uiTokenAmount")
        if (
            (owner is not None and not isinstance(owner, str))
            or not isinstance(mint, str)
            or not isinstance(ui_amount, dict)
        ):
            raise RunnerError("chain aggregation failed")
        amount = ui_amount.get("amount")
        if not isinstance(amount, str) or re.fullmatch(r"[0-9]+", amount) is None:
            raise RunnerError("chain aggregation failed")
        parsed.append((owner, mint, int(amount)))
    return parsed


def _parse_account_pubkeys(message):
    account_keys = message.get("accountKeys")
    if not isinstance(account_keys, list):
        raise RunnerError("chain aggregation failed")
    pubkeys = []
    for item in account_keys:
        pubkey = item.get("pubkey") if isinstance(item, dict) else item
        if not isinstance(pubkey, str) or not pubkey:
            raise RunnerError("chain aggregation failed")
        pubkeys.append(pubkey)
    return pubkeys


def _parse_instructions(meta, message):
    instructions = message.get("instructions")
    if (
        not isinstance(instructions, list)
        or any(not isinstance(item, dict) for item in instructions)
    ):
        raise RunnerError("chain aggregation failed")
    parsed = list(instructions)
    inner_groups = meta.get("innerInstructions")
    if inner_groups is None:
        inner_groups = []
    if not isinstance(inner_groups, list):
        raise RunnerError("chain aggregation failed")
    for group in inner_groups:
        if not isinstance(group, dict):
            raise RunnerError("chain aggregation failed")
        inner = group.get("instructions")
        if (
            not isinstance(inner, list)
            or any(not isinstance(item, dict) for item in inner)
        ):
            raise RunnerError("chain aggregation failed")
        parsed.extend(inner)
    return parsed


def _signature_entries(caller, rpc_url, wallet, started_at, ended_at):
    entries = []
    seen_signatures = set()
    seen_cursors = set()
    before = None
    while True:
        options = {"limit": 200, "commitment": "finalized"}
        if before is not None:
            options["before"] = before
        page = _rpc_result(
            caller(
                rpc_url,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getSignaturesForAddress",
                    "params": [wallet, options],
                },
                10,
            ),
            list,
        )
        if not page:
            break
        oldest_usable = None
        for entry in page:
            if not isinstance(entry, dict):
                raise RunnerError("chain aggregation failed")
            signature = entry.get("signature")
            block_time = entry.get("blockTime")
            confirmation = entry.get("confirmationStatus")
            if (
                not isinstance(signature, str)
                or not signature
                or confirmation != "finalized"
                or (
                    block_time is not None
                    and (
                        type(block_time) is not int
                        or block_time < 0
                    )
                )
            ):
                raise RunnerError("chain aggregation failed")
            if block_time is not None:
                oldest_usable = (
                    block_time
                    if oldest_usable is None
                    else min(oldest_usable, block_time)
                )
            if (
                (block_time is None or started_at <= block_time <= ended_at)
                and signature not in seen_signatures
            ):
                entries.append(signature)
            seen_signatures.add(signature)
        cursor = page[-1]["signature"]
        if cursor in seen_cursors:
            raise RunnerError("chain aggregation failed")
        seen_cursors.add(cursor)
        before = cursor
        if oldest_usable is not None and oldest_usable < started_at:
            break
    return entries


def aggregate_chain(
    config,
    mint,
    started_at,
    ended_at,
    transport=None,
    pubkey_resolver=None,
):
    caller = transport or rpc_call
    rpc_url = config["rpc"]["url"]
    wallet = (
        pubkey_resolver(config)
        if pubkey_resolver
        else zavod_guard.wallet_pubkey(config["wallet"]["private_key"])
    )
    try:
        signatures = _signature_entries(
            caller,
            rpc_url,
            wallet,
            started_at,
            ended_at,
        )
        summary = _empty_chain_summary()
        wsol_mint = "So11111111111111111111111111111111111111112"
        for signature in signatures:
            transaction = _rpc_result(
                caller(
                    rpc_url,
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "getTransaction",
                        "params": [
                            signature,
                            {
                                "encoding": "jsonParsed",
                                "commitment": "finalized",
                                "maxSupportedTransactionVersion": 0,
                            },
                        ],
                    },
                    10,
                ),
                dict,
            )
            meta = transaction.get("meta")
            transaction_body = transaction.get("transaction")
            transaction_time = transaction.get("blockTime")
            if type(transaction_time) is not int or transaction_time < 0:
                raise RunnerError("chain aggregation failed")
            if not started_at <= transaction_time <= ended_at:
                continue
            if not isinstance(meta, dict) or not isinstance(transaction_body, dict):
                raise RunnerError("chain aggregation failed")
            if "err" not in meta:
                raise RunnerError("chain aggregation failed")
            message = transaction_body.get("message")
            if not isinstance(message, dict):
                raise RunnerError("chain aggregation failed")
            account_pubkeys = _parse_account_pubkeys(message)
            pre_balances = meta.get("preBalances")
            post_balances = meta.get("postBalances")
            if (
                not isinstance(pre_balances, list)
                or not isinstance(post_balances, list)
            ):
                raise RunnerError("chain aggregation failed")
            pre_balances = [
                _require_nonnegative_integer(value)
                for value in pre_balances
            ]
            post_balances = [
                _require_nonnegative_integer(value)
                for value in post_balances
            ]
            if (
                len(pre_balances) != len(account_pubkeys)
                or len(post_balances) != len(account_pubkeys)
            ):
                raise RunnerError("chain aggregation failed")
            fee = _require_nonnegative_integer(meta.get("fee"))
            pre_tokens = _parse_token_balances(meta, "preTokenBalances")
            post_tokens = _parse_token_balances(meta, "postTokenBalances")
            instructions = _parse_instructions(meta, message)
            if mint not in account_pubkeys and not any(
                token_mint == mint
                for _owner, token_mint, _amount in pre_tokens + post_tokens
            ):
                continue
            if wallet not in account_pubkeys:
                raise RunnerError("chain aggregation failed")
            wallet_index = account_pubkeys.index(wallet)
            if (
                wallet_index >= len(pre_balances)
                or wallet_index >= len(post_balances)
            ):
                raise RunnerError("chain aggregation failed")

            summary["landed"] += 1
            if meta.get("err") is None:
                summary["successful"] += 1
                for instruction in instructions:
                    if instruction.get("program") != "system":
                        continue
                    parsed = instruction.get("parsed")
                    if not isinstance(parsed, dict):
                        raise RunnerError("chain aggregation failed")
                    info = parsed.get("info")
                    instruction_type = parsed.get("type")
                    if not isinstance(info, dict):
                        raise RunnerError("chain aggregation failed")
                    if instruction_type in (
                        "createAccount",
                        "createAccountWithSeed",
                        "transfer",
                        "transferWithSeed",
                    ):
                        lamports = _require_nonnegative_integer(
                            info.get("lamports")
                        )
                        if info.get("source") != wallet:
                            continue
                        if instruction_type in (
                            "createAccount",
                            "createAccountWithSeed",
                        ):
                            summary["rent_lamports"] += lamports
                        else:
                            summary["transfers_lamports"] += lamports
            else:
                summary["failed"] += 1
            summary["fees_lamports"] += fee
            summary["sol_delta_lamports"] += (
                post_balances[wallet_index] - pre_balances[wallet_index]
            )
            for balances, sign in ((pre_tokens, -1), (post_tokens, 1)):
                for owner, token_mint, amount in balances:
                    if owner == wallet and token_mint == wsol_mint:
                        summary["wsol_delta_raw"] += sign * amount
        return summary
    except RunnerError:
        raise
    except Exception as exc:
        raise RunnerError("chain aggregation failed") from exc


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


def _validate_finalization_context(
    root,
    run_id,
    backup_dir,
    directories,
):
    _validate_held_result(directories, run_id)
    expected_marker = f"{run_id}\n".encode()
    if (
        _read_owned_file_at(
            directories.state_fd,
            ".mint-run-active",
            mode=0o600,
        )
        != expected_marker
    ):
        raise RunnerError("private run paths are invalid")
    if _path_exists_no_follow(backup_dir / "restored"):
        raise RunnerError("private run paths are invalid")
    _validate_recovery_data(backup_dir, run_id)
    try:
        metadata = json.loads(
            _read_owned_file_no_follow(
                backup_dir / "metadata.json",
                mode=0o600,
            )
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RunnerError("private recovery data is invalid") from exc
    _validate_held_result(directories, run_id)
    return metadata


def _validate_log_path(root, value):
    if not isinstance(value, str) or not value:
        return None
    relative = Path(value)
    if (
        relative.is_absolute()
        or not relative.parts
        or relative.parts[0] != "logs"
        or any(part in ("", ".", "..") for part in relative.parts)
    ):
        raise RunnerError("private run paths are invalid")
    logs_dir = root / "logs"
    _lstat_owned_path(logs_dir, "directory")
    parent = logs_dir
    for part in relative.parts[1:-1]:
        parent = parent / part
        _lstat_owned_path(parent, "directory")
    candidate = root / relative
    try:
        candidate.resolve(strict=True).relative_to(logs_dir.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise RunnerError("private run paths are invalid") from exc
    _lstat_owned_path(candidate, "file", mode=0o600)
    return candidate


def _sanitize_json_value(value, policy, depth=0):
    if depth > 64:
        raise GeneratedArtifactContentError("generated runtime artifact is invalid")
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise GeneratedArtifactContentError("generated runtime artifact is invalid")
        return value
    if isinstance(value, str):
        return policy.redact_text(value)
    if isinstance(value, list):
        return [
            _sanitize_json_value(item, policy, depth + 1)
            for item in value
        ]
    if isinstance(value, dict):
        sanitized = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise GeneratedArtifactContentError(
                    "generated runtime artifact is invalid"
                )
            safe_key = policy.redact_text(key)
            if safe_key in sanitized:
                raise GeneratedArtifactContentError(
                    "generated runtime artifact is invalid"
                )
            sanitized[safe_key] = _sanitize_json_value(
                item,
                policy,
                depth + 1,
            )
        return sanitized
    raise GeneratedArtifactContentError("generated runtime artifact is invalid")


def _sanitize_generated_artifact(data, policy):
    try:
        parsed = json.loads(
            data.decode(),
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(value)
            ),
        )
    except (UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise GeneratedArtifactContentError(
            "generated runtime artifact is invalid"
        ) from exc
    if not isinstance(parsed, (dict, list)):
        raise GeneratedArtifactContentError("generated runtime artifact is invalid")
    sanitized = _sanitize_json_value(parsed, policy)
    rendered = (
        json.dumps(
            sanitized,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        + "\n"
    )
    if policy.contains_protected(rendered):
        raise GeneratedArtifactContentError("generated runtime artifact is invalid")
    try:
        return rendered.encode()
    except UnicodeError as exc:
        raise GeneratedArtifactContentError(
            "generated runtime artifact is invalid"
        ) from exc


def _capture_generated_artifact(directories, name, policy):
    data = _read_owned_file_at(
        directories.root_fd,
        name,
        mode=0o600,
        missing_ok=True,
    )
    destination = f"generated-{name}"
    _existing_owned_file_at(directories.result_fd, destination)
    if data is None:
        return "missing"
    try:
        sanitized = _sanitize_generated_artifact(data, policy)
    except GeneratedArtifactContentError:
        return "rejected_content"
    _atomic_write_at(
        directories.result_fd,
        destination,
        sanitized,
    )
    return "captured"


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
    directories = _open_finalization_directories(root, run_id)
    try:
        metadata = _validate_finalization_context(
            root,
            run_id,
            backup_dir,
            directories,
        )
        manifest = None
        try:
            guard = _parse_guard_result_bytes(
                _read_owned_file_at(
                    directories.result_fd,
                    "guard-result.txt",
                    mode=0o600,
                )
            )
            log_path = _validate_log_path(root, guard.get("log_path"))
            log_summary = (
                aggregate_log(log_path)
                if log_path is not None
                else {name: 0 for name in LOG_PATTERNS}
            )
            if isinstance(started_at, bool) or isinstance(ended_at, bool):
                raise RunnerError("run window is invalid")
            started_at = int(started_at)
            ended_at = int(ended_at)
            if started_at <= 0 or ended_at < started_at:
                raise RunnerError("run window is invalid")
            try:
                duration = float(guard.get("duration_seconds", "0"))
            except (TypeError, ValueError) as exc:
                raise RunnerError("guard result is invalid") from exc
            if not math.isfinite(duration) or duration < 0:
                raise RunnerError("guard result is invalid")
            try:
                config = zavod_guard.load_config_bytes(
                    _read_owned_file_no_follow(
                        backup_dir / "config.toml",
                        mode=0o600,
                    )
                )
            except zavod_guard.GuardError as exc:
                raise RunnerError("finalization failed") from exc
            output_policy = zavod_guard.ProtectedOutputPolicy.from_config(
                config
            )
            try:
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
            artifact_status = {}
            for name in OPTIONAL_FILES:
                artifact_status[name] = _capture_generated_artifact(
                    directories,
                    name,
                    output_policy,
                )
            if isinstance(guard_exit, bool):
                raise RunnerError("guard result is invalid")
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
                "artifact_status": artifact_status,
                "log_events": log_summary,
                "chain": chain,
            }
            _atomic_write_at(
                directories.result_fd,
                "manifest.json",
                (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode(),
            )
            _validate_held_result(directories, run_id)
        finally:
            _validate_held_state(directories)
            restore_run(root, run_id)
        if manifest is None:
            raise RunnerError("finalization failed")
        heading = f"{run_id} — single-mint guarded run"
        bullets = [
            f"Target mint recorded in private run manifest; timeout `{metadata['timeout_seconds']} s`.",
            f"Stop reason `{manifest['stop_reason']}`; landed `{chain['landed']}`, successful `{chain['successful']}`, failed `{chain['failed']}`.",
            f"Fees `{chain['fees_lamports']}`, rent `{chain['rent_lamports']}`, transfers `{chain['transfers_lamports']}` lamports.",
            f"SOL delta `{chain['sol_delta_lamports']}` lamports; wSOL delta `{chain['wsol_delta_raw']}` raw units.",
            "Original config, token list, and runtime artifacts restored; no automatic retry.",
        ]
        _record_state(root, heading, bullets)
        return manifest
    finally:
        directories.close()


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


def _prepare_with_signal_handlers(root, mint, timeout):
    previous_handlers = {
        signum: signal.getsignal(signum)
        for signum in (signal.SIGINT, signal.SIGTERM)
    }
    for signum in previous_handlers:
        signal.signal(signum, _sigterm_as_keyboard_interrupt)
    try:
        return prepare_run(root, mint, timeout)
    finally:
        for signum, previous_handler in previous_handlers.items():
            signal.signal(signum, previous_handler)


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
    validate_parser = subparsers.add_parser("validate-live")
    validate_parser.add_argument("--run-id", required=True)
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
            prepared = _prepare_with_signal_handlers(
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
        if args.command == "validate-live":
            validate_live_state(root, args.run_id)
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
