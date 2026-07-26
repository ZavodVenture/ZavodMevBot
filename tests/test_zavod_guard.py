import copy
import fcntl
import hashlib
import io
import json
import os
import signal
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import mint_auto_diagnoser, mint_runner, zavod_guard
from scripts.zavod_guard import (
    EARLY_STOP_LAMPORTS,
    GuardError,
    base58_encode,
    get_balance_lamports,
    load_config,
    preflight,
    redact_text,
    should_stop_for_loss,
    supervise,
    validate_config,
    wallet_pubkey,
)


MANUAL_MINT = "FB44zC6s2jkysjaB2NC8u6XqwhPJwir1DYFzEhXbpump"
DIAGNOSTIC_TARGET = "So11111111111111111111111111111111111111112"
CONTROL_MINT = "11111111111111111111111111111111"
MANUAL_POOLS = (
    "8dxAgMTRUmCMVProMisWFS26EgiJwbMoiwfMZNeopSQZ",
    "7CTjvXcZhm2R5CvUXn3SyAKWvZtz2ZgNtv4f8BoBv57K",
)
MANUAL_LUTS = (
    "GbHfFWfwaSK7Ecumh3RsvQyCL6WeEqQHg6SYKdccm8Sm",
    "97TLJmbiCX3ofBCgX49XFbCh5BHo11MkwTvqwEJq65iQ",
    "2hNP86KRsNFHioPKDRcNvf9FZFU9qf1PUd4P7skrQNKz",
    "5xJRPb8gUMRw7J2jpbm3hjpW4mhXyWS3xPzhT72s93uY",
    "4zHUZSQDCboLVF8wmmcZG7KQXTmRhdUviQik4yDGYTsU",
    "EB8vJL5Ay1d33bCvZcKof3rTcK6XCAk8V1ZsJsBvNQck",
    "6nmnvbeZpxAtQTnCCE6VfNLjSYAH8raU5GcvgphGQmpE",
    "6s8A8iVdqJotD7BPnr3ZBYWPhfsiXbEyGSJY8TV3ydCf",
    "GP57a1T8vJjeAnF2zfKT1bnZysNwVprLaw6Mjw2d1gPy",
    "Cqdari5C6Lrszk5e9whuYerbtauMJta86VVdqMn56b6X",
)


def write_manual_markets(root, pools=MANUAL_POOLS, luts=MANUAL_LUTS, mint=MANUAL_MINT):
    (root / "markets.toml").write_text(
        "[[group]]\n"
        f"mint_a = {json.dumps(mint)}\n"
        f"markets_a = {json.dumps(list(pools))}\n"
        f"luts = {json.dumps(list(luts))}\n"
    )


def valid_config():
    dynamic_off = {
        "enable": False,
        "min_percentile": 10,
        "max_percentile": 85,
        "min": 1,
        "max": 2,
    }
    return {
        "wallet": {"private_key": "secret-wallet"},
        "rpc": {"url": "https://rpc.invalid/?token=secret-rpc"},
        "flashloan": {"enabled": True},
        "stop": {"min_balance_lamports": 100_000_000},
        "dynamic_fees": {"enabled": False},
        "auto": {"enabled": True},
        "spam": {
            "enabled": True,
            "sending_rpc_urls": ["https://send.invalid/secret"],
            "dynamic_priority_fee": copy.deepcopy(dynamic_off),
        },
        "jito": {
            "enabled": True,
            "dynamic_tip": copy.deepcopy(dynamic_off),
        },
        "helius": {
            "enabled": False,
            "dynamic_priority_fee": copy.deepcopy(dynamic_off),
            "dynamic_tip": copy.deepcopy(dynamic_off),
        },
        "helius_swqos": {
            "enabled": True,
            "dynamic_priority_fee": copy.deepcopy(dynamic_off),
            "dynamic_tip": copy.deepcopy(dynamic_off),
        },
        "circular": {
            "enabled": True,
            "api-key": "secret-circular",
            "dynamic_priority_fee": copy.deepcopy(dynamic_off),
            "dynamic_tip": copy.deepcopy(dynamic_off),
        },
        "temporal": {
            "enabled": False,
            "dynamic_priority_fee": copy.deepcopy(dynamic_off),
            "dynamic_tip": copy.deepcopy(dynamic_off),
        },
        "falcon": {
            "enabled": True,
            "uuid": "secret-falcon",
            "dynamic_priority_fee": copy.deepcopy(dynamic_off),
            "dynamic_tip": copy.deepcopy(dynamic_off),
        },
    }


