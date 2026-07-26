#!/usr/bin/env python3
"""Private, immutable configuration staging for the auto-filter diagnoser."""

import argparse
import copy
import hashlib
import json
import math
import os
import re
import secrets
import signal
import stat
import sys
import threading
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

try:
    from scripts import zavod_guard
except ModuleNotFoundError:
    import zavod_guard


BATCH_ID_PATTERN = re.compile(r"[0-9]{8}T[0-9]{6}Z", re.ASCII)
EARLY_STOP_LAMPORTS = 25_000_000
LOSS_LIMIT_LAMPORTS = 30_000_000
TIMEOUT_SECONDS = 300
BINARY_NAME = "zavod-mev-bot-rust-version-cli"
ACTIVE_MARKER = ".mint-auto-diagnose-active"
RUNS_DIRECTORY = "auto-diagnose-runs"


class DiagnoserError(RuntimeError):
    pass


class _PreparationInterrupted(DiagnoserError):
    def __init__(self, signum):
        self.signum = signum
        super().__init__("batch preparation interrupted")


@dataclass(frozen=True)
class StageMutation:
    name: str
    section: tuple[str, ...]
    key: str
    value: bool | int | float
    expected_type: type


@dataclass(frozen=True)
class PreparedStage:
    index: int
    name: str
    relative_root: str
    contract_relative_path: str
    skipped: bool
    skip_reason: str | None


@dataclass(frozen=True)
class PreparedBatch:
    batch_id: str
    mint: str
    relative_root: str
    stages: tuple[PreparedStage, ...]


BASELINE_MUTATIONS = (
    StageMutation("force_two_mints", ("auto",), "force_two_mints", False, bool),
    StageMutation("limit", ("auto", "filters"), "limit", 1, int),
    StageMutation("merge_mints", ("bot",), "merge_mints", False, bool),
    StageMutation("three_hop", ("auto",), "enable_three_hop", True, bool),
)
STAGE_MUTATIONS = (
    StageMutation("offchain", ("auto", "filters"), "ignore_offchain_bots", False, bool),
    StageMutation("activity", ("auto", "filters"), "min_tx_len", 0, int),
    StageMutation("aggregate_profit", ("auto", "filters"), "min_profit", 0, int),
    StageMutation("per_arb_profit", ("auto", "filters"), "min_profit_per_arb", 0, int),
    StageMutation("roi", ("auto", "filters"), "min_roi", 0.0, float),
    StageMutation("volume", ("auto", "filters"), "min_volume_lamports", 0, int),
    StageMutation("pool_liquidity", ("auto", "markets"), "min_pool_liquidity_lamports", 0, int),
)


def _configuration_error():
    return DiagnoserError("staged configuration is invalid")


