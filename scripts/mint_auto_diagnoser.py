#!/usr/bin/env python3
"""Private, immutable configuration staging for the auto-filter diagnoser."""

import argparse
import codecs
import copy
import fcntl
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
    from scripts import mint_runner, zavod_guard
except ModuleNotFoundError:
    import mint_runner
    import zavod_guard


BATCH_ID_PATTERN = re.compile(r"[0-9]{8}T[0-9]{6}Z", re.ASCII)
EARLY_STOP_LAMPORTS = 25_000_000
LOSS_LIMIT_LAMPORTS = 30_000_000
TIMEOUT_SECONDS = 300
BINARY_NAME = "zavod-mev-bot-rust-version-cli"
ACTIVE_MARKER = ".mint-auto-diagnose-active"
STATE_MARKER_LOCK = ".mint-auto-diagnose.lock"
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
STAGE_NAMES = ("baseline",) + tuple(item.name for item in STAGE_MUTATIONS)
TERMINAL_BATCH_STATES = frozenset(
    {"target_positive", "exhausted", "failed", "declined"}
)
STATE_KEYS = frozenset(
    {
        "schema",
        "status",
        "completed_stages",
        "cumulative_observed_loss_lamports",
        "next_stage",
    }
)
ROUTING_KEYS = frozenset({"routes"})
ROUTE_KEYS = frozenset({"target_mint", "pool_ids"})
STAGE_MANIFEST_KEYS = frozenset(
    {
        "stage_name",
        "stage_status",
        "stop_reason",
        "duration_seconds",
        "guard_exit",
        "selector_histogram",
        "target_artifact_count",
        "target_pool_count",
        "target_lut_count",
        "target_runtime_count",
        "route_status",
        "three_hop_status",
        "sender_acceptance_count",
        "sender_rejection_count",
        "target_filtered_landed",
        "target_filtered_successful",
        "target_filtered_failed",
        "fees_lamports",
        "rent_lamports",
        "transfers_lamports",
        "sol_delta_lamports",
        "wsol_delta_raw",
        "cumulative_observed_loss_lamports",
        "next_decision",
    }
)
BATCH_RESULT_KEYS = frozenset(
    {
        "status",
        "completed_stages",
        "cumulative_observed_loss_lamports",
    }
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


def _acquire_state_marker_lock(state_fd):
    """Lock marker state; batch lock must be acquired first when both are held."""
    descriptor = None
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(
            STATE_MARKER_LOCK,
            flags,
            0o600,
            dir_fd=state_fd,
        )
        _validate_owned(descriptor, "file", mode=0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        return descriptor
    except (OSError, DiagnoserError) as exc:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise DiagnoserError("active batch marker lock is invalid") from exc


def _remove_active_marker(
    state_fd,
    batch_id,
    *,
    strict=False,
    batch_fd=None,
    result_fd=None,
):
    if strict:
        _remove_finalized_active_marker(
            state_fd,
            batch_id,
            batch_fd=batch_fd,
            result_fd=result_fd,
        )
        return
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
    if threading.current_thread() is not threading.main_thread():
        raise DiagnoserError("batch preparation must run on the main thread")
    root_fd = state_fd = state_lock_fd = None
    runs_fd = batch_fd = stages_fd = None
    batch_id = None
    active_created = False
    try:
        root_fd = _open_root(root)
        config_bytes = _read_owned_file(root_fd, "config.toml", mode=0o600)
        tokens_bytes = _read_owned_file(root_fd, "tokens.toml", mode=0o600)
        binary_bytes = _read_owned_file(root_fd, BINARY_NAME, executable=True)
        try:
            runtime_config = zavod_guard.load_config_bytes(config_bytes)
        except zavod_guard.GuardError as exc:
            raise DiagnoserError("production configuration is invalid") from exc
        rpc_url = _nested_value(runtime_config, ("rpc",), "url")
        private_key = _nested_value(runtime_config, ("wallet",), "private_key")
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
        state_lock_fd = _acquire_state_marker_lock(state_fd)
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
        for descriptor in (
            stages_fd,
            batch_fd,
            runs_fd,
            state_lock_fd,
            state_fd,
            root_fd,
        ):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


def _batch_descriptors(root, batch_id, include_results=False):
    descriptors = {
        "root": None,
        "state": None,
        "runs": None,
        "batch": None,
        "stages": None,
        "results": None,
        "lock": None,
        "state_lock": None,
    }
    try:
        descriptors["root"] = _open_root(root)
        descriptors["state"] = _open_relative_directory(
            descriptors["root"], "state"
        )
        descriptors["runs"] = _open_relative_directory(
            descriptors["state"], RUNS_DIRECTORY, mode=0o700
        )
        descriptors["batch"] = _open_relative_directory(
            descriptors["runs"], batch_id, mode=0o700
        )
        descriptors["stages"] = _open_relative_directory(
            descriptors["batch"], "stages", mode=0o700
        )
        if include_results:
            descriptors["results"] = _open_or_create_directory(
                descriptors["batch"], "results", mode=0o700
            )
        return descriptors
    except BaseException:
        _close_descriptors(descriptors)
        raise


def _close_descriptors(descriptors):
    for name in (
        "state_lock",
        "lock",
        "results",
        "stages",
        "batch",
        "runs",
        "state",
        "root",
    ):
        descriptor = descriptors.get(name)
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass



def _validate_stage_contract(stage_fd, batch_id, stage_name):
    index = STAGE_NAMES.index(stage_name)
    try:
        contract = zavod_guard._load_auto_filter_contract(
            _read_owned_file(
                stage_fd, "stage-contract.json", mode=0o600
            )
        )
        zavod_guard._validate_auto_filter_contract_fields(
            contract,
            _stage_relative_root(batch_id, index, stage_name),
        )
        inputs = {
            "config_sha256": _read_owned_file(
                stage_fd, "config.toml", mode=0o600
            ),
            "tokens_sha256": _read_owned_file(
                stage_fd, "tokens.toml", mode=0o600
            ),
            "binary_sha256": _read_owned_file(
                stage_fd, BINARY_NAME, executable=True
            ),
        }
        for digest_name, data in inputs.items():
            zavod_guard._auto_filter_require_digest(
                data, contract[digest_name]
            )
        if inputs["tokens_sha256"] != (
            f'tokens = ["{contract["target_mint"]}"]\n'.encode()
        ):
            raise zavod_guard.GuardError("stage input is invalid")
        config = zavod_guard.load_config_bytes(inputs["config_sha256"])
        if _nested_value(config, ("auto",), "enable_three_hop") is not True:
            raise zavod_guard.GuardError("stage input is invalid")
    except (DiagnoserError, zavod_guard.GuardError) as exc:
        raise DiagnoserError("stage contract is invalid") from exc
    return contract


def _validate_window(started_at, ended_at):
    if (
        isinstance(started_at, bool)
        or isinstance(ended_at, bool)
        or not isinstance(started_at, int)
        or not isinstance(ended_at, int)
        or started_at <= 0
        or ended_at < started_at
    ):
        raise DiagnoserError("stage window is invalid")


def _parse_stage_guard(stage_fd, guard_exit):
    if (
        isinstance(guard_exit, bool)
        or not isinstance(guard_exit, int)
        or not -255 <= guard_exit <= 255
    ):
        raise DiagnoserError("guard result is invalid")
    try:
        guard = mint_runner._parse_guard_result_bytes(
            _read_owned_file(stage_fd, "guard-result.txt", mode=0o600)
        )
    except (mint_runner.RunnerError, ValueError) as exc:
        raise DiagnoserError("guard result is invalid") from exc
    required = {
        "reason",
        "duration_seconds",
        "child_exit_code",
        "loss_limit_lamports",
        "early_stop_lamports",
        "log_path",
    }
    if set(guard) != required or guard["reason"] not in mint_runner.STOP_REASONS:
        raise DiagnoserError("guard result is invalid")
    integers = {}
    for name in (
        "loss_limit_lamports",
        "early_stop_lamports",
    ):
        try:
            integers[name] = int(guard[name])
        except (TypeError, ValueError) as exc:
            raise DiagnoserError("guard result is invalid") from exc
        if integers[name] < 0:
            raise DiagnoserError("guard result is invalid")
    child_exit = guard["child_exit_code"]
    if child_exit == "None":
        integers["child_exit_code"] = None
    else:
        try:
            integers["child_exit_code"] = int(child_exit)
        except (TypeError, ValueError) as exc:
            raise DiagnoserError("guard result is invalid") from exc
        if not -255 <= integers["child_exit_code"] <= 255:
            raise DiagnoserError("guard result is invalid")
    if (
        guard["reason"] == "child_exit"
        and integers["child_exit_code"] is None
    ):
        raise DiagnoserError("guard result is invalid")
    if (
        integers["loss_limit_lamports"] != LOSS_LIMIT_LAMPORTS
        or integers["early_stop_lamports"] != EARLY_STOP_LAMPORTS
    ):
        raise DiagnoserError("guard result is invalid")
    try:
        duration = float(guard["duration_seconds"])
    except (TypeError, ValueError) as exc:
        raise DiagnoserError("guard result is invalid") from exc
    if not math.isfinite(duration) or duration < 0:
        raise DiagnoserError("guard result is invalid")
    return guard, integers, duration


def _stage_log_snapshot(stage_fd, log_value):
    if not isinstance(log_value, str):
        raise DiagnoserError("stage log is invalid")
    relative = Path(log_value)
    if (
        relative.is_absolute()
        or len(relative.parts) != 2
        or relative.parts[0] != "logs"
        or any(part in ("", ".", "..") for part in relative.parts)
    ):
        raise DiagnoserError("stage log is invalid")
    logs_fd = _open_relative_directory(stage_fd, "logs", mode=0o700)
    descriptor = None
    try:
        descriptor = os.open(
            relative.parts[1],
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=logs_fd,
        )
        before = _validate_owned(descriptor, "file", mode=0o600)
        if before.st_size > mint_runner.SELECTOR_LOG_MAX_BYTES:
            raise DiagnoserError("stage log is invalid")
        snapshot = bytearray()
        while len(snapshot) <= mint_runner.SELECTOR_LOG_MAX_BYTES:
            chunk = os.read(
                descriptor,
                min(
                    mint_runner.SELECTOR_LOG_READ_SIZE,
                    mint_runner.SELECTOR_LOG_MAX_BYTES
                    + 1
                    - len(snapshot),
                ),
            )
            if not chunk:
                break
            snapshot.extend(chunk)
        if len(snapshot) > mint_runner.SELECTOR_LOG_MAX_BYTES:
            raise DiagnoserError("stage log is invalid")
        after = os.fstat(descriptor)
        named = os.stat(
            relative.parts[1],
            dir_fd=logs_fd,
            follow_symlinks=False,
        )
        stable_fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_uid",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if (
            len(snapshot) != after.st_size
            or any(
                getattr(before, field) != getattr(after, field)
                for field in stable_fields
            )
            or any(
                getattr(after, field) != getattr(named, field)
                for field in stable_fields
            )
            or not stat.S_ISREG(named.st_mode)
            or named.st_uid != os.geteuid()
            or stat.S_IMODE(named.st_mode) != 0o600
        ):
            raise DiagnoserError("stage log is invalid")
        return bytes(snapshot)
    except (OSError, DiagnoserError) as exc:
        raise DiagnoserError("stage log is invalid") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(logs_fd)


def _iter_stage_log_lines(snapshot):
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    buffer = ""
    discarding_line = False
    offset = 0
    while True:
        chunk = snapshot[
            offset : offset + mint_runner.SELECTOR_LOG_READ_SIZE
        ]
        offset += len(chunk)
        physical_eof = not chunk
        text = decoder.decode(chunk, final=physical_eof)
        while text:
            if discarding_line:
                newline = text.find("\n")
                if newline < 0:
                    text = ""
                    continue
                text = text[newline + 1 :]
                discarding_line = False
                continue
            newline = text.find("\n")
            if newline < 0:
                if (
                    len(buffer) + len(text)
                    > mint_runner.SELECTOR_LOG_MAX_LINE_CHARS
                ):
                    buffer = ""
                    discarding_line = True
                else:
                    buffer += text
                text = ""
                continue
            segment = text[:newline]
            text = text[newline + 1 :]
            if (
                len(buffer) + len(segment)
                <= mint_runner.SELECTOR_LOG_MAX_LINE_CHARS
            ):
                yield (buffer + segment).removesuffix("\r")
            buffer = ""
        if physical_eof:
            if buffer and not discarding_line:
                yield buffer.removesuffix("\r")
            break


def _aggregate_stage_log(snapshot):
    counts = {name: 0 for name in mint_runner.LOG_PATTERNS}
    for line in _iter_stage_log_lines(snapshot):
        for name, pattern in mint_runner.LOG_PATTERNS.items():
            counts[name] += len(pattern.findall(line))
    return counts


def _selector_stage_log_summary(
    snapshot,
    target_mint,
    artifacts,
    guard_reason,
):
    summary = mint_runner._selector_diagnostic_summary(
        None,
        target_mint,
        artifacts,
        guard_reason,
    )
    refresh_count = 0
    zero_refresh_count = 0
    histogram_counts = {}
    selected_count_min = None
    selected_count_max = None
    construction_observed = False
    dispatch_marker_observed = False
    for line in _iter_stage_log_lines(snapshot):
        if line == mint_runner.SELECTOR_CONSTRUCTION_MARKER:
            construction_observed = True
        if line == mint_runner.SELECTOR_DISPATCH_VIOLATION_MARKER:
            dispatch_marker_observed = True
        match = mint_runner.SELECTOR_REFRESH_PATTERN.fullmatch(line)
        if (
            match is not None
            and refresh_count < mint_runner.SELECTOR_MAX_OBSERVATIONS
        ):
            count = int(match.group(1))
            refresh_count += 1
            if count == 0:
                zero_refresh_count += 1
            histogram_counts[count] = (
                histogram_counts.get(count, 0) + 1
            )
            if (
                selected_count_min is None
                or count < selected_count_min
            ):
                selected_count_min = count
            if (
                selected_count_max is None
                or count > selected_count_max
            ):
                selected_count_max = count
            if refresh_count >= mint_runner.SELECTOR_MAX_OBSERVATIONS:
                break
    summary.update(
        {
            "refresh_count": refresh_count,
            "zero_refresh_count": zero_refresh_count,
            "selected_count_histogram": {
                str(count): occurrences
                for count, occurrences in histogram_counts.items()
            },
            "selected_count_min": (
                selected_count_min
                if selected_count_min is not None
                else mint_runner.SELECTOR_UNAVAILABLE
            ),
            "selected_count_max": (
                selected_count_max
                if selected_count_max is not None
                else mint_runner.SELECTOR_UNAVAILABLE
            ),
            "candidate_construction": (
                "observed_in_log"
                if construction_observed
                else "not_observed_in_log"
            ),
            "dispatch": (
                "test_mode_dispatch_violation"
                if (
                    dispatch_marker_observed
                    or summary["dispatch"]
                    == "test_mode_dispatch_violation"
                )
                else "not_applicable"
            ),
        }
    )
    return summary


def _read_stage_artifact(stage_fd, name, started_at, ended_at, policy):
    maximum_bytes = 1024 * 1024
    descriptor = None
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=stage_fd,
        )
    except FileNotFoundError:
        return "missing", None, None
    except OSError:
        return "artifact_error", None, None
    try:
        before = os.fstat(descriptor)
        artifact_second = before.st_mtime_ns // 1_000_000_000
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_size > maximum_bytes
            or not started_at <= artifact_second <= ended_at
        ):
            return "artifact_error", None, None
        snapshot = bytearray()
        while len(snapshot) <= maximum_bytes:
            chunk = os.read(
                descriptor,
                min(64 * 1024, maximum_bytes + 1 - len(snapshot)),
            )
            if not chunk:
                break
            snapshot.extend(chunk)
        after = os.fstat(descriptor)
        named = os.stat(name, dir_fd=stage_fd, follow_symlinks=False)
        stable_fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_uid",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if (
            len(snapshot) > maximum_bytes
            or len(snapshot) != after.st_size
            or any(
                getattr(before, field) != getattr(after, field)
                for field in stable_fields
            )
            or any(
                getattr(after, field) != getattr(named, field)
                for field in stable_fields
            )
        ):
            return "artifact_error", None, None
        data = bytes(snapshot)
        sanitized = mint_runner._sanitize_generated_artifact(data, policy)
        parsed = json.loads(sanitized)
    except (
        mint_runner.RunnerError,
        mint_runner.GeneratedArtifactContentError,
        OSError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ):
        return "artifact_error", None, None
    finally:
        os.close(descriptor)
    return "captured", parsed, sanitized


def _routing_summary(routing, target):
    if not isinstance(routing, dict) or set(routing) != ROUTING_KEYS:
        return None
    routes = routing["routes"]
    if not isinstance(routes, list) or len(routes) > 100_000:
        return None
    target_routes = 0
    three_hop_routes = 0
    observed = set()
    for route in routes:
        if not isinstance(route, dict) or set(route) != ROUTE_KEYS:
            return None
        route_target = route["target_mint"]
        pool_ids = route["pool_ids"]
        if (
            not isinstance(route_target, str)
            or not route_target
            or not isinstance(pool_ids, list)
            or not 1 <= len(pool_ids) <= 3
            or any(not isinstance(pool, str) or not pool for pool in pool_ids)
            or len(set(pool_ids)) != len(pool_ids)
        ):
            return None
        identity = (route_target, tuple(pool_ids))
        if identity in observed:
            return None
        observed.add(identity)
        if route_target == target:
            target_routes += 1
            if len(pool_ids) == 3:
                three_hop_routes += 1
    return target_routes, three_hop_routes


def _artifact_evidence(
    stage_fd,
    target,
    started_at,
    ended_at,
    policy,
):
    artifacts = {}
    sanitized = {}
    for name in mint_runner.OPTIONAL_FILES:
        status, value, rendered = _read_stage_artifact(
            stage_fd, name, started_at, ended_at, policy
        )
        if status == "artifact_error":
            return None
        if status == "captured":
            artifacts[name] = value
            sanitized[name] = rendered
    hot_entry = None
    if "hot_tokens.json" in artifacts:
        valid_hot, hot_entry = mint_runner._selector_artifact_entry(
            artifacts["hot_tokens.json"], target
        )
        if not valid_hot:
            return None
    routing_target_count = 0
    three_hop_count = 0
    if "routing.json" in artifacts:
        routing = _routing_summary(artifacts["routing.json"], target)
        if routing is None:
            return None
        routing_target_count, three_hop_count = routing
    return {
        "artifacts": artifacts,
        "sanitized": sanitized,
        "routing_target_count": routing_target_count,
        "three_hop_count": three_hop_count,
    }