class ConfigGuardTests(unittest.TestCase):
    def test_load_config_expands_exact_environment_values(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config.toml"
            path.write_text('[wallet]\nprivate_key = "$TEST_PRIVATE_KEY"\n')
            old = __import__("os").environ.get("TEST_PRIVATE_KEY")
            __import__("os").environ["TEST_PRIVATE_KEY"] = "expanded-secret"
            try:
                self.assertEqual(load_config(path)["wallet"]["private_key"], "expanded-secret")
            finally:
                if old is None:
                    __import__("os").environ.pop("TEST_PRIVATE_KEY", None)
                else:
                    __import__("os").environ["TEST_PRIVATE_KEY"] = old

    def test_accepts_valid_config(self):
        self.assertEqual(validate_config(valid_config()), [])

    def test_requires_exact_sender_set(self):
        expected = {
            "spam": True,
            "jito": True,
            "helius": False,
            "helius_swqos": True,
            "circular": True,
            "temporal": False,
            "falcon": True,
        }
        for section, enabled in expected.items():
            with self.subTest(section=section):
                config = valid_config()
                config[section]["enabled"] = not enabled
                self.assertTrue(any(section in error for error in validate_config(config)))

    def test_ab_profile_allows_only_swqos_to_be_disabled(self):
        config = valid_config()
        config["helius_swqos"]["enabled"] = False
        self.assertEqual(validate_config(config, profile="ab-no-swqos"), [])
        config["falcon"]["enabled"] = False
        self.assertTrue(validate_config(config, profile="ab-no-swqos"))

    def test_requires_auto_flashloan_and_static_fees(self):
        mutations = [
            ("auto", "enabled", False),
            ("flashloan", "enabled", False),
            ("dynamic_fees", "enabled", True),
            ("spam", "dynamic_priority_fee", {"enable": True}),
            ("jito", "dynamic_tip", {"enable": True}),
        ]
        for section, key, value in mutations:
            with self.subTest(section=section, key=key):
                config = valid_config()
                config[section][key] = value
                self.assertNotEqual(validate_config(config), [])

    def test_requires_credentials(self):
        mutations = [
            ("wallet", "private_key"),
            ("rpc", "url"),
            ("circular", "api-key"),
            ("falcon", "uuid"),
        ]
        for section, key in mutations:
            with self.subTest(section=section, key=key):
                config = valid_config()
                config[section][key] = ""
                self.assertTrue(any(f"{section}.{key}" in error for error in validate_config(config)))

        config = valid_config()
        config["spam"]["sending_rpc_urls"] = [""]
        self.assertTrue(any("spam.sending_rpc_urls" in error for error in validate_config(config)))

    def test_fee_ranges_require_strictly_increasing_bounds(self):
        range_fields = [
            ("spam", "priority_fee", "from", "to"),
            ("jito", "tip_config", "from", "to"),
            ("helius_swqos", "tip_lamports", "from", "to"),
            ("circular", "tip_lamports", "from", "to"),
            ("falcon", "tip_lamports", "from", "to"),
            ("circular", "dynamic_tip", "min", "max"),
        ]
        for section, key, lower_key, upper_key in range_fields:
            with self.subTest(section=section, key=key):
                config = valid_config()
                config[section][key] = {
                    **config[section].get(key, {}),
                    lower_key: 100,
                    upper_key: 100,
                }
                errors = validate_config(config)
                self.assertTrue(any(f"{section}.{key}" in error for error in errors))

    def test_manual_single_requires_exact_manual_market_group(self):
        config = valid_config()
        config["auto"]["enabled"] = False
        config["markets_file"] = [
            {
                "enabled": True,
                "path": "markets.toml",
                "update_seconds": 0,
                "auto_luts": False,
            }
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_manual_markets(root)
            self.assertEqual(validate_config(config, profile="manual-single", root=root), [])

            write_manual_markets(root, pools=MANUAL_POOLS[:-1])
            self.assertTrue(validate_config(config, profile="manual-single", root=root))

            write_manual_markets(root, pools=MANUAL_POOLS[:-1] + ("wrong-manual-pool",))
            self.assertTrue(validate_config(config, profile="manual-single", root=root))

            write_manual_markets(root, luts=MANUAL_LUTS[:-1])
            self.assertTrue(validate_config(config, profile="manual-single", root=root))

            write_manual_markets(root, luts=())
            self.assertTrue(validate_config(config, profile="manual-single", root=root))

            write_manual_markets(root, luts=("",) + MANUAL_LUTS[1:])
            self.assertTrue(validate_config(config, profile="manual-single", root=root))

            write_manual_markets(root, luts=MANUAL_LUTS[:-1] + ("wrong-manual-lut",))
            self.assertTrue(validate_config(config, profile="manual-single", root=root))

            write_manual_markets(root, mint="wrong-manual-mint")
            self.assertTrue(validate_config(config, profile="manual-single", root=root))

            write_manual_markets(root)
            (root / "markets.toml").write_text(
                (root / "markets.toml").read_text() + 'mint_b = "second-manual-mint"\n'
            )
            self.assertTrue(validate_config(config, profile="manual-single", root=root))

            write_manual_markets(root)
            config["auto"]["enabled"] = True
            self.assertTrue(validate_config(config, profile="manual-single", root=root))

    def test_automatic_profiles_reject_disabled_auto(self):
        for profile in (
            "default",
            "ab-no-swqos",
            "only-spam",
            "only-jito",
            "circular-falcon",
            "circular-falcon-spam",
        ):
            with self.subTest(profile=profile):
                config = valid_config()
                config["auto"]["enabled"] = False
                self.assertIn("auto.enabled must be true", validate_config(config, profile=profile))

    def test_single_mint_auto_rejects_every_enabled_static_market_source(self):
        config = valid_config()
        config["markets_file"] = [
            {"enabled": False, "path": "disabled.toml"},
            {"enabled": False, "path": "also-disabled.toml"},
        ]
        self.assertEqual(
            validate_config(config, profile="single-mint-auto"),
            [],
        )

        for sources in (
            [{"enabled": True, "path": "markets.toml"}],
            [{"path": "markets.toml"}],
            ["markets.toml"],
            {"enabled": False},
        ):
            with self.subTest(sources=sources):
                config["markets_file"] = sources
                errors = validate_config(
                    config,
                    profile="single-mint-auto",
                )
                self.assertIn(
                    "single-mint auto requires all static market sources disabled",
                    errors,
                )

    def test_selector_diagnostic_requires_cardinality_controls(self):
        config = valid_config()
        config["markets_file"] = [
            {"enabled": False, "path": "disabled-markets.toml"},
        ]
        config["auto"]["force_two_mints"] = False
        config["auto"]["filters"] = {"limit": 1}
        config["bot"] = {"merge_mints": False}

        self.assertEqual(
            validate_config(config, profile="selector-diagnostic"),
            [],
        )

        for section, key, value, expected_error in (
            ("auto", "enabled", False, "auto.enabled must be true"),
            (
                "markets_file",
                None,
                [{"enabled": True, "path": "enabled-markets.toml"}],
                "single-mint auto requires all static market sources disabled",
            ),
            ("auto", "force_two_mints", True, "auto.force_two_mints must be false"),
            ("auto", "filters", {"limit": 2}, "auto.filters.limit must be 1"),
            ("bot", "merge_mints", True, "bot.merge_mints must be false"),
        ):
            with self.subTest(expected_error=expected_error):
                changed = copy.deepcopy(config)
                if key is None:
                    changed[section] = value
                else:
                    changed[section][key] = value
                self.assertIn(
                    expected_error,
                    validate_config(changed, profile="selector-diagnostic"),
                )

    def test_never_includes_secret_values_in_errors(self):
        sentinel = "NEVER_PRINT_THIS_SECRET"
        config = valid_config()
        config["wallet"]["private_key"] = sentinel
        config["rpc"]["url"] = sentinel
        config["circular"]["api-key"] = sentinel
        config["falcon"]["uuid"] = sentinel
        config["spam"]["enabled"] = False
        self.assertNotIn(sentinel, "\n".join(validate_config(config)))

    def test_wallet_secret_derives_solana_pubkey(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            keypair_path = Path(temp_dir) / "keypair.json"
            subprocess.run(
                [
                    "solana-keygen",
                    "new",
                    "--no-bip39-passphrase",
                    "--silent",
                    "--force",
                    "--outfile",
                    str(keypair_path),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            expected = subprocess.run(
                ["solana-keygen", "pubkey", str(keypair_path)],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            secret = base58_encode(bytes(json.loads(keypair_path.read_text())))
            self.assertEqual(wallet_pubkey(secret), expected)

    def test_stop_decision_uses_25m_early_threshold(self):
        self.assertEqual(EARLY_STOP_LAMPORTS, 25_000_000)
        self.assertFalse(should_stop_for_loss(100_000_000, 75_000_001))
        self.assertTrue(should_stop_for_loss(100_000_000, 75_000_000))


class RpcTests(unittest.TestCase):
    def test_get_balance_accepts_valid_response(self):
        def transport(url, payload, timeout):
            self.assertEqual(payload["method"], "getBalance")
            self.assertEqual(timeout, 5)
            return {"jsonrpc": "2.0", "result": {"value": 123}, "id": 1}

        self.assertEqual(get_balance_lamports("https://rpc.invalid", "pubkey", transport), 123)

    def test_get_balance_sanitizes_transport_errors(self):
        sentinel = "NEVER_PRINT_THIS_SECRET"

        def transport(url, payload, timeout):
            raise RuntimeError(sentinel)

        with self.assertRaisesRegex(GuardError, "RPC balance check failed") as caught:
            get_balance_lamports(f"https://rpc.invalid/{sentinel}", "pubkey", transport)
        self.assertNotIn(sentinel, str(caught.exception))

    def test_token_account_snapshot_returns_exact_public_key_union(self):
        observed_programs = []

        def transport(url, payload, timeout):
            del url
            self.assertEqual(payload["method"], "getTokenAccountsByOwner")
            self.assertEqual(timeout, 5)
            program = payload["params"][1]["programId"]
            observed_programs.append(program)
            suffix = "legacy" if program == zavod_guard.TOKEN_PROGRAM_ID else "2022"
            return {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "value": [
                        {"pubkey": "shared-account"},
                        {"pubkey": f"{suffix}-account"},
                    ]
                },
            }

        try:
            accounts = zavod_guard.get_token_account_pubkeys(
                "https://rpc.invalid",
                "wallet-public-key",
                transport,
            )
        except AttributeError as exc:
            self.fail(f"token-account snapshot is unavailable: {exc}")

        self.assertEqual(
            accounts,
            frozenset(
                {
                    "shared-account",
                    "legacy-account",
                    "2022-account",
                }
            ),
        )
        self.assertEqual(
            observed_programs,
            [
                zavod_guard.TOKEN_PROGRAM_ID,
                zavod_guard.TOKEN_2022_PROGRAM_ID,
            ],
        )

    def test_token_mint_validation_uses_read_only_fake_rpc(self):
        observed = []

        def transport(url, payload, timeout):
            observed.append((url, payload, timeout))
            return {
                "result": {
                    "value": {
                        "executable": False,
                        "owner": zavod_guard.TOKEN_2022_PROGRAM_ID,
                        "data": {
                            "parsed": {
                                "type": "mint",
                                "info": {"isInitialized": True},
                            }
                        },
                    }
                }
            }

        try:
            zavod_guard.validate_token_mint_account(
                "https://fixture.invalid",
                DIAGNOSTIC_TARGET,
                transport=transport,
            )
        except AttributeError as exc:
            self.fail(f"guarded mint validation is missing: {exc}")

        self.assertEqual(len(observed), 1)
        _url, payload, timeout = observed[0]
        self.assertEqual(payload["method"], "getAccountInfo")
        self.assertEqual(
            payload["params"],
            [
                DIAGNOSTIC_TARGET,
                {"encoding": "jsonParsed", "commitment": "confirmed"},
            ],
        )
        self.assertEqual(timeout, 5)

        invalid_values = (
            None,
            {
                "executable": True,
                "owner": zavod_guard.TOKEN_PROGRAM_ID,
                "data": {
                    "parsed": {
                        "type": "mint",
                        "info": {"isInitialized": True},
                    }
                },
            },
            {
                "executable": False,
                "owner": "invalid-owner",
                "data": {
                    "parsed": {
                        "type": "mint",
                        "info": {"isInitialized": True},
                    }
                },
            },
            {
                "executable": False,
                "owner": zavod_guard.TOKEN_PROGRAM_ID,
                "data": {
                    "parsed": {
                        "type": "account",
                        "info": {"isInitialized": True},
                    }
                },
            },
            {
                "executable": False,
                "owner": zavod_guard.TOKEN_PROGRAM_ID,
                "data": {
                    "parsed": {
                        "type": "mint",
                        "info": {"isInitialized": False},
                    }
                },
            },
        )
        for value in invalid_values:
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    GuardError,
                    "^RPC mint-account check failed$",
                ):
                    zavod_guard.validate_token_mint_account(
                        "https://fixture.invalid",
                        DIAGNOSTIC_TARGET,
                        transport=lambda *_args, value=value: {
                            "result": {"value": value}
                        },
                    )


class PreflightTests(unittest.TestCase):
    def test_load_config_rejects_invalid_toml_without_echoing_content(self):
        sentinel = "NEVER_PRINT_THIS_SECRET"
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config.toml"
            path.write_text(f'private_key = "{sentinel}"\ninvalid = [')
            with self.assertRaisesRegex(GuardError, "config.toml is invalid") as caught:
                load_config(path)
            self.assertNotIn(sentinel, str(caught.exception))

    def test_preflight_returns_safe_summary(self):
        config = valid_config()
        config["wallet"]["private_key"] = "test-secret"
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            binary = root / "zavod-mev-bot-rust-version-cli"
            binary.write_text("#!/usr/bin/env bash\necho 'zavod-mev-bot-rust-version-cli 0.2.2'\n")
            binary.chmod(0o755)
            config_path = root / "config.toml"
            config_path.write_text("# test fixture")
            config_path.chmod(0o600)

            summary = preflight(
                config_path,
                root=root,
                config=config,
                pubkey_resolver=lambda secret: "public-address",
                balance_reader=lambda url, pubkey: 200_000_000,
                disk_free_reader=lambda path: 200 * 1024 * 1024,
            )
            self.assertEqual(summary["preflight"], "ok")
            self.assertEqual(summary["cli_version"], "0.2.2")
            self.assertEqual(summary["wallet"], "public-address")
            self.assertEqual(summary["balance_lamports"], 200_000_000)
            self.assertNotIn("test-secret", json.dumps(summary))

    def test_preflight_executes_held_binary_descriptor_for_version(self):
        config = valid_config()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            binary = root / "held-cli"
            binary.write_text(
                "#!/usr/bin/env bash\n"
                "echo 'zavod-mev-bot-rust-version-cli 0.2.2'\n"
            )
            binary.chmod(0o700)
            binary_descriptor = os.open(binary, os.O_RDONLY)
            config_path = root / "config.toml"
            config_path.write_text("# test fixture")
            config_path.chmod(0o600)
            try:
                summary = preflight(
                    config_path,
                    root=root,
                    config=config,
                    pubkey_resolver=lambda secret: "public-address",
                    balance_reader=lambda url, pubkey: 200_000_000,
                    disk_free_reader=lambda path: 200 * 1024 * 1024,
                    binary_path=Path(
                        f"/proc/self/fd/{binary_descriptor}"
                    ),
                    binary_fd=binary_descriptor,
                )
            finally:
                os.close(binary_descriptor)
        self.assertEqual(summary["cli_version"], "0.2.2")

    def test_preflight_rejects_insufficient_balance(self):
        config = valid_config()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            binary = root / "zavod-mev-bot-rust-version-cli"
            binary.write_text("#!/usr/bin/env bash\necho 'zavod-mev-bot-rust-version-cli 0.2.2'\n")
            binary.chmod(0o755)
            config_path = root / "config.toml"
            config_path.write_text("# test fixture")
            config_path.chmod(0o600)
            with self.assertRaisesRegex(GuardError, "insufficient wallet balance"):
                preflight(
                    config_path,
                    root=root,
                    config=config,
                    pubkey_resolver=lambda secret: "public-address",
                    balance_reader=lambda url, pubkey: 130_000_000,
                    disk_free_reader=lambda path: 200 * 1024 * 1024,
                )


class FakeChild:
    def __init__(self, pid=4321, poll_values=None):
        self.pid = pid
        self.poll_values = iter(poll_values or [None])
        self.returncode = None

    def poll(self):
        try:
            value = next(self.poll_values)
        except StopIteration:
            value = None
        if value is not None:
            self.returncode = value
        return value

    def wait(self, timeout=None):
        self.returncode = 0 if self.returncode is None else self.returncode
        return self.returncode


class SupervisorTests(unittest.TestCase):
    def test_stops_at_25m_observed_loss(self):
        balances = iter([75_000_001, 75_000_000])
        signals = []
        result = supervise(
            child=FakeChild(),
            start_balance=100_000_000,
            balance_reader=lambda: next(balances),
            monotonic=iter([0, 1, 2]).__next__,
            sleep=lambda seconds: None,
            killpg=lambda pid, sig: signals.append((pid, sig)),
            group_exists=Mock(side_effect=[True, False]),
            signal_grace=((signal.SIGINT, 0),),
            timeout_seconds=300,
        )
        self.assertEqual(result["reason"], "loss_threshold")
        self.assertEqual(result["end_balance"], 75_000_000)
        self.assertEqual(signals[0], (4321, signal.SIGINT))

    def test_diagnostic_stops_on_any_positive_loss_without_changing_live_limit(self):
        diagnostic = supervise(
            child=FakeChild(poll_values=[None, 0]),
            start_balance=100_000_000,
            balance_reader=lambda: 99_999_999,
            monotonic=iter([0, 1]).__next__,
            sleep=lambda seconds: None,
            killpg=lambda pid, sig: None,
            group_exists=Mock(side_effect=[True, False]),
            signal_grace=((signal.SIGINT, 0),),
            timeout_seconds=300,
            diagnostic=True,
        )
        live = supervise(
            child=FakeChild(poll_values=[None, 0]),
            start_balance=100_000_000,
            balance_reader=lambda: 99_999_999,
            monotonic=iter([0, 1]).__next__,
            sleep=lambda seconds: None,
            killpg=lambda pid, sig: None,
            group_exists=lambda pid: False,
            timeout_seconds=300,
        )

        self.assertEqual(
            diagnostic["reason"],
            "diagnostic_loss_violation",
        )
        self.assertEqual(diagnostic["observed_loss"], 1)
        self.assertEqual(live["reason"], "child_exit")

    def test_live_integrity_profile_stops_on_protected_output(self):
        protected_output = threading.Event()
        protected_output.set()
        result = supervise(
            child=FakeChild(),
            start_balance=100_000_000,
            balance_reader=lambda: 100_000_000,
            monotonic=iter([0, 1]).__next__,
            sleep=lambda seconds: None,
            protected_output_event=protected_output,
            killpg=lambda pid, sig: None,
            group_exists=Mock(side_effect=[True, False]),
            signal_grace=((signal.SIGINT, 0),),
            enforce_input_integrity=True,
        )
        self.assertEqual(result["reason"], "protected_output_violation")

    def test_diagnostic_stops_on_token_account_growth(self):
        try:
            result = supervise(
                child=FakeChild(),
                start_balance=100_000_000,
                balance_reader=lambda: 100_000_000,
                token_account_reader=lambda: frozenset(
                    {"existing-account", "new-account"}
                ),
                starting_token_accounts=frozenset({"existing-account"}),
                monotonic=iter([0, 1]).__next__,
                sleep=lambda seconds: None,
                killpg=lambda pid, sig: None,
                group_exists=Mock(side_effect=[True, False]),
                signal_grace=((signal.SIGINT, 0),),
                timeout_seconds=300,
                diagnostic=True,
            )
        except TypeError as exc:
            self.fail(f"token-account growth is not supervised: {exc}")
        self.assertEqual(
            result["reason"],
            "token_account_growth_violation",
        )

    def test_diagnostic_token_account_rpc_failure_is_generic_rpc_error(self):
        def failed_snapshot():
            raise GuardError("fixture detail must not survive")

        try:
            result = supervise(
                child=FakeChild(),
                start_balance=100_000_000,
                balance_reader=lambda: 100_000_000,
                token_account_reader=failed_snapshot,
                starting_token_accounts=frozenset({"existing-account"}),
                monotonic=iter([0, 1]).__next__,
                sleep=lambda seconds: None,
                killpg=lambda pid, sig: None,
                group_exists=Mock(side_effect=[True, False]),
                signal_grace=((signal.SIGINT, 0),),
                timeout_seconds=300,
                diagnostic=True,
            )
        except TypeError as exc:
            self.fail(f"token-account RPC failure is not supervised: {exc}")
        self.assertEqual(result["reason"], "rpc_error")
        self.assertNotIn("fixture detail", str(result))

    def test_diagnostic_allows_zero_loss_and_account_removal(self):
        result = supervise(
            child=FakeChild(poll_values=[None, 0]),
            start_balance=100_000_000,
            balance_reader=lambda: 100_000_000,
            token_account_reader=lambda: frozenset({"remaining-account"}),
            starting_token_accounts=frozenset(
                {"remaining-account", "removed-account"}
            ),
            monotonic=iter([0, 1]).__next__,
            sleep=lambda seconds: None,
            killpg=lambda pid, sig: None,
            group_exists=lambda pid: False,
            timeout_seconds=300,
            diagnostic=True,
        )

        self.assertEqual(result["reason"], "child_exit")
        self.assertEqual(result["observed_loss"], 0)

    def test_diagnostic_violation_survives_interrupted_successful_cleanup(self):
        cases = (
            (
                "diagnostic_loss_violation",
                {"balance_reader": lambda: 99_999_999},
            ),
            (
                "token_account_growth_violation",
                {
                    "balance_reader": lambda: 100_000_000,
                    "token_account_reader": lambda: frozenset(
                        {"existing-account", "new-account"}
                    ),
                    "starting_token_accounts": frozenset(
                        {"existing-account"}
                    ),
                },
            ),
        )
        for expected, overrides in cases:
            with self.subTest(reason=expected):
                with patch.object(
                    zavod_guard,
                    "_verified_shutdown",
                    return_value={
                        "exit_code": 0,
                        "group_absent": True,
                        "interrupted": True,
                    },
                ):
                    result = supervise(
                        child=FakeChild(),
                        start_balance=100_000_000,
                        monotonic=iter([0, 1]).__next__,
                        sleep=lambda seconds: None,
                        timeout_seconds=300,
                        diagnostic=True,
                        **overrides,
                    )

                self.assertEqual(result["reason"], expected)

    def test_dispatch_signals_process_group_while_balance_rpc_is_blocked(self):
        dispatch_event = threading.Event()
        balance_entered = threading.Event()
        release_balance = threading.Event()
        signal_sent = threading.Event()
        result_holder = {}

        def blocked_balance():
            balance_entered.set()
            release_balance.wait(2)
            return 100_000_000

        def record_signal(pid, selected_signal):
            self.assertEqual(pid, 4321)
            self.assertEqual(selected_signal, signal.SIGINT)
            signal_sent.set()

        def run_supervisor():
            result_holder["result"] = supervise(
                child=FakeChild(poll_values=[None, 0]),
                start_balance=100_000_000,
                balance_reader=blocked_balance,
                monotonic=iter([0, 1]).__next__,
                sleep=lambda seconds: None,
                test_mode_dispatch_event=dispatch_event,
                killpg=record_signal,
                timeout_seconds=300,
                cleanup_child=False,
                diagnostic=True,
            )

        worker = threading.Thread(target=run_supervisor)
        worker.start()
        self.assertTrue(balance_entered.wait(1))
        dispatch_event.set()
        prompt_signal = signal_sent.wait(0.25)
        release_balance.set()
        worker.join(2)

        self.assertFalse(worker.is_alive())
        self.assertTrue(prompt_signal)
        self.assertEqual(
            result_holder["result"]["reason"],
            "test_mode_dispatch_violation",
        )

    def test_stops_at_timeout(self):
        times = iter([0, 300])
        result = supervise(
            child=FakeChild(),
            start_balance=100_000_000,
            balance_reader=lambda: 100_000_000,
            monotonic=times.__next__,
            sleep=lambda seconds: None,
            killpg=lambda pid, sig: None,
            group_exists=lambda pid: False,
            timeout_seconds=300,
        )
        self.assertEqual(result["reason"], "timeout")

    def test_stops_fail_closed_after_rpc_error(self):
        signals = []

        def failed_balance():
            raise GuardError("RPC balance check failed")

        result = supervise(
            child=FakeChild(),
            start_balance=100_000_000,
            balance_reader=failed_balance,
            monotonic=iter([0, 1]).__next__,
            sleep=lambda seconds: None,
            killpg=lambda pid, sig: signals.append((pid, sig)),
            group_exists=Mock(side_effect=[True, False]),
            signal_grace=((signal.SIGINT, 0),),
            timeout_seconds=300,
        )
        self.assertEqual(result["reason"], "rpc_error")
        self.assertEqual(len(signals), 1)

    def test_child_exit_is_not_restarted(self):
        child = FakeChild(poll_values=[2])
        result = supervise(
            child=child,
            start_balance=100_000_000,
            balance_reader=lambda: 100_000_000,
            monotonic=iter([0, 1]).__next__,
            sleep=lambda seconds: None,
            killpg=lambda pid, sig: None,
            group_exists=lambda pid: False,
            timeout_seconds=300,
        )
        self.assertEqual(result["reason"], "child_exit")
        self.assertEqual(result["child_exit_code"], 2)

    def test_operator_interrupt_stops_child(self):
        signals = []

        def interrupted_balance():
            raise KeyboardInterrupt

        result = supervise(
            child=FakeChild(),
            start_balance=100_000_000,
            balance_reader=interrupted_balance,
            monotonic=iter([0, 1]).__next__,
            sleep=lambda seconds: None,
            killpg=lambda pid, sig: signals.append((pid, sig)),
            group_exists=Mock(side_effect=[True, False]),
            signal_grace=((signal.SIGINT, 0),),
            timeout_seconds=300,
        )
        self.assertEqual(result["reason"], "operator_signal")
        self.assertEqual(signals[0][1], signal.SIGINT)

    def test_redacts_all_config_secrets_from_log_text(self):
        config = valid_config()
        text = " ".join(
            [
                config["wallet"]["private_key"],
                config["rpc"]["url"],
                config["spam"]["sending_rpc_urls"][0],
                config["circular"]["api-key"],
                config["falcon"]["uuid"],
            ]
        )
        redacted = redact_text(text, config)
        self.assertNotIn("secret", redacted)
        self.assertGreaterEqual(redacted.count("<redacted>"), 5)


class StreamingRedactorTests(unittest.TestCase):
    def test_signature_policy_covers_leading_zero_encodings(self):
        policy = zavod_guard.ProtectedOutputPolicy()
        signatures = {
            base58_encode(b"\0" * zeros + b"\xff" * (64 - zeros))
            for zeros in range(65)
        }

        self.assertEqual(min(map(len, signatures)), 64)
        self.assertEqual(max(map(len, signatures)), 88)
        for signature in signatures:
            with self.subTest(length=len(signature)):
                self.assertEqual(
                    policy.redact_text(f"<{signature}>"),
                    "<<redacted>>",
                )

    def test_secret_split_across_chunks_is_never_written(self):
        sink = io.StringIO()
        redactor = zavod_guard.StreamingRedactor(sink, ["secret-value"])
        redactor.feed("before secret-")
        redactor.feed("value after")
        redactor.close()
        self.assertEqual(sink.getvalue(), "before <redacted> after")

    def test_output_pump_stop_unblocks_an_idle_pipe(self):
        read_fd, write_fd = os.pipe()
        source = os.fdopen(read_fd, "rb", buffering=0)
        pump = zavod_guard.OutputPump(source, io.StringIO(), {})
        pump.start()
        try:
            pump.stop()
            pump.join(1)
            self.assertFalse(pump.is_alive())
        finally:
            os.close(write_fd)
            pump.join(1)
            source.close()

    def test_output_pump_preserves_multibyte_secret_across_byte_chunks(self):
        secret = "clé-secret"
        encoded = f"before {secret} after".encode()
        split_at = encoded.index(b"\xc3") + 1
        source = Mock()
        source.read.side_effect = [
            encoded[:split_at],
            encoded[split_at:],
            b"",
        ]
        sink = io.StringIO()
        pump = zavod_guard.OutputPump(
            source,
            sink,
            {"wallet": {"private_key": secret}},
        )
        pump.start()
        pump.join(1)
        self.assertFalse(pump.is_alive())
        self.assertFalse(pump.output_error_event.is_set())
        self.assertEqual(sink.getvalue(), "before <redacted> after")

    def test_protected_output_sets_violation_event_across_chunks(self):
        source = Mock()
        source.read.side_effect = [
            b"before secret-",
            b"wallet after",
            b"",
        ]
        sink = io.StringIO()
        pump = zavod_guard.OutputPump(
            source,
            sink,
            valid_config(),
            test_mode=True,
        )

        pump.start()
        pump.join(1)

        self.assertFalse(pump.is_alive())
        self.assertEqual(sink.getvalue(), "before <redacted> after")
        event = getattr(pump, "protected_output_event", None)
        self.assertIsNotNone(event)
        self.assertTrue(event.is_set())

    def test_test_mode_recognizes_canonical_transaction_sent_marker(self):
        pump = zavod_guard.OutputPump(
            io.BytesIO(b"Transaction sent successfully"),
            io.StringIO(),
            valid_config(),
            test_mode=True,
        )

        pump.start()
        pump.join(1)

        self.assertFalse(pump.is_alive())
        self.assertTrue(pump.test_mode_dispatch_event.is_set())

    def test_test_mode_recognizes_dispatch_marker_split_across_chunks(self):
        class ChunkedSource:
            def __init__(self):
                self.chunks = iter(
                    (
                        b"Transaction sent suc",
                        b"cessfully\n",
                        b"",
                    )
                )

            def read(self, size):
                del size
                return next(self.chunks)

            def fileno(self):
                raise OSError("fixture has no descriptor")

        pump = zavod_guard.OutputPump(
            ChunkedSource(),
            io.StringIO(),
            {},
            test_mode=True,
        )
        pump.start()
        pump.join(1)

        self.assertFalse(pump.is_alive())
        self.assertTrue(pump.test_mode_dispatch_event.is_set())

    def test_test_mode_scans_marker_at_start_of_complete_chunk(self):
        marker = b"dispatching transaction\n"
        first_chunk = marker + b"x" * (4096 - len(marker))
        source = Mock()
        source.read.side_effect = [first_chunk, b""]
        pump = zavod_guard.OutputPump(
            source,
            io.StringIO(),
            valid_config(),
            test_mode=True,
        )

        pump.start()
        pump.join(1)

        self.assertFalse(pump.is_alive())
        self.assertTrue(pump.test_mode_dispatch_event.is_set())

    def test_protected_identifiers_are_redacted_across_chunk_boundaries(self):
        protected_uuid = "12345678-1234-4234-9234-123456789abc"
        protected_signature = base58_encode(b"\x01" * 64)
        short_signature = base58_encode(b"\0" * 16 + b"\x01" * 48)
        public_key = base58_encode(b"\x02" * 32)
        self.assertLess(len(short_signature), 86)
        protected_url = "https://example.invalid/path?credential=value"
        protected_exact = "environment-backed-secret"
        text = "|".join(
            (
                protected_uuid,
                protected_signature,
                short_signature,
                public_key,
                protected_url,
                protected_exact,
            )
        )
        sink = io.StringIO()
        redactor = zavod_guard.StreamingRedactor(
            sink,
            zavod_guard.ProtectedOutputPolicy([protected_exact]),
        )
        for index in range(0, len(text), 7):
            redactor.feed(text[index:index + 7])
        redactor.close()

        rendered = sink.getvalue()
        for protected in (
            protected_uuid,
            protected_signature,
            short_signature,
            protected_url,
            protected_exact,
        ):
            self.assertNotIn(protected, rendered)
        self.assertIn(public_key, rendered)
        self.assertGreaterEqual(rendered.count("<redacted>"), 4)


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

    def test_protected_output_stops_diagnostic_with_fixed_reason(self):
        event = threading.Event()
        event.set()
        child = Mock(pid=123, returncode=-2)
        child.poll.return_value = None
        try:
            result = zavod_guard.supervise(
                child=child,
                start_balance=100,
                balance_reader=lambda: 100,
                monotonic=Mock(side_effect=[0, 0]),
                sleep=lambda _: None,
                protected_output_event=event,
                diagnostic=True,
                killpg=lambda pgid, sig: None,
                group_exists=Mock(side_effect=[True, False]),
                signal_grace=((signal.SIGINT, 0),),
            )
        except TypeError as exc:
            self.fail(f"diagnostic protected-output event is not supervised: {exc}")
        self.assertEqual(result["reason"], "protected_output_violation")

    def test_input_integrity_failure_stops_diagnostic_with_fixed_reason(self):
        child = Mock(pid=123, returncode=-2)
        child.poll.return_value = None
        try:
            result = zavod_guard.supervise(
                child=child,
                start_balance=100,
                balance_reader=lambda: 100,
                monotonic=Mock(side_effect=[0, 0]),
                sleep=lambda _: None,
                input_integrity_checker=lambda: False,
                diagnostic=True,
                killpg=lambda pgid, sig: None,
                group_exists=Mock(side_effect=[True, False]),
                signal_grace=((signal.SIGINT, 0),),
            )
        except TypeError as exc:
            self.fail(f"diagnostic input integrity is not supervised: {exc}")
        self.assertEqual(result["reason"], "input_integrity_violation")

    def test_repeated_interrupts_during_cleanup_fail_closed(self):
        child = Mock(pid=123, returncode=None)
        child.poll.return_value = None
        result = zavod_guard._shutdown_child(
            child,
            killpg=lambda pgid, sig: None,
            group_exists=lambda pgid: True,
            monotonic=Mock(
                side_effect=[KeyboardInterrupt()]
                * zavod_guard.MAX_INTERRUPT_RETRIES
            ),
            sleep=lambda _: None,
            signal_grace=((signal.SIGINT, 0),),
        )
        self.assertFalse(result["group_absent"])
        self.assertTrue(result["interrupted"])

    def test_interrupt_between_cleanup_operations_retries_cleanup(self):
        cleanup = {
            "exit_code": -2,
            "group_absent": True,
            "interrupted": False,
        }
        child = Mock(pid=123, returncode=-2)
        child.poll.return_value = -2
        with patch.object(
            zavod_guard,
            "_shutdown_child",
            side_effect=[KeyboardInterrupt(), cleanup],
        ) as shutdown:
            result = zavod_guard.supervise(
                child=child,
                start_balance=100,
                balance_reader=lambda: 100,
                monotonic=lambda: 0,
                sleep=lambda _: None,
            )
        self.assertEqual(shutdown.call_count, 2)
        self.assertEqual(result["reason"], "operator_signal")

    def test_signal_delivery_oserror_returns_failed_cleanup(self):
        child = Mock(pid=123, returncode=None)
        child.poll.return_value = None
        result = zavod_guard._shutdown_child(
            child,
            killpg=Mock(side_effect=PermissionError("not permitted")),
            group_exists=lambda pgid: True,
            signal_grace=((signal.SIGINT, 0),),
        )
        self.assertEqual(
            result,
            {
                "exit_code": None,
                "group_absent": False,
                "interrupted": False,
            },
        )

    def test_signal_delivery_oserror_makes_supervisor_cleanup_failed(self):
        child = Mock(pid=123, returncode=2)
        child.poll.return_value = 2
        result = zavod_guard.supervise(
            child=child,
            start_balance=100,
            balance_reader=lambda: 100,
            monotonic=lambda: 0,
            sleep=lambda _: None,
            killpg=Mock(side_effect=OSError("signal failed")),
            group_exists=lambda pgid: True,
            signal_grace=((signal.SIGINT, 0),),
        )
        self.assertEqual(result["reason"], "cleanup_failed")


class RunGuardedHardeningTests(unittest.TestCase):
    DIAGNOSTIC_RUN_ID = "20260724T190000Z"
    ORIGINAL_DIAGNOSTIC_BYTES = b"original diagnostic descriptor fixture\n"

    def run_with_mocks(self, pump=None, cleanup=None):
        config = valid_config()
        child = Mock(pid=123, returncode=0, stdout=io.BytesIO())
        cleanup = cleanup or {
            "exit_code": 0,
            "group_absent": True,
            "interrupted": False,
        }
        patches = [
            patch.object(zavod_guard, "load_config", return_value=config),
            patch.object(
                zavod_guard,
                "preflight",
                return_value={
                    "wallet": "public-address",
                    "balance_lamports": 100_000_000,
                },
            ),
            patch.object(zavod_guard.subprocess, "Popen", return_value=child),
            patch.object(
                zavod_guard,
                "supervise",
                return_value={
                    "reason": "child_exit",
                    "start_balance": 100_000_000,
                    "end_balance": 100_000_000,
                    "observed_loss": 0,
                    "child_exit_code": 0,
                },
            ),
            patch.object(
                zavod_guard,
                "_verified_shutdown",
                return_value=cleanup,
            ),
        ]
        if pump is not None:
            patches.append(
                patch.object(zavod_guard, "OutputPump", return_value=pump)
            )
        with tempfile.TemporaryDirectory() as temp_dir:
            with patches[0], patches[1], patches[2], patches[3], patches[4]:
                if pump is None:
                    return zavod_guard.run_guarded(
                        Path(temp_dir) / "config.toml"
                    )
                with patches[5]:
                    return zavod_guard.run_guarded(
                        Path(temp_dir) / "config.toml"
                    )

    def prepare_diagnostic_workspace(self, root, config_bytes=None):
        root = Path(root)
        binary = root / "zavod-mev-bot-rust-version-cli"
        binary.touch()
        state = root / "state"
        mint_runs = state / "mint-runs"
        run_dir = mint_runs / self.DIAGNOSTIC_RUN_ID
        state.mkdir(mode=0o700)
        mint_runs.mkdir(mode=0o700)
        run_dir.mkdir(mode=0o700)
        for directory in (state, mint_runs, run_dir):
            directory.chmod(0o700)
        marker = state / ".mint-run-active"
        marker.write_text(f"{self.DIAGNOSTIC_RUN_ID}\n")
        marker.chmod(0o600)
        config_path = run_dir / "selector-diagnostic.toml"
        config_path.write_bytes(
            config_bytes
            if config_bytes is not None
            else self.ORIGINAL_DIAGNOSTIC_BYTES
        )
        config_path.chmod(0o600)
        tokens_path = root / "tokens.toml"
        tokens_path.write_bytes(
            f'tokens = ["{DIAGNOSTIC_TARGET}"]\n'.encode()
        )
        tokens_path.chmod(0o600)
        return {
            "binary": binary,
            "state": state,
            "mint_runs": mint_runs,
            "run_dir": run_dir,
            "marker": marker,
            "config": config_path,
            "tokens": tokens_path,
        }

    def diagnostic_contract(self, paths):
        return {
            "diagnostic_mode": "d0",
            "diagnostic_target": DIAGNOSTIC_TARGET,
            "diagnostic_config_sha256": hashlib.sha256(
                paths["config"].read_bytes()
            ).hexdigest(),
            "diagnostic_tokens_sha256": hashlib.sha256(
                paths["tokens"].read_bytes()
            ).hexdigest(),
        }

    def original_diagnostic_contract(self):
        return {
            "diagnostic_mode": "d0",
            "diagnostic_target": DIAGNOSTIC_TARGET,
            "diagnostic_config_sha256": hashlib.sha256(
                self.ORIGINAL_DIAGNOSTIC_BYTES
            ).hexdigest(),
            "diagnostic_tokens_sha256": hashlib.sha256(
                f'tokens = ["{DIAGNOSTIC_TARGET}"]\n'.encode()
            ).hexdigest(),
        }

    def assert_diagnostic_identity_rejected(
        self,
        config_path,
        root,
        pattern="selector-diagnostic",
    ):
        config = valid_config()
        child = Mock(pid=123, returncode=0, stdout=io.BytesIO())
        summary = {"wallet": "public-address", "balance_lamports": 100_000_000}
        supervised = {
            "reason": "child_exit",
            "start_balance": 100_000_000,
            "end_balance": 100_000_000,
            "observed_loss": 0,
            "child_exit_code": 0,
        }
        cleanup = {"exit_code": 0, "group_absent": True, "interrupted": False}
        with (
            patch.object(zavod_guard, "load_config", return_value=config),
            patch.object(zavod_guard, "load_config_bytes", return_value=config),
            patch.object(zavod_guard, "preflight", return_value=summary),
            patch.object(
                zavod_guard.subprocess,
                "Popen",
                return_value=child,
            ) as launch,
            patch.object(zavod_guard, "supervise", return_value=supervised),
            patch.object(zavod_guard, "_verified_shutdown", return_value=cleanup),
            self.assertRaisesRegex(GuardError, pattern),
        ):
            zavod_guard.run_guarded(
                config_path,
                profile="selector-diagnostic",
                test_mode=True,
                workspace_root=root,
                **self.original_diagnostic_contract(),
            )
        launch.assert_not_called()

    def test_output_pump_drains_and_redacts_before_log_close(self):
        config = valid_config()
        child = Mock(pid=123, returncode=0)
        child.stdout = io.BytesIO(b"before secret-wallet after")

        def fake_popen(*args, **kwargs):
            self.assertIs(kwargs["stdout"], subprocess.PIPE)
            self.assertIs(kwargs["stderr"], subprocess.STDOUT)
            return child

        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            with (
                patch.object(zavod_guard, "load_config", return_value=config),
                patch.object(
                    zavod_guard,
                    "preflight",
                    return_value={
                        "wallet": "public-address",
                        "balance_lamports": 100_000_000,
                    },
                ),
                patch.object(
                    zavod_guard.subprocess,
                    "Popen",
                    side_effect=fake_popen,
                ),
                patch.object(
                    zavod_guard,
                    "supervise",
                    return_value={
                        "reason": "child_exit",
                        "start_balance": 100_000_000,
                        "end_balance": 100_000_000,
                        "observed_loss": 0,
                        "child_exit_code": 0,
                    },
                ),
                patch.object(
                    zavod_guard,
                    "_shutdown_child",
                    return_value={
                        "exit_code": 0,
                        "group_absent": True,
                        "interrupted": False,
                    },
                ),
            ):
                result = zavod_guard.run_guarded(config_path)

            log_text = (Path(temp_dir) / result["log_path"]).read_text()
            self.assertEqual(log_text, "before <redacted> after")

    def test_output_failure_does_not_raise_during_finalization(self):
        pump = Mock()
        pump.output_error_event = threading.Event()
        pump.output_error_event.set()
        pump.is_alive.return_value = False
        pump.redactor.close.side_effect = OSError("sink failed")
        result = self.run_with_mocks(pump=pump)
        self.assertEqual(result["reason"], "output_error")

    def test_cleanup_interrupt_becomes_operator_signal(self):
        result = self.run_with_mocks(
            cleanup={
                "exit_code": 0,
                "group_absent": True,
                "interrupted": True,
            }
        )
        self.assertEqual(result["reason"], "operator_signal")

    def test_interrupt_during_pump_join_is_retried(self):
        pump = Mock()
        pump.output_error_event = threading.Event()
        pump.join.side_effect = [KeyboardInterrupt(), None]
        pump.is_alive.return_value = False
        result = self.run_with_mocks(pump=pump)
        self.assertEqual(pump.join.call_count, 2)
        self.assertEqual(result["reason"], "operator_signal")

    def test_interrupt_during_pump_start_never_joins_unstarted_pump(self):
        prior_sigterm = signal.getsignal(signal.SIGTERM)
        pump = Mock()
        pump.output_error_event = threading.Event()
        pump.start.side_effect = KeyboardInterrupt()
        pump.join.side_effect = RuntimeError(
            "cannot join thread before it is started"
        )
        restored_sigterm = None
        try:
            result = self.run_with_mocks(pump=pump)
            restored_sigterm = signal.getsignal(signal.SIGTERM)
        finally:
            signal.signal(signal.SIGTERM, prior_sigterm)
        self.assertEqual(result["reason"], "operator_signal")
        pump.join.assert_not_called()
        self.assertIs(restored_sigterm, prior_sigterm)

    def test_output_error_during_pump_start_restores_handler(self):
        prior_sigterm = signal.getsignal(signal.SIGTERM)
        pump = Mock()
        pump.output_error_event = threading.Event()
        pump.start.side_effect = OSError("thread start failed")
        pump.join.side_effect = RuntimeError(
            "cannot join thread before it is started"
        )
        restored_sigterm = None
        try:
            result = self.run_with_mocks(pump=pump)
            restored_sigterm = signal.getsignal(signal.SIGTERM)
        finally:
            signal.signal(signal.SIGTERM, prior_sigterm)
        self.assertEqual(result["reason"], "output_error")
        pump.join.assert_not_called()
        self.assertIs(restored_sigterm, prior_sigterm)

    def test_signal_during_popen_startup_latches_then_cleans_returned_group(self):
        config = valid_config()
        child = Mock(pid=123, returncode=-signal.SIGTERM, stdout=io.BytesIO())
        cleanup = {
            "exit_code": -signal.SIGTERM,
            "group_absent": True,
            "interrupted": False,
        }

        def popen_with_startup_signal(*args, **kwargs):
            del args, kwargs
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            return child

        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            with (
                patch.object(zavod_guard, "load_config", return_value=config),
                patch.object(
                    zavod_guard,
                    "preflight",
                    return_value={
                        "wallet": "public-address",
                        "balance_lamports": 100_000_000,
                    },
                ),
                patch.object(
                    zavod_guard.subprocess,
                    "Popen",
                    side_effect=popen_with_startup_signal,
                ),
                patch.object(
                    zavod_guard,
                    "_verified_shutdown",
                    return_value=cleanup,
                ) as shutdown,
                patch.object(zavod_guard, "OutputPump") as output_pump,
            ):
                result = zavod_guard.run_guarded(config_path)

        self.assertEqual(result["reason"], "operator_signal")
        shutdown.assert_called_once_with(child)
        output_pump.assert_not_called()

    def test_run_guarded_defensively_rejects_timeout_outside_bounds(self):
        for value in (29, 301):
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    GuardError,
                    "timeout must be from 30 through 300 seconds",
                ):
                    zavod_guard.run_guarded("unused-config.toml", value)

    def test_selector_diagnostic_uses_exact_test_mode_argv(self):
        config = valid_config()
        config["markets_file"] = [{"enabled": False, "path": "disabled.toml"}]
        config["auto"]["force_two_mints"] = False
        config["auto"]["filters"] = {"limit": 1}
        config["bot"] = {"merge_mints": False}
        child = Mock(pid=123, returncode=0, stdout=io.BytesIO())
        summary = {"wallet": "public-address", "balance_lamports": 100_000_000}
        supervised = {
            "reason": "child_exit",
            "start_balance": 100_000_000,
            "end_balance": 100_000_000,
            "observed_loss": 0,
            "child_exit_code": 0,
        }
        cleanup = {"exit_code": 0, "group_absent": True, "interrupted": False}

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = self.prepare_diagnostic_workspace(root)
            binary = paths["binary"]
            config_path = paths["config"]

            with self.assertRaisesRegex(GuardError, "must be provided together"):
                zavod_guard.run_guarded(
                    config_path,
                    profile="selector-diagnostic",
                    workspace_root=root,
                )
            with self.assertRaisesRegex(GuardError, "must be provided together"):
                zavod_guard.run_guarded(
                    config_path,
                    test_mode=True,
                    workspace_root=root,
                )

            config_path.chmod(0o640)
            with self.assertRaisesRegex(GuardError, "permissions must be mode 600"):
                zavod_guard.run_guarded(
                    config_path,
                    profile="selector-diagnostic",
                    test_mode=True,
                    workspace_root=root,
                    token_account_snapshot_reader=lambda *_: frozenset(),
                    mint_account_validator=lambda *_: None,
                    **self.diagnostic_contract(paths),
                )
            config_path.chmod(0o600)

            outside_path = root.parent / "outside-selector-diagnostic.toml"
            outside_path.touch(mode=0o600)
            outside_path.chmod(0o600)
            try:
                with self.assertRaisesRegex(GuardError, "inside the workspace"):
                    zavod_guard.run_guarded(
                        outside_path,
                        profile="selector-diagnostic",
                        test_mode=True,
                        workspace_root=root,
                        **self.diagnostic_contract(paths),
                    )
            finally:
                outside_path.unlink()

            def popen(argv, **kwargs):
                passed = kwargs.get("pass_fds")
                self.assertIsNotNone(passed)
                self.assertEqual(len(passed), 2)
                descriptor = int(Path(argv[3]).name)
                tokens_descriptor = next(
                    value for value in passed if value != descriptor
                )
                self.assertEqual(
                    argv,
                    [
                        str(binary),
                        "run",
                        "--config",
                        f"/proc/self/fd/{descriptor}",
                        "--test-mode",
                    ],
                )
                self.assertTrue(Path(argv[3]).is_absolute())
                self.assertEqual(
                    Path(argv[3]).read_bytes(),
                    self.ORIGINAL_DIAGNOSTIC_BYTES,
                )
                self.assertEqual(
                    Path(f"/proc/self/fd/{tokens_descriptor}").read_bytes(),
                    f'tokens = ["{DIAGNOSTIC_TARGET}"]\n'.encode(),
                )
                return child

            with (
                patch.object(zavod_guard, "load_config", return_value=config),
                patch.object(
                    zavod_guard,
                    "load_config_bytes",
                    return_value=config,
                ),
                patch.object(zavod_guard, "preflight", return_value=summary),
                patch.object(zavod_guard.subprocess, "Popen", side_effect=popen),
                patch.object(zavod_guard, "supervise", return_value=supervised),
                patch.object(zavod_guard, "_verified_shutdown", return_value=cleanup),
            ):
                zavod_guard.run_guarded(
                    config_path,
                    profile="selector-diagnostic",
                    test_mode=True,
                    workspace_root=root,
                    token_account_snapshot_reader=lambda *_: frozenset(),
                    mint_account_validator=lambda *_: None,
                    **self.diagnostic_contract(paths),
                )

            shutil.rmtree(root / "logs")

            def live_popen(argv, **kwargs):
                self.assertEqual(argv, [str(binary), "run"])
                return child

            with (
                patch.object(zavod_guard, "load_config", return_value=config),
                patch.object(
                    zavod_guard,
                    "load_config_bytes",
                    return_value=config,
                ),
                patch.object(zavod_guard, "preflight", return_value=summary),
                patch.object(
                    zavod_guard.subprocess,
                    "Popen",
                    side_effect=live_popen,
                ) as launch,
                patch.object(zavod_guard, "supervise", return_value=supervised),
                patch.object(zavod_guard, "_verified_shutdown", return_value=cleanup),
            ):
                zavod_guard.run_guarded(config_path, workspace_root=root)
            self.assertEqual(
                launch.call_args.args[0],
                [str(binary), "run"],
            )

    def test_selector_diagnostic_requires_exact_d0_launch_contract(self):
        config = valid_config()
        child = Mock(pid=123, returncode=0, stdout=io.BytesIO())
        summary = {"wallet": "public-address", "balance_lamports": 100_000_000}
        supervised = {
            "reason": "child_exit",
            "start_balance": 100_000_000,
            "end_balance": 100_000_000,
            "observed_loss": 0,
            "child_exit_code": 0,
        }
        cleanup = {"exit_code": 0, "group_absent": True, "interrupted": False}

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = self.prepare_diagnostic_workspace(root)
            patches = (
                patch.object(
                    zavod_guard,
                    "load_config_bytes",
                    return_value=config,
                ),
                patch.object(zavod_guard, "preflight", return_value=summary),
                patch.object(
                    zavod_guard.subprocess,
                    "Popen",
                    return_value=child,
                ),
                patch.object(
                    zavod_guard,
                    "supervise",
                    return_value=supervised,
                ),
                patch.object(
                    zavod_guard,
                    "_verified_shutdown",
                    return_value=cleanup,
                ),
            )
            with (
                patches[0],
                patches[1],
                patches[2] as launch,
                patches[3],
                patches[4],
                self.assertRaisesRegex(GuardError, "launch contract"),
            ):
                zavod_guard.run_guarded(
                    paths["config"],
                    profile="selector-diagnostic",
                    test_mode=True,
                    workspace_root=root,
                )
            launch.assert_not_called()

            valid_contract = self.diagnostic_contract(paths)
            invalid_contracts = (
                (
                    {**valid_contract, "diagnostic_mode": "d1"},
                    "launch contract",
                ),
                (
                    {**valid_contract, "diagnostic_target": CONTROL_MINT},
                    "input integrity",
                ),
                (
                    {
                        **valid_contract,
                        "diagnostic_config_sha256": "0" * 64,
                    },
                    "input integrity",
                ),
                (
                    {
                        **valid_contract,
                        "diagnostic_tokens_sha256": "not-a-digest",
                    },
                    "launch contract",
                ),
            )
            for contract, pattern in invalid_contracts:
                with self.subTest(contract=contract):
                    with self.assertRaisesRegex(GuardError, pattern):
                        zavod_guard.run_guarded(
                            paths["config"],
                            profile="selector-diagnostic",
                            test_mode=True,
                            workspace_root=root,
                            **contract,
                        )

    def test_produced_contract_rejects_config_or_tokens_swap_before_open(self):
        source = (
            b"[auto]\n"
            b"enabled = true\n"
            b"force_two_mints = true\n"
            b"[auto.filters]\n"
            b"limit = 2\n"
            b"[bot]\n"
            b"merge_mints = true\n"
        )
        for swapped_name in ("config", "tokens"):
            with self.subTest(swapped=swapped_name):
                with tempfile.TemporaryDirectory() as temp_dir:
                    root = Path(temp_dir)
                    (root / "state" / "backups").mkdir(parents=True)
                    (root / "state" / "mint-runs").mkdir(parents=True)
                    for private_dir in (
                        root / "state",
                        root / "state" / "backups",
                        root / "state" / "mint-runs",
                    ):
                        private_dir.chmod(0o700)
                    (root / "config.toml").write_bytes(source)
                    (root / "config.toml").chmod(0o600)
                    (root / "tokens.toml").write_text('tokens = ["old"]\n')
                    (root / "tokens.toml").chmod(0o600)
                    (root / "zavod-mev-bot-rust-version-cli").touch()
                    prepared = mint_runner.prepare_run(
                        root,
                        DIAGNOSTIC_TARGET,
                        60,
                        diagnostic="d0",
                        now=lambda: datetime(
                            2026,
                            7,
                            24,
                            19,
                            0,
                            tzinfo=timezone.utc,
                        ),
                        process_checker=lambda: False,
                    )
                    contract = {
                        key: value
                        for key, value in prepared.safe_summary().items()
                        if key.startswith("diagnostic_")
                    }
                    if swapped_name == "config":
                        swapped_path = root / prepared.diagnostic_config
                        swapped_path.write_bytes(b"swapped config fixture\n")
                    else:
                        swapped_path = root / "tokens.toml"
                        swapped_path.write_bytes(b"tokens = []\n")
                    swapped_path.chmod(0o600)

                    with (
                        patch.object(
                            zavod_guard,
                            "load_config_bytes",
                            return_value=valid_config(),
                        ),
                        patch.object(
                            zavod_guard,
                            "preflight",
                            return_value={
                                "wallet": "public-address",
                                "balance_lamports": 100_000_000,
                            },
                        ),
                        patch.object(
                            zavod_guard.subprocess,
                            "Popen",
                        ) as launch,
                        self.assertRaisesRegex(
                            GuardError,
                            "input integrity",
                        ),
                    ):
                        try:
                            zavod_guard.run_guarded(
                                root / prepared.diagnostic_config,
                                profile="selector-diagnostic",
                                test_mode=True,
                                workspace_root=root,
                                **contract,
                            )
                        except TypeError as exc:
                            self.fail(
                                "guard does not accept the prepared launch "
                                f"contract: {exc}"
                            )
                    launch.assert_not_called()

    def test_selector_diagnostic_holds_config_across_run_directory_swap(self):
        config = valid_config()
        config["markets_file"] = [{"enabled": False, "path": "disabled.toml"}]
        config["auto"]["force_two_mints"] = False
        config["auto"]["filters"] = {"limit": 1}
        config["bot"] = {"merge_mints": False}
        child = Mock(pid=123, returncode=0, stdout=io.BytesIO())
        summary = {"wallet": "public-address", "balance_lamports": 100_000_000}
        supervised = {
            "reason": "child_exit",
            "start_balance": 100_000_000,
            "end_balance": 100_000_000,
            "observed_loss": 0,
            "child_exit_code": 0,
        }
        cleanup = {"exit_code": 0, "group_absent": True, "interrupted": False}
        replacement_bytes = b"replacement attacker-controlled fixture\n"

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = self.prepare_diagnostic_workspace(root)
            original_run_dir = paths["run_dir"].with_name("held-original-run")

            def swap_after_config_load(*args, **kwargs):
                del args, kwargs
                paths["run_dir"].rename(original_run_dir)
                paths["run_dir"].mkdir(mode=0o700)
                paths["run_dir"].chmod(0o700)
                replacement = paths["run_dir"] / "selector-diagnostic.toml"
                replacement.write_bytes(replacement_bytes)
                replacement.chmod(0o600)
                return summary

            def popen(argv, **kwargs):
                passed = kwargs.get("pass_fds")
                self.assertIsNotNone(passed)
                self.assertEqual(len(passed), 2)
                self.assertEqual(
                    argv[3],
                    f"/proc/self/fd/{int(Path(argv[3]).name)}",
                )
                self.assertEqual(
                    Path(argv[3]).read_bytes(),
                    self.ORIGINAL_DIAGNOSTIC_BYTES,
                )
                self.assertNotEqual(Path(argv[3]).read_bytes(), replacement_bytes)
                return child

            with (
                patch.object(zavod_guard, "load_config", return_value=config),
                patch.object(
                    zavod_guard,
                    "load_config_bytes",
                    return_value=config,
                ),
                patch.object(
                    zavod_guard,
                    "preflight",
                    side_effect=swap_after_config_load,
                ),
                patch.object(
                    zavod_guard.subprocess,
                    "Popen",
                    side_effect=popen,
                ),
                patch.object(zavod_guard, "supervise", return_value=supervised),
                patch.object(zavod_guard, "_verified_shutdown", return_value=cleanup),
            ):
                zavod_guard.run_guarded(
                    paths["config"],
                    profile="selector-diagnostic",
                    test_mode=True,
                    workspace_root=root,
                    token_account_snapshot_reader=lambda *_: frozenset(),
                    mint_account_validator=lambda *_: None,
                    **self.diagnostic_contract(paths),
                )

    def test_selector_diagnostic_monitors_tokens_path_identity_after_popen(self):
        config = valid_config()
        config["markets_file"] = [{"enabled": False, "path": "disabled.toml"}]
        config["auto"]["force_two_mints"] = False
        config["auto"]["filters"] = {"limit": 1}
        config["bot"] = {"merge_mints": False}
        child = Mock(pid=123, returncode=0, stdout=io.BytesIO())
        summary = {"wallet": "public-address", "balance_lamports": 100_000_000}
        cleanup = {"exit_code": 0, "group_absent": True, "interrupted": False}

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = self.prepare_diagnostic_workspace(root)
            contract = self.diagnostic_contract(paths)
            original_tokens = paths["tokens"].read_bytes()

            def popen(argv, **kwargs):
                del argv, kwargs
                held_path = paths["tokens"].with_name("held-original-tokens")
                paths["tokens"].rename(held_path)
                paths["tokens"].write_bytes(original_tokens)
                paths["tokens"].chmod(0o600)
                return child

            def supervised(**kwargs):
                checker = kwargs.get("input_integrity_checker")
                if checker is None:
                    reason = "child_exit"
                else:
                    reason = (
                        "child_exit"
                        if checker()
                        else "input_integrity_violation"
                    )
                return {
                    "reason": reason,
                    "start_balance": 100_000_000,
                    "end_balance": 100_000_000,
                    "observed_loss": 0,
                    "child_exit_code": 0,
                }

            with (
                patch.object(
                    zavod_guard,
                    "load_config_bytes",
                    return_value=config,
                ),
                patch.object(zavod_guard, "preflight", return_value=summary),
                patch.object(
                    zavod_guard.subprocess,
                    "Popen",
                    side_effect=popen,
                ),
                patch.object(
                    zavod_guard,
                    "supervise",
                    side_effect=supervised,
                ),
                patch.object(
                    zavod_guard,
                    "_verified_shutdown",
                    return_value=cleanup,
                ),
            ):
                result = zavod_guard.run_guarded(
                    paths["config"],
                    profile="selector-diagnostic",
                    test_mode=True,
                    workspace_root=root,
                    token_account_snapshot_reader=lambda *_: frozenset(),
                    mint_account_validator=lambda *_: None,
                    **contract,
                )

        self.assertEqual(result["reason"], "input_integrity_violation")

    def test_selector_diagnostic_snapshots_token_accounts_before_launch(self):
        config = valid_config()
        config["markets_file"] = [{"enabled": False, "path": "disabled.toml"}]
        config["auto"]["force_two_mints"] = False
        config["auto"]["filters"] = {"limit": 1}
        config["bot"] = {"merge_mints": False}
        child = Mock(pid=123, returncode=0, stdout=io.BytesIO())
        summary = {"wallet": "public-address", "balance_lamports": 100_000_000}
        cleanup = {"exit_code": 0, "group_absent": True, "interrupted": False}
        events = []
        observed_supervision = {}

        def token_accounts(rpc_url, public_key):
            self.assertEqual(rpc_url, config["rpc"]["url"])
            self.assertEqual(public_key, "public-address")
            events.append("token_accounts")
            return frozenset({"existing-account"})

        def popen(argv, **kwargs):
            del argv, kwargs
            events.append("popen")
            return child

        def supervised(**kwargs):
            observed_supervision.update(kwargs)
            return {
                "reason": "child_exit",
                "start_balance": 100_000_000,
                "end_balance": 100_000_000,
                "observed_loss": 0,
                "child_exit_code": 0,
            }

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = self.prepare_diagnostic_workspace(root)
            with (
                patch.object(
                    zavod_guard,
                    "load_config_bytes",
                    return_value=config,
                ),
                patch.object(zavod_guard, "preflight", return_value=summary),
                patch.object(
                    zavod_guard,
                    "get_token_account_pubkeys",
                    side_effect=token_accounts,
                ),
                patch.object(
                    zavod_guard.subprocess,
                    "Popen",
                    side_effect=popen,
                ),
                patch.object(
                    zavod_guard,
                    "supervise",
                    side_effect=supervised,
                ),
                patch.object(
                    zavod_guard,
                    "_verified_shutdown",
                    return_value=cleanup,
                ),
            ):
                zavod_guard.run_guarded(
                    paths["config"],
                    profile="selector-diagnostic",
                    test_mode=True,
                    workspace_root=root,
                    mint_account_validator=lambda *_: None,
                    **self.diagnostic_contract(paths),
                )

        self.assertEqual(events[:2], ["token_accounts", "popen"])
        self.assertEqual(
            observed_supervision.get("starting_token_accounts"),
            frozenset({"existing-account"}),
        )
        self.assertTrue(
            callable(observed_supervision.get("token_account_reader"))
        )

    def test_selector_diagnostic_validates_target_mint_before_snapshot_and_launch(self):
        config = valid_config()
        config["markets_file"] = [{"enabled": False, "path": "disabled.toml"}]
        config["auto"]["force_two_mints"] = False
        config["auto"]["filters"] = {"limit": 1}
        config["bot"] = {"merge_mints": False}
        child = Mock(pid=123, returncode=0, stdout=io.BytesIO())
        summary = {"wallet": "public-address", "balance_lamports": 100_000_000}
        cleanup = {"exit_code": 0, "group_absent": True, "interrupted": False}
        events = []

        def validate_target(rpc_url, target):
            self.assertEqual(rpc_url, config["rpc"]["url"])
            self.assertEqual(target, DIAGNOSTIC_TARGET)
            events.append("mint_validation")

        def snapshot(rpc_url, public_key):
            del rpc_url, public_key
            events.append("token_snapshot")
            return frozenset()

        def popen(argv, **kwargs):
            del argv, kwargs
            events.append("popen")
            return child

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = self.prepare_diagnostic_workspace(root)
            with (
                patch.object(
                    zavod_guard,
                    "load_config_bytes",
                    return_value=config,
                ),
                patch.object(zavod_guard, "preflight", return_value=summary),
                patch.object(
                    zavod_guard.subprocess,
                    "Popen",
                    side_effect=popen,
                ),
                patch.object(
                    zavod_guard,
                    "supervise",
                    return_value={
                        "reason": "child_exit",
                        "start_balance": 100_000_000,
                        "end_balance": 100_000_000,
                        "observed_loss": 0,
                        "child_exit_code": 0,
                    },
                ),
                patch.object(
                    zavod_guard,
                    "_verified_shutdown",
                    return_value=cleanup,
                ),
            ):
                try:
                    zavod_guard.run_guarded(
                        paths["config"],
                        profile="selector-diagnostic",
                        test_mode=True,
                        workspace_root=root,
                        token_account_snapshot_reader=snapshot,
                        mint_account_validator=validate_target,
                        **self.diagnostic_contract(paths),
                    )
                except TypeError as exc:
                    self.fail(f"guarded mint validator is not wired: {exc}")

        self.assertEqual(
            events[:3],
            ["mint_validation", "token_snapshot", "popen"],
        )

    def test_selector_diagnostic_invalid_target_mint_never_launches(self):
        config = valid_config()
        config["markets_file"] = [{"enabled": False, "path": "disabled.toml"}]
        config["auto"]["force_two_mints"] = False
        config["auto"]["filters"] = {"limit": 1}
        config["bot"] = {"merge_mints": False}
        summary = {"wallet": "public-address", "balance_lamports": 100_000_000}

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = self.prepare_diagnostic_workspace(root)
            with (
                patch.object(
                    zavod_guard,
                    "load_config_bytes",
                    return_value=config,
                ),
                patch.object(zavod_guard, "preflight", return_value=summary),
                patch.object(zavod_guard.subprocess, "Popen") as launch,
            ):
                result = zavod_guard.run_guarded(
                    paths["config"],
                    profile="selector-diagnostic",
                    test_mode=True,
                    workspace_root=root,
                    token_account_snapshot_reader=lambda *_: frozenset(),
                    mint_account_validator=Mock(
                        side_effect=GuardError(
                            "fixture invalid mint detail"
                        )
                    ),
                    **self.diagnostic_contract(paths),
                )

        launch.assert_not_called()
        self.assertEqual(result["reason"], "rpc_error")
        self.assertNotIn("fixture invalid mint detail", str(result))

    def test_selector_diagnostic_rechecks_inputs_after_snapshot_before_popen(self):
        config = valid_config()
        config["markets_file"] = [{"enabled": False, "path": "disabled.toml"}]
        config["auto"]["force_two_mints"] = False
        config["auto"]["filters"] = {"limit": 1}
        config["bot"] = {"merge_mints": False}
        child = Mock(pid=123, returncode=0, stdout=io.BytesIO())
        summary = {"wallet": "public-address", "balance_lamports": 100_000_000}
        cleanup = {"exit_code": 0, "group_absent": True, "interrupted": False}

        for swapped_name in ("config_contents", "tokens_path"):
            with self.subTest(swapped=swapped_name):
                with tempfile.TemporaryDirectory() as temp_dir:
                    root = Path(temp_dir)
                    paths = self.prepare_diagnostic_workspace(root)
                    contract = self.diagnostic_contract(paths)
                    original_tokens = paths["tokens"].read_bytes()

                    def swap_during_snapshot(rpc_url, public_key):
                        del rpc_url, public_key
                        if swapped_name == "config_contents":
                            paths["config"].write_bytes(
                                b"mutated config during snapshot\n"
                            )
                            paths["config"].chmod(0o600)
                        else:
                            held = paths["tokens"].with_name(
                                "tokens-held-before-snapshot-swap"
                            )
                            paths["tokens"].rename(held)
                            paths["tokens"].write_bytes(original_tokens)
                            paths["tokens"].chmod(0o600)
                        return frozenset()

                    with (
                        patch.object(
                            zavod_guard,
                            "load_config_bytes",
                            return_value=config,
                        ),
                        patch.object(
                            zavod_guard,
                            "preflight",
                            return_value=summary,
                        ),
                        patch.object(
                            zavod_guard.subprocess,
                            "Popen",
                            return_value=child,
                        ) as launch,
                        patch.object(
                            zavod_guard,
                            "supervise",
                            return_value={
                                "reason": "child_exit",
                                "start_balance": 100_000_000,
                                "end_balance": 100_000_000,
                                "observed_loss": 0,
                                "child_exit_code": 0,
                            },
                        ),
                        patch.object(
                            zavod_guard,
                            "_verified_shutdown",
                            return_value=cleanup,
                        ),
                        self.assertRaisesRegex(
                            GuardError,
                            "input integrity",
                        ),
                    ):
                        zavod_guard.run_guarded(
                            paths["config"],
                            profile="selector-diagnostic",
                            test_mode=True,
                            workspace_root=root,
                            token_account_snapshot_reader=(
                                swap_during_snapshot
                            ),
                            mint_account_validator=lambda *_: None,
                            **contract,
                        )

                    launch.assert_not_called()

    def test_selector_diagnostic_initial_token_snapshot_failure_never_launches(self):
        config = valid_config()
        config["markets_file"] = [{"enabled": False, "path": "disabled.toml"}]
        config["auto"]["force_two_mints"] = False
        config["auto"]["filters"] = {"limit": 1}
        config["bot"] = {"merge_mints": False}
        summary = {"wallet": "public-address", "balance_lamports": 100_000_000}
        cleanup = {"exit_code": None, "group_absent": True, "interrupted": False}

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = self.prepare_diagnostic_workspace(root)
            with (
                patch.object(
                    zavod_guard,
                    "load_config_bytes",
                    return_value=config,
                ),
                patch.object(zavod_guard, "preflight", return_value=summary),
                patch.object(
                    zavod_guard,
                    "get_token_account_pubkeys",
                    side_effect=GuardError("fixture RPC detail"),
                ),
                patch.object(zavod_guard.subprocess, "Popen") as launch,
                patch.object(
                    zavod_guard,
                    "_verified_shutdown",
                    return_value=cleanup,
                ),
            ):
                result = zavod_guard.run_guarded(
                    paths["config"],
                    profile="selector-diagnostic",
                    test_mode=True,
                    workspace_root=root,
                    mint_account_validator=lambda *_: None,
                    **self.diagnostic_contract(paths),
                )

        launch.assert_not_called()
        self.assertEqual(result["reason"], "rpc_error")
        self.assertNotIn("fixture RPC detail", str(result))

    def test_selector_diagnostic_descriptor_walk_rejects_symlinked_components(self):
        components = ("state", "mint_runs", "run_dir", "config")
        for component_name in components:
            with self.subTest(component=component_name):
                with tempfile.TemporaryDirectory() as temp_dir:
                    root = Path(temp_dir)
                    paths = self.prepare_diagnostic_workspace(root)
                    component = paths[component_name]
                    target = component.with_name(f"{component.name}-target")
                    component.rename(target)
                    component.symlink_to(
                        target.name,
                        target_is_directory=component_name != "config",
                    )

                    self.assert_diagnostic_identity_rejected(
                        paths["config"],
                        root,
                    )

    def test_selector_diagnostic_descriptor_walk_requires_private_modes(self):
        components = {
            "state": 0o755,
            "mint_runs": 0o755,
            "run_dir": 0o755,
            "marker": 0o640,
            "config": 0o640,
        }
        for component_name, mode in components.items():
            with self.subTest(component=component_name):
                with tempfile.TemporaryDirectory() as temp_dir:
                    root = Path(temp_dir)
                    paths = self.prepare_diagnostic_workspace(root)
                    paths[component_name].chmod(mode)

                    self.assert_diagnostic_identity_rejected(
                        paths["config"],
                        root,
                    )

    def test_selector_diagnostic_requires_exact_descriptor_bound_marker(self):
        marker_variants = ("wrong-run", "symlink")
        for variant in marker_variants:
            with self.subTest(variant=variant):
                with tempfile.TemporaryDirectory() as temp_dir:
                    root = Path(temp_dir)
                    paths = self.prepare_diagnostic_workspace(root)
                    if variant == "wrong-run":
                        paths["marker"].write_text("20260724T190001Z\n")
                        paths["marker"].chmod(0o600)
                    else:
                        marker_target = paths["marker"].with_name(
                            ".mint-run-active-target"
                        )
                        paths["marker"].rename(marker_target)
                        paths["marker"].symlink_to(marker_target.name)

                    self.assert_diagnostic_identity_rejected(
                        paths["config"],
                        root,
                    )

    def test_main_binds_diagnostic_to_guard_repository_root(self):
        config = valid_config()
        child = Mock(pid=123, returncode=0, stdout=io.BytesIO())
        summary = {"wallet": "public-address", "balance_lamports": 100_000_000}
        supervised = {
            "reason": "child_exit",
            "start_balance": 100_000_000,
            "end_balance": 100_000_000,
            "observed_loss": 0,
            "child_exit_code": 0,
        }
        cleanup = {"exit_code": 0, "group_absent": True, "interrupted": False}

        with tempfile.TemporaryDirectory() as temp_dir:
            fixture_root = Path(temp_dir)
            trusted_root = fixture_root / "trusted-workspace"
            (trusted_root / "scripts").mkdir(parents=True)
            guard_path = trusted_root / "scripts" / "zavod_guard.py"
            guard_path.touch()

            outside_root = fixture_root / "attacker-workspace"
            outside_root.mkdir()
            (outside_root / "zavod-mev-bot-rust-version-cli").touch()
            config_path = outside_root / "selector-diagnostic.toml"
            config_path.touch(mode=0o600)
            config_path.chmod(0o600)

            with (
                patch.object(zavod_guard, "__file__", str(guard_path)),
                patch.object(zavod_guard, "load_config", return_value=config),
                patch.object(zavod_guard, "preflight", return_value=summary),
                patch.object(
                    zavod_guard.subprocess,
                    "Popen",
                    return_value=child,
                ) as launch,
                patch.object(zavod_guard, "supervise", return_value=supervised),
                patch.object(zavod_guard, "_verified_shutdown", return_value=cleanup),
                patch("sys.stdout", new=io.StringIO()),
                patch("sys.stderr", new=io.StringIO()),
            ):
                exit_code = zavod_guard.main(
                    [
                        "run",
                        "--config",
                        str(config_path),
                        "--profile",
                        "selector-diagnostic",
                        "--test-mode",
                        "--live-confirmed",
                    ]
                )

            self.assertEqual(exit_code, 1)
            launch.assert_not_called()

    def test_selector_diagnostic_rejects_config_owned_by_another_euid(self):
        real_fstat = os.fstat

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = self.prepare_diagnostic_workspace(root)
            config_identity = paths["config"].stat()

            def wrong_owner_fstat(descriptor):
                metadata = real_fstat(descriptor)
                if (
                    metadata.st_dev,
                    metadata.st_ino,
                ) != (
                    config_identity.st_dev,
                    config_identity.st_ino,
                ):
                    return metadata
                fields = list(metadata)
                fields[4] = os.geteuid() + 1
                return os.stat_result(fields)

            with patch.object(os, "fstat", side_effect=wrong_owner_fstat):
                self.assert_diagnostic_identity_rejected(
                    paths["config"],
                    root,
                    "selector-diagnostic",
                )

    def test_selector_diagnostic_rejects_config_mode_with_special_bits(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = self.prepare_diagnostic_workspace(root)
            paths["config"].chmod(0o4600)
            self.assert_diagnostic_identity_rejected(
                paths["config"],
                root,
                "selector-diagnostic",
            )

    def test_selector_diagnostic_rejects_config_symlink(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = self.prepare_diagnostic_workspace(root)
            target_path = paths["config"].with_name(
                "selector-diagnostic-target.toml"
            )
            paths["config"].rename(target_path)
            paths["config"].symlink_to(target_path.name)
            self.assert_diagnostic_identity_rejected(
                paths["config"],
                root,
                "selector-diagnostic",
            )

    def test_test_mode_dispatch_violation_stops_child(self):
        marker = "dispatching transaction marker-payload"
        sink = io.StringIO()
        pump = zavod_guard.OutputPump(
            io.BytesIO(marker.encode()),
            sink,
            valid_config(),
            test_mode=True,
        )
        pump.start()
        pump.join(1)
        self.assertFalse(pump.is_alive())
        self.assertTrue(pump.test_mode_dispatch_event.is_set())

        child = Mock(pid=123, returncode=None)
        child.poll.return_value = None
        killpg = Mock()
        result = zavod_guard.supervise(
            child=child,
            start_balance=100_000_000,
            balance_reader=lambda: 100_000_000,
            monotonic=lambda: 0,
            sleep=lambda _: None,
            test_mode_dispatch_event=pump.test_mode_dispatch_event,
            killpg=killpg,
            group_exists=Mock(side_effect=[True, False]),
            signal_grace=((signal.SIGINT, 0),),
        )

        self.assertEqual(result["reason"], "test_mode_dispatch_violation")
        killpg.assert_called_once_with(123, signal.SIGINT)
        self.assertNotIn(marker, str(result))

    def test_main_returns_nonzero_for_persisted_diagnostic_violations(self):
        result = {
            "reason": "unused",
            "start_balance": 100,
            "end_balance": 100,
            "observed_loss": 0,
            "duration_seconds": 1,
            "child_exit_code": -signal.SIGINT,
            "loss_limit_lamports": zavod_guard.LOSS_LIMIT_LAMPORTS,
            "early_stop_lamports": zavod_guard.EARLY_STOP_LAMPORTS,
            "log_path": "logs/fake.log",
        }
        for reason in (
            "test_mode_dispatch_violation",
            "cleanup_failed",
            "diagnostic_loss_violation",
            "input_integrity_violation",
            "token_account_growth_violation",
            "protected_output_violation",
            "rpc_error",
        ):
            with self.subTest(reason=reason):
                stdout = io.StringIO()
                with (
                    patch.object(
                        zavod_guard,
                        "run_guarded",
                        return_value={**result, "reason": reason},
                    ),
                    patch("sys.stdout", new=stdout),
                ):
                    status = zavod_guard.main(
                        [
                            "run",
                            "--live-confirmed",
                            "--profile",
                            "selector-diagnostic",
                            "--test-mode",
                            "--diagnostic-mode",
                            "d0",
                            "--diagnostic-target",
                            DIAGNOSTIC_TARGET,
                            "--config-sha256",
                            "1" * 64,
                            "--tokens-sha256",
                            "2" * 64,
                        ]
                    )
                self.assertEqual(status, 1)
                self.assertIn(f"reason={reason}", stdout.getvalue())

    def test_main_requires_exactly_one_of_each_diagnostic_contract_argument(self):
        base_args = [
            "run",
            "--live-confirmed",
            "--profile",
            "selector-diagnostic",
            "--test-mode",
            "--diagnostic-mode",
            "d0",
            "--diagnostic-target",
            DIAGNOSTIC_TARGET,
            "--config-sha256",
            "1" * 64,
            "--tokens-sha256",
            "2" * 64,
        ]
        invalid = (
            base_args[:-2],
            [*base_args, "--diagnostic-mode", "d0"],
            [
                *base_args,
                "--diagnostic-target",
                DIAGNOSTIC_TARGET,
            ],
            [*base_args, "--config-sha256", "1" * 64],
            [*base_args, "--tokens-sha256", "3" * 64],
        )
        result = {
            "reason": "child_exit",
            "start_balance": 100,
            "end_balance": 100,
            "observed_loss": 0,
            "duration_seconds": 1,
            "child_exit_code": 0,
            "loss_limit_lamports": zavod_guard.LOSS_LIMIT_LAMPORTS,
            "early_stop_lamports": zavod_guard.EARLY_STOP_LAMPORTS,
            "log_path": "logs/fake.log",
        }
        for argv in invalid:
            with self.subTest(argv=argv):
                with (
                    patch.object(zavod_guard, "run_guarded") as launch,
                    patch("sys.stderr", new=io.StringIO()),
                ):
                    try:
                        status = zavod_guard.main(argv)
                    except SystemExit as exc:
                        self.fail(
                            "diagnostic argument rejection escaped main: "
                            f"{exc}"
                        )
                self.assertNotEqual(status, 0)
                launch.assert_not_called()

        with (
            patch.object(
                zavod_guard,
                "run_guarded",
                return_value=result,
            ) as launch,
            patch("sys.stdout", new=io.StringIO()),
        ):
            status = zavod_guard.main(base_args)

        self.assertEqual(status, 0)
        self.assertEqual(
            launch.call_args.kwargs,
            {
                "test_mode": True,
                "workspace_root": Path(zavod_guard.__file__).resolve().parents[1],
                "diagnostic_mode": "d0",
                "diagnostic_target": DIAGNOSTIC_TARGET,
                "diagnostic_config_sha256": "1" * 64,
                "diagnostic_tokens_sha256": "2" * 64,
            },
        )


class AutoFilterLiveContractTests(unittest.TestCase):
    BATCH_ID = "20260726T123000Z"
    SOURCE = b"""[wallet]
private_key = "fixture-wallet"
[rpc]
url = "https://fixture.invalid"
[auto]
enabled = true
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

    def prepare_workspace(self, root, stage_index=0, baseline=100_000_000):
        root = Path(root)
        (root / "scripts").mkdir()
        (root / "scripts" / "zavod_guard.py").touch()
        config_path = root / "config.toml"
        config_path.write_bytes(self.SOURCE)
        config_path.chmod(0o600)
        tokens_path = root / "tokens.toml"
        tokens_path.write_text('tokens = ["production-fixture"]\n')
        tokens_path.chmod(0o600)
        binary_path = root / "zavod-mev-bot-rust-version-cli"
        binary_path.write_bytes(b"fixture executable\n")
        binary_path.chmod(0o700)
        with (
            patch.object(
                mint_auto_diagnoser.zavod_guard,
                "validate_token_mint_account",
                return_value=None,
            ),
            patch.object(
                mint_auto_diagnoser.zavod_guard,
                "wallet_pubkey",
                return_value="fixture-public-key",
            ),
        ):
            batch = mint_auto_diagnoser.prepare_batch(
                root,
                DIAGNOSTIC_TARGET,
                now=lambda: datetime(
                    2026,
                    7,
                    26,
                    12,
                    30,
                    tzinfo=timezone.utc,
                ),
                balance_reader=lambda *_: baseline,
            )
        stage = batch.stages[stage_index]
        contract_path = root / stage.contract_relative_path
        contract_descriptor = os.open(contract_path, os.O_RDONLY)
        live_lock_path = root / "state" / ".zavod-live.lock"
        live_lock_path.touch(mode=0o600)
        live_lock_path.chmod(0o600)
        live_lock_descriptor = os.open(live_lock_path, os.O_RDWR)
        fcntl.flock(
            live_lock_descriptor,
            fcntl.LOCK_EX | fcntl.LOCK_NB,
        )
        self.addCleanup(os.close, live_lock_descriptor)
        return {
            "root": root,
            "stage": stage,
            "stage_path": root / stage.relative_root,
            "contract_path": contract_path,
            "contract_fd": contract_descriptor,
            "live_lock_path": live_lock_path,
            "live_lock_fd": live_lock_descriptor,
            "binary_path": binary_path,
        }

    def run_auto(
        self,
        paths,
        current_balance=100_000_000,
        supervise_side_effect=None,
        popen_side_effect=None,
        shutdown_observer=None,
    ):
        child = Mock(pid=123, returncode=0, stdout=io.BytesIO())
        launched = {}

        def launch(argv, **kwargs):
            launched["argv"] = argv
            launched["kwargs"] = kwargs
            if popen_side_effect is not None:
                popen_side_effect()
            return child

        def supervised(**kwargs):
            if supervise_side_effect is not None:
                return supervise_side_effect(**kwargs)
            self.assertTrue(kwargs["input_integrity_checker"]())
            return {
                "reason": "child_exit",
                "start_balance": kwargs["start_balance"],
                "end_balance": current_balance,
                "observed_loss": max(
                    0,
                    kwargs["start_balance"] - current_balance,
                ),
                "child_exit_code": 0,
            }

        def preflighted(*args, **kwargs):
            del args
            self.assertEqual(kwargs["profile"], "auto-filter-live")
            self.assertRegex(
                str(kwargs["binary_path"]),
                r"^/proc/self/fd/[0-9]+$",
            )
            return {
                "wallet": "fixture-public-key",
                "balance_lamports": current_balance,
            }

        with (
            patch.object(
                zavod_guard,
                "__file__",
                str(paths["root"] / "scripts" / "zavod_guard.py"),
            ),
            patch.object(
                zavod_guard,
                "preflight",
                side_effect=preflighted,
            ),
            patch.object(
                zavod_guard.subprocess,
                "Popen",
                side_effect=launch,
            ),
            patch.object(
                zavod_guard,
                "supervise",
                side_effect=supervised,
            ),
            patch.object(
                zavod_guard,
                "_verified_shutdown",
                side_effect=lambda created_child: (
                    launched.setdefault("shutdown_children", []).append(
                        created_child
                    )
                    or (
                        shutdown_observer(created_child)
                        if shutdown_observer is not None
                        else None
                    )
                    or {
                        "exit_code": 0,
                        "group_absent": True,
                        "interrupted": False,
                    }
                ),
            ),
        ):
            result = zavod_guard.run_guarded(
                "config.toml",
                profile="auto-filter-live",
                workspace_root=paths["stage"].relative_root,
                batch_contract_fd=paths["contract_fd"],
                live_lock_fd=paths["live_lock_fd"],
            )
        return result, launched

    def rewrite_contract(self, paths, **changes):
        contract = json.loads(paths["contract_path"].read_bytes())
        contract.update(changes)
        paths["contract_path"].write_text(
            json.dumps(contract, sort_keys=True, separators=(",", ":"))
            + "\n"
        )
        paths["contract_path"].chmod(0o600)

    def test_descriptor_bound_profile_launches_held_stage_inputs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = self.prepare_workspace(Path(temp_dir))
            try:
                result, launched = self.run_auto(
                    paths,
                    current_balance=80_000_001,
                )
            finally:
                os.close(paths["contract_fd"])

        argv = launched["argv"]
        self.assertEqual(argv[1], "run")
        self.assertRegex(argv[0], r"^/proc/self/fd/[0-9]+$")
        self.assertEqual(argv[2], "--config")
        self.assertRegex(argv[3], r"^/proc/self/fd/[0-9]+$")
        self.assertRegex(
            str(launched["kwargs"]["cwd"]),
            r"^/proc/self/fd/[0-9]+$",
        )
        self.assertGreaterEqual(len(launched["kwargs"]["pass_fds"]), 4)
        self.assertNotIn(
            paths["live_lock_fd"],
            launched["kwargs"]["pass_fds"],
        )
        self.assertNotIn(
            "ZAVOD_LIVE_LOCK_FD",
            launched["kwargs"]["env"],
        )
        self.assertNotIn(
            "ZAVOD_BATCH_CONTRACT_FD",
            launched["kwargs"]["env"],
        )
        self.assertEqual(result["batch_start_balance"], 100_000_000)
        self.assertEqual(result["stage_start_balance"], 80_000_001)
        self.assertEqual(result["loss_limit_lamports"], 30_000_000)
        self.assertEqual(result["early_stop_lamports"], 25_000_000)

    def test_profile_rejects_missing_descriptor_and_test_mode(self):
        with self.assertRaisesRegex(GuardError, "auto-filter-live"):
            zavod_guard.run_guarded(
                "config.toml",
                profile="auto-filter-live",
                workspace_root=(
                    "state/auto-diagnose-runs/"
                    f"{self.BATCH_ID}/stages/0-baseline"
                ),
            )
        with self.assertRaisesRegex(GuardError, "test mode"):
            zavod_guard.run_guarded(
                "config.toml",
                profile="auto-filter-live",
                test_mode=True,
                workspace_root=(
                    "state/auto-diagnose-runs/"
                    f"{self.BATCH_ID}/stages/0-baseline"
                ),
                batch_contract_fd=9,
                live_lock_fd=8,
            )
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = self.prepare_workspace(Path(temp_dir))
            try:
                with (
                    patch.object(
                        zavod_guard,
                        "__file__",
                        str(
                            paths["root"]
                            / "scripts"
                            / "zavod_guard.py"
                        ),
                    ),
                    self.assertRaisesRegex(
                        GuardError,
                        "auto-filter-live",
                    ),
                ):
                    zavod_guard.run_guarded(
                        "config.toml",
                        timeout_seconds=60,
                        profile="auto-filter-live",
                        workspace_root=paths["stage"].relative_root,
                        batch_contract_fd=paths["contract_fd"],
                        live_lock_fd=paths["live_lock_fd"],
                    )
            finally:
                os.close(paths["contract_fd"])

    def test_contract_public_fields_and_workspace_are_exact(self):
        mutations = (
            ("schema", 2),
            ("batch_id", "20260726T123001Z"),
            ("stage_index", 1),
            ("stage_name", "offchain"),
            ("target_mint", CONTROL_MINT),
            ("timeout_seconds", 299),
            ("batch_start_balance_lamports", -1),
            ("early_stop_lamports", 24_999_999),
            ("loss_limit_lamports", 29_999_999),
            ("three_hop_required", False),
        )
        for field, value in mutations:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temp_dir:
                paths = self.prepare_workspace(Path(temp_dir))
                try:
                    self.rewrite_contract(paths, **{field: value})
                    with self.assertRaisesRegex(
                        GuardError,
                        "auto-filter-live",
                    ):
                        self.run_auto(paths)
                finally:
                    os.close(paths["contract_fd"])

        with tempfile.TemporaryDirectory() as temp_dir:
            paths = self.prepare_workspace(Path(temp_dir))
            try:
                paths["stage"] = paths["stage"].__class__(
                    paths["stage"].index,
                    paths["stage"].name,
                    paths["stage"].relative_root.replace(
                        self.BATCH_ID,
                        "20260726T123001Z",
                    ),
                    paths["stage"].contract_relative_path,
                    paths["stage"].skipped,
                    paths["stage"].skip_reason,
                )
                with self.assertRaisesRegex(GuardError, "auto-filter-live"):
                    self.run_auto(paths)
            finally:
                os.close(paths["contract_fd"])

    def test_profile_requires_exact_active_batch_marker(self):
        for variant in ("wrong-batch", "wrong-mode", "symlink"):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as temp_dir:
                paths = self.prepare_workspace(Path(temp_dir))
                marker = (
                    paths["root"]
                    / "state"
                    / ".mint-auto-diagnose-active"
                )
                try:
                    if variant == "wrong-batch":
                        marker.write_text("20260726T123001Z\n")
                        marker.chmod(0o600)
                    elif variant == "wrong-mode":
                        marker.chmod(0o644)
                    else:
                        held = marker.with_name(
                            ".mint-auto-diagnose-active-held"
                        )
                        marker.rename(held)
                        marker.symlink_to(held.name)
                    with self.assertRaisesRegex(
                        GuardError,
                        "auto-filter-live",
                    ):
                        self.run_auto(paths)
                finally:
                    os.close(paths["contract_fd"])

    def test_rejects_digest_content_and_declared_mutation_mismatches(self):
        cases = (
            ("config.toml", b"\n# changed\n", None),
            ("tokens.toml", b'tokens = ["11111111111111111111111111111111"]\n', "tokens_sha256"),
            (
                "zavod-mev-bot-rust-version-cli",
                b"different executable\n",
                "binary_sha256",
            ),
        )
        for name, replacement, updated_digest in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp_dir:
                paths = self.prepare_workspace(Path(temp_dir))
                try:
                    selected = paths["stage_path"] / name
                    if name == "config.toml":
                        selected.write_bytes(selected.read_bytes() + replacement)
                    elif name == "zavod-mev-bot-rust-version-cli":
                        selected.unlink()
                        selected.write_bytes(replacement)
                        selected.chmod(0o700)
                    else:
                        selected.write_bytes(replacement)
                    if updated_digest is not None:
                        self.rewrite_contract(
                            paths,
                            **{
                                updated_digest: hashlib.sha256(
                                    selected.read_bytes()
                                ).hexdigest()
                            },
                        )
                    with self.assertRaisesRegex(GuardError, "auto-filter-live"):
                        self.run_auto(paths)
                finally:
                    os.close(paths["contract_fd"])

        with tempfile.TemporaryDirectory() as temp_dir:
            paths = self.prepare_workspace(Path(temp_dir), stage_index=1)
            try:
                config_path = paths["stage_path"] / "config.toml"
                changed = config_path.read_bytes().replace(
                    b"min_tx_len = 3",
                    b"min_tx_len = 0",
                )
                config_path.write_bytes(changed)
                self.rewrite_contract(
                    paths,
                    config_sha256=hashlib.sha256(changed).hexdigest(),
                )
                with self.assertRaisesRegex(GuardError, "auto-filter-live"):
                    self.run_auto(paths)
            finally:
                os.close(paths["contract_fd"])

        with tempfile.TemporaryDirectory() as temp_dir:
            paths = self.prepare_workspace(Path(temp_dir))
            try:
                snapshot = (
                    paths["root"]
                    / "state"
                    / "auto-diagnose-runs"
                    / self.BATCH_ID
                    / "production-binary"
                )
                snapshot.write_bytes(b"different prepared executable\n")
                snapshot.chmod(0o600)
                with self.assertRaisesRegex(GuardError, "auto-filter-live"):
                    self.run_auto(paths)
            finally:
                os.close(paths["contract_fd"])

    def test_rejects_mode_symlink_and_owner_violations_across_stage_walk(self):
        relative_components = (
            "state",
            "state/auto-diagnose-runs",
            f"state/auto-diagnose-runs/{self.BATCH_ID}",
            f"state/auto-diagnose-runs/{self.BATCH_ID}/stages",
            f"state/auto-diagnose-runs/{self.BATCH_ID}/stages/0-baseline",
            (
                f"state/auto-diagnose-runs/{self.BATCH_ID}/"
                "production-config.toml"
            ),
            (
                f"state/auto-diagnose-runs/{self.BATCH_ID}/"
                "production-tokens.toml"
            ),
            (
                f"state/auto-diagnose-runs/{self.BATCH_ID}/"
                "production-binary"
            ),
            (
                f"state/auto-diagnose-runs/{self.BATCH_ID}/stages/"
                "0-baseline/stage-contract.json"
            ),
            (
                f"state/auto-diagnose-runs/{self.BATCH_ID}/stages/"
                "0-baseline/config.toml"
            ),
            (
                f"state/auto-diagnose-runs/{self.BATCH_ID}/stages/"
                "0-baseline/tokens.toml"
            ),
            (
                f"state/auto-diagnose-runs/{self.BATCH_ID}/stages/"
                "0-baseline/zavod-mev-bot-rust-version-cli"
            ),
        )
        for relative in relative_components:
            with self.subTest(kind="mode", path=relative), tempfile.TemporaryDirectory() as temp_dir:
                paths = self.prepare_workspace(Path(temp_dir))
                try:
                    selected = paths["root"] / relative
                    selected.chmod(
                        0o755 if selected.is_dir() else 0o644
                    )
                    with self.assertRaisesRegex(GuardError, "auto-filter-live"):
                        self.run_auto(paths)
                finally:
                    os.close(paths["contract_fd"])

            with self.subTest(kind="symlink", path=relative), tempfile.TemporaryDirectory() as temp_dir:
                paths = self.prepare_workspace(Path(temp_dir))
                try:
                    selected = paths["root"] / relative
                    moved = selected.with_name(selected.name + "-held")
                    selected.rename(moved)
                    selected.symlink_to(moved.name, target_is_directory=moved.is_dir())
                    with self.assertRaisesRegex(GuardError, "auto-filter-live"):
                        self.run_auto(paths)
                finally:
                    os.close(paths["contract_fd"])

            with self.subTest(kind="owner", path=relative), tempfile.TemporaryDirectory() as temp_dir:
                paths = self.prepare_workspace(Path(temp_dir))
                selected_identity = (paths["root"] / relative).stat()
                real_fstat = os.fstat

                def wrong_owner(descriptor):
                    metadata = real_fstat(descriptor)
                    if (
                        metadata.st_dev,
                        metadata.st_ino,
                    ) == (
                        selected_identity.st_dev,
                        selected_identity.st_ino,
                    ):
                        fields = list(metadata)
                        fields[4] = os.geteuid() + 1
                        return os.stat_result(fields)
                    return metadata

                try:
                    with (
                        patch.object(os, "fstat", side_effect=wrong_owner),
                        self.assertRaisesRegex(
                            GuardError,
                            "auto-filter-live",
                        ),
                    ):
                        self.run_auto(paths)
                finally:
                    os.close(paths["contract_fd"])

    def test_stage_path_swaps_fail_before_and_during_supervision(self):
        for timing in ("before-child", "popen-return", "during-supervision"):
            with self.subTest(timing=timing), tempfile.TemporaryDirectory() as temp_dir:
                paths = self.prepare_workspace(Path(temp_dir))

                def swap():
                    held = paths["stage_path"].with_name("0-baseline-held")
                    paths["stage_path"].rename(held)
                    paths["stage_path"].mkdir(mode=0o700)

                def supervise_after_swap(**kwargs):
                    swap()
                    self.assertFalse(kwargs["input_integrity_checker"]())
                    return {
                        "reason": "input_integrity_violation",
                        "start_balance": kwargs["start_balance"],
                        "end_balance": kwargs["start_balance"],
                        "observed_loss": 0,
                        "child_exit_code": None,
                    }

                try:
                    if timing == "before-child":
                        swap()
                        with self.assertRaisesRegex(GuardError, "auto-filter-live"):
                            self.run_auto(paths)
                    elif timing == "popen-return":
                        observed = {}
                        shutdown_children = []

                        def swap_after_launch():
                            swap()
                            observed["launched"] = True

                        with self.assertRaisesRegex(
                            GuardError,
                            "auto-filter-live",
                        ):
                            self.run_auto(
                                paths,
                                popen_side_effect=swap_after_launch,
                                shutdown_observer=(
                                    shutdown_children.append
                                ),
                            )
                        self.assertTrue(observed["launched"])
                        self.assertEqual(len(shutdown_children), 1)
                    else:
                        result, _ = self.run_auto(
                            paths,
                            supervise_side_effect=supervise_after_swap,
                        )
                        self.assertEqual(
                            result["reason"],
                            "input_integrity_violation",
                        )
                finally:
                    os.close(paths["contract_fd"])

    def test_live_lock_path_swap_is_rejected_while_original_lock_is_held(self):
        for timing in ("after-shell-validation", "popen-return"):
            with (
                self.subTest(timing=timing),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                paths = self.prepare_workspace(Path(temp_dir))
                held_path = paths["live_lock_path"].with_name(
                    ".zavod-live.lock-held"
                )

                def swap_lock():
                    paths["live_lock_path"].rename(held_path)
                    paths["live_lock_path"].touch(mode=0o600)
                    paths["live_lock_path"].chmod(0o600)

                try:
                    if timing == "after-shell-validation":
                        swap_lock()
                        with self.assertRaisesRegex(
                            GuardError,
                            "auto-filter-live",
                        ):
                            self.run_auto(paths)
                    else:
                        with self.assertRaisesRegex(
                            GuardError,
                            "auto-filter-live",
                        ):
                            self.run_auto(
                                paths,
                                popen_side_effect=swap_lock,
                            )

                    probe = os.open(held_path, os.O_RDWR)
                    try:
                        with self.assertRaises(BlockingIOError):
                            fcntl.flock(
                                probe,
                                fcntl.LOCK_EX | fcntl.LOCK_NB,
                            )
                    finally:
                        os.close(probe)
                finally:
                    os.close(paths["contract_fd"])

    def test_live_lock_descriptor_must_retain_contention(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = self.prepare_workspace(Path(temp_dir))
            try:
                fcntl.flock(paths["live_lock_fd"], fcntl.LOCK_UN)
                with self.assertRaisesRegex(
                    GuardError,
                    "auto-filter-live",
                ):
                    self.run_auto(paths)
            finally:
                os.close(paths["contract_fd"])

    def test_auto_filter_logs_are_descriptor_bound_and_private(self):
        for variant in ("symlink", "wrong-mode", "wrong-owner"):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as temp_dir:
                paths = self.prepare_workspace(Path(temp_dir))
                logs_path = paths["stage_path"] / "logs"
                outside = paths["root"] / "outside-logs"
                outside.mkdir()
                if variant == "symlink":
                    logs_path.symlink_to(outside, target_is_directory=True)
                else:
                    logs_path.mkdir(mode=0o700)
                    if variant == "wrong-mode":
                        logs_path.chmod(0o755)
                try:
                    if variant == "wrong-owner":
                        logs_identity = logs_path.stat()
                        real_fstat = os.fstat

                        def wrong_owner(descriptor):
                            metadata = real_fstat(descriptor)
                            if (
                                metadata.st_dev,
                                metadata.st_ino,
                            ) == (
                                logs_identity.st_dev,
                                logs_identity.st_ino,
                            ):
                                fields = list(metadata)
                                fields[4] = os.geteuid() + 1
                                return os.stat_result(fields)
                            return metadata

                        owner_patch = patch.object(
                            os,
                            "fstat",
                            side_effect=wrong_owner,
                        )
                    else:
                        owner_patch = patch.object(
                            os,
                            "fstat",
                            wraps=os.fstat,
                        )
                    with (
                        owner_patch,
                        self.assertRaisesRegex(
                            GuardError,
                            "auto-filter-live",
                        ),
                    ):
                        self.run_auto(paths)
                    self.assertEqual(tuple(outside.iterdir()), ())
                finally:
                    os.close(paths["contract_fd"])

    def test_cumulative_baseline_is_used_and_threshold_refuses_launch(self):
        for current in (75_000_000, 70_000_000):
            with self.subTest(current=current), tempfile.TemporaryDirectory() as temp_dir:
                paths = self.prepare_workspace(Path(temp_dir))
                try:
                    with self.assertRaisesRegex(
                        GuardError,
                        "cumulative loss",
                    ):
                        self.run_auto(paths, current_balance=current)
                finally:
                    os.close(paths["contract_fd"])

        with tempfile.TemporaryDirectory() as temp_dir:
            paths = self.prepare_workspace(
                Path(temp_dir),
                stage_index=2,
            )
            seen = {}

            def supervised(**kwargs):
                seen["start_balance"] = kwargs["start_balance"]
                return {
                    "reason": "child_exit",
                    "start_balance": kwargs["start_balance"],
                    "end_balance": 78_000_000,
                    "observed_loss": 22_000_000,
                    "child_exit_code": 0,
                }

            try:
                result, _ = self.run_auto(
                    paths,
                    current_balance=80_000_001,
                    supervise_side_effect=supervised,
                )
            finally:
                os.close(paths["contract_fd"])
            self.assertEqual(seen["start_balance"], 100_000_000)
            self.assertEqual(
                result["stage_start_balance"],
                80_000_001,
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            paths = self.prepare_workspace(Path(temp_dir))
            try:
                with (
                    patch.object(
                        zavod_guard,
                        "__file__",
                        str(
                            paths["root"]
                            / "scripts"
                            / "zavod_guard.py"
                        ),
                    ),
                    patch.object(
                        zavod_guard,
                        "preflight",
                        side_effect=GuardError(
                            "RPC balance check failed"
                        ),
                    ),
                    patch.object(
                        zavod_guard.subprocess,
                        "Popen",
                    ) as launch,
                    self.assertRaisesRegex(
                        GuardError,
                        "RPC balance check failed",
                    ),
                ):
                    zavod_guard.run_guarded(
                        "config.toml",
                        profile="auto-filter-live",
                        workspace_root=paths["stage"].relative_root,
                        batch_contract_fd=paths["contract_fd"],
                        live_lock_fd=paths["live_lock_fd"],
                    )
                launch.assert_not_called()
            finally:
                os.close(paths["contract_fd"])

    def test_auto_result_printer_omits_cumulative_balance_fields(self):
        result = {
            "reason": "child_exit",
            "start_balance": 100,
            "end_balance": 90,
            "observed_loss": 10,
            "batch_start_balance": 100,
            "stage_start_balance": 95,
            "duration_seconds": 1,
            "child_exit_code": 0,
            "loss_limit_lamports": 30_000_000,
            "early_stop_lamports": 25_000_000,
            "log_path": "logs/fixture.log",
        }
        output = io.StringIO()
        with (
            patch.object(zavod_guard, "run_guarded", return_value=result),
            patch("sys.stdout", new=output),
        ):
            status = zavod_guard.main(
                [
                    "run",
                    "--live-confirmed",
                    "--profile",
                    "auto-filter-live",
                    "--workspace-root",
                    (
                        "state/auto-diagnose-runs/"
                        f"{self.BATCH_ID}/stages/0-baseline"
                    ),
                    "--batch-contract-fd",
                    "9",
                    "--live-lock-fd",
                    "8",
                ]
            )
        self.assertEqual(status, 0)
        self.assertNotIn("balance", output.getvalue())
        self.assertNotIn("observed_loss", output.getvalue())


class RunGuardedWrapperTests(unittest.TestCase):
    DIAGNOSTIC_RUN_ID = "20260724T190000Z"

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "scripts").mkdir()
        (self.root / "state").mkdir()
        source = Path(__file__).resolve().parents[1] / "scripts" / "run-guarded.sh"
        shutil.copy2(source, self.root / "scripts" / "run-guarded.sh")
        fake = self.root / "scripts" / "zavod_guard.py"
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, pathlib, sys, time\n"
            "if 'ZAVOD_LIVE_LOCK_FD' in os.environ or "
            "'ZAVOD_BATCH_CONTRACT_FD' in os.environ:\n"
            "    raise SystemExit(91)\n"
            "root = pathlib.Path.cwd()\n"
            "with (root / 'guard-launches').open('a') as handle:\n"
            "    handle.write('launch\\n')\n"
            "launch_count = len((root / 'guard-launches').read_text().splitlines())\n"
            "if (root / 'hold-first-guard').exists() and launch_count == 1:\n"
            "    (root / 'first-guard-started').touch()\n"
            "    while not (root / 'release-first-guard').exists():\n"
            "        time.sleep(0.01)\n"
            "print(json.dumps(sys.argv[1:]))\n"
        )
        fake.chmod(0o755)

    def tearDown(self):
        shutil.rmtree(self.root)

    def invoke(self, *args, env=None, pass_fds=()):
        return subprocess.run(
            ["bash", "scripts/run-guarded.sh", *args],
            cwd=self.root,
            text=True,
            capture_output=True,
            env=env,
            pass_fds=pass_fds,
            timeout=5,
        )

    def invoke_with_inherited_lock(self, *args):
        lock_path = self.root / "state" / ".zavod-live.lock"
        lock_path.touch(mode=0o600)
        lock_path.chmod(0o600)
        descriptor = os.open(lock_path, os.O_RDWR)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        environment = os.environ.copy()
        environment["ZAVOD_LIVE_LOCK_FD"] = str(descriptor)
        try:
            return self.invoke(
                *args,
                env=environment,
                pass_fds=(descriptor,),
            )
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def prepare_diagnostic_config(self):
        state_path = self.root / "state"
        state_path.chmod(0o700)
        active = self.root / "state" / ".mint-run-active"
        active.write_text(f"{self.DIAGNOSTIC_RUN_ID}\n")
        active.chmod(0o600)
        relative_path = (
            f"state/mint-runs/{self.DIAGNOSTIC_RUN_ID}/"
            "selector-diagnostic.toml"
        )
        config_path = self.root / relative_path
        config_path.parent.mkdir(parents=True)
        (state_path / "mint-runs").chmod(0o700)
        config_path.parent.chmod(0o700)
        config_path.write_text("# fake diagnostic config\n")
        config_path.chmod(0o600)
        return relative_path, config_path

    def diagnostic_args(self, relative_path):
        config_sha256 = hashlib.sha256(
            (self.root / relative_path).read_bytes()
        ).hexdigest()
        tokens_sha256 = hashlib.sha256(
            f'tokens = ["{DIAGNOSTIC_TARGET}"]\n'.encode()
        ).hexdigest()
        return (
            "--live-confirmed",
            "--timeout",
            "60",
            "--profile",
            "selector-diagnostic",
            "--config",
            relative_path,
            "--test-mode",
            "--diagnostic-mode",
            "d0",
            "--diagnostic-target",
            DIAGNOSTIC_TARGET,
            "--config-sha256",
            config_sha256,
            "--tokens-sha256",
            tokens_sha256,
        )

    def prepare_auto_filter_workspace(
        self,
        batch_id="20260726T123000Z",
        stage_index=0,
        stage_name="baseline",
    ):
        state_path = self.root / "state"
        state_path.chmod(0o700)
        relative_path = (
            f"state/auto-diagnose-runs/{batch_id}/stages/"
            f"{stage_index}-{stage_name}"
        )
        stage_path = self.root / relative_path
        stage_path.mkdir(parents=True)
        for directory in (
            state_path / "auto-diagnose-runs",
            state_path / "auto-diagnose-runs" / batch_id,
            state_path / "auto-diagnose-runs" / batch_id / "stages",
            stage_path,
        ):
            directory.chmod(0o700)
        contract_path = stage_path / "stage-contract.json"
        contract_path.write_text("{}\n")
        contract_path.chmod(0o600)
        return relative_path, contract_path

    def invoke_with_auto_descriptors(self, workspace, contract_path, *extra):
        lock_path = self.root / "state" / ".zavod-live.lock"
        lock_path.touch(mode=0o600)
        lock_path.chmod(0o600)
        lock_descriptor = os.open(lock_path, os.O_RDWR)
        contract_descriptor = os.open(contract_path, os.O_RDONLY)
        fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        environment = os.environ.copy()
        environment["ZAVOD_LIVE_LOCK_FD"] = str(lock_descriptor)
        environment["ZAVOD_BATCH_CONTRACT_FD"] = str(contract_descriptor)
        try:
            return self.invoke(
                "--live-confirmed",
                "--timeout",
                "300",
                "--profile",
                "auto-filter-live",
                "--workspace",
                workspace,
                *extra,
                env=environment,
                pass_fds=(lock_descriptor, contract_descriptor),
            )
        finally:
            fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
            os.close(contract_descriptor)
            os.close(lock_descriptor)

    def test_defaults_to_300_seconds(self):
        result = self.invoke("--live-confirmed")
        self.assertEqual(result.returncode, 0)
        self.assertIn('"--timeout-seconds", "300"', result.stdout)

    def test_accepts_bounded_timeout(self):
        result = self.invoke("--live-confirmed", "--timeout", "60")
        self.assertEqual(result.returncode, 0)
        self.assertIn('"--timeout-seconds", "60"', result.stdout)

    def test_accepts_single_mint_profile(self):
        result = self.invoke(
            "--live-confirmed",
            "--timeout",
            "60",
            "--profile",
            "single-mint-auto",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"--profile", "single-mint-auto"', result.stdout)

    def test_auto_filter_live_requires_exact_descriptor_bound_arguments(self):
        workspace, contract_path = self.prepare_auto_filter_workspace()

        missing_descriptors = self.invoke(
            "--live-confirmed",
            "--timeout",
            "300",
            "--profile",
            "auto-filter-live",
            "--workspace",
            workspace,
        )
        self.assertNotEqual(missing_descriptors.returncode, 0)

        accepted = self.invoke_with_auto_descriptors(
            workspace,
            contract_path,
        )
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        launched = json.loads(accepted.stdout)
        self.assertEqual(
            launched[:8],
            [
                "run",
                "--live-confirmed",
                "--timeout-seconds",
                "300",
                "--profile",
                "auto-filter-live",
                "--workspace-root",
                workspace,
            ],
        )
        self.assertEqual(launched[8], "--live-lock-fd")
        self.assertRegex(launched[9], r"^[0-9]+$")
        self.assertEqual(launched[10], "--batch-contract-fd")
        self.assertRegex(launched[11], r"^[0-9]+$")

        forbidden = (
            ("--test-mode",),
            ("--config", "config.toml"),
            ("--diagnostic-target", DIAGNOSTIC_TARGET),
            ("--config-sha256", "1" * 64),
            ("--tokens-sha256", "2" * 64),
            ("--workspace", workspace),
            ("--profile", "auto-filter-live"),
            ("--timeout", "300"),
        )
        for extra in forbidden:
            with self.subTest(extra=extra):
                result = self.invoke_with_auto_descriptors(
                    workspace,
                    contract_path,
                    *extra,
                )
                self.assertNotEqual(result.returncode, 0, result.stdout)

    def test_auto_filter_live_rejects_noncanonical_and_unknown_stage_paths(self):
        workspace, contract_path = self.prepare_auto_filter_workspace()
        invalid_paths = (
            "/absolute/stage",
            "state/auto-diagnose-runs/20260726T123000Z/stages/0-unknown",
            "state/auto-diagnose-runs/20260726T123001Z/stages/1-baseline",
            "state/auto-diagnose-runs/20260726T123000Z/stages/1-offchain",
            "state/auto-diagnose-runs/20260726T123000Z/stages/00-baseline",
            "state/auto-diagnose-runs/../20260726T123000Z/stages/0-baseline",
        )
        for invalid in invalid_paths:
            with self.subTest(path=invalid):
                result = self.invoke_with_auto_descriptors(
                    invalid,
                    contract_path,
                )
                self.assertNotEqual(result.returncode, 0, result.stdout)

    def test_auto_filter_live_rejects_partial_or_invalid_contract_descriptor(self):
        workspace, contract_path = self.prepare_auto_filter_workspace()
        auto_args = (
            "--live-confirmed",
            "--timeout",
            "300",
            "--profile",
            "auto-filter-live",
            "--workspace",
            workspace,
        )

        live_only = self.invoke_with_inherited_lock(*auto_args)
        self.assertNotEqual(live_only.returncode, 0)

        contract_descriptor = os.open(contract_path, os.O_RDONLY)
        environment = os.environ.copy()
        environment["ZAVOD_BATCH_CONTRACT_FD"] = str(
            contract_descriptor
        )
        try:
            batch_only = self.invoke(
                *auto_args,
                env=environment,
                pass_fds=(contract_descriptor,),
            )
        finally:
            os.close(contract_descriptor)
        self.assertNotEqual(batch_only.returncode, 0)

        contract_path.chmod(0o644)
        wrong_mode = self.invoke_with_auto_descriptors(
            workspace,
            contract_path,
        )
        self.assertNotEqual(wrong_mode.returncode, 0)
        contract_path.chmod(0o600)

        held = contract_path.with_name("stage-contract-held.json")
        contract_path.rename(held)
        contract_path.symlink_to(held.name)
        symlink = self.invoke_with_auto_descriptors(
            workspace,
            contract_path,
        )
        self.assertNotEqual(symlink.returncode, 0)
        contract_path.unlink()
        held.rename(contract_path)

        unrelated = self.root / "unrelated-contract"
        unrelated.write_text("{}\n")
        unrelated.chmod(0o600)
        wrong_identity = self.invoke_with_auto_descriptors(
            workspace,
            unrelated,
        )
        self.assertNotEqual(wrong_identity.returncode, 0)

    def test_diagnostic_requires_test_mode_and_fixed_config(self):
        relative_path, _config_path = self.prepare_diagnostic_config()
        diagnostic_args = self.diagnostic_args(relative_path)
        without_test_mode = tuple(
            value
            for value in diagnostic_args
            if value != "--test-mode"
        )

        rejected = [
            self.invoke(*diagnostic_args),
            self.invoke_with_inherited_lock(*without_test_mode),
            self.invoke_with_inherited_lock(*diagnostic_args, "--test-mode"),
            self.invoke_with_inherited_lock(*diagnostic_args[:-2]),
            self.invoke_with_inherited_lock(
                "--live-confirmed",
                "--timeout",
                "60",
                "--profile",
                "single-mint-auto",
                "--test-mode",
            ),
        ]
        singleton_options = (
            ("--timeout", "60"),
            ("--profile", "selector-diagnostic"),
            ("--config", relative_path),
            ("--diagnostic-mode", "d0"),
            ("--diagnostic-target", DIAGNOSTIC_TARGET),
            (
                "--config-sha256",
                diagnostic_args[
                    diagnostic_args.index("--config-sha256") + 1
                ],
            ),
            (
                "--tokens-sha256",
                diagnostic_args[
                    diagnostic_args.index("--tokens-sha256") + 1
                ],
            ),
        )
        rejected.extend(
            self.invoke_with_inherited_lock(
                *diagnostic_args,
                option,
                value,
            )
            for option, value in singleton_options
        )

        arbitrary = self.root / "state" / "arbitrary.toml"
        arbitrary.write_text("# unrelated config\n")
        arbitrary.chmod(0o600)
        arbitrary_args = list(diagnostic_args)
        arbitrary_args[arbitrary_args.index(relative_path)] = "state/arbitrary.toml"
        rejected.append(self.invoke_with_inherited_lock(*arbitrary_args))

        for result in rejected:
            self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertFalse((self.root / "guard-launches").exists())

        accepted = self.invoke_with_inherited_lock(*diagnostic_args)
        config_sha256 = diagnostic_args[
            diagnostic_args.index("--config-sha256") + 1
        ]
        tokens_sha256 = diagnostic_args[
            diagnostic_args.index("--tokens-sha256") + 1
        ]

        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertEqual(
            json.loads(accepted.stdout),
            [
                "run",
                "--live-confirmed",
                "--config",
                relative_path,
                "--timeout-seconds",
                "60",
                "--profile",
                "selector-diagnostic",
                "--test-mode",
                "--diagnostic-mode",
                "d0",
                "--diagnostic-target",
                DIAGNOSTIC_TARGET,
                "--config-sha256",
                config_sha256,
                "--tokens-sha256",
                tokens_sha256,
            ],
        )
        self.assertEqual(
            (self.root / "guard-launches").read_text().splitlines(),
            ["launch"],
        )

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

    def test_direct_live_invocations_contend_without_second_launch(self):
        (self.root / "hold-first-guard").touch()
        first = subprocess.Popen(
            ["bash", "scripts/run-guarded.sh", "--live-confirmed"],
            cwd=self.root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            for _ in range(200):
                if (self.root / "first-guard-started").exists():
                    break
                time.sleep(0.01)
            else:
                self.fail("first fake guard did not start")

            second = self.invoke("--live-confirmed")
            self.assertNotEqual(second.returncode, 0)
            self.assertEqual(
                (self.root / "guard-launches").read_text().splitlines(),
                ["launch"],
            )
        finally:
            (self.root / "release-first-guard").touch()
            first.communicate(timeout=5)

    def test_inherited_lock_descriptor_must_match_owned_workspace_lock(self):
        unrelated = self.root / "unrelated-lock"
        descriptor = os.open(unrelated, os.O_RDWR | os.O_CREAT, 0o600)
        environment = os.environ.copy()
        environment["ZAVOD_LIVE_LOCK_FD"] = str(descriptor)
        try:
            result = self.invoke(
                "--live-confirmed",
                env=environment,
                pass_fds=(descriptor,),
            )
        finally:
            os.close(descriptor)

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "guard-launches").exists())

    def test_valid_inherited_workspace_lock_launches_without_deadlock(self):
        lock_path = self.root / "state" / ".zavod-live.lock"
        lock_path.touch(mode=0o600)
        lock_path.chmod(0o600)
        descriptor = os.open(lock_path, os.O_RDWR)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        environment = os.environ.copy()
        environment["ZAVOD_LIVE_LOCK_FD"] = str(descriptor)
        try:
            result = self.invoke(
                "--live-confirmed",
                "--profile",
                "single-mint-auto",
                env=environment,
                pass_fds=(descriptor,),
            )
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            (self.root / "guard-launches").read_text().splitlines(),
            ["launch"],
        )


if __name__ == "__main__":
    unittest.main()