def _parse_toml(data):
    if not isinstance(data, bytes):
        raise _configuration_error()
    try:
        parsed = tomllib.loads(data.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise _configuration_error() from exc
    pending = [parsed]
    while pending:
        value = pending.pop()
        if isinstance(value, float) and not math.isfinite(value):
            raise _configuration_error()
        if isinstance(value, dict):
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    return parsed


def _nested_value(config, section, key):
    value = config
    for part in section:
        if not isinstance(value, dict) or part not in value:
            raise _configuration_error()
        value = value[part]
    if not isinstance(value, dict) or key not in value:
        raise _configuration_error()
    return value[key]


def _set_nested_value(config, section, key, replacement):
    destination = config
    for part in section:
        destination = destination[part]
    destination[key] = replacement


def _replace_assignment(lines, mutation):
    """Rewrite exactly one bare assignment inside one plain table header."""
    section_name = ".".join(mutation.section).encode()
    header_pattern = re.compile(
        rb"^[ \t]*\[[ \t]*([^]\r\n]+?)[ \t]*\]"
        rb"[ \t]*(?:#[^\r\n]*)?(?:\r\n|\n|\r)?$"
    )
    if mutation.expected_type is bool:
        value_pattern = rb"(?:true|false)"
    elif mutation.expected_type is int:
        value_pattern = rb"[+-]?[0-9](?:_?[0-9])*"
    elif mutation.expected_type is float:
        value_pattern = rb"[+-]?(?:[0-9](?:_?[0-9])*)?\.[0-9](?:_?[0-9])*"
    else:
        raise _configuration_error()
    assignment_pattern = re.compile(
        rb"^([ \t]*" + re.escape(mutation.key.encode()) + rb"[ \t]*=[ \t]*)"
        + rb"(" + value_pattern + rb")"
        + rb"([ \t]*(?:#[^\r\n]*)?(?:\r\n|\n|\r)?)$"
    )
    active_section = None
    matches = []
    for index, line in enumerate(lines):
        header = header_pattern.fullmatch(line)
        if header is not None:
            active_section = b"".join(header.group(1).split())
            continue
        if active_section != section_name:
            continue
        assignment = assignment_pattern.fullmatch(line)
        if assignment is not None:
            matches.append((index, assignment))
    if len(matches) != 1:
        raise _configuration_error()
    index, assignment = matches[0]
    if mutation.expected_type is bool:
        rendered = str(mutation.value).lower().encode()
    else:
        rendered = str(mutation.value).encode()
    lines[index] = assignment.group(1) + rendered + assignment.group(3)


def _semantic_changes(before, after):
    changes = set()

    def visit(left, right, path=()):
        if isinstance(left, dict) and isinstance(right, dict):
            if set(left) != set(right):
                raise _configuration_error()
            for key in left:
                visit(left[key], right[key], path + (key,))
            return
        if left != right or type(left) is not type(right):
            changes.add(path)

    visit(before, after)
    return changes


def _apply_mutations(source, mutations):
    before = _parse_toml(source)
    expected = copy.deepcopy(before)
    lines = source.splitlines(keepends=True)
    expected_changes = set()
    for mutation in mutations:
        current = _nested_value(before, mutation.section, mutation.key)
        if type(current) is not mutation.expected_type:
            raise _configuration_error()
        _replace_assignment(lines, mutation)
        _set_nested_value(expected, mutation.section, mutation.key, mutation.value)
        if current != mutation.value:
            expected_changes.add(mutation.section + (mutation.key,))
    candidate = b"".join(lines)
    after = _parse_toml(candidate)
    if after != expected or _semantic_changes(before, after) != expected_changes:
        raise _configuration_error()
    return candidate


def stage_skip_reasons(source):
    """Return skip reasons without exposing or persisting source configuration."""
    current = _apply_mutations(source, BASELINE_MUTATIONS)
    reasons = {}
    for mutation in STAGE_MUTATIONS:
        config = _parse_toml(current)
        if _nested_value(config, mutation.section, mutation.key) == mutation.value:
            reasons[mutation.name] = "already_permissive"
            continue
        current = _apply_mutations(current, (mutation,))
    return reasons


def build_stage_configs(source: bytes) -> list[tuple[str, bytes, tuple[str, ...]]]:
    """Produce cumulative executable configs and their named relaxed filters."""
    current = _apply_mutations(source, BASELINE_MUTATIONS)
    stages = [("baseline", current, ())]
    applied = []
    for mutation in STAGE_MUTATIONS:
        config = _parse_toml(current)
        if _nested_value(config, mutation.section, mutation.key) == mutation.value:
            continue
        current = _apply_mutations(current, (mutation,))
        applied.append(mutation.name)
        stages.append((mutation.name, current, tuple(applied)))
    return stages


def _validate_batch_id(batch_id):
    if not isinstance(batch_id, str) or BATCH_ID_PATTERN.fullmatch(batch_id) is None:
        raise DiagnoserError("batch identifier is invalid")
    return batch_id


def _validate_owned(descriptor, kind, mode=None):
    try:
        identity = os.fstat(descriptor)
    except OSError as exc:
        raise DiagnoserError("private workspace paths are invalid") from exc
    expected = stat.S_ISDIR if kind == "directory" else stat.S_ISREG
    if not expected(identity.st_mode) or identity.st_uid != os.geteuid():
        raise DiagnoserError("private workspace paths are invalid")
    if mode is not None and stat.S_IMODE(identity.st_mode) != mode:
        raise DiagnoserError("private workspace paths are invalid")
    return identity


def _open_root(root):
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(Path(root), flags)
    except OSError as exc:
        raise DiagnoserError("workspace root is invalid") from exc
    try:
        _validate_owned(descriptor, "directory")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_relative_directory(parent_fd, name, mode=None):
    if not isinstance(name, str) or not name or Path(name).name != name:
        raise DiagnoserError("private workspace paths are invalid")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise DiagnoserError("private workspace paths are invalid") from exc
    try:
        _validate_owned(descriptor, "directory", mode)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _create_directory(parent_fd, name, mode=0o700):
    try:
        os.mkdir(name, mode, dir_fd=parent_fd)
    except FileExistsError as exc:
        raise DiagnoserError("a batch is already prepared") from exc
    except OSError as exc:
        raise DiagnoserError("private workspace preparation failed") from exc
    return _open_relative_directory(parent_fd, name, mode)


def _open_or_create_directory(parent_fd, name, mode=0o700):
    try:
        return _open_relative_directory(parent_fd, name, mode)
    except DiagnoserError:
        return _create_directory(parent_fd, name, mode)


def _read_owned_file(parent_fd, name, mode=None, executable=False):
    if not isinstance(name, str) or not name or Path(name).name != name:
        raise DiagnoserError("private workspace paths are invalid")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise DiagnoserError("required production input is invalid") from exc
    try:
        identity = _validate_owned(descriptor, "file", mode)
        if executable and not identity.st_mode & 0o111:
            raise DiagnoserError("required production input is invalid")
        chunks = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    except OSError as exc:
        raise DiagnoserError("required production input is invalid") from exc
    finally:
        os.close(descriptor)


def _write_private_at(parent_fd, name, data):
    """Atomically publish a mode-600 file without replacing an existing path."""
    temporary = f".{name}.{secrets.token_hex(16)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = None
    try:
        descriptor = os.open(temporary, flags, 0o600, dir_fd=parent_fd)
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, data)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.link(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd, follow_symlinks=False)
        os.unlink(temporary, dir_fd=parent_fd)
    except FileExistsError as exc:
        raise DiagnoserError("private workspace path already exists") from exc
    except OSError as exc:
        raise DiagnoserError("private workspace preparation failed") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=parent_fd)
        except FileNotFoundError:
            pass


