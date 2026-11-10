import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import tomllib
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from scripts import mint_auto_diagnoser


TARGET_MINT = "So11111111111111111111111111111111111111112"
EXPECTED_STAGES = (
    "baseline",
    "offchain",
    "activity",
    "aggregate_profit",
    "per_arb_profit",
    "roi",
    "volume",
    "pool_liquidity",
)
SOURCE = b"""[wallet]
private_key = "test-wallet"
[rpc]
url = "https://fixture.invalid"
[auto]
force_two_mints = true
enable_three_hop = false
[auto.filters]
limit = 2
ignore_offchain_bots = true
min_tx_len = 3
min_profit = 9
min_profit_per_arb = 4
min_roi = 0.2
min_volume_lamports = 7
[bot]
merge_mints = true
[auto.markets]
min_pool_liquidity_lamports = 11
"""


class StageGenerationTests(unittest.TestCase):
    def test_stages_are_cumulative_and_preserve_required_controls(self):
        """A missing or reordered relaxation must fail this public stage contract."""
        stages = mint_auto_diagnoser.build_stage_configs(SOURCE)

        self.assertEqual(tuple(name for name, _, _ in stages), EXPECTED_STAGES)
        expected_additions = {
            "baseline": (),
            "offchain": ("offchain",),
            "activity": ("offchain", "activity"),
            "aggregate_profit": ("offchain", "activity", "aggregate_profit"),
            "per_arb_profit": (
                "offchain", "activity", "aggregate_profit", "per_arb_profit"
            ),
            "roi": (
                "offchain", "activity", "aggregate_profit", "per_arb_profit", "roi"
            ),
            "volume": (
                "offchain", "activity", "aggregate_profit", "per_arb_profit", "roi", "volume"
            ),
            "pool_liquidity": (
                "offchain", "activity", "aggregate_profit", "per_arb_profit", "roi", "volume", "pool_liquidity"
            ),
        }
        permissive = {
            "offchain": ("auto", "filters", "ignore_offchain_bots", False),
            "activity": ("auto", "filters", "min_tx_len", 0),
            "aggregate_profit": ("auto", "filters", "min_profit", 0),
            "per_arb_profit": ("auto", "filters", "min_profit_per_arb", 0),
            "roi": ("auto", "filters", "min_roi", 0.0),
            "volume": ("auto", "filters", "min_volume_lamports", 0),
            "pool_liquidity": ("auto", "markets", "min_pool_liquidity_lamports", 0),
        }
        for name, source, mutations in stages:
            config = tomllib.loads(source.decode())
            self.assertFalse(config["auto"]["force_two_mints"])
            self.assertEqual(config["auto"]["filters"]["limit"], 1)
            self.assertFalse(config["bot"]["merge_mints"])
            self.assertTrue(config["auto"]["enable_three_hop"])
            self.assertEqual(mutations, expected_additions[name])
            for mutation_name in mutations:
                section, subsection, key, value = permissive[mutation_name]
                self.assertEqual(config[section][subsection][key], value)

    def test_already_permissive_mutation_is_omitted_with_reason(self):
        """Launching a no-op relaxation must remain distinguishable from an executable stage."""
        source = SOURCE.replace(b"min_roi = 0.2", b"min_roi = 0.0")
        stages = mint_auto_diagnoser.build_stage_configs(source)

        self.assertNotIn("roi", tuple(name for name, _, _ in stages))
        self.assertEqual(
            mint_auto_diagnoser.stage_skip_reasons(source)["roi"],
            "already_permissive",
        )

    def test_ambiguous_or_wrong_typed_assignments_are_rejected(self):
        """A config rewrite cannot silently choose among duplicate or noncanonical keys."""
        for source in (
            SOURCE.replace(b"min_profit = 9", b"min_profit = 9\nmin_profit = 8"),
            SOURCE.replace(b"min_profit = 9", b'"min_profit" = 9'),
            SOURCE.replace(b"min_profit = 9", b"min_profit = 9.0"),
        ):
            with self.subTest(source=hashlib.sha256(source).hexdigest()):
                with self.assertRaises(mint_auto_diagnoser.DiagnoserError):
                    mint_auto_diagnoser.build_stage_configs(source)


class PrivateWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "state").mkdir()
        (self.root / "config.toml").write_bytes(SOURCE)
        (self.root / "tokens.toml").write_bytes(b'tokens = ["old"]\n')
        (self.root / "zavod-mev-bot-rust-version-cli").write_bytes(b"test binary")
        for path in (self.root / "config.toml", self.root / "tokens.toml"):
            path.chmod(0o600)
        (self.root / "zavod-mev-bot-rust-version-cli").chmod(0o700)
        self.production = {
            name: (self.root / name).read_bytes()
            for name in ("config.toml", "tokens.toml", "zavod-mev-bot-rust-version-cli")
        }

    def tearDown(self):
        shutil.rmtree(self.root)

    @staticmethod
    def transport(url, payload, timeout):
        del url, timeout
        if payload["method"] != "getAccountInfo":
            raise AssertionError("unexpected RPC method")
        return {
            "result": {
                "value": {
                    "executable": False,
                    "owner": mint_auto_diagnoser.zavod_guard.TOKEN_PROGRAM_ID,
                    "data": {"parsed": {"type": "mint", "info": {"isInitialized": True}}},
                }
            }
        }

    def prepare(self, **overrides):
        args = {
            "root": self.root,
            "mint": TARGET_MINT,
            "now": lambda: datetime(2026, 7, 26, 12, 30, tzinfo=timezone.utc),
            "transport": self.transport,
            "balance_reader": lambda url, wallet: 123_456_789,
        }
        args.update(overrides)
        with patch.object(mint_auto_diagnoser.zavod_guard, "wallet_pubkey", return_value="wallet"):
            return mint_auto_diagnoser.prepare_batch(**args)

    def test_prepare_creates_private_isolated_stages_without_mutating_production(self):
        """A batch must place all identities and execution inputs under its private root."""
        batch = self.prepare()

        self.assertEqual(batch.batch_id, "20260726T123000Z")
        self.assertEqual(batch.relative_root, "state/auto-diagnose-runs/20260726T123000Z")
        batch_root = self.root / batch.relative_root
        self.assertEqual(stat.S_IMODE(batch_root.stat().st_mode), 0o700)
        active = self.root / "state" / ".mint-auto-diagnose-active"
        self.assertEqual(active.read_text(), batch.batch_id + "\n")
        self.assertEqual(stat.S_IMODE(active.stat().st_mode), 0o600)
        executable = [stage for stage in batch.stages if not stage.skipped]
        self.assertEqual(len(executable), len(EXPECTED_STAGES))
        for stage in executable:
            stage_root = self.root / stage.relative_root
            self.assertEqual(stat.S_IMODE(stage_root.stat().st_mode), 0o700)
            for name in ("config.toml", "tokens.toml", "stage-contract.json"):
                self.assertEqual(stat.S_IMODE((stage_root / name).stat().st_mode), 0o600)
            self.assertEqual((stage_root / "tokens.toml").read_bytes(), f'tokens = ["{TARGET_MINT}"]\n'.encode())
            self.assertEqual(
                (stage_root / "zavod-mev-bot-rust-version-cli").read_bytes(),
                self.production["zavod-mev-bot-rust-version-cli"],
            )
            contract = json.loads((stage_root / "stage-contract.json").read_bytes())
            self.assertEqual(set(contract), {
                "schema", "batch_id", "stage_index", "stage_name", "target_mint",
                "timeout_seconds", "batch_start_balance_lamports", "early_stop_lamports",
                "loss_limit_lamports", "config_sha256", "tokens_sha256", "binary_sha256",
                "three_hop_required",
            })
            self.assertEqual(contract["batch_start_balance_lamports"], 123_456_789)
            self.assertEqual(contract["early_stop_lamports"], 25_000_000)
            self.assertEqual(contract["loss_limit_lamports"], 30_000_000)
            self.assertTrue(contract["three_hop_required"])
        self.assertEqual(
            os.stat(self.root / "zavod-mev-bot-rust-version-cli").st_ino,
            os.stat(self.root / executable[0].relative_root / "zavod-mev-bot-rust-version-cli").st_ino,
        )
        for name, source in self.production.items():
            self.assertEqual((self.root / name).read_bytes(), source)

    def test_prepare_reads_baseline_once_and_restore_is_idempotent(self):
        """A second balance read or lingering live marker would break the batch loss bound."""
        calls = []
        def balance_reader(url, wallet):
            calls.append((url, wallet))
            return 55
        batch = self.prepare(balance_reader=balance_reader)
        self.assertEqual(len(calls), 1)

        mint_auto_diagnoser.restore_batch(self.root, batch.batch_id)
        mint_auto_diagnoser.restore_batch(self.root, batch.batch_id)
        self.assertFalse((self.root / "state" / ".mint-auto-diagnose-active").exists())
        for name, source in self.production.items():
            self.assertEqual((self.root / name).read_bytes(), source)

    def test_prepare_failure_removes_active_marker(self):
        """An interrupted preparation must not leave a lock that looks like a live batch."""
        def fail_link(*args, **kwargs):
            del args, kwargs
            raise OSError("link unavailable")
        with patch.object(mint_auto_diagnoser.os, "link", side_effect=fail_link):
            with self.assertRaises(mint_auto_diagnoser.DiagnoserError):
                self.prepare()
        self.assertFalse((self.root / "state" / ".mint-auto-diagnose-active").exists())

    def test_sigterm_after_active_marker_restores_before_exit(self):
        """A standard termination signal after activation must release only this batch marker."""
        program = """
import os
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from scripts import mint_auto_diagnoser as diagnoser

def transport(url, payload, timeout):
    return {"result": {"value": {"executable": False, "owner": diagnoser.zavod_guard.TOKEN_PROGRAM_ID, "data": {"parsed": {"type": "mint", "info": {"isInitialized": True}}}}}}

def interrupt(*args):
    os.kill(os.getpid(), getattr(signal, sys.argv[3]))

try:
    with patch.object(diagnoser.zavod_guard, "wallet_pubkey", return_value="wallet"), patch.object(diagnoser, "_prepare_stage", side_effect=interrupt):
        diagnoser.prepare_batch(Path(sys.argv[1]), sys.argv[2], now=lambda: datetime(2026, 7, 26, 12, 30, tzinfo=timezone.utc), transport=transport, balance_reader=lambda url, wallet: 1)
except BaseException as exc:
    raise SystemExit(128 + getattr(exc, "signum", 15))
"""
        for signal_name, expected_status in (("SIGTERM", 143), ("SIGINT", 130)):
            with self.subTest(signal_name=signal_name):
                result = subprocess.run(
                    [sys.executable, "-c", program, str(self.root), TARGET_MINT, signal_name],
                    cwd=Path(__file__).resolve().parents[1],
                    check=False,
                )
                self.assertEqual(result.returncode, expected_status)
                self.assertFalse((self.root / "state" / ".mint-auto-diagnose-active").exists())
                shutil.rmtree(self.root / "state" / "auto-diagnose-runs")

    def test_binary_copy_fallback_completes_short_writes(self):
        """A partial write during fallback must not truncate the private executable."""
        real_link = mint_auto_diagnoser.os.link
        real_write = mint_auto_diagnoser.os.write

        def no_cross_directory_link(source, destination, *args, **kwargs):
            if kwargs.get("src_dir_fd") != kwargs.get("dst_dir_fd"):
                raise OSError("cross-directory links unavailable")
            return real_link(source, destination, *args, **kwargs)

        def short_write(descriptor, data):
            return real_write(descriptor, data[:1])

        with patch.object(mint_auto_diagnoser.os, "link", side_effect=no_cross_directory_link), patch.object(
            mint_auto_diagnoser.os, "write", side_effect=short_write
        ):
            batch = self.prepare()
        stage = next(stage for stage in batch.stages if not stage.skipped)
        self.assertEqual(
            (self.root / stage.relative_root / "zavod-mev-bot-rust-version-cli").read_bytes(),
            self.production["zavod-mev-bot-rust-version-cli"],
        )

    def test_environment_backed_runtime_values_are_expanded_for_validation(self):
        """Runtime RPC and wallet consumers must receive exact expanded TOML values."""
        source = SOURCE.replace(
            b'url = "https://fixture.invalid"', b'url = "${AUTODIAG_TEST_RPC}"'
        ).replace(
            b'private_key = "test-wallet"', b'private_key = "$AUTODIAG_TEST_KEY"'
        )
        (self.root / "config.toml").write_bytes(source)
        runtime_rpc = "runtime-rpc"
        runtime_key = "runtime-key"
        observed = {"transport": [], "wallet": [], "balance": []}

        def transport(url, payload, timeout):
            del payload, timeout
            observed["transport"].append(url)
            return self.transport(url, {"method": "getAccountInfo"}, 5)

        def wallet_pubkey(value):
            observed["wallet"].append(value)
            return "wallet"

        def balance_reader(url, wallet):
            observed["balance"].append((url, wallet))
            return 1

        with patch.dict(os.environ, {"AUTODIAG_TEST_RPC": runtime_rpc, "AUTODIAG_TEST_KEY": runtime_key}), patch.object(
            mint_auto_diagnoser.zavod_guard, "wallet_pubkey", side_effect=wallet_pubkey
        ):
            mint_auto_diagnoser.prepare_batch(
                self.root,
                TARGET_MINT,
                now=lambda: datetime(2026, 7, 26, 12, 30, tzinfo=timezone.utc),
                transport=transport,
                balance_reader=balance_reader,
            )

        self.assertEqual(observed["transport"], [runtime_rpc])
        self.assertEqual(observed["wallet"], [runtime_key])
        self.assertEqual(observed["balance"], [(runtime_rpc, "wallet")])
        batch_config = next((self.root / "state" / "auto-diagnose-runs").glob("*/production-config.toml"))
        self.assertEqual(batch_config.read_bytes(), source)

    def test_worker_thread_rejects_preparation_before_private_state_changes(self):
        """A thread without signal-handler authority must not create an active batch."""
        failures = []

        def worker():
            try:
                self.prepare()
            except BaseException as exc:
                failures.append(exc)

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()

        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], mint_auto_diagnoser.DiagnoserError)
        self.assertFalse((self.root / "state" / ".mint-auto-diagnose-active").exists())
        self.assertFalse((self.root / "state" / "auto-diagnose-runs").exists())


class StageEvidenceTests(unittest.TestCase):
    RESULT_KEYS = {
        "stage_name",
        "decision",
        "stop_reason",
        "target_status",
        "three_hop_status",
        "sender_accepted",
        "sender_rejected",
        "target_landed",
        "cumulative_loss_lamports",
    }

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "state").mkdir()
        (self.root / "config.toml").write_bytes(SOURCE)
        (self.root / "tokens.toml").write_bytes(b'tokens = ["old"]\n')
        (self.root / "zavod-mev-bot-rust-version-cli").write_bytes(b"test binary")
        (self.root / "config.toml").chmod(0o600)
        (self.root / "tokens.toml").chmod(0o600)
        (self.root / "zavod-mev-bot-rust-version-cli").chmod(0o700)
        with patch.object(
            mint_auto_diagnoser.zavod_guard,
            "wallet_pubkey",
            return_value="wallet",
        ):
            self.batch = mint_auto_diagnoser.prepare_batch(
                self.root,
                TARGET_MINT,
                now=lambda: datetime(2026, 7, 26, 12, 30, tzinfo=timezone.utc),
                transport=PrivateWorkspaceTests.transport,
                balance_reader=lambda url, wallet: 123_456_789,
            )
        self.stage = next(stage for stage in self.batch.stages if not stage.skipped)
        self.stage_root = self.root / self.stage.relative_root
        logs = self.stage_root / "logs"
        logs.mkdir(mode=0o700)
        (logs / "stage.log").write_bytes(b"")
        (logs / "stage.log").chmod(0o600)
        guard = (
            "reason=timeout\n"
            "duration_seconds=60\n"
            "child_exit_code=0\n"
            "loss_limit_lamports=30000000\n"
            "early_stop_lamports=25000000\n"
            "log_path=logs/stage.log\n"
        )
        (self.stage_root / "guard-result.txt").write_text(guard)
        (self.stage_root / "guard-result.txt").chmod(0o600)

    def tearDown(self):
        shutil.rmtree(self.root)

    @staticmethod
    def hot_tokens(target):
        return {
            "count": 1,
            "arb_mint_info": [
                {
                    "arbs_count": 1,
                    "bridge_mint": "bridge",
                    "bridge_pool_ids_info": [],
                    "cross_pool_ids_info": [],
                    "lookup_table_accounts": ["lut"],
                    "mint": target,
                    "pool_ids": ["pool"],
                    "pool_ids_info": [],
                    "roi": 0.0,
                    "total_fee": 0,
                    "total_liquidity_lamports": 0,
                    "total_profit": 0,
                    "total_volume": 0,
                    "txs": [{}],
                }
            ],
        }

    def write_artifact(self, name, value):
        path = self.stage_root / name
        path.write_text(json.dumps(value))
        path.chmod(0o600)
        os.utime(path, (150, 150))

    def transport(self, url, payload, timeout):
        del url, timeout
        if payload["method"] == "getBalance":
            return {"result": {"value": 123_456_789}}
        if payload["method"] == "getSignaturesForAddress":
            return {"result": []}
        raise AssertionError("unexpected RPC method")

    def evaluate(self, *, guard_exit=0, transport=None):
        with patch.object(
            mint_auto_diagnoser.zavod_guard,
            "wallet_pubkey",
            return_value="wallet",
        ):
            result = mint_auto_diagnoser.evaluate_stage(
                self.root,
                self.batch.batch_id,
                self.stage.name,
                guard_exit,
                100,
                200,
                transport=transport or self.transport,
            )
        self.assertEqual(set(result), self.RESULT_KEYS)
        return result

    def test_exact_target_returns_target_positive(self):
        self.write_artifact("hot_tokens.json", self.hot_tokens(TARGET_MINT))

        result = self.evaluate()

        self.assertEqual(result["decision"], "target_positive")
        self.assertEqual(result["target_status"], "positive")

    def test_sender_acceptance_alone_returns_continue(self):
        log = self.stage_root / "logs" / "stage.log"
        log.write_text("Transaction sent successfully\n")
        log.chmod(0o600)

        result = self.evaluate()

        self.assertEqual(result["decision"], "continue")
        self.assertEqual(result["sender_accepted"], 1)
        self.assertEqual(result["target_landed"], 0)

    def test_missing_target_returns_continue(self):
        result = self.evaluate()

        self.assertEqual(result["decision"], "continue")
        self.assertEqual(result["target_status"], "absent")

    def test_nonzero_guard_exit_returns_failed(self):
        result = self.evaluate(guard_exit=2)

        self.assertEqual(result["decision"], "failed")
        self.assertEqual(result["stop_reason"], "guard_exit")

    def test_threshold_or_rpc_error_returns_failed(self):
        def threshold_transport(url, payload, timeout):
            del url, timeout
            if payload["method"] == "getBalance":
                return {"result": {"value": 98_456_789}}
            if payload["method"] == "getSignaturesForAddress":
                return {"result": []}
            raise AssertionError("unexpected RPC method")

        result = self.evaluate(transport=threshold_transport)

        self.assertEqual(result["decision"], "failed")
        self.assertEqual(result["stop_reason"], "loss_threshold")
        self.assertEqual(result["cumulative_loss_lamports"], 25_000_000)

    def test_malformed_or_substituted_evidence_returns_failed(self):
        artifact = self.stage_root / "hot_tokens.json"
        artifact.write_bytes(b"{not-json")
        artifact.chmod(0o600)
        os.utime(artifact, (150, 150))

        result = self.evaluate()

        self.assertEqual(result["decision"], "failed")
        self.assertEqual(result["stop_reason"], "artifact_error")

    def test_interrupted_batch_has_no_resume_interface(self):
        batch_root = self.root / self.batch.relative_root

        self.assertFalse((batch_root / "batch-state.json").exists())
        self.assertFalse(hasattr(mint_auto_diagnoser, "record_stage_result"))
        self.assertFalse(hasattr(mint_auto_diagnoser, "next_stage"))
        self.assertFalse(hasattr(mint_auto_diagnoser, "finalize_batch"))

    def test_batch_result_is_reporting_only(self):
        result = mint_auto_diagnoser.write_batch_result(
            self.root,
            self.batch.batch_id,
            TARGET_MINT,
            "target_positive",
            "timeout",
            ("baseline",),
            "positive",
            "unproven",
        )
        path = self.root / self.batch.relative_root / "batch-result.json"

        self.assertEqual(result, json.loads(path.read_bytes()))
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(result["executed_stage_names"], ["baseline"])
        self.assertEqual(result["cumulative_early_stop_lamports"], 25_000_000)
        self.assertEqual(result["cumulative_loss_limit_lamports"], 30_000_000)
        self.assertFalse((path.parent / "batch-state.json").exists())


if __name__ == "__main__":
    unittest.main()
