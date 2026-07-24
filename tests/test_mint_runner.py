import contextlib
import io
import json
import shutil
import signal
import stat
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