def _write_all(descriptor, data):
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


def _link_or_copy_binary(root_fd, stage_fd, binary):
    try:
        os.link(binary, binary, src_dir_fd=root_fd, dst_dir_fd=stage_fd, follow_symlinks=False)
    except OSError:
        source = _read_owned_file(root_fd, binary, executable=True)
        temporary = f".{binary}.{secrets.token_hex(16)}"
        descriptor = None
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o700,
                dir_fd=stage_fd,
            )
            os.fchmod(descriptor, 0o700)
            _write_all(descriptor, source)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            os.link(temporary, binary, src_dir_fd=stage_fd, dst_dir_fd=stage_fd, follow_symlinks=False)
            os.unlink(temporary, dir_fd=stage_fd)
        except OSError as exc:
            raise DiagnoserError("private binary preparation failed") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                os.unlink(temporary, dir_fd=stage_fd)
            except FileNotFoundError:
                pass
    staged = _read_owned_file(stage_fd, binary, executable=True)
    if staged != _read_owned_file(root_fd, binary, executable=True):
        raise DiagnoserError("private binary preparation failed")


def _remove_active_marker(state_fd, batch_id):
    try:
        marker = _read_owned_file(state_fd, ACTIVE_MARKER, mode=0o600)
    except DiagnoserError:
        return
    if marker == f"{batch_id}\n".encode():
        try:
            os.unlink(ACTIVE_MARKER, dir_fd=state_fd)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise DiagnoserError("active batch marker is invalid") from exc


