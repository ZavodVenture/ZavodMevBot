# Mint Auto Runner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `./scripts/mint-run.sh <MINT> [--timeout SECONDS]`, which safely restricts ZavodMevBot auto mode to one Solana mint, requests immediate live confirmation, runs one guarded window, restores the workspace, and records a secret-safe result.

**Architecture:** Restore the audited guard as a baseline, harden its streaming redaction and process-group cleanup, and add a Python state manager for mint validation, snapshots, restoration, and finalized run aggregation. A small Bash entrypoint coordinates preparation, the exact confirmation phrase, one call to `run-guarded.sh`, and idempotent cleanup.

**Tech Stack:** Bash 5, Python 3.12 standard library, `unittest`, Solana JSON-RPC, existing ZavodMevBot `0.2.2` binary.

## Global Constraints

- Never print or persist values from `config.toml`, wallet material, authenticated RPC URLs, API keys, transaction signatures, or UUIDs.
- Never execute the binary's `run` subcommand directly; live execution must go through `./scripts/run-guarded.sh`.
- Every live invocation requires the exact phrase `RUN <MINT> FOR <SECONDS>` immediately before execution.
- Accept one target mint and a timeout from 30 through 300 seconds; default to 300 seconds.
- Keep `[auto].enabled = true`; do not create or activate `markets.toml`.
- Let ZavodMevBot auto mode discover pools, routes, and LUTs at runtime.
- Do not create a new on-chain LUT and do not retry live execution automatically.
- Preserve the `0.025 SOL` early-stop threshold and `0.03 SOL` loss target.
- Create mode-600 recovery copies before local edits and restore original files byte-for-byte on every exit path.
- Keep `state/CURRENT.md` and `state/EXPERIMENTS.md` current after preparation failures and material runs.
- All automated tests use fake processes and mock RPC transports; they must create no transactions.

---

### Task 1: Restore the audited guard baseline and add a bounded timeout interface

**Files:**
- Create mechanically from archive: `scripts/__init__.py`
- Create mechanically from archive: `scripts/preflight.sh`
- Create mechanically from archive: `scripts/run-guarded.sh`
- Create mechanically from archive: `scripts/zavod_guard.py`
- Create mechanically from archive: `tests/__init__.py`
- Create mechanically from archive: `tests/test_zavod_guard.py`
- Modify: `scripts/run-guarded.sh`
- Modify: `tests/test_zavod_guard.py`

**Interfaces:**
- Consumes: `/home/diablo/zavod-archive-20260724T171941Z.tar.gz`.
- Produces: `./scripts/run-guarded.sh --live-confirmed [--timeout SECONDS]` with a validated 30–300 second timeout.
- Produces: `python3 scripts/zavod_guard.py preflight --config config.toml`.

- [ ] **Step 1: Restore only the audited baseline files**

Run:

```bash
restore_dir=$(mktemp -d /tmp/zavod-guard-restore.XXXXXX)
tar -xzf /home/diablo/zavod-archive-20260724T171941Z.tar.gz \
  -C "$restore_dir" \
  zavod/scripts/__init__.py \
  zavod/scripts/preflight.sh \
  zavod/scripts/run-guarded.sh \
  zavod/scripts/zavod_guard.py \
  zavod/tests/__init__.py \
  zavod/tests/test_zavod_guard.py
install -d -m 755 scripts tests
install -m 644 "$restore_dir/zavod/scripts/__init__.py" scripts/__init__.py
install -m 755 "$restore_dir/zavod/scripts/preflight.sh" scripts/preflight.sh
install -m 755 "$restore_dir/zavod/scripts/run-guarded.sh" scripts/run-guarded.sh
install -m 755 "$restore_dir/zavod/scripts/zavod_guard.py" scripts/zavod_guard.py
install -m 644 "$restore_dir/zavod/tests/__init__.py" tests/__init__.py
install -m 644 "$restore_dir/zavod/tests/test_zavod_guard.py" tests/test_zavod_guard.py
gio trash "$restore_dir"
```

Expected: only the six named files are restored; `config.toml`, `tokens.toml`, and the bot binary are unchanged.

- [ ] **Step 2: Run the restored baseline tests**

Run:

```bash
python3 -m unittest -v tests.test_zavod_guard
```

Expected: `Ran 23 tests` and `OK`.

- [ ] **Step 3: Add failing wrapper argument tests**

Append these tests to `tests/test_zavod_guard.py`:

```python
class RunGuardedWrapperTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "scripts").mkdir()
        source = Path(__file__).resolve().parents[1] / "scripts" / "run-guarded.sh"
        shutil.copy2(source, self.root / "scripts" / "run-guarded.sh")
        fake = self.root / "scripts" / "zavod_guard.py"
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            "print(json.dumps(sys.argv[1:]))\n"
        )
        fake.chmod(0o755)

    def tearDown(self):
        shutil.rmtree(self.root)

    def invoke(self, *args):
        return subprocess.run(
            ["bash", "scripts/run-guarded.sh", *args],
            cwd=self.root,
            text=True,
            capture_output=True,
        )

    def test_defaults_to_300_seconds(self):
        result = self.invoke("--live-confirmed")
        self.assertEqual(result.returncode, 0)
        self.assertIn('"--timeout-seconds", "300"', result.stdout)

    def test_accepts_bounded_timeout(self):
        result = self.invoke("--live-confirmed", "--timeout", "60")
        self.assertEqual(result.returncode, 0)
        self.assertIn('"--timeout-seconds", "60"', result.stdout)

    def test_rejects_timeout_outside_bounds(self):
        for value in ("29", "301", "invalid"):
            with self.subTest(value=value):
                result = self.invoke("--live-confirmed", "--timeout", value)
                self.assertEqual(result.returncode, 64)

    def test_rejects_missing_confirmation_and_extra_arguments(self):
        self.assertEqual(self.invoke().returncode, 64)
        self.assertEqual(
            self.invoke("--live-confirmed", "--timeout", "60", "extra").returncode,
            64,
        )
```

- [ ] **Step 4: Run the wrapper tests to verify RED**

Run:

```bash
python3 -m unittest -v tests.test_zavod_guard.RunGuardedWrapperTests
```

Expected: failures because the restored wrapper treats `--timeout` as a profile name.

- [ ] **Step 5: Replace `scripts/run-guarded.sh` with the bounded parser**

Use this complete content:

```bash
#!/usr/bin/env bash
set -euo pipefail
umask 077
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."

if [[ "${1:-}" != "--live-confirmed" ]]; then
  echo 'Refusing live run: explicit confirmation is required.' >&2
  exit 64
fi
shift

timeout_seconds=300
if [[ "${1:-}" == "--timeout" ]]; then
  [[ $# -eq 2 ]] || {
    echo 'Usage: run-guarded.sh --live-confirmed [--timeout 30..300]' >&2
    exit 64
  }
  timeout_seconds="$2"
  shift 2
fi
[[ $# -eq 0 ]] || {
  echo 'Usage: run-guarded.sh --live-confirmed [--timeout 30..300]' >&2
  exit 64
}
[[ "$timeout_seconds" =~ ^[0-9]+$ ]] || {
  echo 'Timeout must be an integer from 30 through 300.' >&2
  exit 64
}
(( timeout_seconds >= 30 && timeout_seconds <= 300 )) || {
  echo 'Timeout must be an integer from 30 through 300.' >&2
  exit 64
}

exec python3 scripts/zavod_guard.py run \
  --live-confirmed \
  --config config.toml \
  --timeout-seconds "$timeout_seconds" \
  --profile default
```

