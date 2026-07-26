import hashlib
import json
import os
import shutil
import stat
import tempfile
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


if __name__ == "__main__":
    unittest.main()
