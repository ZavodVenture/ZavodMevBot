import contextlib
import io
import json
import os
import shutil
import stat
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

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
        self.assertEqual(stat.S_IMODE((self.root / "config.toml").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.root / "tokens.toml").stat().st_mode), 0o600)
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


if __name__ == "__main__":
    unittest.main()