def _empty_stage_chain():
    return mint_runner._empty_chain_summary()


def _stage_evaluation(
    stage_name,
    decision,
    stop_reason,
    target_status="unproven",
    three_hop_status="unproven",
    sender_accepted=0,
    sender_rejected=0,
    target_landed=0,
    cumulative_loss_lamports=0,
):
    return {
        "stage_name": stage_name,
        "decision": decision,
        "stop_reason": stop_reason,
        "target_status": target_status,
        "three_hop_status": three_hop_status,
        "sender_accepted": sender_accepted,
        "sender_rejected": sender_rejected,
        "target_landed": target_landed,
        "cumulative_loss_lamports": cumulative_loss_lamports,
    }


def evaluate_stage(
    root: Path,
    batch_id: str,
    stage_name: str,
    guard_exit: int,
    started_at: int,
    ended_at: int,
    transport=None,
) -> dict:
    """Evaluate one completed stage without storing execution state."""
    batch_id = _validate_batch_id(batch_id)
    if stage_name not in STAGE_NAMES:
        raise DiagnoserError("stage name is invalid")
    _validate_window(started_at, ended_at)
    descriptors = _batch_descriptors(root, batch_id)
    stage_fd = None
    try:
        index = STAGE_NAMES.index(stage_name)
        stage_fd = _open_relative_directory(
            descriptors["stages"],
            f"{index}-{stage_name}",
            mode=0o700,
        )
        contract = _validate_stage_contract(stage_fd, batch_id, stage_name)
        guard, guard_integers, _ = _parse_stage_guard(stage_fd, guard_exit)
        log_snapshot = _stage_log_snapshot(stage_fd, guard["log_path"])
        log_events = _aggregate_stage_log(log_snapshot)
        config_bytes = _read_owned_file(stage_fd, "config.toml", mode=0o600)
        try:
            config = zavod_guard.load_config_bytes(config_bytes)
            wallet = zavod_guard.wallet_pubkey(
                config["wallet"]["private_key"]
            )
        except (KeyError, TypeError, zavod_guard.GuardError) as exc:
            raise DiagnoserError("stage configuration is invalid") from exc
        base_policy = zavod_guard.ProtectedOutputPolicy.from_config(config)
        policy = zavod_guard.ProtectedOutputPolicy(
            base_policy.secrets + (wallet,)
        )
        evidence = _artifact_evidence(
            stage_fd,
            contract["target_mint"],
            started_at,
            ended_at,
            policy,
        )
        if evidence is None:
            return _stage_evaluation(
                stage_name,
                "failed",
                "artifact_error",
                sender_accepted=log_events["sent_events"],
                sender_rejected=log_events["error_events"],
            )
        try:
            current_balance = zavod_guard.get_balance_lamports(
                config["rpc"]["url"],
                wallet,
                transport=transport,
            )
            chain = mint_runner._sanitize_chain_summary(
                mint_runner.aggregate_chain(
                    config,
                    contract["target_mint"],
                    started_at,
                    ended_at,
                    transport=transport,
                    pubkey_resolver=lambda ignored: wallet,
                )
            )
        except Exception:
            return _stage_evaluation(
                stage_name,
                "failed",
                "rpc_error",
                sender_accepted=log_events["sent_events"],
                sender_rejected=log_events["error_events"],
            )
        cumulative_loss = max(
            0,
            contract["batch_start_balance_lamports"] - current_balance,
        )
        selector = _selector_stage_log_summary(
            log_snapshot,
            contract["target_mint"],
            evidence["artifacts"],
            guard["reason"],
        )
        target_positive = (
            selector["target_artifact_present"]
            or evidence["routing_target_count"] > 0
            or chain["landed"] > 0
        )
        if guard_exit != 0:
            decision, stop_reason = "failed", "guard_exit"
        elif guard["reason"] == "loss_threshold":
            decision, stop_reason = "failed", "loss_threshold"
        elif cumulative_loss >= EARLY_STOP_LAMPORTS:
            decision, stop_reason = "failed", "loss_threshold"
        elif (
            guard["reason"] not in {"timeout", "child_exit"}
            or (
                guard["reason"] == "child_exit"
                and guard_integers["child_exit_code"] not in (None, 0)
            )
        ):
            decision, stop_reason = "failed", guard["reason"]
        elif target_positive:
            decision, stop_reason = "target_positive", guard["reason"]
        else:
            decision, stop_reason = "continue", guard["reason"]
        return _stage_evaluation(
            stage_name,
            decision,
            stop_reason,
            target_status=("positive" if target_positive else "absent"),
            three_hop_status=(
                "observed"
                if evidence["three_hop_count"] > 0
                else "unproven"
            ),
            sender_accepted=log_events["sent_events"],
            sender_rejected=log_events["error_events"],
            target_landed=chain["landed"],
            cumulative_loss_lamports=cumulative_loss,
        )
    except DiagnoserError:
        return _stage_evaluation(stage_name, "failed", "evaluation_error")
    finally:
        if stage_fd is not None:
            os.close(stage_fd)
        _close_descriptors(descriptors)