- [ ] **Step 6: Run baseline and wrapper tests**

Run:

```bash
bash -n scripts/run-guarded.sh scripts/preflight.sh
python3 -m unittest -v tests.test_zavod_guard
```

Expected: all 27 tests pass.

- [ ] **Step 7: Commit Task 1**

Run:

```bash
git add scripts/__init__.py scripts/preflight.sh scripts/run-guarded.sh \
  scripts/zavod_guard.py tests/__init__.py tests/test_zavod_guard.py
git -c user.name=Codex -c user.email=codex@local commit \
  -m "test: restore guarded Zavod runner"
```

Expected: one commit containing only guard baseline and timeout-interface files.

---

### Task 2: Harden streaming redaction and process-group cleanup without a WSOL stop

**Files:**
- Modify: `scripts/zavod_guard.py`
- Modify: `tests/test_zavod_guard.py`

**Interfaces:**
- Consumes: `run_guarded(config_path: str, timeout_seconds: int, profile: str) -> dict`.
- Produces: `StreamingRedactor.feed(text: str) -> None` and `StreamingRedactor.close() -> None`.
- Produces: `_shutdown_child(child, ...) -> dict` with `exit_code`, `group_absent`, and `interrupted`.
- Produces: `supervise(...) -> dict` with stop reasons `child_exit`, `timeout`, `rpc_error`, `loss_threshold`, `operator_signal`, `output_error`, or `cleanup_failed`.

- [ ] **Step 1: Add failing cross-chunk redaction and cleanup tests**

Add:

```python
class StreamingRedactorTests(unittest.TestCase):
    def test_secret_split_across_chunks_is_never_written(self):
        sink = io.StringIO()
        redactor = zavod_guard.StreamingRedactor(sink, ["secret-value"])
        redactor.feed("before secret-")
        redactor.feed("value after")
        redactor.close()
        self.assertEqual(sink.getvalue(), "before <redacted> after")


class HardenedCleanupTests(unittest.TestCase):
    def test_keyboard_interrupt_from_poll_still_cleans_group(self):
        child = Mock()
        child.pid = 123
        child.returncode = -2
        child.poll.side_effect = [KeyboardInterrupt(), None, -2]
        signals = []
        result = zavod_guard.supervise(
            child=child,
            start_balance=100,
            balance_reader=lambda: 100,
            monotonic=Mock(side_effect=[0, 0]),
            sleep=lambda _: None,
            killpg=lambda pgid, sig: signals.append((pgid, sig)),
            group_exists=Mock(side_effect=[True, False]),
            signal_grace=((signal.SIGINT, 0),),
            timeout_seconds=300,
        )
        self.assertEqual(result["reason"], "operator_signal")
        self.assertIn((123, signal.SIGINT), signals)

    def test_surviving_descendant_reports_cleanup_failed(self):
        child = Mock(pid=123, returncode=None)
        child.poll.return_value = None
        result = zavod_guard._shutdown_child(
            child,
            killpg=lambda pgid, sig: None,
            group_exists=lambda pgid: True,
            monotonic=Mock(side_effect=[0, 1, 2, 3, 4, 5, 6]),
            sleep=lambda _: None,
            signal_grace=((signal.SIGINT, 0), (signal.SIGKILL, 0)),
        )
        self.assertFalse(result["group_absent"])

    def test_output_error_stops_fail_closed(self):
        event = threading.Event()
        event.set()
        child = Mock(pid=123, returncode=-2)
        child.poll.return_value = None
        result = zavod_guard.supervise(
            child=child,
            start_balance=100,
            balance_reader=lambda: 100,
            monotonic=Mock(side_effect=[0, 0]),
            sleep=lambda _: None,
            output_error_event=event,
            killpg=lambda pgid, sig: None,
            group_exists=Mock(side_effect=[True, False]),
            signal_grace=((signal.SIGINT, 0),),
        )
        self.assertEqual(result["reason"], "output_error")
```

Add imports:

```python
import io
import signal
import threading
from unittest.mock import Mock, patch
```

- [ ] **Step 2: Run the focused tests to verify RED**

Run:

```bash
python3 -m unittest -v \
  tests.test_zavod_guard.StreamingRedactorTests \
  tests.test_zavod_guard.HardenedCleanupTests
```

Expected: errors because `StreamingRedactor`, `group_exists`, `signal_grace`, and `output_error_event` do not exist.

- [ ] **Step 3: Add streaming redaction primitives**

Add `import threading` and these definitions after `redact_text`:

```python
def _redaction_secrets(config):
    values = [
        _get(config, "wallet", "private_key"),
        _get(config, "rpc", "url"),
        _get(config, "circular", "api-key"),
        _get(config, "falcon", "uuid"),
        _get(config, "jito", "uuid"),
    ]
    values.extend(_get(config, "spam", "sending_rpc_urls", []))
    return tuple(
        sorted(
            {value for value in values if isinstance(value, str) and len(value) >= 4},
            key=len,
            reverse=True,
        )
    )


class StreamingRedactor:
    def __init__(self, sink, secrets):
        self.sink = sink
        self.secrets = tuple(sorted(set(secrets), key=len, reverse=True))
        self.buffer = ""
        self.keep = max((len(secret) - 1 for secret in self.secrets), default=0)
        self.closed = False

    def _drain_one(self):
        for secret in self.secrets:
            if self.buffer.startswith(secret):
                self.sink.write("<redacted>")
                self.buffer = self.buffer[len(secret):]
                return
        self.sink.write(self.buffer[0])
        self.buffer = self.buffer[1:]

    def feed(self, text):
        if self.closed:
            raise ValueError("streaming redactor is closed")
        self.buffer += text
        while len(self.buffer) > self.keep:
            self._drain_one()
        self.sink.flush()

    def close(self):
        if self.closed:
            return
        while self.buffer:
            self._drain_one()
        self.sink.flush()
        self.closed = True


class OutputPump:
    def __init__(self, source, sink, config):
        self.source = source
        self.redactor = StreamingRedactor(sink, _redaction_secrets(config))
        self.output_error_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        try:
            while True:
                chunk = self.source.read(4096)
                if not chunk:
                    break
                if isinstance(chunk, bytes):
                    chunk = chunk.decode("utf-8", errors="replace")
                self.redactor.feed(chunk)
            self.redactor.close()
        except Exception:
            self.output_error_event.set()

    def start(self):
        self.thread.start()

    def join(self, timeout):
        self.thread.join(timeout)

    def is_alive(self):
        return self.thread.is_alive()
```

- [ ] **Step 4: Replace cleanup and supervision with verified process-group logic**

Add these constants and functions in place of the old `_shutdown_child`:

```python
DEFAULT_SIGNAL_GRACE = (
    (signal.SIGINT, 5),
    (signal.SIGTERM, 3),
    (signal.SIGKILL, 3),
)
MAX_INTERRUPT_RETRIES = 32


def _retry_keyboard_interrupt(call):
    interrupted = False
    for _ in range(MAX_INTERRUPT_RETRIES):
        try:
            return call(), interrupted
        except KeyboardInterrupt:
            interrupted = True
    raise GuardError("cleanup repeatedly interrupted")


def _process_group_exists(pgid):
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _shutdown_child(
    child,
    killpg=os.killpg,
    group_exists=None,
    monotonic=time.monotonic,
    sleep=time.sleep,
    signal_grace=DEFAULT_SIGNAL_GRACE,
):
    verifier = group_exists or _process_group_exists
    interrupted = False
    try:
        exit_code, was_interrupted = _retry_keyboard_interrupt(child.poll)
        interrupted |= was_interrupted
        exists, was_interrupted = _retry_keyboard_interrupt(
            lambda: verifier(child.pid)
        )
        interrupted |= was_interrupted
    except GuardError:
        return {
            "exit_code": None,
            "group_absent": False,
            "interrupted": True,
        }
    if not exists:
        return {
            "exit_code": exit_code,
            "group_absent": True,
            "interrupted": interrupted,
        }
    for sig, grace in signal_grace:
        try:
            _, was_interrupted = _retry_keyboard_interrupt(
                lambda sig=sig: killpg(child.pid, sig)
            )
            interrupted |= was_interrupted
        except ProcessLookupError:
            pass
        except GuardError:
            interrupted = True
            continue
        started, was_interrupted = _retry_keyboard_interrupt(monotonic)
        interrupted |= was_interrupted
        while True:
            try:
                exists, was_interrupted = _retry_keyboard_interrupt(
                    lambda: verifier(child.pid)
                )
                interrupted |= was_interrupted
                now, was_interrupted = _retry_keyboard_interrupt(monotonic)
                interrupted |= was_interrupted
            except GuardError:
                interrupted = True
                exists = True
                break
            if not exists or now - started >= grace:
                break
            try:
                _, was_interrupted = _retry_keyboard_interrupt(
                    lambda: sleep(0.05)
                )
                interrupted |= was_interrupted
            except GuardError:
                interrupted = True
                break
        try:
            polled, was_interrupted = _retry_keyboard_interrupt(child.poll)
            interrupted |= was_interrupted
        except GuardError:
            interrupted = True
            polled = None
        if polled is not None:
            exit_code = polled
        if not exists:
            break
    try:
        exists, was_interrupted = _retry_keyboard_interrupt(
            lambda: verifier(child.pid)
        )
        interrupted |= was_interrupted
    except GuardError:
        exists = True
        interrupted = True
    return {
        "exit_code": exit_code,
        "group_absent": not exists,
        "interrupted": interrupted,
    }
```

Replace `supervise` with:

```python
def supervise(
    child,
    start_balance,
    balance_reader,
    monotonic,
    sleep,
    output_error_event=None,
    killpg=os.killpg,
    group_exists=None,
    signal_grace=DEFAULT_SIGNAL_GRACE,
    timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
    cleanup_child=True,
):
    end_balance = start_balance
    reason = None
    exit_code = None
    try:
        started_at = monotonic()
        while reason is None:
            if output_error_event is not None and output_error_event.is_set():
                reason = "output_error"
                break
            polled = child.poll()
            if polled is not None:
                reason = "child_exit"
                exit_code = polled
                break
            if monotonic() - started_at >= timeout_seconds:
                reason = "timeout"
                break
            try:
                end_balance = balance_reader()
            except GuardError:
                reason = "rpc_error"
                break
            if should_stop_for_loss(start_balance, end_balance):
                reason = "loss_threshold"
                break
            sleep(1)
    except KeyboardInterrupt:
        reason = "operator_signal"
    finally:
        if cleanup_child:
            cleanup = _shutdown_child(
                child,
                killpg=killpg,
                group_exists=group_exists,
                signal_grace=signal_grace,
            )
            if cleanup["exit_code"] is not None:
                exit_code = cleanup["exit_code"]
            if not cleanup["group_absent"]:
                reason = "cleanup_failed"
            elif cleanup["interrupted"] and reason != "output_error":
                reason = "operator_signal"
    return {
        "reason": reason,
        "start_balance": start_balance,
        "end_balance": end_balance,
        "observed_loss": max(0, start_balance - end_balance),
        "child_exit_code": exit_code,
    }
```

- [ ] **Step 5: Route child output through `OutputPump` and always verify cleanup**

In `run_guarded`, replace the direct logfile `Popen` block with this structure. The
log handle stays open until the output thread has drained:

```python
    child = None
    pump = None
    result = None
    prior_sigterm = signal.getsignal(signal.SIGTERM)

    def interrupt_handler(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupt_handler)
    log_handle = os.fdopen(fd, "w", buffering=1)
    try:
        child = subprocess.Popen(
            [str(root / "zavod-mev-bot-rust-version-cli"), "run"],
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        pump = OutputPump(child.stdout, log_handle, config)
        pump.start()
        result = supervise(
            child=child,
            start_balance=start_balance,
            balance_reader=lambda: get_balance_lamports(rpc_url, public_key),
            monotonic=time.monotonic,
            sleep=time.sleep,
            output_error_event=pump.output_error_event,
            timeout_seconds=timeout_seconds,
            cleanup_child=False,
        )
    except KeyboardInterrupt:
        result = {
            "reason": "operator_signal",
            "start_balance": start_balance,
            "end_balance": start_balance,
            "observed_loss": 0,
            "child_exit_code": None,
        }
    finally:
        cleanup = (
            _shutdown_child(child)
            if child is not None
            else {"exit_code": None, "group_absent": True, "interrupted": False}
        )
        if pump is not None:
            pump.join(5)
            if pump.is_alive():
                pump.output_error_event.set()
            else:
                pump.redactor.close()
        log_handle.close()
        signal.signal(signal.SIGTERM, prior_sigterm)
        log_path.chmod(0o600)
    if result is None:
        result = {
            "reason": "output_error",
            "start_balance": start_balance,
            "end_balance": start_balance,
            "observed_loss": 0,
            "child_exit_code": cleanup["exit_code"],
        }
    if cleanup["exit_code"] is not None:
        result["child_exit_code"] = cleanup["exit_code"]
    if not cleanup["group_absent"]:
        result["reason"] = "cleanup_failed"
    elif pump is not None and (pump.output_error_event.is_set() or pump.is_alive()):
        result["reason"] = "output_error"
```

Keep the existing duration, log path, and financial-limit fields after this block. Do not copy `WsolLoopDetector`, WSOL message constants, or `wsol_ata_loop` branches from the archived enhanced guard.

- [ ] **Step 6: Run focused and complete guard tests**

Run:

```bash
python3 -m unittest -v \
  tests.test_zavod_guard.StreamingRedactorTests \
  tests.test_zavod_guard.HardenedCleanupTests
python3 -m unittest -v tests.test_zavod_guard
python3 -m py_compile scripts/zavod_guard.py
bash -n scripts/run-guarded.sh scripts/preflight.sh
```

Expected: all tests pass, compilation succeeds, and shell syntax is valid.

- [ ] **Step 7: Commit Task 2**

Run:

```bash
git add scripts/zavod_guard.py tests/test_zavod_guard.py
git -c user.name=Codex -c user.email=codex@local commit \
  -m "fix: harden guarded process cleanup"
```

---

### Task 3: Implement mint validation, workspace snapshots, and idempotent restoration

**Files:**
- Create: `scripts/mint_runner.py`
- Create: `tests/test_mint_runner.py`

**Interfaces:**
- Produces: `validate_timeout(value: str | int) -> int`.
- Produces: `decode_pubkey(value: str) -> bytes`.
- Produces: `validate_mint_account(rpc_url: str, mint: str, transport=None) -> None`.
- Produces: `prepare_run(root: Path, mint: str, timeout: int, transport=None, preflight_runner=None, now=None, process_checker=None) -> PreparedRun`.
- Produces: `restore_run(root: Path, run_id: str) -> None`.
- Produces: `restore_active(root: Path) -> None`.
- Produces CLI: `mint_runner.py prepare`, `restore`, `restore-active`, and `result-path`.

- [ ] **Step 1: Create failing validation and recovery tests**

Create `tests/test_mint_runner.py` with:

```python
import json
import os
import shutil
import stat
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

from scripts import mint_runner


TARGET_MINT = "So11111111111111111111111111111111111111112"


class MintRunnerTestCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "state" / "backups").mkdir(parents=True)
        (self.root / "state" / "mint-runs").mkdir(parents=True)
        (self.root / "state" / "CURRENT.md").write_text("# Current\n")
        (self.root / "state" / "EXPERIMENTS.md").write_text("# Experiments\n")
        (self.root / "state" / "CURRENT.md").chmod(0o600)
        (self.root / "state" / "EXPERIMENTS.md").chmod(0o600)
        (self.root / "config.toml").write_text(
            '[auto]\nenabled = true\n[rpc]\nurl = "https://secret.invalid"\n'
        )
        (self.root / "config.toml").chmod(0o600)
        (self.root / "tokens.toml").write_text('tokens = ["old"]\n')
        (self.root / "tokens.toml").chmod(0o600)
        (self.root / "zavod-mev-bot-rust-version-cli").write_text("fake")
        (self.root / "zavod-mev-bot-rust-version-cli").chmod(0o755)
        self.original_config = (self.root / "config.toml").read_bytes()
        self.original_tokens = (self.root / "tokens.toml").read_bytes()

    def tearDown(self):
        shutil.rmtree(self.root)

    @staticmethod
    def valid_transport(url, payload, timeout):
        return {
            "result": {
                "value": {
                    "executable": False,
                    "owner": mint_runner.TOKEN_PROGRAM_ID,
                    "data": {"parsed": {"type": "mint", "info": {}}},
                }
            }
        }

    def prepare(self, **overrides):
        args = {
            "root": self.root,
            "mint": TARGET_MINT,
            "timeout": 300,
            "transport": self.valid_transport,
            "preflight_runner": lambda root: {
                "preflight": "ok",
                "cli_version": "0.2.2",
                "loss_limit_lamports": 30_000_000,
                "early_stop_lamports": 25_000_000,
            },
            "now": lambda: datetime(2026, 7, 24, 18, 30, tzinfo=timezone.utc),
            "process_checker": lambda: False,
        }
        args.update(overrides)
        return mint_runner.prepare_run(**args)

    def test_timeout_is_bounded(self):
        self.assertEqual(mint_runner.validate_timeout("30"), 30)
        self.assertEqual(mint_runner.validate_timeout("300"), 300)
        for value in ("29", "301", "x"):
            with self.subTest(value=value):
                with self.assertRaises(mint_runner.RunnerError):
                    mint_runner.validate_timeout(value)

    def test_mint_must_decode_to_32_bytes(self):
        self.assertEqual(len(mint_runner.decode_pubkey(TARGET_MINT)), 32)
        for value in ("", "0", "short"):
            with self.subTest(value=value):
                with self.assertRaises(mint_runner.RunnerError):
                    mint_runner.decode_pubkey(value)

    def test_rpc_account_must_be_token_mint(self):
        mint_runner.validate_mint_account(
            "https://secret.invalid", TARGET_MINT, self.valid_transport
        )
        for value in (
            None,
            {"executable": True, "owner": mint_runner.TOKEN_PROGRAM_ID, "data": {}},
            {"executable": False, "owner": "wrong", "data": {}},
        ):
            with self.subTest(value=value):
                transport = lambda url, payload, timeout, value=value: {
                    "result": {"value": value}
                }
                with self.assertRaises(mint_runner.RunnerError):
                    mint_runner.validate_mint_account(
                        "https://secret.invalid", TARGET_MINT, transport
                    )

    def test_token_2022_mint_is_accepted(self):
        def transport(url, payload, timeout):
            value = self.valid_transport(url, payload, timeout)["result"]["value"]
            value["owner"] = mint_runner.TOKEN_2022_PROGRAM_ID
            return {"result": {"value": value}}

        mint_runner.validate_mint_account(
            "https://secret.invalid", TARGET_MINT, transport
        )

    def test_invalid_unsafe_and_disabled_configs_fail_closed(self):
        cases = (
            ("not = [valid", 0o600),
            ('[auto]\nenabled = false\n[rpc]\nurl = "x"\n', 0o600),
            ('[auto]\nenabled = true\n[rpc]\nurl = "x"\n', 0o644),
        )
        for content, mode in cases:
            with self.subTest(content=content, mode=mode):
                (self.root / "config.toml").write_text(content)
                (self.root / "config.toml").chmod(mode)
                with self.assertRaises(mint_runner.RunnerError):
                    self.prepare()
                (self.root / "config.toml").write_bytes(self.original_config)
                (self.root / "config.toml").chmod(0o600)

    def test_active_process_and_wrong_cli_version_fail_closed(self):
        with self.assertRaises(mint_runner.RunnerError):
            self.prepare(process_checker=lambda: True)
        with self.assertRaises(mint_runner.RunnerError):
            self.prepare(
                preflight_runner=lambda root: {
                    "preflight": "ok",
                    "cli_version": "9.9.9",
                }
            )
        self.assertEqual((self.root / "tokens.toml").read_bytes(), self.original_tokens)

    def test_prepare_writes_exact_single_mint_and_private_snapshot(self):
        (self.root / "hot_tokens.json").write_text("old-hot")
        (self.root / "routing.json").write_text("old-routing")
        prepared = self.prepare()
        self.assertEqual(
            (self.root / "tokens.toml").read_text(),
            f'tokens = ["{TARGET_MINT}"]\n',
        )
        self.assertFalse((self.root / "hot_tokens.json").exists())
        self.assertFalse((self.root / "routing.json").exists())
        self.assertEqual(stat.S_IMODE(prepared.backup_dir.stat().st_mode), 0o700)
        for name in ("config.toml", "tokens.toml", "hot_tokens.json", "routing.json"):
            self.assertEqual(
                stat.S_IMODE((prepared.backup_dir / name).stat().st_mode),
                0o600,
            )

    def test_restore_is_idempotent_and_byte_exact(self):
        prepared = self.prepare()
        (self.root / "config.toml").write_text("changed")
        (self.root / "tokens.toml").write_text("changed")
        mint_runner.restore_run(self.root, prepared.run_id)
        mint_runner.restore_run(self.root, prepared.run_id)
        self.assertEqual((self.root / "config.toml").read_bytes(), self.original_config)
        self.assertEqual((self.root / "tokens.toml").read_bytes(), self.original_tokens)
        self.assertFalse((self.root / "state" / ".mint-run-active").exists())

    def test_preflight_failure_restores_before_raising(self):
        with self.assertRaises(mint_runner.RunnerError):
            self.prepare(
                preflight_runner=lambda root: (_ for _ in ()).throw(
                    mint_runner.RunnerError("preflight failed")
                )
            )
        self.assertEqual((self.root / "config.toml").read_bytes(), self.original_config)
        self.assertEqual((self.root / "tokens.toml").read_bytes(), self.original_tokens)

    def test_restore_active_recovers_when_caller_lost_run_id(self):
        self.prepare()
        (self.root / "tokens.toml").write_text("changed")
        mint_runner.restore_active(self.root)
        self.assertEqual((self.root / "tokens.toml").read_bytes(), self.original_tokens)
        self.assertFalse((self.root / "state" / ".mint-run-active").exists())

    def test_prepare_output_never_contains_config_secrets(self):
        prepared = self.prepare()
        rendered = json.dumps(prepared.safe_summary(), sort_keys=True)
        self.assertNotIn("https://secret.invalid", rendered)

    def test_prepare_cli_failure_records_generic_state_entry(self):
        with patch.object(
            mint_runner,
            "prepare_run",
            side_effect=mint_runner.RunnerError("secret-specific detail"),
        ):
            status = mint_runner.main(
                ["--root", str(self.root), "prepare", "--mint", TARGET_MINT]
            )
        self.assertEqual(status, 1)
        for name in ("CURRENT.md", "EXPERIMENTS.md"):
            text = (self.root / "state" / name).read_text()
            self.assertIn("single-mint preparation failed", text)
            self.assertNotIn("secret-specific detail", text)
```

- [ ] **Step 2: Run the new tests to verify RED**

Run:

```bash
python3 -m unittest -v tests.test_mint_runner
```

Expected: import error because `scripts/mint_runner.py` does not exist.

- [ ] **Step 3: Implement constants, types, and pure validation**

Create the top of `scripts/mint_runner.py`:

```python
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
    if isinstance(value, bool) or not MIN_TIMEOUT_SECONDS <= parsed <= MAX_TIMEOUT_SECONDS:
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
```

- [ ] **Step 4: Implement atomic files, workspace validation, prepare, and restore**

Append:

```python
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
        with config_path.open("rb") as handle:
            config = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise RunnerError("config.toml is invalid or unreadable") from exc
    if stat.S_IMODE(config_path.stat().st_mode) != 0o600:
        raise RunnerError("config.toml must have mode 600")
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
    if safe.get("preflight") != "ok" or safe.get("cli_version") != "0.2.2":
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
    config, rpc_url = _load_workspace_config(root)
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
        for name in ("config.toml", "tokens.toml"):
            source = root / name
            if not source.is_file():
                raise RunnerError(f"{name} is missing")
            _safe_copy(source, backup_dir / name)
        for name in ("hot_tokens.json", "routing.json"):
            source = root / name
            metadata["optional_files"][name] = source.exists()
            if source.exists():
                _safe_copy(source, backup_dir / name)
                source.unlink()
        _atomic_write(
            backup_dir / "metadata.json",
            (json.dumps(metadata, sort_keys=True) + "\n").encode(),
        )
        _atomic_write(active_marker, f"{run_id}\n".encode())
        _atomic_write(root / "tokens.toml", f'tokens = ["{mint}"]\n'.encode())
        with (root / "tokens.toml").open("rb") as handle:
            tokens = tomllib.load(handle)
        if tokens != {"tokens": [mint]}:
            raise RunnerError("temporary tokens.toml validation failed")
        preflight = (preflight_runner or _run_preflight)(root)
        if preflight.get("preflight") != "ok" or preflight.get("cli_version") != "0.2.2":
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
    for name in ("config.toml", "tokens.toml"):
        backup = backup_dir / name
        if backup.exists():
            _atomic_copy(backup, root / name)
    for name, existed in metadata.get("optional_files", {}).items():
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
    backup_dir = Path(
        tempfile.mkdtemp(prefix=f"state-{stamp}-", dir=backup_root)
    )
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
```

- [ ] **Step 5: Add the preparation CLI**

Append:

```python
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
    except RunnerError as exc:
        _record_failure_safely(root, args.command)
        print(f"status=failed\nerror={exc}", file=os.sys.stderr)
        return 1
    except Exception:
        _record_failure_safely(root, args.command)
        print("status=failed\nerror=operation failed", file=os.sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 6: Run mint-runner tests**

Run:

```bash
python3 -m unittest -v tests.test_mint_runner
python3 -m py_compile scripts/mint_runner.py
```

Expected: all Task 3 tests pass.

- [ ] **Step 7: Commit Task 3**

Run:

```bash
git add scripts/mint_runner.py tests/test_mint_runner.py
git -c user.name=Codex -c user.email=codex@local commit \
  -m "feat: prepare isolated single-mint runs"
```

---

### Task 4: Add secret-safe finalization and finalized on-chain aggregation

**Files:**
- Modify: `scripts/mint_runner.py`
- Modify: `tests/test_mint_runner.py`

**Interfaces:**
- Produces: `aggregate_log(log_path: Path) -> dict[str, int]`.
- Produces: `aggregate_chain(config: dict, mint: str, started_at: int, ended_at: int, transport=None, pubkey_resolver=None) -> dict`.
- Produces: `finalize_run(root: Path, run_id: str, guard_exit: int, started_at: int, ended_at: int, transport=None, pubkey_resolver=None) -> dict`.
- Extends CLI with `mint_runner.py finalize --run-id ID --guard-exit N --started-at EPOCH --ended-at EPOCH`.

- [ ] **Step 1: Add failing log, chain, manifest, and restoration tests**

Add:

```python
class FinalizationTests(MintRunnerTestCase):
    def test_log_aggregation_counts_only_fixed_categories(self):
        log = self.root / "log.txt"
        log.write_text(
            "Payer WSOL account exists\n"
            "Fetched 1 mint list.\n"
            "Finding proper luts info...\n"
            "Transaction sent successfully\n"
            "Transaction sent successfully\n"
        )
        self.assertEqual(
            mint_runner.aggregate_log(log),
            {
                "wsol_exists": 1,
                "wsol_missing": 0,
                "wsol_created": 0,
            "mint_refresh": 1,
            "pool_events": 0,
            "lut_events": 1,
                "sent_events": 2,
                "error_events": 0,
            },
        )

    def test_chain_aggregation_never_returns_signatures(self):
        calls = []

        def transport(url, payload, timeout):
            calls.append(payload["method"])
            if payload["method"] == "getSignaturesForAddress":
                return {
                    "result": [
                        {
                            "signature": "must-not-survive",
                            "blockTime": 100,
                        }
                    ]
                }
            return {
                "result": {
                    "meta": {
                        "err": None,
                        "fee": 5000,
                        "preBalances": [10000],
                        "postBalances": [5000],
                        "preTokenBalances": [
                            {
                                "owner": "wallet",
                                "mint": TARGET_MINT,
                                "uiTokenAmount": {"amount": "1"},
                            }
                        ],
                        "postTokenBalances": [
                            {
                                "owner": "wallet",
                                "mint": TARGET_MINT,
                                "uiTokenAmount": {"amount": "2"},
                            }
                        ],
                    },
                    "transaction": {
                        "message": {
                            "accountKeys": [{"pubkey": "wallet"}],
                            "instructions": [
                                {
                                    "program": "system",
                                    "parsed": {
                                        "type": "createAccount",
                                        "info": {
                                            "source": "wallet",
                                            "lamports": 2039280,
                                        },
                                    },
                                },
                                {
                                    "program": "system",
                                    "parsed": {
                                        "type": "transfer",
                                        "info": {
                                            "source": "wallet",
                                            "lamports": 10000,
                                        },
                                    },
                                },
                            ],
                        }
                    },
                }
            }

        result = mint_runner.aggregate_chain(
            {"rpc": {"url": "https://secret.invalid"}},
            TARGET_MINT,
            90,
            110,
            transport=transport,
            pubkey_resolver=lambda config: "wallet",
        )
        self.assertEqual(result["landed"], 1)
        self.assertEqual(result["successful"], 1)
        self.assertEqual(result["fees_lamports"], 5000)
        self.assertEqual(result["rent_lamports"], 2039280)
        self.assertEqual(result["transfers_lamports"], 10000)
        self.assertNotIn("signature", json.dumps(result).lower())
        self.assertNotIn("https://secret.invalid", json.dumps(result))

    def test_finalize_copies_generated_artifacts_then_restores(self):
        prepared = self.prepare()
        log = self.root / "logs" / "run.log"
        log.parent.mkdir()
        log.write_text("Payer WSOL account exists\n")
        log.chmod(0o600)
        guard_result = prepared.result_dir / "guard-result.txt"
        guard_result.write_text(
            "reason=timeout\n"
            "duration_seconds=300.1\n"
            "log_path=logs/run.log\n"
        )
        guard_result.chmod(0o600)
        (self.root / "hot_tokens.json").write_text('{"generated": true}')
        (self.root / "routing.json").write_text("generated routing")
        result = mint_runner.finalize_run(
            self.root,
            prepared.run_id,
            guard_exit=0,
            started_at=100,
            ended_at=400,
            chain_aggregator=lambda *args, **kwargs: {
                "landed": 0,
                "successful": 0,
                "failed": 0,
                "fees_lamports": 0,
                "rent_lamports": 0,
                "transfers_lamports": 0,
                "sol_delta_lamports": 0,
                "wsol_delta_raw": 0,
            },
        )
        manifest = prepared.result_dir / "manifest.json"
        self.assertTrue(manifest.exists())
        rendered = manifest.read_text()
        self.assertNotIn("https://secret.invalid", rendered)
        self.assertNotIn("signature", rendered.lower())
        self.assertEqual((self.root / "tokens.toml").read_bytes(), self.original_tokens)
        self.assertFalse((self.root / "state" / ".mint-run-active").exists())
        self.assertEqual(result["stop_reason"], "timeout")
        self.assertEqual(result["started_at"], 100)
        self.assertEqual(result["ended_at"], 400)
        state_backups = list(
            (self.root / "state" / "backups").glob("state-*/CURRENT.md")
        )
        self.assertEqual(len(state_backups), 1)
        self.assertEqual(stat.S_IMODE(state_backups[0].stat().st_mode), 0o600)
```

- [ ] **Step 2: Run finalization tests to verify RED**

Run:

```bash
python3 -m unittest -v tests.test_mint_runner.FinalizationTests
```

Expected: errors because aggregation and finalization functions do not exist.

- [ ] **Step 3: Implement fixed-pattern log aggregation and safe key-value parsing**

Append:

```python
LOG_PATTERNS = {
    "wsol_exists": re.compile(r"WSOL account exists", re.I),
    "wsol_missing": re.compile(r"WSOL account does not exist", re.I),
    "wsol_created": re.compile(r"WSOL account created successfully", re.I),
    "mint_refresh": re.compile(r"Fetched [0-9]+ mint list", re.I),
    "pool_events": re.compile(r"found [0-9]+ pools?|selected pool|pool selected", re.I),
    "lut_events": re.compile(r"Resolved LUTs|Finding proper luts", re.I),
    "sent_events": re.compile(r"Transaction sent successfully", re.I),
    "error_events": re.compile(r"error|failed", re.I),
}


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
```

- [ ] **Step 4: Implement finalized on-chain aggregation**

Append:

```python
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
    ]
    summary = {
        "landed": 0,
        "successful": 0,
        "failed": 0,
        "fees_lamports": 0,
        "rent_lamports": 0,
        "transfers_lamports": 0,
        "sol_delta_lamports": 0,
        "wsol_delta_raw": 0,
    }
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
        token_balances = (
            list((transaction.get("meta") or {}).get("preTokenBalances", []) or [])
            + list((transaction.get("meta") or {}).get("postTokenBalances", []) or [])
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
        meta = transaction.get("meta") or {}
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
        keys = (
            transaction.get("transaction", {})
            .get("message", {})
            .get("accountKeys", [])
        )
        pubkeys = [
            item.get("pubkey") if isinstance(item, dict) else item
            for item in keys
        ]
        if wallet in pubkeys:
            index = pubkeys.index(wallet)
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
```

- [ ] **Step 5: Implement finalization, artifact capture, restoration, and state append**

Append:

```python
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
    backup_dir = root / "state" / "backups" / f"mint-run-{run_id}"
    result_dir = root / "state" / "mint-runs" / run_id
    metadata = json.loads((backup_dir / "metadata.json").read_text())
    guard_result_path = result_dir / "guard-result.txt"
    guard = _parse_guard_result(guard_result_path)
    log_relative = guard.get("log_path")
    log_path = root / log_relative if log_relative else None
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
        chain = (chain_aggregator or aggregate_chain)(
            config,
            metadata["mint"],
            started_at,
            ended_at,
            transport=transport,
            pubkey_resolver=pubkey_resolver,
        )
        aggregation_status = "ok"
    except Exception:
        chain = {
            "landed": 0,
            "successful": 0,
            "failed": 0,
            "fees_lamports": 0,
            "rent_lamports": 0,
            "transfers_lamports": 0,
            "sol_delta_lamports": 0,
            "wsol_delta_raw": 0,
        }
        aggregation_status = "failed"
    for name in ("hot_tokens.json", "routing.json"):
        generated = root / name
        if generated.exists():
            _safe_copy(generated, result_dir / f"generated-{name}")
    manifest = {
        "run_id": run_id,
        "mint": metadata["mint"],
        "timeout_seconds": metadata["timeout_seconds"],
        "guard_exit": int(guard_exit),
        "stop_reason": guard.get("reason", "unknown"),
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
```

The state bullets deliberately omit the mint value. The private manifest may include the public mint but never includes config values, URLs, signatures, API keys, wallet material, or UUIDs.

- [ ] **Step 6: Add the `finalize` CLI command**

Add:

```python
    finalize_parser = subparsers.add_parser("finalize")
    finalize_parser.add_argument("--run-id", required=True)
    finalize_parser.add_argument("--guard-exit", required=True, type=int)
    finalize_parser.add_argument("--started-at", required=True, type=int)
    finalize_parser.add_argument("--ended-at", required=True, type=int)
```

Handle it before the final `return 2`:

```python
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
```

- [ ] **Step 7: Run finalization and complete mint-runner tests**

Run:

```bash
python3 -m unittest -v tests.test_mint_runner.FinalizationTests
python3 -m unittest -v tests.test_mint_runner
python3 -m py_compile scripts/mint_runner.py
```

Expected: all tests pass and compilation succeeds.

- [ ] **Step 8: Commit Task 4**

Run:

```bash
git add scripts/mint_runner.py tests/test_mint_runner.py
git -c user.name=Codex -c user.email=codex@local commit \
  -m "feat: finalize single-mint run records"
```

---

### Task 5: Add the one-command interactive entrypoint and transaction-free integration tests

**Files:**
- Create: `scripts/mint-run.sh`
- Create: `tests/test_mint_run_shell.py`

**Interfaces:**
- Consumes: `mint_runner.py prepare`, `result-path`, `finalize`, `restore`, and `restore-active`.
- Consumes: `run-guarded.sh --live-confirmed --timeout SECONDS`.
- Produces: `./scripts/mint-run.sh <MINT> [--timeout SECONDS]`.

- [ ] **Step 1: Create failing shell integration tests**

Create `tests/test_mint_run_shell.py`:

```python
import os
import signal
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


TARGET_MINT = "So11111111111111111111111111111111111111112"


class MintRunShellTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "scripts").mkdir()
        (self.root / "state" / "mint-runs" / "20260724T190000Z").mkdir(
            parents=True
        )
        source = Path(__file__).resolve().parents[1] / "scripts" / "mint-run.sh"
        shutil.copy2(source, self.root / "scripts" / "mint-run.sh")
        helper = self.root / "scripts" / "mint_runner.py"
        helper.write_text(
            "#!/usr/bin/env python3\n"
            "import pathlib, sys\n"
            "cmd = sys.argv[sys.argv.index('--root') + 2]\n"
            "root = pathlib.Path(sys.argv[sys.argv.index('--root') + 1])\n"
            "if cmd == 'prepare':\n"
            " print('run_id=20260724T190000Z')\n"
            " print('timeout_seconds=' + sys.argv[sys.argv.index('--timeout') + 1])\n"
            " print('cli_version=0.2.2')\n"
            " print('loss_limit_lamports=30000000')\n"
            " print('early_stop_lamports=25000000')\n"
            "elif cmd == 'result-path':\n"
            " print(root / 'state/mint-runs/20260724T190000Z/guard-result.txt')\n"
            "elif cmd in ('restore', 'restore-active', 'finalize'):\n"
            " (root / (cmd + '.called')).write_text(' '.join(sys.argv))\n"
            " if cmd == 'finalize': (root / 'restore.called').write_text('yes')\n"
        )
        helper.chmod(0o755)
        guarded = self.root / "scripts" / "run-guarded.sh"
        guarded.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "printf '%s\\n' \"$*\" >> run-guarded.args\n"
            "printf 'reason=timeout\\nduration_seconds=60\\nlog_path=logs/fake.log\\n'\n"
        )
        guarded.chmod(0o755)

    def tearDown(self):
        shutil.rmtree(self.root)

    def invoke(self, stdin, *args):
        return subprocess.run(
            ["bash", "scripts/mint-run.sh", *args],
            cwd=self.root,
            input=stdin,
            text=True,
            capture_output=True,
        )

    def test_declined_confirmation_never_runs_guard(self):
        result = self.invoke("no\n", TARGET_MINT, "--timeout", "60")
        self.assertEqual(result.returncode, 0)
        self.assertFalse((self.root / "run-guarded.args").exists())
        self.assertTrue((self.root / "restore.called").exists())

    def test_exact_confirmation_runs_once_and_finalizes(self):
        phrase = f"RUN {TARGET_MINT} FOR 60\n"
        result = self.invoke(phrase, TARGET_MINT, "--timeout", "60")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            (self.root / "run-guarded.args").read_text().splitlines(),
            ["--live-confirmed --timeout 60"],
        )
        self.assertTrue((self.root / "finalize.called").exists())
        finalize_args = (self.root / "finalize.called").read_text()
        self.assertRegex(finalize_args, r"--started-at [0-9]+")
        self.assertRegex(finalize_args, r"--ended-at [0-9]+")

    def test_wrong_arguments_fail_before_prepare(self):
        result = self.invoke("", TARGET_MINT, "--timeout", "301")
        self.assertEqual(result.returncode, 64)
        self.assertFalse((self.root / "restore.called").exists())

    def test_guard_failure_still_finalizes_and_restores(self):
        guarded = self.root / "scripts" / "run-guarded.sh"
        guarded.write_text(
            "#!/usr/bin/env bash\n"
            "printf 'reason=rpc_error\\nduration_seconds=1\\nlog_path=logs/fake.log\\n'\n"
            "exit 1\n"
        )
        guarded.chmod(0o755)
        phrase = f"RUN {TARGET_MINT} FOR 60\n"
        result = self.invoke(phrase, TARGET_MINT, "--timeout", "60")
        self.assertEqual(result.returncode, 1)
        self.assertTrue((self.root / "finalize.called").exists())
        self.assertTrue((self.root / "restore.called").exists())

    def test_invalid_prepare_output_uses_active_marker_recovery(self):
        helper = self.root / "scripts" / "mint_runner.py"
        helper.write_text(
            "#!/usr/bin/env python3\n"
            "import pathlib, sys\n"
            "root = pathlib.Path(sys.argv[sys.argv.index('--root') + 1])\n"
            "cmd = sys.argv[sys.argv.index('--root') + 2]\n"
            "if cmd == 'prepare': print('run_id=broken')\n"
            "elif cmd == 'restore-active':\n"
            " (root / 'restore-active.called').write_text('yes')\n"
        )
        helper.chmod(0o755)
        result = self.invoke("", TARGET_MINT, "--timeout", "60")
        self.assertEqual(result.returncode, 1)
        self.assertTrue((self.root / "restore-active.called").exists())

    def test_sigterm_is_forwarded_then_run_is_finalized(self):
        guarded = self.root / "scripts" / "run-guarded.sh"
        guarded.write_text(
            "#!/usr/bin/env bash\n"
            "trap 'printf \"reason=operator_signal\\nduration_seconds=1\\n\"; exit 0' TERM\n"
            "touch guard.started\n"
            "while :; do sleep 0.05; done\n"
        )
        guarded.chmod(0o755)
        process = subprocess.Popen(
            ["bash", "scripts/mint-run.sh", TARGET_MINT, "--timeout", "60"],
            cwd=self.root,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        process.stdin.write(f"RUN {TARGET_MINT} FOR 60\n")
        process.stdin.flush()
        for _ in range(100):
            if (self.root / "guard.started").exists():
                break
            time.sleep(0.01)
        else:
            self.fail("fake guard did not start")
        os.kill(process.pid, signal.SIGTERM)
        process.communicate(timeout=5)
        self.assertTrue((self.root / "finalize.called").exists())
        self.assertTrue((self.root / "restore.called").exists())
```

- [ ] **Step 2: Run shell integration tests to verify RED**

Run:

```bash
python3 -m unittest -v tests.test_mint_run_shell
```

Expected: import/setup failure because `scripts/mint-run.sh` does not exist.

- [ ] **Step 3: Create the complete operator entrypoint**

Create `scripts/mint-run.sh`:

```bash
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
cleanup() {
  status=$?
  trap - EXIT
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

forward_signal() {
  if [[ -n "$guard_pid" ]] && kill -0 "$guard_pid" 2>/dev/null; then
    kill -TERM "$guard_pid" 2>/dev/null || true
  fi
}
trap forward_signal INT TERM

prepare_output="$(
  python3 scripts/mint_runner.py --root "$root" prepare \
    --mint "$mint" \
    --timeout "$timeout_seconds"
)"
printf '%s\n' "$prepare_output"
run_id="$(
  printf '%s\n' "$prepare_output" |
    awk -F= '$1 == "run_id" {print $2; exit}'
)"
[[ "$run_id" =~ ^[0-9]{8}T[0-9]{6}Z$ ]] || {
  echo 'Preparation returned an invalid run identifier.' >&2
  exit 1
}

confirmation="RUN $mint FOR $timeout_seconds"
printf 'Type exactly: %s\n> ' "$confirmation"
IFS= read -r answer
if [[ "$answer" != "$confirmation" ]]; then
  echo 'Live run declined; restoring workspace.'
  exit 0
fi

result_path="$(
  python3 scripts/mint_runner.py --root "$root" result-path --run-id "$run_id"
)"
mkdir -p -- "$(dirname -- "$result_path")"
started_at="$(date -u +%s)"
set +e
./scripts/run-guarded.sh --live-confirmed --timeout "$timeout_seconds" \
  >"$result_path" 2>&1 &
guard_pid=$!
while true; do
  wait "$guard_pid"
  wait_status=$?
  if ! kill -0 "$guard_pid" 2>/dev/null; then
    guard_status=$wait_status
    break
  fi
done
guard_pid=""
set -e
ended_at="$(date -u +%s)"
chmod 600 "$result_path"
sed -n '1,200p' "$result_path"

python3 scripts/mint_runner.py --root "$root" finalize \
  --run-id "$run_id" \
  --guard-exit "$guard_status" \
  --started-at "$started_at" \
  --ended-at "$ended_at"
finalized=1
exit "$guard_status"
```

- [ ] **Step 4: Run shell tests and syntax checks**

Run:

```bash
chmod 755 scripts/mint-run.sh
bash -n scripts/mint-run.sh
python3 -m unittest -v tests.test_mint_run_shell
```

Expected: all six shell integration tests pass.

- [ ] **Step 5: Run the complete transaction-free test suite**

Run:

```bash
python3 -m unittest -v \
  tests.test_zavod_guard \
  tests.test_mint_runner \
  tests.test_mint_run_shell
python3 -m py_compile scripts/zavod_guard.py scripts/mint_runner.py
bash -n scripts/preflight.sh scripts/run-guarded.sh scripts/mint-run.sh
```

Expected: every test passes; no bot process starts and no RPC request escapes the mocks in tests.

- [ ] **Step 6: Commit Task 5**

Run:

```bash
git add scripts/mint-run.sh tests/test_mint_run_shell.py
git -c user.name=Codex -c user.email=codex@local commit \
  -m "feat: add interactive mint run command"
```

---

### Task 6: Document operation, verify restoration, and prepare a transaction-free handoff

**Files:**
- Create: `runbooks/mint-run.md`
- Modify: `state/CURRENT.md`
- Modify: `state/EXPERIMENTS.md`

**Interfaces:**
- Consumes: all Tasks 1–5.
- Produces: operator documentation and fresh verification evidence.

- [ ] **Step 1: Write the operator runbook**

Create `runbooks/mint-run.md` with:

````markdown
# Single-mint guarded run

## Command

```bash
./scripts/mint-run.sh <MINT> [--timeout SECONDS]
```

`SECONDS` defaults to `300` and must be from `30` through `300`.

## Behavior

The command validates the token mint, snapshots the active workspace, temporarily
places only the requested mint in `tokens.toml`, runs guarded preflight, and prints
the exact confirmation phrase. The bot starts only after that phrase is entered.

Pool discovery, routing, and LUT resolution remain inside ZavodMevBot auto mode.
No static `markets.toml` or new on-chain LUT is created.

After timeout, stop, failure, or Ctrl-C, the original files are restored. Results
are written under `state/mint-runs/<timestamp>/`; recovery copies remain under
`state/backups/mint-run-<timestamp>/`.

## Safety

- Never call the binary's `run` command directly.
- Never reuse an old confirmation.
- There is no automatic retry.
- Early-stop is `0.025 SOL`; loss target is `0.03 SOL`.
- A preparation error means no transaction-capable command was executed.
````

- [ ] **Step 2: Create mode-600 state backups**

Run:

```bash
stamp=$(date -u +%Y%m%dT%H%M%SZ)
install -m 600 state/CURRENT.md "state/backups/CURRENT.md.pre-mint-runner-$stamp"
install -m 600 state/EXPERIMENTS.md "state/backups/EXPERIMENTS.md.pre-mint-runner-$stamp"
```

Expected: both backups exist with mode `600`.

- [ ] **Step 3: Record the transaction-free implementation**

Append to both state files:

```markdown
## 2026-07-24 — single-mint auto runner implemented

- Added one-command single-mint preparation with strict mint/RPC validation, mode-600 snapshots, auto-mode isolation, and byte-exact restoration.
- Live execution remains gated by the exact immediate confirmation phrase and runs only through `scripts/run-guarded.sh`.
- Timeout is bounded to 30–300 seconds; early-stop `0.025 SOL` and loss target `0.03 SOL` are unchanged.
- Pool, route, and LUT discovery remain inside ZavodMevBot auto mode; static markets and new on-chain LUT creation are excluded.
- Transaction-free fake-process/mock-RPC tests cover preparation, refusal, cleanup, restoration, redaction, aggregation, and absence of retries.
- No transaction-capable command was executed during implementation verification.
```

- [ ] **Step 4: Run fresh full verification**

Run:

```bash
python3 -m unittest -v \
  tests.test_zavod_guard \
  tests.test_mint_runner \
  tests.test_mint_run_shell
python3 -m py_compile scripts/zavod_guard.py scripts/mint_runner.py
bash -n scripts/preflight.sh scripts/run-guarded.sh scripts/mint-run.sh
git diff --check
test -z "$(ps -eo comm= | awk '$1 ~ /^zavod-mev-bot/ {print; exit}')"
test "$(stat -c '%a' config.toml)" = "600"
```

Expected: all tests and syntax checks pass, Git reports no whitespace errors, no bot process is active, and config mode is `600`.

- [ ] **Step 5: Run the composed transaction-free recovery proof**

Run the exact tests that cover byte restoration, lost-run-id recovery, guarded
invocation count, finalization, private permissions, and secret omission:

```bash
python3 -m unittest -v \
  tests.test_mint_runner.MintRunnerTestCase.test_restore_is_idempotent_and_byte_exact \
  tests.test_mint_runner.MintRunnerTestCase.test_restore_active_recovers_when_caller_lost_run_id \
  tests.test_mint_runner.FinalizationTests.test_finalize_copies_generated_artifacts_then_restores \
  tests.test_mint_run_shell.MintRunShellTests.test_exact_confirmation_runs_once_and_finalizes \
  tests.test_mint_run_shell.MintRunShellTests.test_invalid_prepare_output_uses_active_marker_recovery
```

Expected: `Ran 5 tests` and `OK`; all subprocesses are fake and all RPC
transports are in-memory mocks.

- [ ] **Step 6: Commit Task 6**

Run:

```bash
git add runbooks/mint-run.md state/CURRENT.md state/EXPERIMENTS.md
git -c user.name=Codex -c user.email=codex@local commit \
  -m "docs: document guarded mint runs"
```

- [ ] **Step 7: Stop before any live test**

Report the transaction-free verification evidence. A real mint run is a separate transaction-capable action and requires a new explicit user approval immediately before `mint-run.sh` reaches its confirmation gate.