@contextmanager
def _restore_marker_on_termination(state_fd, batch_id):
    """Handle preparation-only termination without retaining ownership afterward."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    watched = (signal.SIGINT, signal.SIGTERM)
    previous = {}
    masked = hasattr(signal, "pthread_sigmask")
    previous_mask = None
    if masked:
        previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, watched)

    def interrupted(signum, frame):
        del frame
        _remove_active_marker(state_fd, batch_id)
        raise _PreparationInterrupted(signum)

    try:
        for signum in watched:
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, interrupted)
        if masked:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        yield
    finally:
        if masked:
            signal.pthread_sigmask(signal.SIG_BLOCK, watched)
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        if masked:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)


def _stage_relative_root(batch_id, index, name):
    return f"state/{RUNS_DIRECTORY}/{batch_id}/stages/{index}-{name}"


def _prepare_stage(stage_fd, root_fd, batch_id, index, name, config, mint, balance):
    config_data = config
    tokens_data = f'tokens = ["{mint}"]\n'.encode()
    _write_private_at(stage_fd, "config.toml", config_data)
    _write_private_at(stage_fd, "tokens.toml", tokens_data)
    _link_or_copy_binary(root_fd, stage_fd, BINARY_NAME)
    binary_data = _read_owned_file(stage_fd, BINARY_NAME)
    contract = {
        "schema": 1,
        "batch_id": batch_id,
        "stage_index": index,
        "stage_name": name,
        "target_mint": mint,
        "timeout_seconds": TIMEOUT_SECONDS,
        "batch_start_balance_lamports": balance,
        "early_stop_lamports": EARLY_STOP_LAMPORTS,
        "loss_limit_lamports": LOSS_LIMIT_LAMPORTS,
        "config_sha256": hashlib.sha256(config_data).hexdigest(),
        "tokens_sha256": hashlib.sha256(tokens_data).hexdigest(),
        "binary_sha256": hashlib.sha256(binary_data).hexdigest(),
        "three_hop_required": True,
    }
    _write_private_at(
        stage_fd,
        "stage-contract.json",
        (json.dumps(contract, sort_keys=True, separators=(",", ":")) + "\n").encode(),
    )


def prepare_batch(root: Path, mint: str, now=None, transport=None, balance_reader=None) -> PreparedBatch:
    """Validate one mint and create private stage workspaces without editing production."""
    root_fd = state_fd = runs_fd = batch_fd = stages_fd = None
    batch_id = None
    active_created = False
    try:
        root_fd = _open_root(root)
        config_bytes = _read_owned_file(root_fd, "config.toml", mode=0o600)
        tokens_bytes = _read_owned_file(root_fd, "tokens.toml", mode=0o600)
        binary_bytes = _read_owned_file(root_fd, BINARY_NAME, executable=True)
        config = _parse_toml(config_bytes)
        rpc_url = _nested_value(config, ("rpc",), "url")
        private_key = _nested_value(config, ("wallet",), "private_key")
        if not isinstance(rpc_url, str) or not isinstance(private_key, str):
            raise DiagnoserError("production configuration is invalid")
        try:
            zavod_guard.validate_token_mint_account(rpc_url, mint, transport)
            wallet = zavod_guard.wallet_pubkey(private_key)
            balance = (balance_reader or zavod_guard.get_balance_lamports)(rpc_url, wallet)
        except (zavod_guard.GuardError, OSError, ValueError) as exc:
            raise DiagnoserError("read-only batch validation failed") from exc
        if isinstance(balance, bool) or not isinstance(balance, int) or balance < 0:
            raise DiagnoserError("read-only batch validation failed")
        batch_id = _validate_batch_id((now or (lambda: datetime.now(timezone.utc)))().strftime("%Y%m%dT%H%M%SZ"))
        generated = build_stage_configs(config_bytes)
        skips = stage_skip_reasons(config_bytes)
        try:
            state_fd = _open_relative_directory(root_fd, "state")
        except DiagnoserError:
            state_fd = _create_directory(root_fd, "state")
        runs_fd = _open_or_create_directory(state_fd, RUNS_DIRECTORY)
        batch_fd = _create_directory(runs_fd, batch_id)
        _write_private_at(batch_fd, "production-config.toml", config_bytes)
        _write_private_at(batch_fd, "production-tokens.toml", tokens_bytes)
        _write_private_at(batch_fd, "production-binary", binary_bytes)
        stages_fd = _create_directory(batch_fd, "stages")
        with _restore_marker_on_termination(state_fd, batch_id):
            _write_private_at(state_fd, ACTIVE_MARKER, f"{batch_id}\n".encode())
            active_created = True
            generated_by_name = {name: config for name, config, _ in generated}
            prepared = []
            for index, name in enumerate(("baseline",) + tuple(item.name for item in STAGE_MUTATIONS)):
                relative_root = _stage_relative_root(batch_id, index, name)
                contract_relative_path = relative_root + "/stage-contract.json"
                if name not in generated_by_name:
                    prepared.append(PreparedStage(index, name, relative_root, contract_relative_path, True, skips[name]))
                    continue
                stage_fd = _create_directory(stages_fd, f"{index}-{name}")
                try:
                    _prepare_stage(stage_fd, root_fd, batch_id, index, name, generated_by_name[name], mint, balance)
                finally:
                    os.close(stage_fd)
                prepared.append(PreparedStage(index, name, relative_root, contract_relative_path, False, None))
        return PreparedBatch(
            batch_id=batch_id,
            mint=mint,
            relative_root=f"state/{RUNS_DIRECTORY}/{batch_id}",
            stages=tuple(prepared),
        )
    except BaseException:
        if active_created and state_fd is not None and batch_id is not None:
            _remove_active_marker(state_fd, batch_id)
        raise
    finally:
        for descriptor in (stages_fd, batch_fd, runs_fd, state_fd, root_fd):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


def restore_batch(root: Path, batch_id: str) -> None:
    """Idempotently release the active batch marker; production was never modified."""
    _validate_batch_id(batch_id)
    root_fd = state_fd = None
    try:
        root_fd = _open_root(root)
        state_fd = _open_relative_directory(root_fd, "state")
        _remove_active_marker(state_fd, batch_id)
    finally:
        for descriptor in (state_fd, root_fd):
            if descriptor is not None:
                os.close(descriptor)


def restore_active(root: Path) -> None:
    root_fd = state_fd = None
    try:
        root_fd = _open_root(root)
        state_fd = _open_relative_directory(root_fd, "state")
        try:
            marker = _read_owned_file(state_fd, ACTIVE_MARKER, mode=0o600)
        except DiagnoserError:
            return
        try:
            marker_text = marker.decode("ascii")
        except UnicodeError as exc:
            raise DiagnoserError("active batch marker is invalid") from exc
        if not marker_text.endswith("\n"):
            raise DiagnoserError("active batch marker is invalid")
        batch_id = _validate_batch_id(marker_text[:-1])
        _remove_active_marker(state_fd, batch_id)
    finally:
        for descriptor in (state_fd, root_fd):
            if descriptor is not None:
                os.close(descriptor)


def stage_contract_path(batch_id: str, index: int, name: str) -> str:
    _validate_batch_id(batch_id)
    if not isinstance(index, int) or isinstance(index, bool) or index < 0:
        raise DiagnoserError("stage index is invalid")
    if name not in ("baseline",) + tuple(item.name for item in STAGE_MUTATIONS):
        raise DiagnoserError("stage name is invalid")
    if index != (("baseline",) + tuple(item.name for item in STAGE_MUTATIONS)).index(name):
        raise DiagnoserError("stage index is invalid")
    return _stage_relative_root(batch_id, index, name) + "/stage-contract.json"


def batch_result_path(batch_id: str) -> str:
    _validate_batch_id(batch_id)
    return f"state/{RUNS_DIRECTORY}/{batch_id}/batch-result.json"


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        del message
        raise DiagnoserError("invalid arguments")


def _safe_batch_summary(batch):
    return {
        "batch_id": batch.batch_id,
        "target_mint": batch.mint,
        "timeout_seconds": TIMEOUT_SECONDS,
        "early_stop_lamports": EARLY_STOP_LAMPORTS,
        "loss_limit_lamports": LOSS_LIMIT_LAMPORTS,
        "stages": [
            {"name": stage.name, "skipped": stage.skipped, "skip_reason": stage.skip_reason}
            for stage in batch.stages
        ],
    }


def main(argv=None):
    parser = _ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("root", type=Path)
    prepare.add_argument("mint")
    restore = commands.add_parser("restore")
    restore.add_argument("root", type=Path)
    restore.add_argument("batch_id")
    active = commands.add_parser("restore-active")
    active.add_argument("root", type=Path)
    contract = commands.add_parser("stage-contract-path")
    contract.add_argument("batch_id")
    contract.add_argument("index", type=int)
    contract.add_argument("name")
    result = commands.add_parser("batch-result-path")
    result.add_argument("batch_id")
    args = parser.parse_args(argv)
    if args.command == "prepare":
        print(json.dumps(_safe_batch_summary(prepare_batch(args.root, args.mint)), sort_keys=True))
    elif args.command == "restore":
        restore_batch(args.root, args.batch_id)
    elif args.command == "restore-active":
        restore_active(args.root)
    elif args.command == "stage-contract-path":
        print(stage_contract_path(args.batch_id, args.index, args.name))
    else:
        print(batch_result_path(args.batch_id))


if __name__ == "__main__":
    try:
        main()
    except _PreparationInterrupted as exc:
        raise SystemExit(128 + exc.signum) from exc
    except DiagnoserError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc
