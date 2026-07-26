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
                now=lambda: datetime(
                    2026, 7, 26, 12, 30, tzinfo=timezone.utc
                ),
                transport=PrivateWorkspaceTests.transport,
                balance_reader=lambda url, wallet: 123_456_789,
            )
        self.stage = next(stage for stage in self.batch.stages if not stage.skipped)
        self.stage_root = self.root / self.stage.relative_root
        self.started_at = 100
        self.ended_at = 200

    def tearDown(self):
        shutil.rmtree(self.root)

    def write_guard_result(
        self,
        *,
        reason="timeout",
        observed_loss=0,
        child_exit_code=0,
        stage_root=None,
    ):
        del observed_loss
        stage_root = stage_root or self.stage_root
        result = (
            f"reason={reason}\n"
            "duration_seconds=60\n"
            f"child_exit_code={child_exit_code}\n"
            "loss_limit_lamports=30000000\n"
            "early_stop_lamports=25000000\n"
            "log_path=logs/stage.log\n"
        )
        path = stage_root / "guard-result.txt"
        path.write_text(result)
        path.chmod(0o600)
        logs = stage_root / "logs"
        logs.mkdir(exist_ok=True)
        logs.chmod(0o700)
        log = logs / "stage.log"
        log.write_text("")
        log.chmod(0o600)
        return log

    def write_artifact(self, name, value):
        path = self.stage_root / name
        path.write_text(json.dumps(value))
        path.chmod(0o600)
        os.utime(path, (150, 150))
        return path

    @staticmethod
    def hot_tokens(target=None, *, route_length=0):
        entry = {
            "arbs_count": route_length,
            "bridge_mint": "bridge",
            "bridge_pool_ids_info": [],
            "cross_pool_ids_info": [],
            "lookup_table_accounts": ["lut"],
            "mint": target or "unrelated",
            "pool_ids": [f"pool-{index}" for index in range(route_length)],
            "pool_ids_info": [],
            "roi": 0.0,
            "total_fee": 0,
            "total_liquidity_lamports": 0,
            "total_profit": 0,
            "total_volume": 0,
            "txs": [{} for _ in range(route_length)],
        }
        return {"count": 1, "arb_mint_info": [entry]}

    @staticmethod
    def empty_transport(url, payload, timeout):
        del url, timeout
        if payload["method"] == "getBalance":
            return {"result": {"value": 123_456_789}}
        if payload["method"] == "getSignaturesForAddress":
            return {"result": []}
        raise AssertionError("unexpected RPC method")

    @staticmethod
    def landed_transport(url, payload, timeout):
        del url, timeout
        if payload["method"] == "getBalance":
            return {"result": {"value": 123_456_789}}
        if payload["method"] == "getSignaturesForAddress":
            if payload["params"][1].get("before") is not None:
                return {"result": []}
            return {
                "result": [
                    {
                        "signature": "fixture-signature",
                        "blockTime": 150,
                        "confirmationStatus": "finalized",
                    }
                ]
            }
        if payload["method"] == "getTransaction":
            return {
                "result": {
                    "blockTime": 150,
                    "meta": {
                        "err": None,
                        "fee": 5,
                        "preBalances": [100, 0],
                        "postBalances": [95, 0],
                        "preTokenBalances": [],
                        "postTokenBalances": [
                            {
                                "owner": "wallet",
                                "mint": TARGET_MINT,
                                "uiTokenAmount": {"amount": "1"},
                            }
                        ],
                        "innerInstructions": [],
                    },
                    "transaction": {
                        "message": {
                            "accountKeys": ["wallet", TARGET_MINT],
                            "instructions": [],
                        }
                    },
                }
            }
        raise AssertionError("unexpected RPC method")

    def record(self, *, transport=None, guard_exit=0):
        with patch.object(
            mint_auto_diagnoser.zavod_guard,
            "wallet_pubkey",
            return_value="wallet",
        ):
            return mint_auto_diagnoser.record_stage_result(
                self.root,
                self.batch.batch_id,
                self.stage.name,
                guard_exit,
                self.started_at,
                self.ended_at,
                transport=transport or self.empty_transport,
            )

    def test_exact_structural_target_artifact_is_target_positive(self):
        """Dropping exact hot-token identity evidence must stop satisfying the stage."""
        self.write_guard_result()
        self.write_artifact("hot_tokens.json", self.hot_tokens(TARGET_MINT))

        result = self.record()

        self.assertEqual(result["stage_status"], "target_positive")
        self.assertEqual(result["target_artifact_count"], 1)
        self.assertEqual(result["target_filtered_landed"], 0)
        self.assertEqual(result["next_decision"], "stop")
        self.assertEqual(mint_auto_diagnoser.next_stage(self.root, self.batch.batch_id), "stop")

    def test_finalized_target_filtered_landing_is_target_positive(self):
        """Ignoring finalized target-filtered landings would advance past real evidence."""
        self.write_guard_result()

        result = self.record(transport=self.landed_transport)

        self.assertEqual(result["stage_status"], "target_positive")
        self.assertEqual(result["target_artifact_count"], 0)
        self.assertEqual(result["target_filtered_landed"], 1)
        self.assertEqual(result["target_filtered_successful"], 1)

    def test_sender_acceptance_never_counts_as_target_evidence(self):
        """Treating an unrelated send marker as target evidence would stop too early."""
        log = self.write_guard_result()
        log.write_text(
            "Transaction sent successfully\n"
            "Fetched 1 mint list.\n"
        )
        log.chmod(0o600)
        self.write_artifact("hot_tokens.json", self.hot_tokens())

        result = self.record()

        self.assertEqual(result["stage_status"], "no_target")
        self.assertEqual(result["sender_acceptance_count"], 1)
        self.assertEqual(result["target_filtered_landed"], 0)
        self.assertEqual(result["next_decision"], "offchain")
        self.assertEqual(
            mint_auto_diagnoser.next_stage(self.root, self.batch.batch_id),
            "offchain",
        )

    def test_explicit_structural_three_pool_route_is_observed(self):
        """Reducing a three-pool target route to an implicit marker loses hop proof."""
        self.write_guard_result()
        routing = {
            "routes": [
                {
                    "target_mint": TARGET_MINT,
                    "pool_ids": ["pool-a", "pool-b", "pool-c"],
                }
            ]
        }
        self.write_artifact("routing.json", routing)

        result = self.record()

        self.assertEqual(result["route_status"], "target_route_observed")
        self.assertEqual(result["three_hop_status"], "three_hop_observed")

    def test_no_explicit_route_marker_is_three_hop_unproven(self):
        """Inferring hops from sender or target presence would manufacture evidence."""
        self.write_guard_result()
        self.write_artifact("hot_tokens.json", self.hot_tokens(TARGET_MINT))

        result = self.record()

        self.assertEqual(result["three_hop_status"], "three_hop_unproven")

    def test_bad_artifact_terminates_batch_without_retaining_raw_content(self):
        """Accepting malformed generated JSON would let untrusted evidence advance."""
        self.write_guard_result()
        artifact = self.stage_root / "hot_tokens.json"
        artifact.write_bytes(b"{not-json")
        artifact.chmod(0o600)
        os.utime(artifact, (150, 150))

        result = self.record()

        self.assertEqual(result["stage_status"], "artifact_error")
        self.assertEqual(result["next_decision"], "stop")
        result_dir = self.stage_root.parent.parent / "results" / "0-baseline"
        self.assertFalse((result_dir / "generated-hot_tokens.json").exists())

    def test_symlink_wrong_mode_and_stale_artifacts_fail_closed(self):
        """Path substitution, public permissions, or prior-run data must never be evidence."""
        cases = ("symlink", "wrong_mode", "stale")
        for index, case in enumerate(cases):
            with self.subTest(case=case):
                if index:
                    self.tearDown()
                    self.setUp()
                self.write_guard_result()
                path = self.stage_root / "routing.json"
                if case == "symlink":
                    outside = self.root / "outside-artifact"
                    outside.write_text("{}")
                    outside.chmod(0o600)
                    path.symlink_to(outside)
                else:
                    self.write_artifact("routing.json", {"routes": []})
                    if case == "wrong_mode":
                        path.chmod(0o644)
                    else:
                        os.utime(path, (99, 99))

                result = self.record()

                self.assertEqual(result["stage_status"], "artifact_error")
                self.assertEqual(result["next_decision"], "stop")

    def test_artifact_freshness_uses_inclusive_integer_second_buckets(self):
        """Nanoseconds within either run boundary second must not change freshness."""
        cases = (
            ("start-second", 100_000_000_001, "no_target"),
            ("before-start", 99_999_999_999, "artifact_error"),
            ("end-second", 200_999_999_999, "no_target"),
            ("after-end", 201_000_000_000, "artifact_error"),
        )
        for index, (case, mtime_ns, expected_status) in enumerate(cases):
            with self.subTest(case=case):
                if index:
                    self.tearDown()
                    self.setUp()
                self.write_guard_result()
                artifact = self.write_artifact(
                    "routing.json", {"routes": []}
                )
                os.utime(artifact, ns=(mtime_ns, mtime_ns))

                result = self.record()

                self.assertEqual(result["stage_status"], expected_status)

    def test_terminal_and_repeated_stage_transitions_are_rejected(self):
        """Re-entry after a published result would replace evidence or retry automatically."""
        self.write_guard_result()
        first = self.record()
        self.assertEqual(first["stage_status"], "no_target")

        with self.assertRaises(mint_auto_diagnoser.DiagnoserError):
            self.record()

        state_path = (
            self.root
            / "state"
            / "auto-diagnose-runs"
            / self.batch.batch_id
            / "batch-state.json"
        )
        state = json.loads(state_path.read_text())
        state["status"] = "prepared"
        state_path.write_text(json.dumps(state))
        state_path.chmod(0o600)
        with self.assertRaises(mint_auto_diagnoser.DiagnoserError):
            mint_auto_diagnoser.next_stage(self.root, self.batch.batch_id)

    def test_interrupted_attempt_is_terminal_before_publication(self):
        """A recorder crash must not make the same live stage selectable again."""
        self.write_guard_result()
        with patch.object(
            mint_auto_diagnoser,
            "_artifact_evidence",
            side_effect=OSError("fixture interruption"),
        ):
            with self.assertRaises(OSError):
                self.record()

        self.assertEqual(
            mint_auto_diagnoser.next_stage(
                self.root, self.batch.batch_id
            ),
            "stop",
        )
        with self.assertRaises(mint_auto_diagnoser.DiagnoserError):
            self.record()

    def test_validation_failure_after_live_attempt_is_terminal(self):
        """Invalid post-run evidence must reserve the attempt before validation fails."""
        self.write_guard_result()
        (self.stage_root / "guard-result.txt").chmod(0o644)

        with self.assertRaises(mint_auto_diagnoser.DiagnoserError):
            self.record()

        self.assertEqual(
            mint_auto_diagnoser.next_stage(
                self.root, self.batch.batch_id
            ),
            "stop",
        )

    def test_hard_crash_pending_directory_is_discarded_fail_closed(self):
        """A partial owned publication must not strand state or become evidence."""
        batch_root = (
            self.root
            / "state"
            / "auto-diagnose-runs"
            / self.batch.batch_id
        )
        state_path = batch_root / "batch-state.json"
        state = json.loads(state_path.read_text())
        state["status"] = "running"
        state["next_stage"] = "stop"
        state_path.write_text(json.dumps(state))
        state_path.chmod(0o600)
        results = batch_root / "results"
        results.mkdir(mode=0o700)
        pending = results / (".pending-0-baseline-" + "a" * 24)
        pending.mkdir(mode=0o700)
        partial = pending / "generated-hot_tokens.json"
        partial.write_text("{}")
        partial.chmod(0o600)

        self.assertEqual(
            mint_auto_diagnoser.next_stage(
                self.root, self.batch.batch_id
            ),
            "stop",
        )
        self.assertFalse(pending.exists())
        finalized = mint_auto_diagnoser.finalize_batch(
            self.root, self.batch.batch_id
        )
        self.assertEqual(finalized["status"], "failed")

    def test_published_result_recovers_without_replacing_or_rerunning(self):
        """A crash after rename must commit that result instead of rerunning evidence."""
        self.write_guard_result()
        real_store = mint_auto_diagnoser._store_batch_state
        calls = []

        def fail_final_store(batch_fd, previous, replacement):
            calls.append(replacement["status"])
            if len(calls) == 2:
                raise OSError("fixture state interruption")
            return real_store(batch_fd, previous, replacement)

        with patch.object(
            mint_auto_diagnoser,
            "_store_batch_state",
            side_effect=fail_final_store,
        ):
            with self.assertRaises(OSError):
                self.record()

        self.assertEqual(calls, ["running", "running"])
        self.assertEqual(
            mint_auto_diagnoser.next_stage(
                self.root, self.batch.batch_id
            ),
            "offchain",
        )
        with self.assertRaises(mint_auto_diagnoser.DiagnoserError):
            self.record()

    def test_post_rename_directory_fsync_error_keeps_published_result(self):
        """A durability-report error after rename must not create state/result skew."""
        self.write_guard_result()
        real_fsync = mint_auto_diagnoser.os.fsync
        final_result = (
            self.stage_root.parent.parent
            / "results"
            / "0-baseline"
        )

        def fail_post_rename(descriptor):
            if final_result.exists():
                try:
                    target = os.readlink(
                        f"/proc/self/fd/{descriptor}"
                    )
                except OSError:
                    target = ""
                if target.endswith("/results"):
                    raise OSError("fixture directory fsync failure")
            return real_fsync(descriptor)

        with patch.object(
            mint_auto_diagnoser.os,
            "fsync",
            side_effect=fail_post_rename,
        ):
            result = self.record()

        self.assertEqual(result["stage_status"], "no_target")
        self.assertEqual(
            mint_auto_diagnoser.next_stage(
                self.root, self.batch.batch_id
            ),
            "offchain",
        )

    def test_exception_after_rename_recovers_published_result(self):
        """An asynchronous post-rename failure must commit, not skew, fixed evidence."""
        self.write_guard_result()
        real_publish = mint_auto_diagnoser._publish_stage_result

        def publish_then_interrupt(*args, **kwargs):
            real_publish(*args, **kwargs)
            raise KeyboardInterrupt

        with patch.object(
            mint_auto_diagnoser,
            "_publish_stage_result",
            side_effect=publish_then_interrupt,
        ):
            with self.assertRaises(KeyboardInterrupt):
                self.record()

        self.assertEqual(
            mint_auto_diagnoser.next_stage(
                self.root, self.batch.batch_id
            ),
            "offchain",
        )
        with self.assertRaises(mint_auto_diagnoser.DiagnoserError):
            self.record()

    def test_rename_then_interrupt_preserves_published_result(self):
        """A rename wrapper that raises after moving must not erase final evidence."""
        self.write_guard_result()
        real_rename = mint_auto_diagnoser.os.rename

        def rename_then_interrupt(*args, **kwargs):
            real_rename(*args, **kwargs)
            raise KeyboardInterrupt

        with patch.object(
            mint_auto_diagnoser.os,
            "rename",
            side_effect=rename_then_interrupt,
        ):
            with self.assertRaises(KeyboardInterrupt):
                self.record()

        manifest_path = (
            self.stage_root.parent.parent
            / "results"
            / "0-baseline"
            / "stage-manifest.json"
        )
        manifest = json.loads(manifest_path.read_text())
        self.assertEqual(manifest["stage_name"], "baseline")
        self.assertEqual(
            mint_auto_diagnoser.next_stage(
                self.root, self.batch.batch_id
            ),
            "offchain",
        )
        with self.assertRaises(mint_auto_diagnoser.DiagnoserError):
            mint_auto_diagnoser.finalize_batch(
                self.root, self.batch.batch_id
            )
        with self.assertRaises(mint_auto_diagnoser.DiagnoserError):
            self.record()

    def test_threshold_cleanup_rpc_and_last_stage_stop(self):
        """Every safety terminal must stop instead of selecting another live stage."""
        for index, (reason, loss) in enumerate(
            (
                ("loss_threshold", 25_000_000),
                ("cleanup_failed", 0),
                ("rpc_error", 0),
            )
        ):
            with self.subTest(reason=reason):
                if index:
                    self.tearDown()
                    self.setUp()
                self.write_guard_result(reason=reason, observed_loss=loss)
                result = self.record()
                self.assertEqual(result["next_decision"], "stop")

    def test_crash_and_safety_reason_override_target_evidence(self):
        """A crash or integrity stop must never become a target-positive retry decision."""
        self.write_guard_result(
            reason="child_exit", child_exit_code=2
        )
        result = self.record()
        self.assertEqual(result["stage_status"], "failed")
        self.assertEqual(result["next_decision"], "stop")

        self.tearDown()
        self.setUp()
        self.write_guard_result(
            reason="operator_signal", child_exit_code=-15
        )
        self.write_artifact(
            "hot_tokens.json", self.hot_tokens(TARGET_MINT)
        )
        result = self.record()
        self.assertEqual(result["stage_status"], "failed")
        self.assertEqual(result["target_artifact_count"], 1)
        self.assertEqual(result["next_decision"], "stop")

    def test_read_only_balance_sets_cumulative_loss_and_rpc_failure_stops(self):
        """Ignoring the immutable baseline or a failed balance read would overrun safety."""
        self.write_guard_result()

        def lower_balance(url, payload, timeout):
            del url, timeout
            if payload["method"] == "getBalance":
                return {"result": {"value": 100_000_000}}
            if payload["method"] == "getSignaturesForAddress":
                return {"result": []}
            raise AssertionError("unexpected RPC method")

        result = self.record(transport=lower_balance)
        self.assertEqual(
            result["cumulative_observed_loss_lamports"],
            23_456_789,
        )

        self.tearDown()
        self.setUp()
        self.write_guard_result()

        def failed_balance(url, payload, timeout):
            del url, payload, timeout
            raise OSError("fixture unavailable")

        result = self.record(transport=failed_balance)
        self.assertEqual(result["stage_status"], "rpc_error")
        self.assertEqual(result["next_decision"], "stop")

    def test_last_non_skipped_stage_exhausts_without_retry(self):
        """Selecting a stage after the prepared sequence would create an automatic retry."""
        result = None
        with patch.object(
            mint_auto_diagnoser.zavod_guard,
            "wallet_pubkey",
            return_value="wallet",
        ):
            for stage in (
                stage for stage in self.batch.stages if not stage.skipped
            ):
                stage_root = self.root / stage.relative_root
                self.write_guard_result(stage_root=stage_root)
                result = mint_auto_diagnoser.record_stage_result(
                    self.root,
                    self.batch.batch_id,
                    stage.name,
                    0,
                    self.started_at,
                    self.ended_at,
                    transport=self.empty_transport,
                )

        self.assertIsNotNone(result)
        self.assertEqual(result["stage_name"], "pool_liquidity")
        self.assertEqual(result["stage_status"], "no_target")
        self.assertEqual(result["next_decision"], "stop")
        self.assertEqual(
            mint_auto_diagnoser.next_stage(
                self.root, self.batch.batch_id
            ),
            "stop",
        )

    def test_finalize_reuses_result_and_decline_is_one_way(self):
        """Repeated finalization must reuse one result and keep decline terminal."""
        self.write_guard_result()
        self.write_artifact("hot_tokens.json", self.hot_tokens(TARGET_MINT))
        self.record()

        result = mint_auto_diagnoser.finalize_batch(
            self.root, self.batch.batch_id
        )
        result_path = (
            self.root
            / mint_auto_diagnoser.batch_result_path(self.batch.batch_id)
        )
        self.assertEqual(result["status"], "target_positive")
        self.assertEqual(stat.S_IMODE(result_path.stat().st_mode), 0o600)
        self.assertFalse(
            (self.root / "state" / ".mint-auto-diagnose-active").exists()
        )
        published = result_path.read_bytes()
        published_inode = result_path.stat().st_ino
        repeated = mint_auto_diagnoser.finalize_batch(
            self.root, self.batch.batch_id
        )
        self.assertEqual(repeated, result)
        self.assertEqual(result_path.read_bytes(), published)
        self.assertEqual(result_path.stat().st_ino, published_inode)

        self.tearDown()
        self.setUp()
        declined = mint_auto_diagnoser.finalize_batch(
            self.root, self.batch.batch_id
        )
        self.assertEqual(declined["status"], "declined")
        self.assertEqual(
            mint_auto_diagnoser.next_stage(
                self.root, self.batch.batch_id
            ),
            "stop",
        )

    def test_finalize_recovers_after_post_publication_interrupt(self):
        """Losing marker removal after durable publication must not strand finalization."""
        self.write_guard_result()
        self.write_artifact("hot_tokens.json", self.hot_tokens(TARGET_MINT))
        self.record()
        result_path = (
            self.root
            / mint_auto_diagnoser.batch_result_path(self.batch.batch_id)
        )
        active_path = (
            self.root / "state" / mint_auto_diagnoser.ACTIVE_MARKER
        )

        with patch.object(
            mint_auto_diagnoser,
            "_remove_active_marker",
            side_effect=KeyboardInterrupt,
        ):
            with self.assertRaises(KeyboardInterrupt):
                mint_auto_diagnoser.finalize_batch(
                    self.root, self.batch.batch_id
                )

        published = result_path.read_bytes()
        published_inode = result_path.stat().st_ino
        self.assertTrue(active_path.exists())

        recovered = mint_auto_diagnoser.finalize_batch(
            self.root, self.batch.batch_id
        )

        self.assertEqual(
            recovered,
            {
                "status": "target_positive",
                "completed_stages": ["baseline"],
                "cumulative_observed_loss_lamports": 0,
            },
        )
        self.assertEqual(result_path.read_bytes(), published)
        self.assertEqual(result_path.stat().st_ino, published_inode)
        self.assertFalse(active_path.exists())

    def test_finalize_fsyncs_result_directory_before_marker_release(self):
        """Marker release before the result directory fsync could lose both states."""
        self.write_guard_result()
        self.write_artifact("hot_tokens.json", self.hot_tokens(TARGET_MINT))
        self.record()
        batch_root = (
            self.root
            / "state"
            / "auto-diagnose-runs"
            / self.batch.batch_id
        )
        result_path = batch_root / "batch-result.json"
        batch_identity = (
            batch_root.stat().st_dev,
            batch_root.stat().st_ino,
        )
        events = []
        real_fsync = mint_auto_diagnoser.os.fsync
        real_unlink = mint_auto_diagnoser.os.unlink
        real_rename = mint_auto_diagnoser.os.rename

        def track_fsync(descriptor):
            info = os.fstat(descriptor)
            if (
                (info.st_dev, info.st_ino) == batch_identity
                and result_path.exists()
            ):
                events.append("result-directory-fsync")
            return real_fsync(descriptor)

        def track_unlink(name, *args, **kwargs):
            if str(name).startswith(
                mint_auto_diagnoser.ACTIVE_MARKER
            ):
                events.append("marker-release")
            return real_unlink(name, *args, **kwargs)

        def track_rename(source, destination, *args, **kwargs):
            if source == mint_auto_diagnoser.ACTIVE_MARKER:
                events.append("marker-release")
            return real_rename(
                source, destination, *args, **kwargs
            )

        with patch.object(
            mint_auto_diagnoser.os,
            "fsync",
            side_effect=track_fsync,
        ), patch.object(
            mint_auto_diagnoser.os,
            "unlink",
            side_effect=track_unlink,
        ), patch.object(
            mint_auto_diagnoser.os,
            "rename",
            side_effect=track_rename,
        ):
            mint_auto_diagnoser.finalize_batch(
                self.root, self.batch.batch_id
            )

        self.assertIn("result-directory-fsync", events)
        self.assertLess(
            events.index("result-directory-fsync"),
            events.index("marker-release"),
        )

    def test_result_directory_fsync_failure_keeps_active_marker(self):
        """A failed publication boundary must stop before marker release."""
        self.write_guard_result()
        self.write_artifact("hot_tokens.json", self.hot_tokens(TARGET_MINT))
        self.record()
        batch_root = (
            self.root
            / "state"
            / "auto-diagnose-runs"
            / self.batch.batch_id
        )
        result_path = batch_root / "batch-result.json"
        active_path = (
            self.root / "state" / mint_auto_diagnoser.ACTIVE_MARKER
        )
        batch_identity = (
            batch_root.stat().st_dev,
            batch_root.stat().st_ino,
        )
        real_fsync = mint_auto_diagnoser.os.fsync

        def fail_result_directory_fsync(descriptor):
            info = os.fstat(descriptor)
            if (
                (info.st_dev, info.st_ino) == batch_identity
                and result_path.exists()
            ):
                raise OSError("fixture publication fsync failure")
            return real_fsync(descriptor)

        with patch.object(
            mint_auto_diagnoser.os,
            "fsync",
            side_effect=fail_result_directory_fsync,
        ):
            with self.assertRaises(OSError):
                mint_auto_diagnoser.finalize_batch(
                    self.root, self.batch.batch_id
                )

        self.assertTrue(result_path.exists())
        self.assertEqual(
            active_path.read_text(), self.batch.batch_id + "\n"
        )

    def test_marker_substitution_after_identity_check_is_preserved(self):
        """A replacement marker must not be unlinked through a stale identity check."""
        self.write_guard_result()
        self.write_artifact("hot_tokens.json", self.hot_tokens(TARGET_MINT))
        self.record()
        active_path = (
            self.root / "state" / mint_auto_diagnoser.ACTIVE_MARKER
        )
        replacement = b"20260726T123001Z\n"
        real_stat = mint_auto_diagnoser.os.stat
        swapped = False

        def swap_after_identity_check(path, *args, **kwargs):
            nonlocal swapped
            info = real_stat(path, *args, **kwargs)
            if (
                not swapped
                and path == mint_auto_diagnoser.ACTIVE_MARKER
                and kwargs.get("dir_fd") is not None
            ):
                swapped = True
                os.unlink(path, dir_fd=kwargs["dir_fd"])
                descriptor = os.open(
                    path,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                    dir_fd=kwargs["dir_fd"],
                )
                try:
                    os.write(descriptor, replacement)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            return info

        with patch.object(
            mint_auto_diagnoser.os,
            "stat",
            side_effect=swap_after_identity_check,
        ):
            with self.assertRaises(mint_auto_diagnoser.DiagnoserError):
                mint_auto_diagnoser.finalize_batch(
                    self.root, self.batch.batch_id
                )

        self.assertTrue(swapped)
        self.assertEqual(active_path.read_bytes(), replacement)

    def test_finalize_rejects_mismatched_existing_result(self):
        """An existing result that disagrees with state must remain untouched and active."""
        self.write_guard_result()
        self.write_artifact("hot_tokens.json", self.hot_tokens(TARGET_MINT))
        self.record()
        result_path = (
            self.root
            / mint_auto_diagnoser.batch_result_path(self.batch.batch_id)
        )
        mismatch = (
            b'{"completed_stages":["baseline"],'
            b'"cumulative_observed_loss_lamports":1,'
            b'"status":"target_positive"}\n'
        )
        result_path.write_bytes(mismatch)
        result_path.chmod(0o600)
        published_inode = result_path.stat().st_ino
        active_path = (
            self.root / "state" / mint_auto_diagnoser.ACTIVE_MARKER
        )

        with self.assertRaises(mint_auto_diagnoser.DiagnoserError):
            mint_auto_diagnoser.finalize_batch(
                self.root, self.batch.batch_id
            )

        self.assertEqual(result_path.read_bytes(), mismatch)
        self.assertEqual(result_path.stat().st_ino, published_inode)
        self.assertEqual(
            active_path.read_text(), self.batch.batch_id + "\n"
        )

    def test_finalize_rejects_duplicate_key_existing_result(self):
        """Duplicate JSON keys must not satisfy the fixed batch-result schema."""
        self.write_guard_result()
        self.write_artifact("hot_tokens.json", self.hot_tokens(TARGET_MINT))
        self.record()
        result_path = (
            self.root
            / mint_auto_diagnoser.batch_result_path(self.batch.batch_id)
        )
        malformed = (
            b'{"completed_stages":["baseline"],'
            b'"cumulative_observed_loss_lamports":0,'
            b'"status":"target_positive",'
            b'"status":"target_positive"}\n'
        )
        result_path.write_bytes(malformed)
        result_path.chmod(0o600)
        active_path = (
            self.root / "state" / mint_auto_diagnoser.ACTIVE_MARKER
        )

        with self.assertRaises(mint_auto_diagnoser.DiagnoserError):
            mint_auto_diagnoser.finalize_batch(
                self.root, self.batch.batch_id
            )

        self.assertEqual(result_path.read_bytes(), malformed)
        self.assertEqual(
            active_path.read_text(), self.batch.batch_id + "\n"
        )

    def test_manifest_is_fixed_private_and_contains_no_protected_values(self):
        """Adding raw guard, config, RPC, wallet, or signature fields must fail."""
        self.write_guard_result()
        result = self.record()
        manifest_path = (
            self.stage_root.parent.parent
            / "results"
            / "0-baseline"
            / "stage-manifest.json"
        )
        rendered = manifest_path.read_text()

        self.assertEqual(stat.S_IMODE(manifest_path.stat().st_mode), 0o600)
        self.assertEqual(result, json.loads(rendered))
        self.assertNotIn("signature", rendered.lower())
        self.assertNotIn("://", rendered)
        self.assertNotIn("private_key", rendered)
        self.assertNotIn("wallet", rendered.lower())
        self.assertNotIn("test-wallet", rendered)


if __name__ == "__main__":
    unittest.main()
