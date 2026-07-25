import contextlib
import copy
import hashlib
import io
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from scripts import mint_runner


TARGET_MINT = "So11111111111111111111111111111111111111112"
CONTROL_MINT = "11111111111111111111111111111111"
UNRELATED_MINT = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
DIAGNOSTIC_SECRET = "fixture-wallet-material-never-render"
DIAGNOSTIC_SOURCE = (
    b"# selector fixture; formatting and comments must survive\n"
    b"[wallet]\n"
    b'private_key = "fixture-wallet-material-never-render"\n'
    b"[rpc]\n"
    b'url = "https://fixture.invalid/fixture-uuid"\n'
    b"\n"
    b"[auto]\n"
    b"enabled = true\n"
    b"force_two_mints = true # preserve this comment\n"
    b"\n"
    b"[auto.filters]\n"
    b"limit = 17\n"
    b"\n"
    b"[bot]\n"
    b"merge_mints = true\n"
    b"ignore_offchain_bots = true\n"
    b"\n"
    b"[unchanged]\n"
    b'value = "keep exactly"\n'
)
DIAGNOSTIC_D0 = (
    b"# selector fixture; formatting and comments must survive\n"
    b"[wallet]\n"
    b'private_key = "fixture-wallet-material-never-render"\n'
    b"[rpc]\n"
    b'url = "https://fixture.invalid/fixture-uuid"\n'
    b"\n"
    b"[auto]\n"
    b"enabled = true\n"
    b"force_two_mints = false # preserve this comment\n"
    b"\n"
    b"[auto.filters]\n"
    b"limit = 1\n"
    b"\n"
    b"[bot]\n"
    b"merge_mints = false\n"
    b"ignore_offchain_bots = true\n"
    b"\n"
    b"[unchanged]\n"
    b'value = "keep exactly"\n'
)
DIAGNOSTIC_D1 = DIAGNOSTIC_D0.replace(
    b"ignore_offchain_bots = true",
    b"ignore_offchain_bots = false",
)


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
                    "data": {
                        "parsed": {
                            "type": "mint",
                            "info": {"isInitialized": True},
                        }
                    },
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

    def install_diagnostic_source(self, data=DIAGNOSTIC_SOURCE):
        (self.root / "config.toml").write_bytes(data)
        (self.root / "config.toml").chmod(0o600)
        self.original_config = data

    def diagnostic_path(self, prepared):
        return self.root / prepared.diagnostic_config

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
            {
                "executable": False,
                "owner": mint_runner.TOKEN_PROGRAM_ID,
                "data": {"parsed": {"type": "account"}},
            },
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

    def test_uninitialized_token_and_token_2022_mints_are_rejected(self):
        for owner in (
            mint_runner.TOKEN_PROGRAM_ID,
            mint_runner.TOKEN_2022_PROGRAM_ID,
        ):
            with self.subTest(owner=owner):
                def transport(url, payload, timeout, owner=owner):
                    del url, payload, timeout
                    return {
                        "result": {
                            "value": {
                                "executable": False,
                                "owner": owner,
                                "data": {
                                    "parsed": {
                                        "type": "mint",
                                        "info": {"isInitialized": False},
                                    }
                                },
                            }
                        }
                    }

                with self.assertRaises(mint_runner.RunnerError):
                    mint_runner.validate_mint_account(
                        "https://secret.invalid",
                        TARGET_MINT,
                        transport,
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

    def test_prepare_uses_shared_environment_expansion_for_rpc_and_wallet(self):
        rpc_variable = "ZAVOD_TEST_RPC_VALUE"
        wallet_variable = "ZAVOD_TEST_WALLET_VALUE"
        expanded_rpc = "mock-rpc-transport-value"
        expanded_wallet = "mock-wallet-config-value"
        (self.root / "config.toml").write_text(
            "[auto]\n"
            "enabled = true\n"
            "[rpc]\n"
            f'url = "${{{rpc_variable}}}"\n'
            "[wallet]\n"
            f'private_key = "${{{wallet_variable}}}"\n'
        )
        (self.root / "config.toml").chmod(0o600)
        observed = {}

        def transport(url, payload, timeout):
            observed["rpc"] = url
            return self.valid_transport(url, payload, timeout)

        with patch.dict(
            os.environ,
            {
                rpc_variable: expanded_rpc,
                wallet_variable: expanded_wallet,
            },
            clear=False,
        ):
            prepared = self.prepare(transport=transport)

        self.assertEqual(observed["rpc"], expanded_rpc)
        rendered = json.dumps(prepared.safe_summary(), sort_keys=True)
        self.assertNotIn(expanded_rpc, rendered)
        self.assertNotIn(expanded_wallet, rendered)
        mint_runner.restore_run(self.root, prepared.run_id)

    def test_prepare_rejects_enabled_static_markets_before_rpc_or_snapshot(self):
        (self.root / "config.toml").write_text(
            "[auto]\n"
            "enabled = true\n"
            "[rpc]\n"
            'url = "mock-rpc"\n'
            "[[markets_file]]\n"
            "enabled = true\n"
            'path = "markets.toml"\n'
        )
        (self.root / "config.toml").chmod(0o600)
        rpc_called = False

        def transport(url, payload, timeout):
            del url, payload, timeout
            nonlocal rpc_called
            rpc_called = True
            return {}

        with self.assertRaises(mint_runner.RunnerError):
            self.prepare(transport=transport)

        self.assertFalse(rpc_called)
        self.assertFalse((self.root / "state" / ".mint-run-active").exists())

    def test_guarded_preflight_uses_single_mint_auto_profile(self):
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=(
                "preflight=ok\n"
                "cli_version=0.2.2\n"
                "loss_limit_lamports=30000000\n"
                "early_stop_lamports=25000000\n"
            ),
            stderr="",
        )
        with patch.object(
            mint_runner.subprocess,
            "run",
            return_value=completed,
        ) as runner:
            mint_runner._run_preflight(self.root)

        self.assertIn(
            ["--profile", "single-mint-auto"],
            [
                runner.call_args.args[0][index:index + 2]
                for index in range(len(runner.call_args.args[0]) - 1)
            ],
        )

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

    def test_validate_live_state_requires_exact_marker_and_single_mint(self):
        prepared = self.prepare()
        marker = self.root / "state" / ".mint-run-active"
        tokens = self.root / "tokens.toml"
        expected_marker = marker.read_bytes()
        expected_tokens = tokens.read_bytes()

        mint_runner.validate_live_state(self.root, prepared.run_id)

        marker.write_bytes(b"wrong-run-id\n")
        marker.chmod(0o600)
        with self.assertRaises(mint_runner.RunnerError):
            mint_runner.validate_live_state(self.root, prepared.run_id)
        marker.write_bytes(expected_marker)
        marker.chmod(0o600)

        tokens.write_text(f'tokens = ["{TARGET_MINT}", "extra"]\n')
        tokens.chmod(0o600)
        with self.assertRaises(mint_runner.RunnerError):
            mint_runner.validate_live_state(self.root, prepared.run_id)
        tokens.write_bytes(expected_tokens)
        tokens.chmod(0o600)

        mint_runner.restore_run(self.root, prepared.run_id)

    def test_validate_live_cli_is_silent_on_success_and_generic_on_failure(self):
        prepared = self.prepare()
        stdout = io.StringIO()
        stderr = io.StringIO()
        argv = [
            "--root",
            str(self.root),
            "validate-live",
            "--run-id",
            prepared.run_id,
        ]

        with (
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            self.assertEqual(mint_runner.main(argv), 0)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")

        protected = "https://secret.invalid/must-not-survive"
        (self.root / "tokens.toml").write_text(protected)
        (self.root / "tokens.toml").chmod(0o600)
        with (
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            self.assertEqual(mint_runner.main(argv), 1)
        self.assertNotIn(protected, stderr.getvalue())
        self.assertIn("error=operation failed", stderr.getvalue())

        mint_runner.restore_run(self.root, prepared.run_id)

    def test_restore_is_idempotent_and_byte_exact(self):
        prepared = self.prepare()
        (self.root / "config.toml").write_text("changed")
        (self.root / "tokens.toml").write_text("changed")
        mint_runner.restore_run(self.root, prepared.run_id)
        mint_runner.restore_run(self.root, prepared.run_id)
        self.assertEqual((self.root / "config.toml").read_bytes(), self.original_config)
        self.assertEqual((self.root / "tokens.toml").read_bytes(), self.original_tokens)
        self.assertEqual(stat.S_IMODE((self.root / "config.toml").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.root / "tokens.toml").stat().st_mode), 0o600)
        self.assertFalse((self.root / "state" / ".mint-run-active").exists())

    def test_restore_validates_all_required_backups_before_modifying_live_files(self):
        prepared = self.prepare()
        changed_config = b"changed-config"
        changed_tokens = b"changed-tokens"
        (self.root / "config.toml").write_bytes(changed_config)
        (self.root / "tokens.toml").write_bytes(changed_tokens)
        (prepared.backup_dir / "tokens.toml").unlink()

        with self.assertRaises(mint_runner.RunnerError):
            mint_runner.restore_run(self.root, prepared.run_id)

        self.assertEqual((self.root / "config.toml").read_bytes(), changed_config)
        self.assertEqual((self.root / "tokens.toml").read_bytes(), changed_tokens)
        self.assertTrue((self.root / "state" / ".mint-run-active").exists())
        self.assertFalse((prepared.backup_dir / "restored").exists())

    def test_restore_rejects_corrupt_or_inconsistent_metadata_without_changes(self):
        for corruption in ("corrupt", "wrong-run-id"):
            with self.subTest(corruption=corruption):
                prepared = self.prepare(
                    now=lambda: datetime(
                        2026,
                        7,
                        24,
                        18,
                        30,
                        1 if corruption == "corrupt" else 2,
                        tzinfo=timezone.utc,
                    )
                )
                changed_config = f"changed-{corruption}".encode()
                (self.root / "config.toml").write_bytes(changed_config)
                metadata_path = prepared.backup_dir / "metadata.json"
                if corruption == "corrupt":
                    metadata_path.write_text("{")
                else:
                    metadata = json.loads(metadata_path.read_text())
                    metadata["run_id"] = "20260724T183059Z"
                    metadata_path.write_text(json.dumps(metadata))

                try:
                    with self.assertRaises(mint_runner.RunnerError):
                        mint_runner.restore_run(self.root, prepared.run_id)

                    self.assertEqual(
                        (self.root / "config.toml").read_bytes(), changed_config
                    )
                    self.assertTrue(
                        (self.root / "state" / ".mint-run-active").exists()
                    )
                    self.assertFalse((prepared.backup_dir / "restored").exists())
                finally:
                    (self.root / "config.toml").write_bytes(self.original_config)
                    (self.root / "config.toml").chmod(0o600)
                    (self.root / "tokens.toml").write_bytes(self.original_tokens)
                    (self.root / "tokens.toml").chmod(0o600)
                    marker = self.root / "state" / ".mint-run-active"
                    if marker.exists():
                        marker.unlink()

    def test_restore_requires_every_optional_backup_marked_present(self):
        (self.root / "hot_tokens.json").write_bytes(b"old-hot")
        prepared = self.prepare()
        changed_config = b"changed-config"
        (self.root / "config.toml").write_bytes(changed_config)
        (prepared.backup_dir / "hot_tokens.json").unlink()

        with self.assertRaises(mint_runner.RunnerError):
            mint_runner.restore_run(self.root, prepared.run_id)

        self.assertEqual((self.root / "config.toml").read_bytes(), changed_config)
        self.assertFalse((self.root / "hot_tokens.json").exists())
        self.assertTrue((self.root / "state" / ".mint-run-active").exists())
        self.assertFalse((prepared.backup_dir / "restored").exists())

    def test_restore_rejects_corrupted_backup_bytes_before_live_changes(self):
        prepared = self.prepare()
        changed_config = b"changed-config"
        changed_tokens = b"changed-tokens"
        (self.root / "config.toml").write_bytes(changed_config)
        (self.root / "tokens.toml").write_bytes(changed_tokens)
        (prepared.backup_dir / "config.toml").write_bytes(b"corrupted-backup")

        with self.assertRaises(mint_runner.RunnerError):
            mint_runner.restore_run(self.root, prepared.run_id)

        self.assertEqual((self.root / "config.toml").read_bytes(), changed_config)
        self.assertEqual((self.root / "tokens.toml").read_bytes(), changed_tokens)
        self.assertTrue((self.root / "state" / ".mint-run-active").exists())
        self.assertFalse((prepared.backup_dir / "restored").exists())

    def test_preflight_failure_restores_before_raising(self):
        with self.assertRaises(mint_runner.RunnerError):
            self.prepare(
                preflight_runner=lambda root: (_ for _ in ()).throw(
                    mint_runner.RunnerError("preflight failed")
                )
            )
        self.assertEqual((self.root / "config.toml").read_bytes(), self.original_config)
        self.assertEqual((self.root / "tokens.toml").read_bytes(), self.original_tokens)

    def test_wrong_preflight_version_restores_every_file_byte_exact(self):
        (self.root / "hot_tokens.json").write_bytes(b"\x00old-hot\n")
        original_hot = (self.root / "hot_tokens.json").read_bytes()

        with self.assertRaises(mint_runner.RunnerError):
            self.prepare(
                preflight_runner=lambda root: {
                    "preflight": "ok",
                    "cli_version": "9.9.9",
                }
            )

        self.assertEqual((self.root / "config.toml").read_bytes(), self.original_config)
        self.assertEqual((self.root / "tokens.toml").read_bytes(), self.original_tokens)
        self.assertEqual((self.root / "hot_tokens.json").read_bytes(), original_hot)
        self.assertFalse((self.root / "routing.json").exists())
        self.assertFalse((self.root / "state" / ".mint-run-active").exists())

    def test_every_preflight_contract_mismatch_restores_before_raising(self):
        cases = (
            {"preflight": "wrong"},
            {"cli_version": "9.9.9"},
            {"loss_limit_lamports": 29_999_999},
            {"early_stop_lamports": 24_999_999},
            {"loss_limit_lamports": True},
            {"early_stop_lamports": "not-an-integer"},
        )
        valid = {
            "preflight": "ok",
            "cli_version": "0.2.2",
            "loss_limit_lamports": 30_000_000,
            "early_stop_lamports": 25_000_000,
        }
        for index, mismatch in enumerate(cases, start=1):
            with self.subTest(mismatch=mismatch):
                preflight = {**valid, **mismatch}
                try:
                    with self.assertRaises(mint_runner.RunnerError):
                        self.prepare(
                            preflight_runner=lambda root, result=preflight: result,
                            now=lambda second=index: datetime(
                                2026, 7, 24, 18, 31, second, tzinfo=timezone.utc
                            ),
                        )
                    self.assertEqual(
                        (self.root / "config.toml").read_bytes(),
                        self.original_config,
                    )
                    self.assertEqual(
                        (self.root / "tokens.toml").read_bytes(),
                        self.original_tokens,
                    )
                    self.assertFalse(
                        (self.root / "state" / ".mint-run-active").exists()
                    )
                finally:
                    (self.root / "config.toml").write_bytes(self.original_config)
                    (self.root / "config.toml").chmod(0o600)
                    (self.root / "tokens.toml").write_bytes(self.original_tokens)
                    (self.root / "tokens.toml").chmod(0o600)
                    marker = self.root / "state" / ".mint-run-active"
                    if marker.exists():
                        marker.unlink()

    def test_safe_summary_uses_parsed_verified_preflight_limits(self):
        prepared = self.prepare(
            preflight_runner=lambda root: {
                "preflight": "ok",
                "cli_version": "0.2.2",
                "loss_limit_lamports": "30000000",
                "early_stop_lamports": "25000000",
            }
        )

        summary = prepared.safe_summary()

        self.assertEqual(prepared.loss_limit_lamports, 30_000_000)
        self.assertEqual(prepared.early_stop_lamports, 25_000_000)
        self.assertEqual(summary["loss_limit_lamports"], 30_000_000)
        self.assertEqual(summary["early_stop_lamports"], 25_000_000)

    def test_keyboard_interrupt_after_mutation_restores_before_propagating(self):
        with self.assertRaises(KeyboardInterrupt):
            self.prepare(
                preflight_runner=lambda root: (_ for _ in ()).throw(
                    KeyboardInterrupt()
                )
            )

        self.assertEqual((self.root / "config.toml").read_bytes(), self.original_config)
        self.assertEqual((self.root / "tokens.toml").read_bytes(), self.original_tokens)
        self.assertFalse((self.root / "state" / ".mint-run-active").exists())

    def test_prepare_cli_converts_sigterm_and_restores_previous_handler(self):
        previous_handler = object()
        handlers = []

        def fake_signal(signum, handler):
            handlers.append((signum, handler))

        def interrupted_prepare(*args, **kwargs):
            handlers[0][1](signal.SIGTERM, None)

        stderr = io.StringIO()
        with patch.object(signal, "getsignal", return_value=previous_handler):
            with patch.object(signal, "signal", side_effect=fake_signal):
                with patch.object(
                    mint_runner, "prepare_run", side_effect=interrupted_prepare
                ):
                    with contextlib.redirect_stderr(stderr):
                        status = mint_runner.main(
                            [
                                "--root",
                                str(self.root),
                                "prepare",
                                "--mint",
                                TARGET_MINT,
                            ]
                        )

        self.assertEqual(status, 130)
        self.assertEqual(handlers[-1], (signal.SIGTERM, previous_handler))
        self.assertTrue("error=operation interrupted" in stderr.getvalue())

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

    def test_diagnostic_config_changes_only_cardinality_controls(self):
        self.install_diagnostic_source()
        production_hash = hashlib.sha256(DIAGNOSTIC_SOURCE).hexdigest()

        prepared = self.prepare(
            diagnostic="d0",
            transport=lambda *args: self.fail("diagnostic preparation contacted RPC"),
            preflight_runner=lambda *args: self.fail(
                "diagnostic preparation ran preflight"
            ),
        )

        diagnostic_path = self.diagnostic_path(prepared)
        self.assertEqual(
            prepared.diagnostic_config,
            f"state/mint-runs/{prepared.run_id}/selector-diagnostic.toml",
        )
        self.assertEqual(diagnostic_path.read_bytes(), DIAGNOSTIC_D0)
        self.assertEqual(
            hashlib.sha256((self.root / "config.toml").read_bytes()).hexdigest(),
            production_hash,
        )

    def test_diagnostic_config_rejects_ambiguous_assignments(self):
        cases = {
            "missing": DIAGNOSTIC_SOURCE.replace(
                b"force_two_mints = true # preserve this comment\n",
                b"",
            ),
            "duplicate": DIAGNOSTIC_SOURCE.replace(
                b"force_two_mints = true # preserve this comment\n",
                (
                    b"force_two_mints = true # preserve this comment\n"
                    b"force_two_mints = false\n"
                ),
            ),
            "wrong_boolean_type": DIAGNOSTIC_SOURCE.replace(
                b"merge_mints = true",
                b'merge_mints = "true"',
            ),
            "wrong_integer_type": DIAGNOSTIC_SOURCE.replace(
                b"limit = 17",
                b'limit = "17"',
            ),
        }
        for index, (label, source) in enumerate(cases.items(), start=1):
            with self.subTest(label=label):
                self.install_diagnostic_source(source)
                with self.assertRaisesRegex(
                    mint_runner.RunnerError,
                    "^diagnostic configuration is invalid$",
                ):
                    self.prepare(
                        diagnostic="d0",
                        now=lambda second=index: datetime(
                            2026,
                            7,
                            24,
                            18,
                            40,
                            second,
                            tzinfo=timezone.utc,
                        ),
                    )
                self.assertEqual((self.root / "config.toml").read_bytes(), source)
                self.assertFalse(
                    any(
                        (self.root / "state" / "mint-runs").glob(
                            "*/selector-diagnostic.toml"
                        )
                    )
                )

    def test_diagnostic_config_is_private_and_production_unchanged(self):
        self.install_diagnostic_source()
        production_hash = hashlib.sha256(DIAGNOSTIC_SOURCE).hexdigest()

        prepared = self.prepare(diagnostic="d0")
        diagnostic_path = self.diagnostic_path(prepared)
        metadata_path = prepared.backup_dir / "metadata.json"
        metadata = json.loads(metadata_path.read_bytes())
        rendered_summary = json.dumps(prepared.safe_summary(), sort_keys=True)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            mint_runner._print_safe_summary(prepared)
        persisted = (
            metadata_path.read_text()
            + rendered_summary
            + stdout.getvalue()
            + (self.root / "state" / "CURRENT.md").read_text()
            + (self.root / "state" / "EXPERIMENTS.md").read_text()
        )

        self.assertEqual(stat.S_IMODE(diagnostic_path.stat().st_mode), 0o600)
        self.assertEqual(
            hashlib.sha256((self.root / "config.toml").read_bytes()).hexdigest(),
            production_hash,
        )
        self.assertNotIn(DIAGNOSTIC_SECRET, persisted)
        self.assertNotIn("fixture-uuid", persisted)
        self.assertEqual(
            metadata["diagnostic"],
            {
                "config_name": "selector-diagnostic.toml",
                "config_sha256": hashlib.sha256(DIAGNOSTIC_D0).hexdigest(),
                "mints": [TARGET_MINT],
                "mode": "d0",
            },
        )

    def test_diagnostic_d1_changes_only_ignore_offchain_bots_beyond_d0(self):
        self.install_diagnostic_source()
        prepared = self.prepare(
            diagnostic="d1",
            now=lambda: datetime(2026, 7, 24, 18, 41, tzinfo=timezone.utc),
        )

        self.assertEqual(self.diagnostic_path(prepared).read_bytes(), DIAGNOSTIC_D1)
        mint_runner._validate_diagnostic_config_bytes(
            DIAGNOSTIC_SOURCE,
            DIAGNOSTIC_D1,
            "d1",
        )
        with self.assertRaisesRegex(
            mint_runner.RunnerError,
            "^diagnostic configuration is invalid$",
        ):
            mint_runner._validate_diagnostic_config_bytes(
                DIAGNOSTIC_SOURCE,
                DIAGNOSTIC_D1,
                "d0",
            )

    def test_diagnostic_d2_allows_exactly_target_and_control_mints(self):
        self.install_diagnostic_source()
        prepared = self.prepare(
            diagnostic="d2",
            control_mint=CONTROL_MINT,
            now=lambda: datetime(2026, 7, 24, 18, 42, tzinfo=timezone.utc),
        )
        metadata = json.loads((prepared.backup_dir / "metadata.json").read_bytes())

        self.assertEqual(metadata["diagnostic"]["mints"], [TARGET_MINT, CONTROL_MINT])
        self.assertEqual(
            (self.root / "tokens.toml").read_bytes(),
            (
                f'tokens = ["{TARGET_MINT}", "{CONTROL_MINT}"]\n'
            ).encode(),
        )
        self.assertEqual(self.diagnostic_path(prepared).read_bytes(), DIAGNOSTIC_D0)
        for invalid in (None, TARGET_MINT, "not-a-public-key"):
            with self.subTest(control=invalid):
                with self.assertRaises(mint_runner.RunnerError):
                    mint_runner._validate_diagnostic_request(
                        "d2",
                        TARGET_MINT,
                        invalid,
                    )

    def test_diagnostic_config_removed_after_prepare_failure_and_interrupt(self):
        self.install_diagnostic_source()
        real_atomic_write = mint_runner._atomic_write
        active_marker = self.root / "state" / ".mint-run-active"

        def fail_after_diagnostic_write(path, data, mode=0o600):
            if Path(path) == active_marker:
                raise mint_runner.RunnerError("fixture failure")
            return real_atomic_write(path, data, mode)

        with patch.object(
            mint_runner,
            "_atomic_write",
            side_effect=fail_after_diagnostic_write,
        ):
            with self.assertRaises(mint_runner.RunnerError):
                self.prepare(
                    diagnostic="d0",
                    now=lambda: datetime(
                        2026, 7, 24, 18, 43, tzinfo=timezone.utc
                    ),
                )
        self.assertFalse(
            any(
                (self.root / "state" / "mint-runs").glob(
                    "*/selector-diagnostic.toml"
                )
            )
        )

        def interrupt_after_diagnostic_write(path, data, mode=0o600):
            if Path(path) == active_marker:
                raise KeyboardInterrupt()
            return real_atomic_write(path, data, mode)

        with patch.object(
            mint_runner,
            "_atomic_write",
            side_effect=interrupt_after_diagnostic_write,
        ):
            with self.assertRaises(KeyboardInterrupt):
                self.prepare(
                    diagnostic="d0",
                    now=lambda: datetime(
                        2026, 7, 24, 18, 44, tzinfo=timezone.utc
                    ),
                )
        self.assertFalse(
            any(
                (self.root / "state" / "mint-runs").glob(
                    "*/selector-diagnostic.toml"
                )
            )
        )

    def test_diagnostic_config_removed_by_restore_and_stale_active_recovery(self):
        self.install_diagnostic_source()
        prepared = self.prepare(
            diagnostic="d0",
            now=lambda: datetime(2026, 7, 24, 18, 45, tzinfo=timezone.utc),
        )
        diagnostic_path = self.diagnostic_path(prepared)
        self.assertTrue(diagnostic_path.exists())

        mint_runner.restore_run(self.root, prepared.run_id)
        self.assertFalse(diagnostic_path.exists())
        self.assertEqual((self.root / "config.toml").read_bytes(), DIAGNOSTIC_SOURCE)

        prepared = self.prepare(
            diagnostic="d0",
            now=lambda: datetime(2026, 7, 24, 18, 46, tzinfo=timezone.utc),
        )
        diagnostic_path = self.diagnostic_path(prepared)
        mint_runner.restore_active(self.root)
        self.assertFalse(diagnostic_path.exists())
        self.assertFalse((self.root / "state" / ".mint-run-active").exists())

    def test_restore_removes_fixed_diagnostic_config_without_metadata_contract(self):
        self.install_diagnostic_source()

        def omit_diagnostic_metadata(prepared):
            metadata_path = prepared.backup_dir / "metadata.json"
            metadata = json.loads(metadata_path.read_bytes())
            metadata.pop("diagnostic")
            metadata_path.write_bytes(
                (json.dumps(metadata, sort_keys=True) + "\n").encode()
            )
            metadata_path.chmod(0o600)

        prepared = self.prepare(
            diagnostic="d0",
            now=lambda: datetime(2026, 7, 24, 18, 50, tzinfo=timezone.utc),
        )
        diagnostic_path = self.diagnostic_path(prepared)
        omit_diagnostic_metadata(prepared)

        mint_runner.restore_run(self.root, prepared.run_id)

        self.assertFalse(diagnostic_path.exists())
        self.assertEqual((self.root / "config.toml").read_bytes(), DIAGNOSTIC_SOURCE)

        prepared = self.prepare(
            diagnostic="d0",
            now=lambda: datetime(2026, 7, 24, 18, 51, tzinfo=timezone.utc),
        )
        diagnostic_path = self.diagnostic_path(prepared)
        omit_diagnostic_metadata(prepared)

        mint_runner.restore_active(self.root)

        self.assertFalse(diagnostic_path.exists())
        self.assertFalse((self.root / "state" / ".mint-run-active").exists())

    def test_restore_cleanup_failure_preserves_retryable_active_state(self):
        self.install_diagnostic_source()
        prepared = self.prepare(
            diagnostic="d0",
            now=lambda: datetime(2026, 7, 24, 18, 52, tzinfo=timezone.utc),
        )
        diagnostic_path = self.diagnostic_path(prepared)
        active_marker = self.root / "state" / ".mint-run-active"
        restored_marker = prepared.backup_dir / "restored"

        with patch.object(
            mint_runner,
            "_remove_diagnostic_file",
            side_effect=mint_runner.RunnerError(DIAGNOSTIC_SECRET),
        ):
            with self.assertRaisesRegex(
                mint_runner.RunnerError,
                "^diagnostic configuration cleanup failed$",
            ):
                mint_runner.restore_run(self.root, prepared.run_id)

        self.assertTrue(diagnostic_path.exists())
        self.assertEqual(active_marker.read_bytes(), f"{prepared.run_id}\n".encode())
        self.assertFalse(restored_marker.exists())
        self.assertEqual((self.root / "config.toml").read_bytes(), DIAGNOSTIC_SOURCE)
        self.assertEqual((self.root / "tokens.toml").read_bytes(), self.original_tokens)

        mint_runner.restore_active(self.root)

        self.assertFalse(diagnostic_path.exists())
        self.assertFalse(active_marker.exists())
        self.assertTrue(restored_marker.exists())

    def test_prepare_cli_failure_records_generic_state_entry(self):
        stderr = io.StringIO()
        protected_detail = "https://secret.invalid/uuid/api-key"
        with patch.object(
            mint_runner,
            "prepare_run",
            side_effect=mint_runner.RunnerError(protected_detail),
        ):
            with contextlib.redirect_stderr(stderr):
                status = mint_runner.main(
                    ["--root", str(self.root), "prepare", "--mint", TARGET_MINT]
                )
        self.assertEqual(status, 1)
        self.assertNotIn(protected_detail, stderr.getvalue())
        for name in ("CURRENT.md", "EXPERIMENTS.md"):
            text = (self.root / "state" / name).read_text()
            self.assertIn("single-mint preparation failed", text)
            self.assertNotIn(protected_detail, text)

    def test_cli_invalid_arguments_are_generic_and_run_ids_never_reach_paths(self):
        protected = (
            f"{'a' * 8}-{'b' * 4}-{'c' * 4}-{'d' * 4}-{'e' * 12}"
            f"-api-{'k' * 24}"
        )
        cases = (
            ["restore", "--run-id", protected],
            ["result-path", "--run-id", protected],
            [
                "finalize",
                "--run-id",
                protected,
                "--guard-exit",
                "0",
                "--started-at",
                "100",
                "--ended-at",
                "200",
            ],
            [protected],
        )
        for index, arguments in enumerate(cases):
            with self.subTest(case=index):
                stdout = io.StringIO()
                stderr = io.StringIO()
                try:
                    with contextlib.redirect_stdout(stdout):
                        with contextlib.redirect_stderr(stderr):
                            status = mint_runner.main(
                                ["--root", str(self.root), *arguments]
                            )
                except SystemExit as exc:
                    status = exc.code
                rendered = stdout.getvalue() + stderr.getvalue()
                self.assertNotEqual(status, 0)
                self.assertFalse(
                    protected in rendered,
                    "invalid CLI argument was reflected",
                )
                self.assertTrue(
                    "error=invalid arguments" in stderr.getvalue(),
                    "generic CLI argument error was not emitted",
                )

        with self.assertRaises(mint_runner.RunnerError):
            mint_runner.restore_run(self.root, protected)

    def test_state_update_rolls_back_both_files_after_partial_write(self):
        current_path = self.root / "state" / "CURRENT.md"
        experiments_path = self.root / "state" / "EXPERIMENTS.md"
        original_current = current_path.read_bytes()
        original_experiments = experiments_path.read_bytes()
        real_atomic_write = mint_runner._atomic_write
        failed = False

        def fail_second_state_write(path, data, mode=0o600):
            nonlocal failed
            if Path(path) == experiments_path and not failed:
                failed = True
                raise OSError("injected state write failure")
            return real_atomic_write(path, data, mode)

        with patch.object(
            mint_runner, "_atomic_write", side_effect=fail_second_state_write
        ):
            with self.assertRaises(OSError):
                mint_runner.record_preparation_failure(self.root)

        self.assertEqual(current_path.read_bytes(), original_current)
        self.assertEqual(experiments_path.read_bytes(), original_experiments)
        backups = list((self.root / "state" / "backups").glob("state-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(stat.S_IMODE(backups[0].stat().st_mode), 0o700)
        for name in ("CURRENT.md", "EXPERIMENTS.md"):
            self.assertEqual(
                stat.S_IMODE((backups[0] / name).stat().st_mode),
                0o600,
            )

    def test_cli_reports_generic_secondary_state_recording_failure(self):
        protected = f"api-{'k' * 32}"
        stderr = io.StringIO()
        with patch.object(
            mint_runner,
            "prepare_run",
            side_effect=mint_runner.RunnerError("preparation failed"),
        ):
            with patch.object(
                mint_runner,
                "record_preparation_failure",
                side_effect=OSError(protected),
            ):
                with contextlib.redirect_stderr(stderr):
                    status = mint_runner.main(
                        ["--root", str(self.root), "prepare", "--mint", TARGET_MINT]
                    )

        rendered = stderr.getvalue()
        self.assertEqual(status, 1)
        self.assertTrue(
            "state_recording=failed" in rendered,
            "secondary state-recording status was not emitted",
        )
        self.assertFalse(protected in rendered, "state failure detail was reflected")

    def test_result_path_is_inside_private_run_directory(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            status = mint_runner.main(
                [
                    "--root",
                    str(self.root),
                    "result-path",
                    "--run-id",
                    "20260724T183000Z",
                ]
            )
        self.assertEqual(status, 0)
        self.assertEqual(
            stdout.getvalue().strip(),
            str(
                self.root
                / "state"
                / "mint-runs"
                / "20260724T183000Z"
                / "guard-result.txt"
            ),
        )

    def test_selector_histogram_tracks_zero_one_two(self):
        log = self.root / "selector.log"
        log.write_text(
            "Fetched 0 mint list.\n"
            "Fetched 1 mint list.\n"
            "Fetched 2 mint list.\n"
            "fetched 9 mint list.\n"
            "Fetched 7 mint list\n"
            "prefix Fetched 8 mint list.\n"
            "Fetched 6 mint list. https://source.invalid/private\n"
        )
        log.chmod(0o600)

        self.assertEqual(
            mint_runner._selector_diagnostic_summary(
                log,
                TARGET_MINT,
                {},
                "timeout",
            ),
            {
                "refresh_count": 3,
                "zero_refresh_count": 1,
                "selected_count_histogram": {"0": 1, "1": 1, "2": 1},
                "selected_count_min": 0,
                "selected_count_max": 2,
                "target_artifact_present": False,
                "target_pool_count": "unavailable",
                "target_lut_count": "unavailable",
                "target_runtime_observation_count": "unavailable",
                "candidate_construction": "not_observed_in_log",
                "dispatch": "not_applicable",
            },
        )

    def test_selector_summary_separates_discovery_from_selection(self):
        protected_url = "https://source.invalid/private-route"
        protected_signature = "unrelated-signature-fixture"
        log = self.root / "selector.log"
        log.write_text(
            "Fetched 2 mint list.\n"
            "Transaction sent successfully\n"
            "SELECTOR_DIAGNOSTIC candidate_construction=observed\n"
        )
        log.chmod(0o600)
        artifacts = {
            "hot_tokens.json": {
                "mints": [
                    {
                        "mint": UNRELATED_MINT,
                        "pools": [protected_url] * 9,
                    },
                    {
                        "mint": TARGET_MINT,
                        "pools": [{}, {}, {}],
                    },
                ],
            },
            "routing.json": {
                "mints": [
                    {
                        "mint": UNRELATED_MINT,
                        "luts": [protected_signature] * 8,
                        "runtime_observations": [protected_url] * 7,
                    },
                    {
                        "mint": TARGET_MINT,
                        "luts": [{}, {}, {}, {}],
                        "runtime_observations": [{}, {}, {}, {}, {}],
                    },
                ],
            },
        }

        summary = mint_runner._selector_diagnostic_summary(
            log,
            TARGET_MINT,
            artifacts,
            "timeout",
        )

        self.assertEqual(
            summary,
            {
                "refresh_count": 1,
                "zero_refresh_count": 0,
                "selected_count_histogram": {"2": 1},
                "selected_count_min": 2,
                "selected_count_max": 2,
                "target_artifact_present": True,
                "target_pool_count": 3,
                "target_lut_count": 4,
                "target_runtime_observation_count": 5,
                "candidate_construction": "observed_in_log",
                "dispatch": "not_applicable",
            },
        )
        rendered = json.dumps(summary, sort_keys=True)
        self.assertNotIn(UNRELATED_MINT, rendered)
        self.assertNotIn(protected_url, rendered)
        self.assertNotIn(protected_signature, rendered)


class FinalizationTests(MintRunnerTestCase):
    @staticmethod
    def zero_chain():
        return {
            "landed": 0,
            "successful": 0,
            "failed": 0,
            "fees_lamports": 0,
            "rent_lamports": 0,
            "transfers_lamports": 0,
            "sol_delta_lamports": 0,
            "wsol_delta_raw": 0,
        }

    def write_guard_result(self, prepared, content=None):
        log = self.root / "logs" / "run.log"
        log.parent.mkdir(exist_ok=True)
        log.write_text("Payer WSOL account exists\n")
        log.chmod(0o600)
        guard_result = prepared.result_dir / "guard-result.txt"
        guard_result.write_text(
            content
            or (
                "reason=timeout\n"
                "duration_seconds=300.1\n"
                "log_path=logs/run.log\n"
            )
        )
        guard_result.chmod(0o600)
        return log

    def test_generated_artifact_content_failures_use_dedicated_error(self):
        policy = mint_runner.zavod_guard.ProtectedOutputPolicy()
        nested = {}
        cursor = nested
        for _index in range(66):
            cursor["nested"] = {}
            cursor = cursor["nested"]
        collision = {
            "https://route-a.invalid/private": {"weight": 1},
            "https://route-b.invalid/private": {"weight": 2},
        }
        cases = {
            "invalid_utf8": (b"\xff", policy),
            "invalid_json": (b"not-json", policy),
            "scalar": (b'"scalar"', policy),
            "non_finite": (b'{"value":NaN}', policy),
            "too_deep": (json.dumps(nested).encode(), policy),
            "protected_key_collision": (
                json.dumps(collision).encode(),
                policy,
            ),
        }

        class ResidualPolicy:
            @staticmethod
            def redact_text(value):
                return value

            @staticmethod
            def contains_protected(value):
                return True

        cases["residual_protected_content"] = (
            b'{"safe":"value"}',
            ResidualPolicy(),
        )

        self.assertTrue(
            issubclass(
                mint_runner.GeneratedArtifactContentError,
                mint_runner.RunnerError,
            )
        )
        for label, (data, selected_policy) in cases.items():
            with self.subTest(label=label):
                with self.assertRaises(
                    mint_runner.GeneratedArtifactContentError
                ):
                    mint_runner._sanitize_generated_artifact(
                        data,
                        selected_policy,
                    )

    def test_generated_artifact_lone_surrogate_uses_dedicated_content_error(self):
        policy = mint_runner.zavod_guard.ProtectedOutputPolicy()

        with self.assertRaises(mint_runner.GeneratedArtifactContentError):
            mint_runner._sanitize_generated_artifact(
                b'{"value":"\\ud800"}',
                policy,
            )

    def test_non_string_json_key_uses_dedicated_content_error(self):
        policy = mint_runner.zavod_guard.ProtectedOutputPolicy()

        with self.assertRaises(mint_runner.GeneratedArtifactContentError):
            mint_runner._sanitize_json_value({1: "value"}, policy)

    @staticmethod
    def target_transaction(
        mint=TARGET_MINT,
        fee=1,
        pre_balance=10,
        post_balance=9,
        token_amount="1",
        block_time=100,
    ):
        return {
            "blockTime": block_time,
            "meta": {
                "err": None,
                "fee": fee,
                "preBalances": [pre_balance, 0],
                "postBalances": [post_balance, 0],
                "preTokenBalances": [
                    {
                        "owner": "wallet",
                        "mint": mint,
                        "uiTokenAmount": {"amount": token_amount},
                    }
                ],
                "postTokenBalances": [
                    {
                        "owner": "wallet",
                        "mint": mint,
                        "uiTokenAmount": {"amount": token_amount},
                    }
                ],
                "innerInstructions": [],
            },
            "transaction": {
                "message": {
                    "accountKeys": [
                        {"pubkey": "wallet"},
                        {"pubkey": mint},
                    ],
                    "instructions": [],
                }
            },
        }

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

    def test_selector_artifact_counts_require_fixed_shapes(self):
        log = self.root / "selector.log"
        log.write_text("")
        log.chmod(0o600)
        target_absent = {
            "hot_tokens.json": {
                "mints": [{"mint": UNRELATED_MINT, "pools": [{}, {}]}],
            },
            "routing.json": {
                "mints": [
                    {
                        "mint": UNRELATED_MINT,
                        "luts": [{}, {}, {}],
                        "runtime_observations": [{}, {}, {}, {}],
                    }
                ],
            },
        }
        unknown_shape = {
            "hot_tokens.json": {
                "nested": {TARGET_MINT: {"pools": [{}, {}, {}]}},
            },
            "routing.json": {
                "nested": {
                    TARGET_MINT: {
                        "luts": [{}, {}, {}, {}],
                        "runtime_observations": [{}, {}, {}, {}, {}],
                    }
                },
            },
        }

        absent = mint_runner._selector_diagnostic_summary(
            log,
            TARGET_MINT,
            target_absent,
            "timeout",
        )
        unavailable = mint_runner._selector_diagnostic_summary(
            log,
            TARGET_MINT,
            unknown_shape,
            "timeout",
        )

        self.assertEqual(absent["refresh_count"], 0)
        self.assertEqual(absent["selected_count_histogram"], {})
        self.assertEqual(absent["selected_count_min"], "unavailable")
        self.assertEqual(absent["selected_count_max"], "unavailable")
        self.assertFalse(absent["target_artifact_present"])
        self.assertEqual(absent["target_pool_count"], 0)
        self.assertEqual(absent["target_lut_count"], 0)
        self.assertEqual(absent["target_runtime_observation_count"], 0)
        self.assertFalse(unavailable["target_artifact_present"])
        self.assertEqual(unavailable["target_pool_count"], "unavailable")
        self.assertEqual(unavailable["target_lut_count"], "unavailable")
        self.assertEqual(
            unavailable["target_runtime_observation_count"],
            "unavailable",
        )

    def test_selector_stage_markers_must_be_exact(self):
        log = self.root / "selector.log"
        log.write_text(
            "candidate construction observed\n"
            "Transaction sent successfully\n"
            "dispatch=test_mode_dispatch_violation trailing\n"
        )
        log.chmod(0o600)

        summary = mint_runner._selector_diagnostic_summary(
            log,
            TARGET_MINT,
            {},
            "timeout",
        )

        self.assertEqual(
            summary["candidate_construction"],
            "not_observed_in_log",
        )
        self.assertEqual(summary["dispatch"], "not_applicable")

        log.write_text(
            "SELECTOR_DIAGNOSTIC candidate_construction=observed\n"
            "SELECTOR_DIAGNOSTIC dispatch=test_mode_dispatch_violation\n"
        )
        self.assertEqual(
            mint_runner._selector_diagnostic_summary(
                log,
                TARGET_MINT,
                {},
                "timeout",
            )["candidate_construction"],
            "observed_in_log",
        )
        self.assertEqual(
            mint_runner._selector_diagnostic_summary(
                log,
                TARGET_MINT,
                {},
                "timeout",
            )["dispatch"],
            "test_mode_dispatch_violation",
        )

    def test_chain_aggregation_uses_exact_window_and_finalized_entries(self):
        calls = []
        signatures = {
            "before": 89,
            "at-start": 90,
            "at-end": 110,
            "after": 111,
        }

        def transport(url, payload, timeout):
            del url, timeout
            calls.append(payload)
            if payload["method"] == "getSignaturesForAddress":
                return {
                    "result": [
                        {
                            "signature": signature,
                            "blockTime": block_time,
                            "confirmationStatus": "finalized",
                        }
                        for signature, block_time in signatures.items()
                    ]
                }
            return {
                "result": {
                    "blockTime": signatures[payload["params"][0]],
                    "meta": {
                        "err": None,
                        "fee": 1,
                        "preBalances": [1, 0],
                        "postBalances": [1, 0],
                        "preTokenBalances": [],
                        "postTokenBalances": [],
                    },
                    "transaction": {
                        "message": {
                            "accountKeys": [
                                {"pubkey": "wallet"},
                                {"pubkey": TARGET_MINT},
                            ],
                            "instructions": [],
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

        signature_request = calls[0]
        transaction_requests = calls[1:]
        self.assertEqual(
            signature_request["params"],
            ["wallet", {"limit": 200, "commitment": "finalized"}],
        )
        self.assertEqual(
            [
                request["params"][0]
                for request in transaction_requests
            ],
            ["at-start", "at-end"],
        )
        self.assertTrue(
            all(
                request["params"][1]["commitment"] == "finalized"
                for request in transaction_requests
            )
        )
        self.assertEqual(result["landed"], 2)
        self.assertEqual(result["successful"], 2)

    def test_chain_aggregation_filters_target_and_sums_all_fixed_metrics(self):
        wsol_mint = "So11111111111111111111111111111111111111112"

        def token_balance(owner, mint, amount):
            return {
                "owner": owner,
                "mint": mint,
                "uiTokenAmount": {"amount": str(amount)},
            }

        transactions = {
            "successful-target": {
                "blockTime": 100,
                "meta": {
                    "err": None,
                    "fee": 5000,
                    "preBalances": [100000],
                    "postBalances": [80000],
                    "preTokenBalances": [
                        token_balance("wallet", "target-mint", 1),
                        token_balance("wallet", wsol_mint, 10),
                    ],
                    "postTokenBalances": [
                        token_balance("wallet", "target-mint", 2),
                        token_balance("wallet", wsol_mint, 14),
                    ],
                    "innerInstructions": [
                        {
                            "instructions": [
                                {
                                    "program": "system",
                                    "parsed": {
                                        "type": "createAccountWithSeed",
                                        "info": {
                                            "source": "wallet",
                                            "lamports": 3000,
                                        },
                                    },
                                },
                                {
                                    "program": "system",
                                    "parsed": {
                                        "type": "transferWithSeed",
                                        "info": {
                                            "source": "wallet",
                                            "lamports": 4000,
                                        },
                                    },
                                },
                            ]
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
                                        "lamports": 1000,
                                    },
                                },
                            },
                            {
                                "program": "system",
                                "parsed": {
                                    "type": "transfer",
                                    "info": {
                                        "source": "wallet",
                                        "lamports": 2000,
                                    },
                                },
                            },
                            {
                                "program": "system",
                                "parsed": {
                                    "type": "transfer",
                                    "info": {
                                        "source": "someone-else",
                                        "lamports": 999999,
                                    },
                                },
                            },
                        ],
                    }
                },
            },
            "failed-target": {
                "blockTime": 100,
                "meta": {
                    "err": {"InstructionError": [0, "failed"]},
                    "fee": 7000,
                    "preBalances": [80000, 0],
                    "postBalances": [79000, 0],
                    "preTokenBalances": [
                        token_balance("wallet", wsol_mint, 5),
                    ],
                    "postTokenBalances": [
                        token_balance("wallet", wsol_mint, 2),
                    ],
                },
                "transaction": {
                    "message": {
                        "accountKeys": [
                            {"pubkey": "wallet"},
                            {"pubkey": "target-mint"},
                        ],
                        "instructions": [
                            {
                                "program": "system",
                                "parsed": {
                                    "type": "transfer",
                                    "info": {
                                        "source": "wallet",
                                        "lamports": 500000,
                                    },
                                },
                            }
                        ],
                    }
                },
            },
            "unrelated": {
                "blockTime": 100,
                "meta": {
                    "err": None,
                    "fee": 999999,
                    "preBalances": [79000, 0],
                    "postBalances": [1, 0],
                    "preTokenBalances": [],
                    "postTokenBalances": [],
                },
                "transaction": {
                    "message": {
                        "accountKeys": [
                            {"pubkey": "wallet"},
                            {"pubkey": "other-mint"},
                        ],
                        "instructions": [],
                    }
                },
            },
        }

        def transport(url, payload, timeout):
            del url, timeout
            if payload["method"] == "getSignaturesForAddress":
                return {
                    "result": [
                        {
                            "signature": signature,
                            "blockTime": 100,
                            "confirmationStatus": "finalized",
                        }
                        for signature in transactions
                    ] + [
                        {
                            "signature": "older",
                            "blockTime": 89,
                            "confirmationStatus": "finalized",
                        }
                    ]
                }
            return {"result": transactions[payload["params"][0]]}

        result = mint_runner.aggregate_chain(
            {"rpc": {"url": "https://secret.invalid"}},
            "target-mint",
            90,
            110,
            transport=transport,
            pubkey_resolver=lambda config: "wallet",
        )

        self.assertEqual(
            result,
            {
                "landed": 2,
                "successful": 1,
                "failed": 1,
                "fees_lamports": 12000,
                "rent_lamports": 4000,
                "transfers_lamports": 6000,
                "sol_delta_lamports": -21000,
                "wsol_delta_raw": 1,
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
                            "confirmationStatus": "finalized",
                        },
                        {
                            "signature": "older",
                            "blockTime": 89,
                            "confirmationStatus": "finalized",
                        },
                    ]
                }
            return {
                "result": {
                    "blockTime": 100,
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

    def test_chain_aggregation_paginates_past_newer_signatures(self):
        calls = []
        first_page = [
            {
                "signature": f"newer-{index}",
                "blockTime": 200,
                "confirmationStatus": "finalized",
            }
            for index in range(200)
        ]

        def transport(url, payload, timeout):
            del url, timeout
            calls.append(payload)
            if payload["method"] == "getSignaturesForAddress":
                before = payload["params"][1].get("before")
                if before is None:
                    return {"result": first_page}
                if before == "newer-199":
                    return {
                        "result": [
                            {
                                "signature": "in-window",
                                "blockTime": 100,
                                "confirmationStatus": "finalized",
                            },
                            {
                                "signature": "older",
                                "blockTime": 89,
                                "confirmationStatus": "finalized",
                            },
                        ]
                    }
                self.fail(f"unexpected pagination cursor: {before}")
            return {"result": self.target_transaction()}

        result = mint_runner.aggregate_chain(
            {"rpc": {"url": "unused"}},
            TARGET_MINT,
            90,
            110,
            transport=transport,
            pubkey_resolver=lambda config: "wallet",
        )

        signature_calls = [
            call for call in calls
            if call["method"] == "getSignaturesForAddress"
        ]
        transaction_calls = [
            call for call in calls
            if call["method"] == "getTransaction"
        ]
        self.assertEqual(len(signature_calls), 2)
        self.assertNotIn("before", signature_calls[0]["params"][1])
        self.assertEqual(
            signature_calls[1]["params"][1]["before"],
            "newer-199",
        )
        self.assertEqual(
            [call["params"][0] for call in transaction_calls],
            ["in-window"],
        )
        self.assertEqual(result["landed"], 1)

    def test_chain_aggregation_deduplicates_signatures_across_pages(self):
        transaction_signatures = []

        def entry(signature):
            return {
                "signature": signature,
                "blockTime": 100,
                "confirmationStatus": "finalized",
            }

        def transport(url, payload, timeout):
            del url, timeout
            if payload["method"] == "getSignaturesForAddress":
                before = payload["params"][1].get("before")
                if before is None:
                    return {"result": [entry("a"), entry("b")]}
                if before == "b":
                    return {"result": [entry("a"), entry("c")]}
                if before == "c":
                    return {"result": []}
                self.fail(f"unexpected pagination cursor: {before}")
            transaction_signatures.append(payload["params"][0])
            return {"result": self.target_transaction()}

        result = mint_runner.aggregate_chain(
            {"rpc": {"url": "unused"}},
            TARGET_MINT,
            90,
            110,
            transport=transport,
            pubkey_resolver=lambda config: "wallet",
        )

        self.assertEqual(transaction_signatures, ["a", "b", "c"])
        self.assertEqual(result["landed"], 3)

    def test_chain_aggregation_rejects_rpc_errors_and_repeated_cursor(self):
        protected = "protected-rpc-detail"

        def rpc_error_transport(url, payload, timeout):
            del url, payload, timeout
            return {"error": {"message": protected}}

        with self.assertRaises(mint_runner.RunnerError) as raised:
            mint_runner.aggregate_chain(
                {"rpc": {"url": "unused"}},
                TARGET_MINT,
                90,
                110,
                transport=rpc_error_transport,
                pubkey_resolver=lambda config: "wallet",
            )
        self.assertNotIn(protected, str(raised.exception))

        calls = 0

        def repeated_cursor_transport(url, payload, timeout):
            nonlocal calls
            del url, timeout
            calls += 1
            self.assertEqual(payload["method"], "getSignaturesForAddress")
            return {
                "result": [
                    {
                        "signature": "same-cursor",
                        "blockTime": 100,
                        "confirmationStatus": "finalized",
                    }
                ]
            }

        with self.assertRaises(mint_runner.RunnerError):
            mint_runner.aggregate_chain(
                {"rpc": {"url": "unused"}},
                TARGET_MINT,
                90,
                110,
                transport=repeated_cursor_transport,
                pubkey_resolver=lambda config: "wallet",
            )
        self.assertEqual(calls, 2)

    def test_chain_aggregation_rejects_malformed_transactions_without_partials(self):
        protected = "unsupported-version-protected-detail"
        malformed_bodies = {
            "rpc-error": {"error": {"message": protected}},
            "null-result": {"result": None},
            "non-dict-result": {"result": []},
            "null-block-time": {
                "result": self.target_transaction(block_time=None),
            },
            "missing-meta": {
                "result": {
                    "transaction": self.target_transaction()["transaction"],
                }
            },
            "missing-message": {
                "result": {
                    "meta": self.target_transaction()["meta"],
                    "transaction": {},
                }
            },
            "invalid-account-keys": {
                "result": {
                    "meta": self.target_transaction()["meta"],
                    "transaction": {
                        "message": {
                            "accountKeys": "wallet",
                            "instructions": [],
                        }
                    },
                }
            },
        }

        for name, malformed_body in malformed_bodies.items():
            with self.subTest(name=name):
                transaction_calls = 0

                def transport(url, payload, timeout):
                    nonlocal transaction_calls
                    del url, timeout
                    if payload["method"] == "getSignaturesForAddress":
                        return {
                            "result": [
                                {
                                    "signature": "valid-first",
                                    "blockTime": 100,
                                    "confirmationStatus": "finalized",
                                },
                                {
                                    "signature": "malformed-second",
                                    "blockTime": 100,
                                    "confirmationStatus": "finalized",
                                },
                                {
                                    "signature": "older",
                                    "blockTime": 89,
                                    "confirmationStatus": "finalized",
                                },
                            ]
                        }
                    transaction_calls += 1
                    if payload["params"][0] == "valid-first":
                        return {"result": self.target_transaction()}
                    return malformed_body

                with self.assertRaises(mint_runner.RunnerError) as raised:
                    mint_runner.aggregate_chain(
                        {"rpc": {"url": "unused"}},
                        TARGET_MINT,
                        90,
                        110,
                        transport=transport,
                        pubkey_resolver=lambda config: "wallet",
                    )
                self.assertNotIn(protected, str(raised.exception))
                self.assertEqual(transaction_calls, 2)

    def test_chain_aggregation_rejects_invalid_required_numeric_fields(self):
        base = self.target_transaction()
        cases = {}
        for value in (True, "1", -1):
            transaction = copy.deepcopy(base)
            transaction["meta"]["fee"] = value
            cases[f"fee-{value!r}"] = transaction
        for value in (True, "10", -1):
            transaction = copy.deepcopy(base)
            transaction["meta"]["preBalances"][0] = value
            cases[f"balance-{value!r}"] = transaction
        for value in (True, 1, "-1", "not-an-integer"):
            transaction = copy.deepcopy(base)
            transaction["meta"]["preTokenBalances"][0]["uiTokenAmount"][
                "amount"
            ] = value
            cases[f"token-{value!r}"] = transaction

        for name, transaction in cases.items():
            with self.subTest(name=name):
                def transport(url, payload, timeout):
                    del url, timeout
                    if payload["method"] == "getSignaturesForAddress":
                        return {
                            "result": [
                                {
                                    "signature": "malformed",
                                    "blockTime": 100,
                                    "confirmationStatus": "finalized",
                                },
                                {
                                    "signature": "older",
                                    "blockTime": 89,
                                    "confirmationStatus": "finalized",
                                },
                            ]
                        }
                    return {"result": transaction}

                with self.assertRaises(mint_runner.RunnerError):
                    mint_runner.aggregate_chain(
                        {"rpc": {"url": "unused"}},
                        TARGET_MINT,
                        90,
                        110,
                        transport=transport,
                        pubkey_resolver=lambda config: "wallet",
                    )

    def test_chain_aggregation_rejects_balance_vector_length_mismatch(self):
        transaction = self.target_transaction()
        transaction["meta"]["postBalances"] = [9]

        def transport(url, payload, timeout):
            del url, timeout
            if payload["method"] == "getSignaturesForAddress":
                return {
                    "result": [
                        {
                            "signature": "malformed",
                            "blockTime": 100,
                            "confirmationStatus": "finalized",
                        },
                        {
                            "signature": "older",
                            "blockTime": 89,
                            "confirmationStatus": "finalized",
                        },
                    ]
                }
            return {"result": transaction}

        with self.assertRaises(mint_runner.RunnerError):
            mint_runner.aggregate_chain(
                {"rpc": {"url": "unused"}},
                TARGET_MINT,
                90,
                110,
                transport=transport,
                pubkey_resolver=lambda config: "wallet",
            )

    def test_chain_aggregation_resolves_null_signature_times_at_transaction(self):
        transaction_calls = []

        def transport(url, payload, timeout):
            del url, timeout
            if payload["method"] == "getSignaturesForAddress":
                return {
                    "result": [
                        {
                            "signature": "null-inside",
                            "blockTime": None,
                            "confirmationStatus": "finalized",
                        },
                        {
                            "signature": "null-outside",
                            "blockTime": None,
                            "confirmationStatus": "finalized",
                        },
                        {
                            "signature": "known-older",
                            "blockTime": 89,
                            "confirmationStatus": "finalized",
                        },
                    ]
                }
            signature = payload["params"][0]
            transaction_calls.append(signature)
            block_time = 100 if signature == "null-inside" else 111
            return {
                "result": self.target_transaction(block_time=block_time)
            }

        result = mint_runner.aggregate_chain(
            {"rpc": {"url": "unused"}},
            TARGET_MINT,
            90,
            110,
            transport=transport,
            pubkey_resolver=lambda config: "wallet",
        )

        self.assertEqual(transaction_calls, ["null-inside", "null-outside"])
        self.assertEqual(result["landed"], 1)
        self.assertEqual(result["successful"], 1)

    def test_chain_aggregation_continues_past_all_null_time_page(self):
        signature_calls = 0
        transaction_calls = []

        def transport(url, payload, timeout):
            nonlocal signature_calls
            del url, timeout
            if payload["method"] == "getSignaturesForAddress":
                signature_calls += 1
                if signature_calls == 1:
                    return {
                        "result": [
                            {
                                "signature": "null-time",
                                "blockTime": None,
                                "confirmationStatus": "finalized",
                            }
                        ]
                    }
                return {"result": []}
            transaction_calls.append(payload["params"][0])
            return {"result": self.target_transaction(block_time=100)}

        result = mint_runner.aggregate_chain(
            {"rpc": {"url": "unused"}},
            TARGET_MINT,
            90,
            110,
            transport=transport,
            pubkey_resolver=lambda config: "wallet",
        )

        self.assertEqual(signature_calls, 2)
        self.assertEqual(transaction_calls, ["null-time"])
        self.assertEqual(result["landed"], 1)

    def test_chain_aggregation_rejects_missing_or_nonfinalized_status(self):
        for status in (None, "confirmed", "mystery"):
            with self.subTest(status=status):
                entry = {
                    "signature": "bad-status",
                    "blockTime": 100,
                }
                if status is not None:
                    entry["confirmationStatus"] = status

                def transport(url, payload, timeout):
                    del url, timeout
                    if payload["method"] == "getSignaturesForAddress":
                        return {
                            "result": [
                                entry,
                                {
                                    "signature": "older",
                                    "blockTime": 89,
                                    "confirmationStatus": "finalized",
                                },
                            ]
                        }
                    return {"result": self.target_transaction()}

                with self.assertRaises(mint_runner.RunnerError):
                    mint_runner.aggregate_chain(
                        {"rpc": {"url": "unused"}},
                        TARGET_MINT,
                        90,
                        110,
                        transport=transport,
                        pubkey_resolver=lambda config: "wallet",
                    )

    def test_chain_aggregation_requires_explicit_meta_err(self):
        transaction = self.target_transaction()
        del transaction["meta"]["err"]

        def transport(url, payload, timeout):
            del url, timeout
            if payload["method"] == "getSignaturesForAddress":
                return {
                    "result": [
                        {
                            "signature": "missing-err",
                            "blockTime": 100,
                            "confirmationStatus": "finalized",
                        },
                        {
                            "signature": "older",
                            "blockTime": 89,
                            "confirmationStatus": "finalized",
                        },
                    ]
                }
            return {"result": transaction}

        with self.assertRaises(mint_runner.RunnerError):
            mint_runner.aggregate_chain(
                {"rpc": {"url": "unused"}},
                TARGET_MINT,
                90,
                110,
                transport=transport,
                pubkey_resolver=lambda config: "wallet",
            )

    def test_finalize_copies_generated_artifacts_then_restores(self):
        (self.root / "hot_tokens.json").write_bytes(b'{"original": true}\n')
        (self.root / "routing.json").write_bytes(b"original routing\n")
        original_hot = (self.root / "hot_tokens.json").read_bytes()
        original_routing = (self.root / "routing.json").read_bytes()
        prepared = self.prepare()
        self.write_guard_result(prepared)
        generated_hot = b'{"generated": true}\n'
        generated_routing = b'{"generated_route": true}\n'
        (self.root / "hot_tokens.json").write_bytes(generated_hot)
        (self.root / "routing.json").write_bytes(generated_routing)
        (self.root / "hot_tokens.json").chmod(0o600)
        (self.root / "routing.json").chmod(0o600)

        result = mint_runner.finalize_run(
            self.root,
            prepared.run_id,
            guard_exit=0,
            started_at=100,
            ended_at=400,
            chain_aggregator=lambda *args, **kwargs: self.zero_chain(),
        )

        self.assertEqual(
            result["artifact_status"],
            {
                "hot_tokens.json": "captured",
                "routing.json": "captured",
            },
        )
        manifest = prepared.result_dir / "manifest.json"
        self.assertTrue(manifest.exists())
        self.assertEqual(stat.S_IMODE(manifest.stat().st_mode), 0o600)
        rendered = manifest.read_text()
        self.assertNotIn("https://secret.invalid", rendered)
        self.assertNotIn("signature", rendered.lower())
        self.assertEqual(
            json.loads(
                (
                    prepared.result_dir / "generated-hot_tokens.json"
                ).read_bytes()
            ),
            json.loads(generated_hot),
        )
        self.assertEqual(
            json.loads(
                (
                    prepared.result_dir / "generated-routing.json"
                ).read_bytes()
            ),
            json.loads(generated_routing),
        )
        self.assertEqual(
            stat.S_IMODE(
                (prepared.result_dir / "generated-hot_tokens.json").stat().st_mode
            ),
            0o600,
        )
        self.assertEqual((self.root / "config.toml").read_bytes(), self.original_config)
        self.assertEqual((self.root / "tokens.toml").read_bytes(), self.original_tokens)
        self.assertEqual((self.root / "hot_tokens.json").read_bytes(), original_hot)
        self.assertEqual((self.root / "routing.json").read_bytes(), original_routing)
        self.assertFalse((self.root / "state" / ".mint-run-active").exists())
        self.assertEqual(result["stop_reason"], "timeout")
        self.assertEqual(result["started_at"], 100)
        self.assertEqual(result["ended_at"], 400)
        state_backups = list(
            (self.root / "state" / "backups").glob("state-*/CURRENT.md")
        )
        self.assertEqual(len(state_backups), 1)
        self.assertEqual(stat.S_IMODE(state_backups[0].stat().st_mode), 0o600)

    def test_diagnostic_config_removed_on_every_terminal_path(self):
        self.install_diagnostic_source()
        prepared = self.prepare(
            diagnostic="d0",
            now=lambda: datetime(2026, 7, 24, 18, 47, tzinfo=timezone.utc),
        )
        diagnostic_path = self.diagnostic_path(prepared)
        self.write_guard_result(prepared)

        mint_runner.finalize_run(
            self.root,
            prepared.run_id,
            guard_exit=0,
            started_at=100,
            ended_at=400,
            chain_aggregator=lambda *args, **kwargs: self.zero_chain(),
        )

        self.assertFalse(diagnostic_path.exists())
        self.assertEqual((self.root / "config.toml").read_bytes(), DIAGNOSTIC_SOURCE)
        self.assertFalse((self.root / "state" / ".mint-run-active").exists())

        prepared = self.prepare(
            diagnostic="d0",
            now=lambda: datetime(2026, 7, 24, 18, 48, tzinfo=timezone.utc),
        )
        diagnostic_path = self.diagnostic_path(prepared)
        self.write_guard_result(
            prepared,
            content="reason=timeout\nduration_seconds=not-a-number\n",
        )
        with self.assertRaises(mint_runner.RunnerError):
            mint_runner.finalize_run(
                self.root,
                prepared.run_id,
                guard_exit=0,
                started_at=100,
                ended_at=400,
                chain_aggregator=lambda *args, **kwargs: self.zero_chain(),
            )
        self.assertFalse(diagnostic_path.exists())
        self.assertEqual((self.root / "config.toml").read_bytes(), DIAGNOSTIC_SOURCE)

        prepared = self.prepare(
            diagnostic="d0",
            now=lambda: datetime(2026, 7, 24, 18, 49, tzinfo=timezone.utc),
        )
        diagnostic_path = self.diagnostic_path(prepared)
        diagnostic_path.write_bytes(DIAGNOSTIC_D1)
        diagnostic_path.chmod(0o600)
        self.write_guard_result(prepared)
        with self.assertRaises(mint_runner.RunnerError):
            mint_runner.finalize_run(
                self.root,
                prepared.run_id,
                guard_exit=0,
                started_at=100,
                ended_at=400,
                chain_aggregator=lambda *args, **kwargs: self.zero_chain(),
            )
        self.assertFalse(diagnostic_path.exists())

    def test_finalize_records_missing_optional_artifacts(self):
        prepared = self.prepare()
        self.write_guard_result(prepared)

        result = mint_runner.finalize_run(
            self.root,
            prepared.run_id,
            guard_exit=0,
            started_at=100,
            ended_at=400,
            chain_aggregator=lambda *args, **kwargs: self.zero_chain(),
        )

        expected = {
            "hot_tokens.json": "missing",
            "routing.json": "missing",
        }
        self.assertEqual(result["artifact_status"], expected)
        manifest = json.loads(
            (prepared.result_dir / "manifest.json").read_text()
        )
        self.assertEqual(manifest["artifact_status"], expected)
        self.assertEqual(
            set(manifest["artifact_status"].values()),
            {"missing"},
        )
        self.assertEqual(
            (self.root / "tokens.toml").read_bytes(),
            self.original_tokens,
        )
        self.assertFalse(
            (self.root / "state" / ".mint-run-active").exists()
        )

    def test_finalize_rejects_stale_marker_without_restoring(self):
        prepared = self.prepare()
        self.write_guard_result(prepared)
        marker = self.root / "state" / ".mint-run-active"
        marker.write_text("20260724T183001Z\n")
        marker.chmod(0o600)
        temporary_tokens = (self.root / "tokens.toml").read_bytes()

        with self.assertRaises(mint_runner.RunnerError):
            mint_runner.finalize_run(
                self.root,
                prepared.run_id,
                guard_exit=0,
                started_at=100,
                ended_at=400,
                chain_aggregator=lambda *args, **kwargs: self.zero_chain(),
            )

        self.assertEqual((self.root / "tokens.toml").read_bytes(), temporary_tokens)
        self.assertEqual(marker.read_text(), "20260724T183001Z\n")
        self.assertFalse((prepared.backup_dir / "restored").exists())

    def test_finalize_rejects_marker_symlink_without_restoring(self):
        prepared = self.prepare()
        self.write_guard_result(prepared)
        marker = self.root / "state" / ".mint-run-active"
        marker.unlink()
        outside = self.root / "outside-marker"
        outside.write_text(f"{prepared.run_id}\n")
        outside.chmod(0o600)
        marker.symlink_to(outside)
        temporary_tokens = (self.root / "tokens.toml").read_bytes()

        with self.assertRaises(mint_runner.RunnerError):
            mint_runner.finalize_run(
                self.root,
                prepared.run_id,
                guard_exit=0,
                started_at=100,
                ended_at=400,
                chain_aggregator=lambda *args, **kwargs: self.zero_chain(),
            )

        self.assertEqual((self.root / "tokens.toml").read_bytes(), temporary_tokens)
        self.assertFalse((prepared.backup_dir / "restored").exists())

    def test_finalize_rejects_result_directory_mode_without_restoring(self):
        prepared = self.prepare()
        self.write_guard_result(prepared)
        prepared.result_dir.chmod(0o755)
        temporary_tokens = (self.root / "tokens.toml").read_bytes()

        with self.assertRaises(mint_runner.RunnerError):
            mint_runner.finalize_run(
                self.root,
                prepared.run_id,
                guard_exit=0,
                started_at=100,
                ended_at=400,
                chain_aggregator=lambda *args, **kwargs: self.zero_chain(),
            )

        self.assertEqual((self.root / "tokens.toml").read_bytes(), temporary_tokens)
        self.assertTrue((self.root / "state" / ".mint-run-active").exists())
        self.assertFalse((prepared.backup_dir / "restored").exists())

    def test_finalize_rejects_result_directory_symlink_without_restoring(self):
        prepared = self.prepare()
        prepared.result_dir.rmdir()
        outside = self.root / "outside-results"
        outside.mkdir(mode=0o700)
        prepared.result_dir.symlink_to(outside, target_is_directory=True)
        temporary_tokens = (self.root / "tokens.toml").read_bytes()

        with self.assertRaises(mint_runner.RunnerError):
            mint_runner.finalize_run(
                self.root,
                prepared.run_id,
                guard_exit=0,
                started_at=100,
                ended_at=400,
                chain_aggregator=lambda *args, **kwargs: self.zero_chain(),
            )

        self.assertEqual((self.root / "tokens.toml").read_bytes(), temporary_tokens)
        self.assertTrue((self.root / "state" / ".mint-run-active").exists())
        self.assertFalse((prepared.backup_dir / "restored").exists())

    def test_finalize_rejects_symlinked_state_ancestor_without_restoring(self):
        prepared = self.prepare()
        self.write_guard_result(prepared)
        state = self.root / "state"
        held_state = self.root / "state-held"
        state.rename(held_state)
        state.symlink_to(held_state, target_is_directory=True)
        temporary_tokens = (self.root / "tokens.toml").read_bytes()

        with self.assertRaises(mint_runner.RunnerError):
            mint_runner.finalize_run(
                self.root,
                prepared.run_id,
                guard_exit=0,
                started_at=100,
                ended_at=400,
                chain_aggregator=lambda *args, **kwargs: self.zero_chain(),
            )

        self.assertEqual((self.root / "tokens.toml").read_bytes(), temporary_tokens)
        self.assertFalse(
            (held_state / "backups" / f"mint-run-{prepared.run_id}" / "restored").exists()
        )

    def test_finalize_rejects_symlinked_mint_runs_ancestor_without_restoring(self):
        prepared = self.prepare()
        self.write_guard_result(prepared)
        mint_runs = self.root / "state" / "mint-runs"
        held_mint_runs = self.root / "state" / "mint-runs-held"
        mint_runs.rename(held_mint_runs)
        mint_runs.symlink_to(held_mint_runs, target_is_directory=True)
        temporary_tokens = (self.root / "tokens.toml").read_bytes()

        with self.assertRaises(mint_runner.RunnerError):
            mint_runner.finalize_run(
                self.root,
                prepared.run_id,
                guard_exit=0,
                started_at=100,
                ended_at=400,
                chain_aggregator=lambda *args, **kwargs: self.zero_chain(),
            )

        self.assertEqual((self.root / "tokens.toml").read_bytes(), temporary_tokens)
        self.assertFalse((prepared.backup_dir / "restored").exists())

    def test_finalize_ancestor_swap_cannot_redirect_manifest_write(self):
        prepared = self.prepare()
        self.write_guard_result(prepared)
        mint_runs = self.root / "state" / "mint-runs"
        held_mint_runs = self.root / "state" / "mint-runs-held"
        replacement_result = mint_runs / prepared.run_id

        def swap_ancestor(*args, **kwargs):
            mint_runs.rename(held_mint_runs)
            mint_runs.mkdir()
            replacement_result.mkdir(mode=0o700)
            return self.zero_chain()

        with self.assertRaises(mint_runner.RunnerError):
            mint_runner.finalize_run(
                self.root,
                prepared.run_id,
                guard_exit=0,
                started_at=100,
                ended_at=400,
                chain_aggregator=swap_ancestor,
            )

        self.assertFalse((replacement_result / "manifest.json").exists())
        self.assertEqual((self.root / "tokens.toml").read_bytes(), self.original_tokens)
        self.assertFalse((self.root / "state" / ".mint-run-active").exists())

    def test_cli_invalid_log_mode_restores_and_records_generic_failure(self):
        prepared = self.prepare()
        log = self.write_guard_result(prepared)
        log.chmod(0o644)
        stderr = io.StringIO()

        with contextlib.redirect_stderr(stderr):
            status = mint_runner.main(
                [
                    "--root",
                    str(self.root),
                    "finalize",
                    "--run-id",
                    prepared.run_id,
                    "--guard-exit",
                    "0",
                    "--started-at",
                    "100",
                    "--ended-at",
                    "400",
                ]
            )

        self.assertEqual(status, 1)
        self.assertEqual(stderr.getvalue(), "status=failed\nerror=operation failed\n")
        self.assertEqual((self.root / "tokens.toml").read_bytes(), self.original_tokens)
        self.assertFalse((self.root / "state" / ".mint-run-active").exists())
        for name in ("CURRENT.md", "EXPERIMENTS.md"):
            self.assertIn(
                "single-mint finalization failed",
                (self.root / "state" / name).read_text(),
            )

    def test_finalize_rejects_guard_result_symlink_and_mode_and_restores(self):
        cases = ("symlink", "mode")
        for index, case in enumerate(cases, start=20):
            with self.subTest(case=case):
                prepared = self.prepare(
                    now=lambda second=index: datetime(
                        2026,
                        7,
                        24,
                        18,
                        30,
                        second,
                        tzinfo=timezone.utc,
                    )
                )
                self.write_guard_result(prepared)
                guard_result = prepared.result_dir / "guard-result.txt"
                if case == "symlink":
                    outside = self.root / f"outside-guard-{index}"
                    outside.write_text(
                        "reason=timeout\n"
                        "duration_seconds=1\n"
                        "log_path=logs/run.log\n"
                    )
                    outside.chmod(0o600)
                    guard_result.unlink()
                    guard_result.symlink_to(outside)
                else:
                    guard_result.chmod(0o644)

                with self.assertRaises(mint_runner.RunnerError):
                    mint_runner.finalize_run(
                        self.root,
                        prepared.run_id,
                        guard_exit=0,
                        started_at=100,
                        ended_at=400,
                        chain_aggregator=lambda *args, **kwargs: self.zero_chain(),
                    )

                self.assertEqual(
                    (self.root / "tokens.toml").read_bytes(),
                    self.original_tokens,
                )
                self.assertFalse(
                    (self.root / "state" / ".mint-run-active").exists()
                )

    def test_finalize_rejects_log_symlink_and_restores(self):
        prepared = self.prepare()
        log = self.write_guard_result(prepared)
        outside = self.root / "outside-log"
        outside.write_text("Transaction sent successfully\n")
        outside.chmod(0o600)
        log.unlink()
        log.symlink_to(outside)

        with self.assertRaises(mint_runner.RunnerError):
            mint_runner.finalize_run(
                self.root,
                prepared.run_id,
                guard_exit=0,
                started_at=100,
                ended_at=400,
                chain_aggregator=lambda *args, **kwargs: self.zero_chain(),
            )

        self.assertEqual((self.root / "tokens.toml").read_bytes(), self.original_tokens)
        self.assertFalse((self.root / "state" / ".mint-run-active").exists())

    def test_capture_generated_artifact_does_not_downgrade_path_errors(self):
        directories = mint_runner._FinalizationDirectories(
            root_path=self.root,
            root_fd=-1,
            state_fd=-1,
            mint_runs_fd=-1,
            result_fd=-1,
        )
        policy = mint_runner.zavod_guard.ProtectedOutputPolicy()
        path_error = mint_runner.RunnerError(
            "private run paths are invalid"
        )

        with patch.object(
            mint_runner,
            "_read_owned_file_at",
            side_effect=path_error,
        ):
            with self.assertRaises(mint_runner.RunnerError) as raised:
                mint_runner._capture_generated_artifact(
                    directories,
                    "routing.json",
                    policy,
                )

        self.assertIs(raised.exception, path_error)

    def test_finalize_rejects_artifact_symlink_and_restores(self):
        prepared = self.prepare()
        self.write_guard_result(prepared)
        outside = self.root / "outside-artifact"
        outside.write_bytes(b"protected artifact")
        generated = self.root / "hot_tokens.json"
        generated.symlink_to(outside)

        with self.assertRaises(mint_runner.RunnerError):
            mint_runner.finalize_run(
                self.root,
                prepared.run_id,
                guard_exit=0,
                started_at=100,
                ended_at=400,
                chain_aggregator=lambda *args, **kwargs: self.zero_chain(),
            )

        self.assertEqual(outside.read_bytes(), b"protected artifact")
        self.assertEqual((self.root / "tokens.toml").read_bytes(), self.original_tokens)
        self.assertFalse((self.root / "state" / ".mint-run-active").exists())
        self.assertFalse(
            (prepared.result_dir / "generated-hot_tokens.json").exists()
        )

    def test_finalize_rejects_dangling_artifact_symlink_and_restores(self):
        prepared = self.prepare()
        self.write_guard_result(prepared)
        generated = self.root / "hot_tokens.json"
        generated.symlink_to(self.root / "missing-artifact-target")

        with self.assertRaises(mint_runner.RunnerError):
            mint_runner.finalize_run(
                self.root,
                prepared.run_id,
                guard_exit=0,
                started_at=100,
                ended_at=400,
                chain_aggregator=lambda *args, **kwargs: self.zero_chain(),
            )

        self.assertFalse(generated.is_symlink())
        self.assertFalse(generated.exists())
        self.assertEqual((self.root / "tokens.toml").read_bytes(), self.original_tokens)
        self.assertFalse((self.root / "state" / ".mint-run-active").exists())

    def test_finalize_rejects_generated_artifact_not_mode_600(self):
        prepared = self.prepare()
        self.write_guard_result(prepared)
        generated = self.root / "hot_tokens.json"
        generated.write_bytes(b"generated artifact")
        generated.chmod(0o644)

        with self.assertRaises(mint_runner.RunnerError):
            mint_runner.finalize_run(
                self.root,
                prepared.run_id,
                guard_exit=0,
                started_at=100,
                ended_at=400,
                chain_aggregator=lambda *args, **kwargs: self.zero_chain(),
            )

        self.assertFalse(generated.exists())
        self.assertEqual((self.root / "tokens.toml").read_bytes(), self.original_tokens)
        self.assertFalse((self.root / "state" / ".mint-run-active").exists())

    def test_finalize_records_non_json_artifact_as_rejected_content(self):
        prepared = self.prepare()
        self.write_guard_result(prepared)
        generated = self.root / "hot_tokens.json"
        rejected = b"not-json-runtime-output"
        generated.write_bytes(rejected)
        generated.chmod(0o600)

        result = mint_runner.finalize_run(
            self.root,
            prepared.run_id,
            guard_exit=0,
            started_at=100,
            ended_at=400,
            chain_aggregator=lambda *args, **kwargs: self.zero_chain(),
        )

        expected = {
            "hot_tokens.json": "rejected_content",
            "routing.json": "missing",
        }
        self.assertEqual(result["artifact_status"], expected)
        manifest_path = prepared.result_dir / "manifest.json"
        self.assertTrue(manifest_path.exists())
        self.assertEqual(
            json.loads(manifest_path.read_text())["artifact_status"],
            expected,
        )
        self.assertNotIn(
            rejected.decode(),
            manifest_path.read_text(),
        )
        self.assertFalse(
            (prepared.result_dir / "generated-hot_tokens.json").exists()
        )
        self.assertEqual(
            (self.root / "tokens.toml").read_bytes(),
            self.original_tokens,
        )
        self.assertFalse((self.root / "state" / ".mint-run-active").exists())

    def test_finalize_records_lone_surrogate_artifact_as_rejected_content(self):
        prepared = self.prepare()
        self.write_guard_result(prepared)
        rejected = b'{"value":"\\ud800"}'
        generated = self.root / "hot_tokens.json"
        generated.write_bytes(rejected)
        generated.chmod(0o600)

        result = mint_runner.finalize_run(
            self.root,
            prepared.run_id,
            guard_exit=0,
            started_at=100,
            ended_at=400,
            chain_aggregator=lambda *args, **kwargs: self.zero_chain(),
        )

        expected = {
            "hot_tokens.json": "rejected_content",
            "routing.json": "missing",
        }
        self.assertEqual(result["artifact_status"], expected)
        manifest_path = prepared.result_dir / "manifest.json"
        self.assertTrue(manifest_path.exists())
        self.assertEqual(
            json.loads(manifest_path.read_text())["artifact_status"],
            expected,
        )
        self.assertNotIn(rejected.decode(), manifest_path.read_text())
        self.assertFalse(
            (prepared.result_dir / "generated-hot_tokens.json").exists()
        )
        self.assertFalse(generated.exists())
        self.assertEqual(
            (self.root / "tokens.toml").read_bytes(),
            self.original_tokens,
        )
        self.assertFalse((self.root / "state" / ".mint-run-active").exists())

    def test_finalize_preserves_manifest_after_routing_key_collision(self):
        prepared = self.prepare()
        self.write_guard_result(prepared)
        generated_hot = b'{"generated":true}\n'
        protected_routes = (
            "https://route-a.invalid/private",
            "https://route-b.invalid/private",
        )
        routing = {
            protected_routes[0]: {"weight": 1},
            protected_routes[1]: {"weight": 2},
        }
        (self.root / "hot_tokens.json").write_bytes(generated_hot)
        (self.root / "routing.json").write_text(json.dumps(routing))
        (self.root / "hot_tokens.json").chmod(0o600)
        (self.root / "routing.json").chmod(0o600)

        result = mint_runner.finalize_run(
            self.root,
            prepared.run_id,
            guard_exit=0,
            started_at=100,
            ended_at=400,
            chain_aggregator=lambda *args, **kwargs: self.zero_chain(),
        )

        expected = {
            "hot_tokens.json": "captured",
            "routing.json": "rejected_content",
        }
        self.assertEqual(result["artifact_status"], expected)
        manifest_path = prepared.result_dir / "manifest.json"
        rendered = manifest_path.read_text()
        self.assertEqual(
            json.loads(rendered)["artifact_status"],
            expected,
        )
        self.assertTrue(
            (
                prepared.result_dir / "generated-hot_tokens.json"
            ).exists()
        )
        self.assertFalse(
            (prepared.result_dir / "generated-routing.json").exists()
        )
        for protected in protected_routes:
            self.assertNotIn(protected, rendered)
            for name in ("CURRENT.md", "EXPERIMENTS.md"):
                self.assertNotIn(
                    protected,
                    (self.root / "state" / name).read_text(),
                )
        self.assertEqual(
            (self.root / "tokens.toml").read_bytes(),
            self.original_tokens,
        )
        self.assertFalse(
            (self.root / "state" / ".mint-run-active").exists()
        )

    def test_finalize_sanitizes_every_persisted_artifact_and_result_file(self):
        rpc_variable = "ZAVOD_TEST_FINAL_RPC"
        wallet_variable = "ZAVOD_TEST_FINAL_WALLET"
        expanded_rpc = "exact-rpc-config-value"
        expanded_wallet = "exact-wallet-config-value"
        (self.root / "config.toml").write_text(
            "[auto]\n"
            "enabled = true\n"
            "[rpc]\n"
            f'url = "${{{rpc_variable}}}"\n'
            "[wallet]\n"
            f'private_key = "${{{wallet_variable}}}"\n'
        )
        (self.root / "config.toml").chmod(0o600)
        protected_uuid = "12345678-1234-4234-9234-123456789abc"
        protected_signature = mint_runner.zavod_guard.base58_encode(
            b"\x01" * 64
        )
        short_signature = mint_runner.zavod_guard.base58_encode(
            b"\0" * 16 + b"\x01" * 48
        )
        self.assertLess(len(short_signature), 86)
        protected_url = "https://example.invalid/path?credential=value"
        artifact = {
            "nested": {
                "uuid": protected_uuid,
                "signature": protected_signature,
                "short_signature": short_signature,
                "url": protected_url,
                "wallet": expanded_wallet,
                expanded_rpc: "protected-key",
            }
        }

        with patch.dict(
            os.environ,
            {
                rpc_variable: expanded_rpc,
                wallet_variable: expanded_wallet,
            },
            clear=False,
        ):
            prepared = self.prepare()
            log = self.write_guard_result(prepared)
            for name in ("hot_tokens.json", "routing.json"):
                (self.root / name).write_text(json.dumps(artifact))
                (self.root / name).chmod(0o600)
            observed_config = {}

            def aggregate(config, *args, **kwargs):
                del args, kwargs
                observed_config.update(config)
                return self.zero_chain()

            mint_runner.finalize_run(
                self.root,
                prepared.run_id,
                guard_exit=0,
                started_at=100,
                ended_at=400,
                chain_aggregator=aggregate,
            )

        self.assertEqual(observed_config["rpc"]["url"], expanded_rpc)
        self.assertEqual(
            observed_config["wallet"]["private_key"],
            expanded_wallet,
        )
        persisted = [log]
        persisted.extend(
            path
            for path in prepared.result_dir.iterdir()
            if path.is_file()
        )
        for path in persisted:
            with self.subTest(path=path.name):
                rendered = path.read_text(errors="replace")
                self.assertNotRegex(
                    rendered,
                    re.compile(
                        r"[0-9a-fA-F]{8}-"
                        r"[0-9a-fA-F]{4}-"
                        r"[0-9a-fA-F]{4}-"
                        r"[0-9a-fA-F]{4}-"
                        r"[0-9a-fA-F]{12}"
                    ),
                )
                self.assertNotRegex(
                    rendered,
                    re.compile(
                        r"[1-9A-HJ-NP-Za-km-z]{86,}"
                    ),
                )
                for protected in (
                    protected_signature,
                    short_signature,
                    protected_url,
                    expanded_rpc,
                    expanded_wallet,
                ):
                    self.assertNotIn(protected, rendered)

    def test_finalize_rejects_symlink_destination_without_overwriting_target(self):
        prepared = self.prepare()
        self.write_guard_result(prepared)
        (self.root / "hot_tokens.json").write_bytes(b"generated artifact")
        (self.root / "hot_tokens.json").chmod(0o600)
        outside = self.root / "outside-destination"
        outside.write_bytes(b"must stay unchanged")
        destination = prepared.result_dir / "generated-hot_tokens.json"
        destination.symlink_to(outside)

        with self.assertRaises(mint_runner.RunnerError):
            mint_runner.finalize_run(
                self.root,
                prepared.run_id,
                guard_exit=0,
                started_at=100,
                ended_at=400,
                chain_aggregator=lambda *args, **kwargs: self.zero_chain(),
            )

        self.assertEqual(outside.read_bytes(), b"must stay unchanged")
        self.assertEqual((self.root / "tokens.toml").read_bytes(), self.original_tokens)
        self.assertFalse((self.root / "state" / ".mint-run-active").exists())

    def test_finalize_rejects_unsafe_destination_when_source_is_missing(self):
        prepared = self.prepare()
        self.write_guard_result(prepared)
        outside = self.root / "outside-destination"
        outside.write_bytes(b"must stay unchanged")
        destination = prepared.result_dir / "generated-hot_tokens.json"
        destination.symlink_to(outside)

        with self.assertRaises(mint_runner.RunnerError):
            mint_runner.finalize_run(
                self.root,
                prepared.run_id,
                guard_exit=0,
                started_at=100,
                ended_at=400,
                chain_aggregator=lambda *args, **kwargs: self.zero_chain(),
            )

        self.assertEqual(outside.read_bytes(), b"must stay unchanged")
        self.assertEqual((self.root / "tokens.toml").read_bytes(), self.original_tokens)
        self.assertFalse((self.root / "state" / ".mint-run-active").exists())

    def test_finalize_rejects_invalid_existing_result_file_mode_and_restores(self):
        prepared = self.prepare()
        self.write_guard_result(prepared)
        manifest = prepared.result_dir / "manifest.json"
        manifest.write_text("{}\n")
        manifest.chmod(0o644)

        with self.assertRaises(mint_runner.RunnerError):
            mint_runner.finalize_run(
                self.root,
                prepared.run_id,
                guard_exit=0,
                started_at=100,
                ended_at=400,
                chain_aggregator=lambda *args, **kwargs: self.zero_chain(),
            )

        self.assertEqual((self.root / "tokens.toml").read_bytes(), self.original_tokens)
        self.assertFalse((self.root / "state" / ".mint-run-active").exists())
        self.assertEqual(stat.S_IMODE(manifest.stat().st_mode), 0o644)

    def test_finalize_rejects_invalid_duration_and_restores(self):
        values = ("nan", "inf", "-0.1")
        for index, value in enumerate(values, start=10):
            with self.subTest(value=value):
                prepared = self.prepare(
                    now=lambda second=index: datetime(
                        2026,
                        7,
                        24,
                        18,
                        30,
                        second,
                        tzinfo=timezone.utc,
                    )
                )
                self.write_guard_result(
                    prepared,
                    content=(
                        "reason=timeout\n"
                        f"duration_seconds={value}\n"
                        "log_path=logs/run.log\n"
                    ),
                )

                with self.assertRaises(mint_runner.RunnerError):
                    mint_runner.finalize_run(
                        self.root,
                        prepared.run_id,
                        guard_exit=0,
                        started_at=100,
                        ended_at=400,
                        chain_aggregator=lambda *args, **kwargs: self.zero_chain(),
                    )

                self.assertEqual(
                    (self.root / "tokens.toml").read_bytes(),
                    self.original_tokens,
                )
                self.assertFalse(
                    (self.root / "state" / ".mint-run-active").exists()
                )

    def test_finalize_rpc_failure_is_generic_and_restores_without_partial_totals(self):
        prepared = self.prepare()
        self.write_guard_result(prepared)
        protected = "protected-unsupported-version-detail"

        def transport(url, payload, timeout):
            del url, timeout
            if payload["method"] == "getSignaturesForAddress":
                return {
                    "result": [
                        {
                            "signature": "must-not-survive",
                            "blockTime": 100,
                            "confirmationStatus": "finalized",
                        },
                        {
                            "signature": "older",
                            "blockTime": 89,
                            "confirmationStatus": "finalized",
                        },
                    ]
                }
            return {"error": {"message": protected}}

        result = mint_runner.finalize_run(
            self.root,
            prepared.run_id,
            guard_exit=0,
            started_at=90,
            ended_at=110,
            transport=transport,
            pubkey_resolver=lambda config: "wallet",
        )

        rendered = (prepared.result_dir / "manifest.json").read_text()
        self.assertEqual(result["aggregation_status"], "failed")
        self.assertEqual(result["chain"], self.zero_chain())
        self.assertNotIn(protected, rendered)
        self.assertNotIn("must-not-survive", rendered)
        self.assertEqual((self.root / "tokens.toml").read_bytes(), self.original_tokens)
        self.assertFalse((self.root / "state" / ".mint-run-active").exists())

    def test_finalize_restores_exactly_once(self):
        prepared = self.prepare()
        self.write_guard_result(prepared)
        original_restore = mint_runner.restore_run

        with patch.object(
            mint_runner,
            "restore_run",
            wraps=original_restore,
        ) as restore:
            mint_runner.finalize_run(
                self.root,
                prepared.run_id,
                guard_exit=0,
                started_at=100,
                ended_at=400,
                chain_aggregator=lambda *args, **kwargs: self.zero_chain(),
            )

        self.assertEqual(restore.call_count, 1)

    def test_finalize_records_only_generic_aggregation_failure(self):
        prepared = self.prepare()
        self.write_guard_result(prepared)
        protected = "https://secret.invalid/signature/must-not-survive"

        def fail_aggregation(*args, **kwargs):
            raise RuntimeError(protected)

        result = mint_runner.finalize_run(
            self.root,
            prepared.run_id,
            guard_exit=7,
            started_at=100,
            ended_at=400,
            chain_aggregator=fail_aggregation,
        )

        rendered = (prepared.result_dir / "manifest.json").read_text()
        self.assertEqual(result["aggregation_status"], "failed")
        self.assertEqual(result["chain"], self.zero_chain())
        self.assertNotIn(protected, rendered)
        self.assertNotIn("signature", rendered.lower())
        self.assertEqual((self.root / "tokens.toml").read_bytes(), self.original_tokens)
        for name in ("CURRENT.md", "EXPERIMENTS.md"):
            self.assertNotIn(protected, (self.root / "state" / name).read_text())

    def test_finalize_discards_unexpected_aggregator_fields(self):
        prepared = self.prepare()
        self.write_guard_result(prepared)
        protected = "https://secret.invalid/signature/must-not-survive"
        aggregate = {
            **self.zero_chain(),
            "signature": protected,
            "rpc_url": protected,
        }

        result = mint_runner.finalize_run(
            self.root,
            prepared.run_id,
            guard_exit=0,
            started_at=100,
            ended_at=400,
            chain_aggregator=lambda *args, **kwargs: aggregate,
        )

        rendered = (prepared.result_dir / "manifest.json").read_text()
        self.assertEqual(result["aggregation_status"], "ok")
        self.assertEqual(result["chain"], self.zero_chain())
        self.assertNotIn(protected, rendered)
        self.assertNotIn("signature", rendered.lower())
        self.assertNotIn("rpc_url", rendered.lower())

    def test_finalize_sanitizes_unrecognized_guard_reason(self):
        prepared = self.prepare()
        protected = "https://secret.invalid/signature/must-not-survive"
        self.write_guard_result(
            prepared,
            content=(
                f"reason={protected}\n"
                "duration_seconds=1\n"
                "log_path=logs/run.log\n"
            ),
        )

        result = mint_runner.finalize_run(
            self.root,
            prepared.run_id,
            guard_exit=0,
            started_at=100,
            ended_at=400,
            chain_aggregator=lambda *args, **kwargs: self.zero_chain(),
        )

        rendered = (prepared.result_dir / "manifest.json").read_text()
        self.assertEqual(result["stop_reason"], "unknown")
        self.assertNotIn(protected, rendered)
        for name in ("CURRENT.md", "EXPERIMENTS.md"):
            self.assertNotIn(protected, (self.root / "state" / name).read_text())

    def test_manifest_records_test_mode_dispatch_violation(self):
        self.install_diagnostic_source()
        prepared = self.prepare(diagnostic="d0")
        log = self.write_guard_result(
            prepared,
            content=(
                "reason=test_mode_dispatch_violation\n"
                "duration_seconds=1\n"
                "log_path=logs/run.log\n"
            ),
        )
        log.write_text("Fetched 0 mint list.\n")

        result = mint_runner.finalize_run(
            self.root,
            prepared.run_id,
            guard_exit=1,
            started_at=100,
            ended_at=101,
            chain_aggregator=lambda *args, **kwargs: self.zero_chain(),
        )

        self.assertEqual(result["stop_reason"], "test_mode_dispatch_violation")
        self.assertEqual(
            result["selector_diagnostic"],
            {
                "refresh_count": 1,
                "zero_refresh_count": 1,
                "selected_count_histogram": {"0": 1},
                "selected_count_min": 0,
                "selected_count_max": 0,
                "target_artifact_present": False,
                "target_pool_count": "unavailable",
                "target_lut_count": "unavailable",
                "target_runtime_observation_count": "unavailable",
                "candidate_construction": "not_observed_in_log",
                "dispatch": "test_mode_dispatch_violation",
            },
        )
        rendered = (prepared.result_dir / "manifest.json").read_text()
        self.assertNotIn(UNRELATED_MINT, rendered)
        self.assertNotIn("https://fixture.invalid/fixture-uuid", rendered)
        self.assertNotIn("signature", rendered.lower())

    def test_finalize_cli_passes_exact_window_and_outputs_only_safe_paths(self):
        manifest = {
            "stop_reason": "timeout",
        }
        stdout = io.StringIO()
        with patch.object(
            mint_runner, "finalize_run", return_value=manifest
        ) as finalizer:
            with contextlib.redirect_stdout(stdout):
                status = mint_runner.main(
                    [
                        "--root",
                        str(self.root),
                        "finalize",
                        "--run-id",
                        "20260724T183000Z",
                        "--guard-exit",
                        "7",
                        "--started-at",
                        "100",
                        "--ended-at",
                        "400",
                    ]
                )

        self.assertEqual(status, 0)
        finalizer.assert_called_once_with(
            self.root.resolve(),
            "20260724T183000Z",
            7,
            100,
            400,
        )
        rendered = stdout.getvalue()
        self.assertEqual(
            rendered,
            "stop_reason=timeout\n"
            "manifest=state/mint-runs/20260724T183000Z/manifest.json\n",
        )

    def test_finalize_cli_succeeds_without_leaking_rejected_routing(self):
        prepared = self.prepare()
        self.write_guard_result(prepared)
        protected_routes = (
            "https://route-a.invalid/private",
            "https://route-b.invalid/private",
        )
        routing = {
            protected_routes[0]: {"weight": 1},
            protected_routes[1]: {"weight": 2},
        }
        (self.root / "hot_tokens.json").write_text(
            '{"generated":true}\n'
        )
        (self.root / "routing.json").write_text(json.dumps(routing))
        (self.root / "hot_tokens.json").chmod(0o600)
        (self.root / "routing.json").chmod(0o600)
        stdout = io.StringIO()
        stderr = io.StringIO()

        with (
            patch.object(
                mint_runner,
                "aggregate_chain",
                return_value=self.zero_chain(),
            ),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            status = mint_runner.main(
                [
                    "--root",
                    str(self.root),
                    "finalize",
                    "--run-id",
                    prepared.run_id,
                    "--guard-exit",
                    "0",
                    "--started-at",
                    "100",
                    "--ended-at",
                    "400",
                ]
            )

        self.assertEqual(status, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(
            stdout.getvalue(),
            "stop_reason=timeout\n"
            f"manifest=state/mint-runs/{prepared.run_id}/manifest.json\n",
        )
        persisted = stdout.getvalue() + stderr.getvalue()
        persisted += (
            prepared.result_dir / "manifest.json"
        ).read_text()
        for name in ("CURRENT.md", "EXPERIMENTS.md"):
            persisted += (self.root / "state" / name).read_text()
        for protected in protected_routes:
            self.assertNotIn(protected, persisted)
        self.assertEqual(
            json.loads(
                (prepared.result_dir / "manifest.json").read_text()
            )["artifact_status"]["routing.json"],
            "rejected_content",
        )

    def test_finalize_cli_failure_is_generic_and_leaves_restore_to_failsafe(self):
        prepared = self.prepare()
        changed_tokens = b"generated tokens"
        (self.root / "tokens.toml").write_bytes(changed_tokens)
        protected = "https://secret.invalid/signature/must-not-survive"
        stderr = io.StringIO()

        with patch.object(
            mint_runner,
            "finalize_run",
            side_effect=mint_runner.RunnerError(protected),
        ):
            with contextlib.redirect_stderr(stderr):
                status = mint_runner.main(
                    [
                        "--root",
                        str(self.root),
                        "finalize",
                        "--run-id",
                        prepared.run_id,
                        "--guard-exit",
                        "7",
                        "--started-at",
                        "100",
                        "--ended-at",
                        "400",
                    ]
                )

        self.assertEqual(status, 1)
        self.assertNotIn(protected, stderr.getvalue())
        self.assertIn("error=operation failed", stderr.getvalue())
        self.assertEqual((self.root / "tokens.toml").read_bytes(), changed_tokens)
        self.assertTrue((self.root / "state" / ".mint-run-active").exists())
        for name in ("CURRENT.md", "EXPERIMENTS.md"):
            rendered = (self.root / "state" / name).read_text()
            self.assertIn("single-mint finalization failed", rendered)
            self.assertNotIn(protected, rendered)


if __name__ == "__main__":
    unittest.main()
