import copy
import json
import shutil
import subprocess
import tempfile
import unittest
import signal
from pathlib import Path

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
            timeout_seconds=300,
        )
        self.assertEqual(result["reason"], "loss_threshold")
        self.assertEqual(result["end_balance"], 75_000_000)
        self.assertEqual(signals[0], (4321, signal.SIGINT))

    def test_stops_at_timeout(self):
        times = iter([0, 300])
        result = supervise(
            child=FakeChild(),
            start_balance=100_000_000,
            balance_reader=lambda: 100_000_000,
            monotonic=times.__next__,
            sleep=lambda seconds: None,
            killpg=lambda pid, sig: None,
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


if __name__ == "__main__":
    unittest.main()