def restore_batch(root: Path, batch_id: str) -> None:
    """Idempotently release the active batch marker; production was never modified."""
    _validate_batch_id(batch_id)
    root_fd = state_fd = state_lock_fd = None
    try:
        root_fd = _open_root(root)
        state_fd = _open_relative_directory(root_fd, "state")
        state_lock_fd = _acquire_state_marker_lock(state_fd)
        _remove_active_marker(state_fd, batch_id)
    finally:
        for descriptor in (state_lock_fd, state_fd, root_fd):
            if descriptor is not None:
                os.close(descriptor)


def restore_active(root: Path) -> None:
    root_fd = state_fd = state_lock_fd = None
    try:
        root_fd = _open_root(root)
        state_fd = _open_relative_directory(root_fd, "state")
        state_lock_fd = _acquire_state_marker_lock(state_fd)
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
        for descriptor in (state_lock_fd, state_fd, root_fd):
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
    evaluate = commands.add_parser("evaluate-stage")
    evaluate.add_argument("root", type=Path)
    evaluate.add_argument("batch_id")
    evaluate.add_argument("stage_name")
    evaluate.add_argument("--guard-exit", required=True, type=int)
    evaluate.add_argument("--started-at", required=True, type=int)
    evaluate.add_argument("--ended-at", required=True, type=int)
    args = parser.parse_args(argv)
    if args.command == "prepare":
        print(json.dumps(_safe_batch_summary(prepare_batch(args.root, args.mint)), sort_keys=True))
    elif args.command == "restore":
        restore_batch(args.root, args.batch_id)
    elif args.command == "restore-active":
        restore_active(args.root)
    elif args.command == "stage-contract-path":
        print(stage_contract_path(args.batch_id, args.index, args.name))
    elif args.command == "batch-result-path":
        print(batch_result_path(args.batch_id))
    elif args.command == "evaluate-stage":
        print(
            json.dumps(
                evaluate_stage(
                    args.root,
                    args.batch_id,
                    args.stage_name,
                    args.guard_exit,
                    args.started_at,
                    args.ended_at,
                ),
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    try:
        main()
    except _PreparationInterrupted as exc:
        raise SystemExit(128 + exc.signum) from exc
    except DiagnoserError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc
