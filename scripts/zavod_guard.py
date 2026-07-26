#!/usr/bin/env python3
import argparse
import codecs
import errno
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
import tomllib
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
EXPECTED_SENDERS = {
    "spam": True,
    "jito": True,
    "helius": False,
    "helius_swqos": True,
    "circular": True,
    "temporal": False,
    "falcon": True,
}
PROFILE_SENDERS = {
    "only-spam": {**EXPECTED_SENDERS, "jito": False, "helius_swqos": False, "circular": False, "falcon": False},
    "only-jito": {**EXPECTED_SENDERS, "spam": False, "helius_swqos": False, "circular": False, "falcon": False},
    "circular-falcon": {**EXPECTED_SENDERS, "spam": False, "jito": False, "helius_swqos": False},
    "circular-falcon-spam": {**EXPECTED_SENDERS, "jito": False, "helius_swqos": False},
}
LOSS_LIMIT_LAMPORTS = 30_000_000
EARLY_STOP_LAMPORTS = 25_000_000
DEFAULT_TIMEOUT_SECONDS = 300
AUTO_FILTER_STAGE_NAMES = (
    "baseline",
    "offchain",
    "activity",
    "aggregate_profit",
    "per_arb_profit",
    "roi",
    "volume",
    "pool_liquidity",
)
AUTO_FILTER_CONTRACT_KEYS = frozenset(
    {
        "schema",
        "batch_id",
        "stage_index",
        "stage_name",
        "target_mint",
        "timeout_seconds",
        "batch_start_balance_lamports",
        "early_stop_lamports",
        "loss_limit_lamports",
        "config_sha256",
        "tokens_sha256",
        "binary_sha256",
        "three_hop_required",
    }
)
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM_ID = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
DISPATCH_SCAN_OVERLAP = 256
TEST_MODE_DISPATCH_PATTERN = re.compile(
    r"(?:\bTransaction\s+sent\s+successfully\b|"
    r"\b(?:sending|dispatch(?:ing|ed)?)\s+"
    r"(?:a\s+)?(?:transaction|bundle)\b)",
    re.IGNORECASE,
)
DIAGNOSTIC_VIOLATION_REASONS = frozenset(
    {
        "cleanup_failed",
        "diagnostic_loss_violation",
        "input_integrity_violation",
        "protected_output_violation",
        "rpc_error",
        "test_mode_dispatch_violation",
        "token_account_growth_violation",
    }
)
MANUAL_SINGLE_MINT = "FB44zC6s2jkysjaB2NC8u6XqwhPJwir1DYFzEhXbpump"
MANUAL_SINGLE_POOLS = frozenset(
    {
        "8dxAgMTRUmCMVProMisWFS26EgiJwbMoiwfMZNeopSQZ",
        "7CTjvXcZhm2R5CvUXn3SyAKWvZtz2ZgNtv4f8BoBv57K",
    }
)
MANUAL_SINGLE_LUTS = frozenset(
    {
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
    }
)


class GuardError(RuntimeError):
    pass


class _GuardArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        del message
        raise GuardError("invalid command arguments")


class _DiagnosticLaunchSkipped(RuntimeError):
    pass


def load_config_bytes(data):
    try:
        return expand_environment(tomllib.loads(data.decode()))
    except (UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise GuardError("config.toml is invalid or unreadable") from exc


def load_config(path):
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        raise GuardError("config.toml is invalid or unreadable") from exc
    return load_config_bytes(data)


_ENV_VALUE = re.compile(r"^\$(?:\{)?([A-Za-z_][A-Za-z0-9_]*)(?:\})?$")


def expand_environment(value):
    """Expand exact $VAR/${VAR} TOML values and fail if the variable is absent."""
    if isinstance(value, dict):
        return {key: expand_environment(item) for key, item in value.items()}
    if isinstance(value, list):
        return [expand_environment(item) for item in value]
    if isinstance(value, str):
        match = _ENV_VALUE.fullmatch(value)
        if match:
            name = match.group(1)
            if name not in os.environ:
                raise GuardError("required environment configuration is missing")
            return os.environ[name]
    return value


def base58_encode(data):
    if not data:
        return ""
    zeros = len(data) - len(data.lstrip(b"\0"))
    value = int.from_bytes(data, "big")
    encoded = ""
    while value:
        value, remainder = divmod(value, 58)
        encoded = ALPHABET[remainder] + encoded
    return "1" * zeros + encoded


def base58_decode(value):
    if not isinstance(value, str) or not value:
        raise GuardError("wallet.private_key is invalid")
    number = 0
    try:
        for char in value:
            number = number * 58 + ALPHABET.index(char)
    except ValueError as exc:
        raise GuardError("wallet.private_key is invalid") from exc
    decoded = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    return b"\0" * (len(value) - len(value.lstrip("1"))) + decoded


def _get(config, section, key, default=None):
    value = config.get(section, {})
    return value.get(key, default) if isinstance(value, dict) else default


def validate_manual_single(config, root):
    """Validate the sole approved manual market file without exposing its contents."""
    errors = []
    if root is None:
        return ["manual-single requires a preflight root"]

    market_sources = config.get("markets_file")
    if not isinstance(market_sources, list) or len(market_sources) != 1:
        return ["manual-single markets_file must have exactly one source"]
    source = market_sources[0]
    if not isinstance(source, dict) or source.get("enabled") is not True:
        return ["manual-single markets_file must have exactly one enabled source"]
    if (
        source.get("path") != "markets.toml"
        or isinstance(source.get("update_seconds"), bool)
        or source.get("update_seconds") != 0
        or source.get("auto_luts") is not False
    ):
        return ["manual-single markets_file settings are invalid"]

    try:
        with (Path(root) / "markets.toml").open("rb") as handle:
            markets = tomllib.load(handle)
    except OSError:
        return ["manual-single markets file is missing or unreadable"]
    except tomllib.TOMLDecodeError:
        return ["manual-single markets file is invalid"]

    groups = markets.get("group") if isinstance(markets, dict) else None
    if not isinstance(groups, list) or len(groups) != 1 or not isinstance(groups[0], dict):
        return ["manual-single market group shape is invalid"]
    group = groups[0]
    if "mint_b" in group:
        return ["manual-single market group shape is invalid"]

    mint = group.get("mint_a")
    pools = group.get("markets_a")
    luts = group.get("luts")
    if (
        not isinstance(mint, str)
        or not isinstance(pools, list)
        or not isinstance(luts, list)
        or not all(isinstance(pool, str) for pool in pools)
        or not all(isinstance(lut, str) for lut in luts)
    ):
        return ["manual-single market group shape is invalid"]
    if (
        mint != MANUAL_SINGLE_MINT
        or len(pools) != len(MANUAL_SINGLE_POOLS)
        or set(pools) != MANUAL_SINGLE_POOLS
        or len(luts) != len(MANUAL_SINGLE_LUTS)
        or set(luts) != MANUAL_SINGLE_LUTS
    ):
        return ["manual-single market group values do not match the approved set"]
    return errors


def validate_single_mint_auto(config):
    errors = []
    if _get(config, "auto", "enabled") is not True:
        errors.append("auto.enabled must be true")
    market_sources = config.get("markets_file", [])
    if (
        not isinstance(market_sources, list)
        or any(
            not isinstance(source, dict)
            or source.get("enabled") is not False
            for source in market_sources
        )
    ):
        errors.append(
            "single-mint auto requires all static market sources disabled"
        )
    return errors


def validate_selector_diagnostic(config):
    errors = validate_single_mint_auto(config)
    if _get(config, "auto", "force_two_mints") is not False:
        errors.append("auto.force_two_mints must be false")
    filters = _get(config, "auto", "filters")
    if (
        not isinstance(filters, dict)
        or isinstance(filters.get("limit"), bool)
        or filters.get("limit") != 1
    ):
        errors.append("auto.filters.limit must be 1")
    if _get(config, "bot", "merge_mints") is not False:
        errors.append("bot.merge_mints must be false")
    return errors


def validate_config(config, profile="default", root=None):
    errors = []
    expected_senders = dict(PROFILE_SENDERS.get(profile, EXPECTED_SENDERS))
    if profile == "ab-no-swqos":
        expected_senders["helius_swqos"] = False
    elif profile not in (
        "default",
        "manual-single",
        "single-mint-auto",
        "selector-diagnostic",
        "auto-filter-live",
        *PROFILE_SENDERS,
    ):
        errors.append("unknown validation profile")
    for section, expected in expected_senders.items():
        if _get(config, section, "enabled") is not expected:
            errors.append(f"{section}.enabled must be {str(expected).lower()}")
    for section, key in (
        ("wallet", "private_key"),
        ("rpc", "url"),
        ("circular", "api-key"),
        ("falcon", "uuid"),
    ):
        if not isinstance(_get(config, section, key), str) or not _get(config, section, key).strip():
            errors.append(f"{section}.{key} is required")
    send_urls = _get(config, "spam", "sending_rpc_urls", [])
    if not isinstance(send_urls, list) or not any(isinstance(url, str) and url.strip() for url in send_urls):
        errors.append("spam.sending_rpc_urls requires a non-empty URL")
    if profile == "manual-single":
        if _get(config, "auto", "enabled") is not False:
            errors.append("auto.enabled must be false")
        errors.extend(validate_manual_single(config, root))
    else:
        if profile == "selector-diagnostic":
            errors.extend(validate_selector_diagnostic(config))
        elif profile in ("single-mint-auto", "auto-filter-live"):
            errors.extend(validate_single_mint_auto(config))
        elif _get(config, "auto", "enabled") is not True:
            errors.append("auto.enabled must be true")
    if _get(config, "flashloan", "enabled") is not True:
        errors.append("flashloan.enabled must be true")
    if _get(config, "dynamic_fees", "enabled") is not False:
        errors.append("dynamic_fees.enabled must be false")
    for section in expected_senders:
        for key in ("dynamic_priority_fee", "dynamic_tip"):
            setting = _get(config, section, key)
            if isinstance(setting, dict) and setting.get("enable") is not False:
                errors.append(f"{section}.{key}.enable must be false")
        section_config = config.get(section, {})
        if isinstance(section_config, dict):
            for key, setting in section_config.items():
                if not isinstance(setting, dict):
                    continue
                for lower_key, upper_key in (("from", "to"), ("min", "max")):
                    if lower_key not in setting and upper_key not in setting:
                        continue
                    lower = setting.get(lower_key)
                    upper = setting.get(upper_key)
                    if (
                        isinstance(lower, bool)
                        or isinstance(upper, bool)
                        or not isinstance(lower, (int, float))
                        or not isinstance(upper, (int, float))
                        or upper <= lower
                    ):
                        errors.append(f"{section}.{key}.{upper_key} must be greater than {lower_key}")
    return errors


def _redaction_secrets(config):
    values = []
    sensitive_markers = (
        "api_key",
        "apikey",
        "credential",
        "password",
        "private_key",
        "secret",
        "token",
        "url",
        "uuid",
    )

    def collect(value, sensitive=False):
        if isinstance(value, dict):
            for key, item in value.items():
                normalized = str(key).lower().replace("-", "_")
                collect(
                    item,
                    sensitive=sensitive
                    or any(
                        marker in normalized
                        for marker in sensitive_markers
                    ),
                )
        elif isinstance(value, list):
            for item in value:
                collect(item, sensitive=sensitive)
        elif sensitive and isinstance(value, str) and value:
            values.append(value)

    collect(config)
    return tuple(
        sorted(
            set(values),
            key=len,
            reverse=True,
        )
    )


class ProtectedOutputPolicy:
    UUID_PATTERN = re.compile(
        r"(?i)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
        r"[0-9a-f]{4}-[0-9a-f]{12}"
    )
    SIGNATURE_PATTERN = re.compile(
        rf"(?<![{ALPHABET}])[{ALPHABET}]{{64,88}}(?![{ALPHABET}])"
    )
    URL_SCHEME_PATTERN = re.compile(
        r"[a-z][a-z0-9+.-]{0,31}://",
        re.I,
    )
    URL_PATTERN = re.compile(
        r"[a-z][a-z0-9+.-]{0,31}://[^\s<>\"'\[\]{}()]+",
        re.I,
    )
    URL_DELIMITERS = frozenset("\"'<>[]{}()")
    BASE58_CHARS = frozenset(ALPHABET)
    GENERIC_STREAM_KEEP = 91

    def __init__(self, secrets=()):
        self.secrets = tuple(
            sorted(
                {
                    value
                    for value in secrets
                    if isinstance(value, str) and value
                },
                key=len,
                reverse=True,
            )
        )
        self.stream_keep = max(
            self.GENERIC_STREAM_KEEP,
            max((len(secret) for secret in self.secrets), default=0),
        )

    @classmethod
    def from_config(cls, config):
        return cls(_redaction_secrets(config))

    @staticmethod
    def is_signature_token(value):
        if not 64 <= len(value) <= 88:
            return False
        try:
            return len(base58_decode(value)) == 64
        except GuardError:
            return False

    def redact_text(self, text):
        for secret in self.secrets:
            text = text.replace(secret, "<redacted>")
        text = self.URL_PATTERN.sub("<redacted>", text)
        text = self.UUID_PATTERN.sub("<redacted>", text)
        return self.SIGNATURE_PATTERN.sub(
            lambda match: (
                "<redacted>"
                if self.is_signature_token(match.group(0))
                else match.group(0)
            ),
            text,
        )

    def contains_protected(self, text):
        return self.redact_text(text) != text


def redact_text(text, config):
    return ProtectedOutputPolicy.from_config(config).redact_text(text)


class StreamingRedactor:
    def __init__(self, sink, policy, on_protected=None):
        self.sink = sink
        self.policy = (
            policy
            if isinstance(policy, ProtectedOutputPolicy)
            else ProtectedOutputPolicy(policy)
        )
        self.on_protected = on_protected
        self.buffer = ""
        self.closed = False
        self._discard_url = False
        self._inside_base58_token = False

    @staticmethod
    def _is_url_delimiter(character):
        return (
            character.isspace()
            or character in ProtectedOutputPolicy.URL_DELIMITERS
        )

    def _discard_protected_tail(self, kind):
        if kind == "url":
            predicate = lambda character: not self._is_url_delimiter(
                character
            )
        else:
            raise ValueError("unsupported protected tail")
        index = 0
        while index < len(self.buffer) and predicate(self.buffer[index]):
            index += 1
        self.buffer = self.buffer[index:]
        if self.buffer:
            if kind == "url":
                self._discard_url = False

    def _write_redacted(self):
        self.sink.write("<redacted>")
        if self.on_protected is not None:
            self.on_protected()

    def _drain_one(self, final=False):
        if not self.buffer:
            return False
        if self._discard_url:
            self._discard_protected_tail("url")
            return bool(self.buffer)
        if (
            self._inside_base58_token
            and self.buffer[0] not in self.policy.BASE58_CHARS
        ):
            self._inside_base58_token = False

        for secret in self.policy.secrets:
            if self.buffer.startswith(secret):
                self._write_redacted()
                self.buffer = self.buffer[len(secret):]
                return True

        url_scheme_match = self.policy.URL_SCHEME_PATTERN.match(self.buffer)
        if url_scheme_match is not None:
            self._write_redacted()
            self.buffer = self.buffer[url_scheme_match.end():]
            self._discard_url = True
            return True

        uuid_match = self.policy.UUID_PATTERN.match(self.buffer)
        if uuid_match is not None:
            self._write_redacted()
            self.buffer = self.buffer[uuid_match.end():]
            return True

        signature_end = 0
        if not self._inside_base58_token:
            while (
                signature_end < len(self.buffer)
                and self.buffer[signature_end] in self.policy.BASE58_CHARS
            ):
                signature_end += 1
            if signature_end:
                token_complete = final or signature_end < len(self.buffer)
                if token_complete:
                    token = self.buffer[:signature_end]
                    if self.policy.is_signature_token(token):
                        self._write_redacted()
                        self.buffer = self.buffer[signature_end:]
                        return True

        if not final and len(self.buffer) <= self.policy.stream_keep:
            return False
        if self.buffer[0] in self.policy.BASE58_CHARS:
            self._inside_base58_token = True
        self.sink.write(self.buffer[0])
        self.buffer = self.buffer[1:]
        return True

    def feed(self, text):
        if self.closed:
            raise ValueError("streaming redactor is closed")
        self.buffer += text
        while self._drain_one():
            pass
        self.sink.flush()

    def close(self):
        if self.closed:
            return
        while self.buffer:
            self._drain_one(final=True)
        self.sink.flush()
        self.closed = True


class OutputPump:
    def __init__(self, source, sink, config, test_mode=False):
        self.source = source
        self.output_error_event = threading.Event()
        self.protected_output_event = threading.Event()
        self.test_mode_dispatch_event = threading.Event()
        self.redactor = StreamingRedactor(
            sink,
            ProtectedOutputPolicy.from_config(config),
            on_protected=self.protected_output_event.set,
        )
        self.test_mode = test_mode
        self._dispatch_buffer = ""
        self.stop_event = threading.Event()
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        try:
            os.set_blocking(self.source.fileno(), False)
        except (AttributeError, OSError, TypeError, ValueError):
            pass
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        try:
            while not self.stop_event.is_set():
                try:
                    chunk = self.source.read(4096)
                except BlockingIOError:
                    chunk = None
                if chunk is None:
                    self.stop_event.wait(0.05)
                    continue
                if not chunk:
                    tail = self.decoder.decode(b"", final=True)
                    if tail:
                        self.redactor.feed(tail)
                    break
                if isinstance(chunk, bytes):
                    chunk = self.decoder.decode(chunk, final=False)
                if chunk:
                    if self.test_mode:
                        dispatch_text = self._dispatch_buffer + chunk
                        if TEST_MODE_DISPATCH_PATTERN.search(dispatch_text):
                            self.test_mode_dispatch_event.set()
                        self._dispatch_buffer = dispatch_text[
                            -DISPATCH_SCAN_OVERLAP:
                        ]
                    self.redactor.feed(chunk)
        except Exception:
            self.output_error_event.set()
        finally:
            try:
                self.redactor.close()
            except Exception:
                self.output_error_event.set()

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def join(self, timeout):
        self.thread.join(timeout)

    def is_alive(self):
        return self.thread.is_alive()


def wallet_pubkey(secret):
    decoded = base58_decode(secret)
    if len(decoded) != 64:
        raise GuardError("wallet.private_key must decode to a 64-byte Solana keypair")
    descriptor = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", prefix="zavod-keypair-", suffix=".json", delete=False) as handle:
            descriptor = handle.name
            os.fchmod(handle.fileno(), 0o600)
            json.dump(list(decoded), handle)
        result = subprocess.run(
            ["solana-keygen", "pubkey", descriptor],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        public_key = result.stdout.strip()
        if public_key != base58_encode(decoded[32:]):
            raise GuardError("wallet keypair public key mismatch")
        return public_key
    except (OSError, subprocess.SubprocessError) as exc:
        raise GuardError("wallet public key derivation failed") from exc
    finally:
        if descriptor:
            try:
                os.unlink(descriptor)
            except FileNotFoundError:
                pass


def _http_transport(url, payload, timeout):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def get_balance_lamports(rpc_url, pubkey, transport=None):
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getBalance",
        "params": [pubkey, {"commitment": "confirmed"}],
    }
    try:
        body = (transport or _http_transport)(rpc_url, payload, 5)
        value = body["result"]["value"]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("invalid balance")
        return value
    except Exception as exc:
        raise GuardError("RPC balance check failed") from exc


def validate_token_mint_account(rpc_url, mint, transport=None):
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getAccountInfo",
        "params": [
            mint,
            {"encoding": "jsonParsed", "commitment": "confirmed"},
        ],
    }
    try:
        if len(base58_decode(mint)) != 32:
            raise ValueError("invalid mint identity")
        body = (transport or _http_transport)(rpc_url, payload, 5)
        value = body["result"]["value"]
        parsed = value["data"]["parsed"] if value is not None else None
        if (
            not isinstance(value, dict)
            or value.get("executable") is not False
            or value.get("owner")
            not in (TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID)
            or not isinstance(parsed, dict)
            or parsed.get("type") != "mint"
            or parsed.get("info", {}).get("isInitialized") is not True
        ):
            raise ValueError("invalid token mint")
    except Exception as exc:
        raise GuardError("RPC mint-account check failed") from exc


def get_token_account_pubkeys(rpc_url, pubkey, transport=None):
    accounts = set()
    try:
        for program_id in (TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID):
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getTokenAccountsByOwner",
                "params": [
                    pubkey,
                    {"programId": program_id},
                    {"encoding": "base64", "commitment": "confirmed"},
                ],
            }
            body = (transport or _http_transport)(
                rpc_url,
                payload,
                5,
            )
            values = body["result"]["value"]
            if not isinstance(values, list):
                raise ValueError("invalid token accounts")
            for value in values:
                account = value.get("pubkey") if isinstance(value, dict) else None
                if not isinstance(account, str) or not account:
                    raise ValueError("invalid token account")
                accounts.add(account)
        return frozenset(accounts)
    except Exception as exc:
        raise GuardError("RPC token-account check failed") from exc


def should_stop_for_loss(start_balance, current_balance):
    return start_balance - current_balance >= EARLY_STOP_LAMPORTS


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
        except OSError:
            return {
                "exit_code": exit_code,
                "group_absent": False,
                "interrupted": interrupted,
            }
        except GuardError:
            interrupted = True
            continue
        try:
            started, was_interrupted = _retry_keyboard_interrupt(monotonic)
            interrupted |= was_interrupted
        except GuardError:
            interrupted = True
            continue
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
    if exists:
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


def _verified_shutdown(child, **kwargs):
    try:
        cleanup, interrupted = _retry_keyboard_interrupt(
            lambda: _shutdown_child(child, **kwargs)
        )
    except (GuardError, OSError):
        return {
            "exit_code": getattr(child, "returncode", None),
            "group_absent": False,
            "interrupted": True,
        }
    if interrupted:
        cleanup = {**cleanup, "interrupted": True}
    return cleanup


def supervise(
    child,
    start_balance,
    balance_reader,
    monotonic,
    sleep,
    output_error_event=None,
    input_integrity_checker=None,
    protected_output_event=None,
    starting_token_accounts=None,
    test_mode_dispatch_event=None,
    token_account_reader=None,
    killpg=os.killpg,
    group_exists=None,
    signal_grace=DEFAULT_SIGNAL_GRACE,
    timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
    cleanup_child=True,
    operator_signal_event=None,
    diagnostic=False,
    enforce_input_integrity=False,
):
    end_balance = start_balance
    reason = None
    exit_code = None
    dispatch_watcher_stop = threading.Event()
    dispatch_watcher = None
    if test_mode_dispatch_event is not None and not cleanup_child:
        def stop_on_dispatch():
            while not dispatch_watcher_stop.is_set():
                if not test_mode_dispatch_event.wait(0.01):
                    continue
                if dispatch_watcher_stop.is_set():
                    return
                try:
                    killpg(child.pid, signal.SIGINT)
                except ProcessLookupError:
                    pass
                except OSError:
                    pass
                return

        dispatch_watcher = threading.Thread(
            target=stop_on_dispatch,
            daemon=True,
        )
        dispatch_watcher.start()
    try:
        started_at = monotonic()
        while reason is None:
            if (
                operator_signal_event is not None
                and operator_signal_event.is_set()
            ):
                reason = "operator_signal"
                break
            if (
                (diagnostic or enforce_input_integrity)
                and input_integrity_checker is not None
                and not input_integrity_checker()
            ):
                reason = "input_integrity_violation"
                break
            if (
                test_mode_dispatch_event is not None
                and test_mode_dispatch_event.is_set()
            ):
                reason = "test_mode_dispatch_violation"
                break
            if (
                (diagnostic or enforce_input_integrity)
                and protected_output_event is not None
                and protected_output_event.is_set()
            ):
                reason = "protected_output_violation"
                break
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
            if diagnostic and start_balance - end_balance > 0:
                reason = "diagnostic_loss_violation"
                break
            if not diagnostic and should_stop_for_loss(
                start_balance,
                end_balance,
            ):
                reason = "loss_threshold"
                break
            if (
                diagnostic
                and token_account_reader is not None
                and starting_token_accounts is not None
            ):
                try:
                    current_token_accounts = token_account_reader()
                    if (
                        not isinstance(
                            current_token_accounts,
                            (set, frozenset),
                        )
                        or any(
                            not isinstance(account, str) or not account
                            for account in current_token_accounts
                        )
                    ):
                        raise GuardError("invalid token-account snapshot")
                except Exception:
                    reason = "rpc_error"
                    break
                if (
                    set(current_token_accounts)
                    - set(starting_token_accounts)
                ):
                    reason = "token_account_growth_violation"
                    break
            sleep(1)
    except KeyboardInterrupt:
        reason = "operator_signal"
    finally:
        dispatch_watcher_stop.set()
        if dispatch_watcher is not None:
            dispatch_watcher.join(1)
        if cleanup_child:
            cleanup = _verified_shutdown(
                child,
                killpg=killpg,
                group_exists=group_exists,
                signal_grace=signal_grace,
            )
            if cleanup["exit_code"] is not None:
                exit_code = cleanup["exit_code"]
            if not cleanup["group_absent"]:
                reason = "cleanup_failed"
            elif (
                cleanup["interrupted"]
                and reason not in DIAGNOSTIC_VIOLATION_REASONS
                and reason != "output_error"
            ):
                reason = "operator_signal"
    return {
        "reason": reason,
        "start_balance": start_balance,
        "end_balance": end_balance,
        "observed_loss": max(0, start_balance - end_balance),
        "child_exit_code": exit_code,
    }


def _cli_version(binary, pass_fds=()):
    try:
        result = subprocess.run(
            [str(binary), "--version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
            pass_fds=pass_fds,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GuardError("CLI version check failed") from exc
    match = re.search(r"zavod-mev-bot-rust-version-cli\s+([0-9]+\.[0-9]+\.[0-9]+)", result.stdout + result.stderr)
    if not match:
        raise GuardError("CLI version check failed")
    return match.group(1)


def _selector_diagnostic_error(message="private path is invalid"):
    return GuardError(f"selector-diagnostic {message}")


def _selector_input_integrity_error():
    return GuardError("selector-diagnostic input integrity violation")


def _validate_selector_launch_contract(
    profile,
    test_mode,
    diagnostic_mode,
    diagnostic_target,
    diagnostic_config_sha256,
    diagnostic_tokens_sha256,
):
    contract_values = (
        diagnostic_mode,
        diagnostic_target,
        diagnostic_config_sha256,
        diagnostic_tokens_sha256,
    )
    if profile != "selector-diagnostic" or not test_mode:
        if any(value is not None for value in contract_values):
            raise GuardError("selector-diagnostic launch contract is invalid")
        return
    try:
        if (
            diagnostic_mode != "d0"
            or not isinstance(diagnostic_target, str)
            or len(base58_decode(diagnostic_target)) != 32
            or not isinstance(diagnostic_config_sha256, str)
            or re.fullmatch(
                r"[0-9a-f]{64}",
                diagnostic_config_sha256,
            )
            is None
            or not isinstance(diagnostic_tokens_sha256, str)
            or re.fullmatch(
                r"[0-9a-f]{64}",
                diagnostic_tokens_sha256,
            )
            is None
        ):
            raise ValueError("invalid launch contract")
    except (GuardError, TypeError, ValueError) as exc:
        raise GuardError(
            "selector-diagnostic launch contract is invalid"
        ) from exc


def _validate_owned_descriptor(descriptor, kind, mode=None):
    try:
        identity = os.fstat(descriptor)
    except OSError as exc:
        raise _selector_diagnostic_error() from exc
    expected_type = stat.S_ISDIR if kind == "directory" else stat.S_ISREG
    if not expected_type(identity.st_mode):
        raise _selector_diagnostic_error()
    if identity.st_uid != os.geteuid():
        raise _selector_diagnostic_error("path must be owned by the current user")
    if mode is not None and stat.S_IMODE(identity.st_mode) != mode:
        raise _selector_diagnostic_error(
            f"path permissions must be mode {mode:o}"
        )
    return identity


def _open_owned_relative(parent_descriptor, name, kind, mode=None):
    if (
        not isinstance(name, str)
        or not name
        or Path(name).name != name
    ):
        raise _selector_diagnostic_error()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    if kind == "directory":
        flags |= getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    except OSError as exc:
        raise _selector_diagnostic_error() from exc
    try:
        _validate_owned_descriptor(descriptor, kind, mode=mode)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _read_descriptor_bytes(descriptor):
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        chunks = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        os.lseek(descriptor, 0, os.SEEK_SET)
        return b"".join(chunks)
    except OSError as exc:
        raise _selector_diagnostic_error("config is unreadable") from exc


def _require_descriptor_bytes(
    descriptor,
    expected_sha256,
    expected_bytes=None,
):
    try:
        data = _read_descriptor_bytes(descriptor)
    except GuardError as exc:
        raise _selector_input_integrity_error() from exc
    if (
        hashlib.sha256(data).hexdigest() != expected_sha256
        or (expected_bytes is not None and data != expected_bytes)
    ):
        raise _selector_input_integrity_error()
    return data


def _open_selector_diagnostic_tokens(
    workspace_root,
    target,
    expected_sha256,
):
    root = Path(workspace_root).absolute()
    root_descriptor = None
    tokens_descriptor = None
    try:
        directory_flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        root_descriptor = os.open(root, directory_flags)
        root_identity = _validate_owned_descriptor(
            root_descriptor,
            "directory",
        )
        tokens_descriptor = _open_owned_relative(
            root_descriptor,
            "tokens.toml",
            "file",
            mode=0o600,
        )
        expected_bytes = f'tokens = ["{target}"]\n'.encode()
        _require_descriptor_bytes(
            tokens_descriptor,
            expected_sha256,
            expected_bytes=expected_bytes,
        )
        tokens_identity = _validate_owned_descriptor(
            tokens_descriptor,
            "file",
            mode=0o600,
        )
        held_descriptor = tokens_descriptor
        tokens_descriptor = None
        return (
            held_descriptor,
            expected_bytes,
            (root_identity.st_dev, root_identity.st_ino),
            (tokens_identity.st_dev, tokens_identity.st_ino),
        )
    except (GuardError, OSError) as exc:
        raise _selector_input_integrity_error() from exc
    finally:
        if tokens_descriptor is not None:
            os.close(tokens_descriptor)
        if root_descriptor is not None:
            os.close(root_descriptor)


def _selector_tokens_path_matches(
    workspace_root,
    expected_root_identity,
    expected_tokens_identity,
    expected_sha256,
    expected_bytes,
):
    root_descriptor = None
    tokens_descriptor = None
    try:
        root_descriptor = os.open(
            workspace_root,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        root_identity = _validate_owned_descriptor(
            root_descriptor,
            "directory",
        )
        if (
            root_identity.st_dev,
            root_identity.st_ino,
        ) != expected_root_identity:
            return False
        tokens_descriptor = _open_owned_relative(
            root_descriptor,
            "tokens.toml",
            "file",
            mode=0o600,
        )
        tokens_identity = _validate_owned_descriptor(
            tokens_descriptor,
            "file",
            mode=0o600,
        )
        if (
            tokens_identity.st_dev,
            tokens_identity.st_ino,
        ) != expected_tokens_identity:
            return False
        _require_descriptor_bytes(
            tokens_descriptor,
            expected_sha256,
            expected_bytes=expected_bytes,
        )
        return True
    except (GuardError, OSError):
        return False
    finally:
        if tokens_descriptor is not None:
            os.close(tokens_descriptor)
        if root_descriptor is not None:
            os.close(root_descriptor)


def _open_selector_diagnostic_config(workspace_root, requested_path):
    if workspace_root is None:
        raise _selector_diagnostic_error("workspace root is required")
    root = Path(workspace_root).absolute()
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    directories = []
    config_descriptor = None
    try:
        try:
            root_descriptor = os.open(root, directory_flags)
        except OSError as exc:
            raise _selector_diagnostic_error() from exc
        directories.append(root_descriptor)
        _validate_owned_descriptor(root_descriptor, "directory")

        state_descriptor = _open_owned_relative(
            root_descriptor,
            "state",
            "directory",
            mode=0o700,
        )
        directories.append(state_descriptor)

        marker_descriptor = _open_owned_relative(
            state_descriptor,
            ".mint-run-active",
            "file",
            mode=0o600,
        )
        try:
            marker = _read_descriptor_bytes(marker_descriptor)
        finally:
            os.close(marker_descriptor)
        try:
            marker_text = marker.decode("ascii")
        except UnicodeError as exc:
            raise _selector_diagnostic_error(
                "active marker is invalid"
            ) from exc
        match = re.fullmatch(r"([0-9]{8}T[0-9]{6}Z)\n", marker_text)
        if match is None:
            raise _selector_diagnostic_error("active marker is invalid")
        run_id = match.group(1)

        mint_runs_descriptor = _open_owned_relative(
            state_descriptor,
            "mint-runs",
            "directory",
            mode=0o700,
        )
        directories.append(mint_runs_descriptor)
        run_descriptor = _open_owned_relative(
            mint_runs_descriptor,
            run_id,
            "directory",
            mode=0o700,
        )
        directories.append(run_descriptor)

        expected_relative = (
            Path("state")
            / "mint-runs"
            / run_id
            / "selector-diagnostic.toml"
        )
        expected_absolute = root / expected_relative
        requested = Path(requested_path)
        expected = expected_absolute if requested.is_absolute() else expected_relative
        if requested != expected:
            if requested.is_absolute():
                try:
                    requested.relative_to(root)
                except ValueError as exc:
                    raise _selector_diagnostic_error(
                        "config must be inside the workspace"
                    ) from exc
            raise _selector_diagnostic_error("config path is invalid")

        config_descriptor = _open_owned_relative(
            run_descriptor,
            "selector-diagnostic.toml",
            "file",
            mode=0o600,
        )
        held_descriptor = config_descriptor
        config_descriptor = None
        return root, held_descriptor
    finally:
        if config_descriptor is not None:
            os.close(config_descriptor)
        for descriptor in reversed(directories):
            os.close(descriptor)


def _auto_filter_error(message="contract is invalid"):
    return GuardError(f"auto-filter-live {message}")


def _auto_filter_validate_descriptor(
    descriptor,
    kind,
    mode=None,
    executable=False,
):
    try:
        identity = os.fstat(descriptor)
    except OSError as exc:
        raise _auto_filter_error() from exc
    expected_type = stat.S_ISDIR if kind == "directory" else stat.S_ISREG
    if (
        not expected_type(identity.st_mode)
        or identity.st_uid != os.geteuid()
        or (
            mode is not None
            and stat.S_IMODE(identity.st_mode) != mode
        )
        or (executable and identity.st_mode & 0o111 == 0)
    ):
        raise _auto_filter_error()
    return identity


def _auto_filter_open_relative(
    parent_descriptor,
    name,
    kind,
    mode=None,
    executable=False,
):
    if (
        not isinstance(name, str)
        or not name
        or Path(name).name != name
    ):
        raise _auto_filter_error()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    if kind == "directory":
        flags |= getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    except OSError as exc:
        raise _auto_filter_error() from exc
    try:
        identity = _auto_filter_validate_descriptor(
            descriptor,
            kind,
            mode=mode,
            executable=executable,
        )
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, identity


def _auto_filter_read_descriptor(descriptor):
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        chunks = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        os.lseek(descriptor, 0, os.SEEK_SET)
        return b"".join(chunks)
    except OSError as exc:
        raise _auto_filter_error() from exc


def _load_auto_filter_contract(data):
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate contract key")
            result[key] = value
        return result

    try:
        contract = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=unique_object,
        )
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise _auto_filter_error() from exc
    if (
        not isinstance(contract, dict)
        or set(contract) != AUTO_FILTER_CONTRACT_KEYS
    ):
        raise _auto_filter_error()
    return contract


def _validate_auto_filter_contract_fields(contract, workspace_root):
    batch_id = contract["batch_id"]
    stage_index = contract["stage_index"]
    stage_name = contract["stage_name"]
    target = contract["target_mint"]
    hashes = (
        contract["config_sha256"],
        contract["tokens_sha256"],
        contract["binary_sha256"],
    )
    if (
        type(contract["schema"]) is not int
        or contract["schema"] != 1
        or not isinstance(batch_id, str)
        or re.fullmatch(r"[0-9]{8}T[0-9]{6}Z", batch_id) is None
        or type(stage_index) is not int
        or not 0 <= stage_index < len(AUTO_FILTER_STAGE_NAMES)
        or not isinstance(stage_name, str)
        or stage_name != AUTO_FILTER_STAGE_NAMES[stage_index]
        or not isinstance(target, str)
        or type(contract["timeout_seconds"]) is not int
        or contract["timeout_seconds"] != DEFAULT_TIMEOUT_SECONDS
        or type(contract["batch_start_balance_lamports"]) is not int
        or contract["batch_start_balance_lamports"] < 0
        or type(contract["early_stop_lamports"]) is not int
        or contract["early_stop_lamports"] != EARLY_STOP_LAMPORTS
        or type(contract["loss_limit_lamports"]) is not int
        or contract["loss_limit_lamports"] != LOSS_LIMIT_LAMPORTS
        or any(
            not isinstance(value, str)
            or re.fullmatch(r"[0-9a-f]{64}", value) is None
            for value in hashes
        )
        or contract["three_hop_required"] is not True
    ):
        raise _auto_filter_error()
    try:
        if len(base58_decode(target)) != 32:
            raise _auto_filter_error()
    except (GuardError, TypeError, ValueError) as exc:
        raise _auto_filter_error() from exc

    expected_workspace = (
        f"state/auto-diagnose-runs/{batch_id}/stages/"
        f"{stage_index}-{stage_name}"
    )
    if (
        not isinstance(workspace_root, (str, Path))
        or str(workspace_root) != expected_workspace
        or Path(workspace_root).is_absolute()
    ):
        raise _auto_filter_error("workspace is invalid")


def _auto_filter_require_digest(data, expected):
    if hashlib.sha256(data).hexdigest() != expected:
        raise _auto_filter_error("input integrity violation")


def _auto_filter_validate_live_lock(
    live_lock_descriptor,
    state_descriptor,
    expected_identity=None,
):
    lock_identity = _auto_filter_validate_descriptor(
        live_lock_descriptor,
        "file",
        mode=0o600,
    )
    try:
        current_descriptor = os.open(
            ".zavod-live.lock",
            os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=state_descriptor,
        )
    except OSError as exc:
        raise _auto_filter_error("live lock is invalid") from exc
    try:
        current_identity = _auto_filter_validate_descriptor(
            current_descriptor,
            "file",
            mode=0o600,
        )
        expected = expected_identity or lock_identity
        if (
            lock_identity.st_dev,
            lock_identity.st_ino,
        ) != (
            current_identity.st_dev,
            current_identity.st_ino,
        ) or (
            lock_identity.st_dev,
            lock_identity.st_ino,
        ) != (
            expected.st_dev,
            expected.st_ino,
        ):
            raise _auto_filter_error("live lock is invalid")

        try:
            fcntl.flock(
                current_descriptor,
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN):
                raise _auto_filter_error("live lock is invalid") from exc
        else:
            fcntl.flock(current_descriptor, fcntl.LOCK_UN)
            raise _auto_filter_error("live lock is not held")

        try:
            fcntl.flock(
                live_lock_descriptor,
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except OSError as exc:
            raise _auto_filter_error("live lock is invalid") from exc
        return lock_identity
    finally:
        os.close(current_descriptor)


def _open_auto_filter_live_contract(
    workspace_root,
    batch_contract_fd,
    live_lock_fd,
):
    if any(
        isinstance(descriptor, bool)
        or not isinstance(descriptor, int)
        or descriptor < 0
        for descriptor in (batch_contract_fd, live_lock_fd)
    ):
        raise _auto_filter_error("batch contract descriptor is required")
    inherited_identity = _auto_filter_validate_descriptor(
        batch_contract_fd,
        "file",
        mode=0o600,
    )
    contract_data = _auto_filter_read_descriptor(batch_contract_fd)
    contract = _load_auto_filter_contract(contract_data)
    _validate_auto_filter_contract_fields(contract, workspace_root)

    trusted_root = Path(__file__).resolve().parents[1]
    descriptors = []
    identities = {}
    files = {}
    file_data = {}
    try:
        root_descriptor = os.open(
            trusted_root,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        descriptors.append(root_descriptor)
        identities["root"] = _auto_filter_validate_descriptor(
            root_descriptor,
            "directory",
        )

        parent = root_descriptor
        directory_specs = (
            ("state", "state"),
            ("auto-diagnose-runs", "runs"),
            (contract["batch_id"], "batch"),
            ("stages", "stages"),
            (
                f"{contract['stage_index']}-{contract['stage_name']}",
                "stage",
            ),
        )
        for name, label in directory_specs:
            descriptor, identity = _auto_filter_open_relative(
                parent,
                name,
                "directory",
                mode=0o700,
            )
            descriptors.append(descriptor)
            identities[label] = identity
            parent = descriptor
        state_descriptor = descriptors[1]
        stage_descriptor = parent
        batch_descriptor = descriptors[3]
        try:
            held_live_lock_descriptor = os.dup(live_lock_fd)
        except OSError as exc:
            raise _auto_filter_error("live lock is invalid") from exc
        descriptors.append(held_live_lock_descriptor)
        identities["live_lock"] = _auto_filter_validate_live_lock(
            held_live_lock_descriptor,
            state_descriptor,
        )
        active_descriptor, active_identity = (
            _auto_filter_open_relative(
                state_descriptor,
                ".mint-auto-diagnose-active",
                "file",
                mode=0o600,
            )
        )
        descriptors.append(active_descriptor)
        identities["active_marker"] = active_identity
        files["active_marker"] = active_descriptor
        file_data["active_marker"] = _auto_filter_read_descriptor(
            active_descriptor
        )
        if file_data["active_marker"] != (
            f"{contract['batch_id']}\n".encode()
        ):
            raise _auto_filter_error()

        for name, label, mode, executable in (
            ("stage-contract.json", "contract", 0o600, False),
            ("config.toml", "config", 0o600, False),
            ("tokens.toml", "tokens", 0o600, False),
            (
                "zavod-mev-bot-rust-version-cli",
                "binary",
                None,
                True,
            ),
        ):
            descriptor, identity = _auto_filter_open_relative(
                stage_descriptor,
                name,
                "file",
                mode=mode,
                executable=executable,
            )
            descriptors.append(descriptor)
            identities[label] = identity
            files[label] = descriptor
            file_data[label] = _auto_filter_read_descriptor(descriptor)

        if (
            inherited_identity.st_dev,
            inherited_identity.st_ino,
        ) != (
            identities["contract"].st_dev,
            identities["contract"].st_ino,
        ) or file_data["contract"] != contract_data:
            raise _auto_filter_error()

        _auto_filter_require_digest(
            file_data["config"],
            contract["config_sha256"],
        )
        _auto_filter_require_digest(
            file_data["tokens"],
            contract["tokens_sha256"],
        )
        _auto_filter_require_digest(
            file_data["binary"],
            contract["binary_sha256"],
        )
        if file_data["tokens"] != (
            f'tokens = ["{contract["target_mint"]}"]\n'.encode()
        ):
            raise _auto_filter_error("input integrity violation")

        production = {}
        for name, label, parent_descriptor, executable in (
            ("production-config.toml", "config", batch_descriptor, False),
            ("production-tokens.toml", "tokens", batch_descriptor, False),
            (
                "production-binary",
                "binary",
                batch_descriptor,
                False,
            ),
            ("config.toml", "root_config", root_descriptor, False),
            ("tokens.toml", "root_tokens", root_descriptor, False),
            (
                "zavod-mev-bot-rust-version-cli",
                "root_binary",
                root_descriptor,
                True,
            ),
        ):
            descriptor, _identity = _auto_filter_open_relative(
                parent_descriptor,
                name,
                "file",
                mode=None if executable else 0o600,
                executable=executable,
            )
            try:
                production[label] = _auto_filter_read_descriptor(
                    descriptor
                )
            finally:
                os.close(descriptor)
        if (
            production["config"] != production["root_config"]
            or production["tokens"] != production["root_tokens"]
            or production["binary"] != production["root_binary"]
            or file_data["binary"] != production["root_binary"]
        ):
            raise _auto_filter_error("input integrity violation")

        try:
            try:
                from scripts import mint_auto_diagnoser
            except ModuleNotFoundError:
                import mint_auto_diagnoser

            expected_stages = {
                name: data
                for name, data, _mutations
                in mint_auto_diagnoser.build_stage_configs(
                    production["config"]
                )
            }
        except Exception as exc:
            raise _auto_filter_error("input integrity violation") from exc
        if expected_stages.get(contract["stage_name"]) != file_data["config"]:
            raise _auto_filter_error("input integrity violation")

        config = load_config_bytes(file_data["config"])
        if _get(config, "auto", "enable_three_hop") is not True:
            raise _auto_filter_error("input integrity violation")
        return {
            "contract": contract,
            "trusted_root": trusted_root,
            "descriptors": descriptors,
            "identities": identities,
            "files": files,
            "file_data": file_data,
            "stage_fd": stage_descriptor,
            "live_lock_fd": held_live_lock_descriptor,
            "config": config,
        }
    except BaseException:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        raise


def _auto_filter_integrity_matches(
    opened,
    include_all_inputs=False,
):
    fresh = []
    try:
        root_descriptor = os.open(
            opened["trusted_root"],
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        fresh.append(root_descriptor)
        root_identity = _auto_filter_validate_descriptor(
            root_descriptor,
            "directory",
        )
        if (
            root_identity.st_dev,
            root_identity.st_ino,
        ) != (
            opened["identities"]["root"].st_dev,
            opened["identities"]["root"].st_ino,
        ):
            return False
        contract = opened["contract"]
        parent = root_descriptor
        directory_specs = (
            ("state", "state"),
            ("auto-diagnose-runs", "runs"),
            (contract["batch_id"], "batch"),
            ("stages", "stages"),
            (
                f"{contract['stage_index']}-{contract['stage_name']}",
                "stage",
            ),
        )
        for name, label in directory_specs:
            descriptor, identity = _auto_filter_open_relative(
                parent,
                name,
                "directory",
                mode=0o700,
            )
            fresh.append(descriptor)
            expected = opened["identities"][label]
            if (identity.st_dev, identity.st_ino) != (
                expected.st_dev,
                expected.st_ino,
            ):
                return False
            parent = descriptor
        _auto_filter_validate_live_lock(
            opened["live_lock_fd"],
            fresh[1],
            expected_identity=opened["identities"]["live_lock"],
        )
        active_descriptor, active_identity = (
            _auto_filter_open_relative(
                fresh[1],
                ".mint-auto-diagnose-active",
                "file",
                mode=0o600,
            )
        )
        fresh.append(active_descriptor)
        expected_active = opened["identities"]["active_marker"]
        if (
            (
                active_identity.st_dev,
                active_identity.st_ino,
            )
            != (
                expected_active.st_dev,
                expected_active.st_ino,
            )
            or _auto_filter_read_descriptor(active_descriptor)
            != opened["file_data"]["active_marker"]
            or _auto_filter_read_descriptor(
                opened["files"]["active_marker"]
            )
            != opened["file_data"]["active_marker"]
        ):
            return False
        file_specs = (
            (
                "stage-contract.json",
                "contract",
                0o600,
                False,
            ),
            ("config.toml", "config", 0o600, False),
            ("tokens.toml", "tokens", 0o600, False),
            (
                "zavod-mev-bot-rust-version-cli",
                "binary",
                None,
                True,
            ),
        ) if include_all_inputs else (
            ("tokens.toml", "tokens", 0o600, False),
        )
        for name, label, mode, executable in file_specs:
            descriptor, identity = _auto_filter_open_relative(
                parent,
                name,
                "file",
                mode=mode,
                executable=executable,
            )
            fresh.append(descriptor)
            expected = opened["identities"][label]
            if (
                (identity.st_dev, identity.st_ino)
                != (expected.st_dev, expected.st_ino)
                or _auto_filter_read_descriptor(descriptor)
                != opened["file_data"][label]
                or _auto_filter_read_descriptor(opened["files"][label])
                != opened["file_data"][label]
            ):
                return False
        return True
    except (GuardError, OSError):
        return False
    finally:
        for descriptor in reversed(fresh):
            os.close(descriptor)


def _close_auto_filter_live_contract(opened):
    if opened is None:
        return
    for descriptor in reversed(opened["descriptors"]):
        try:
            os.close(descriptor)
        except OSError:
            pass


def _open_auto_filter_log_directory(stage_descriptor):
    try:
        os.mkdir(
            "logs",
            0o700,
            dir_fd=stage_descriptor,
        )
    except FileExistsError:
        pass
    except OSError as exc:
        raise _auto_filter_error(
            "private log directory is invalid"
        ) from exc
    try:
        descriptor, _identity = _auto_filter_open_relative(
            stage_descriptor,
            "logs",
            "directory",
            mode=0o700,
        )
    except GuardError as exc:
        raise _auto_filter_error(
            "private log directory is invalid"
        ) from exc
    return descriptor


def preflight(
    config_path,
    root=None,
    config=None,
    pubkey_resolver=wallet_pubkey,
    balance_reader=get_balance_lamports,
    disk_free_reader=None,
    profile="default",
    binary_path=None,
    binary_fd=None,
):
    root = Path(root or Path(config_path).resolve().parent)
    config_path = Path(config_path)
    config = config if config is not None else load_config(config_path)
    errors = validate_config(config, profile=profile, root=root)
    if errors:
        raise GuardError("configuration validation failed: " + "; ".join(errors))
    try:
        mode = config_path.stat().st_mode & 0o777
    except OSError as exc:
        raise GuardError("config.toml is invalid or unreadable") from exc
    if mode & 0o077:
        raise GuardError("config.toml permissions must not allow group or other access")
    binary = (
        Path(binary_path)
        if binary_path is not None
        else root / "zavod-mev-bot-rust-version-cli"
    )
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise GuardError("Zavod CLI is missing or not executable")
    version = _cli_version(
        binary,
        pass_fds=(
            (binary_fd,)
            if binary_fd is not None
            else ()
        ),
    )
    if version != "0.2.2":
        raise GuardError("unexpected Zavod CLI version")
    free_bytes = (disk_free_reader or (lambda path: shutil.disk_usage(path).free))(root)
    if free_bytes < 100 * 1024 * 1024:
        raise GuardError("insufficient free disk space")
    public_key = pubkey_resolver(_get(config, "wallet", "private_key"))
    balance = balance_reader(_get(config, "rpc", "url"), public_key)
    required = int(_get(config, "stop", "min_balance_lamports", 0)) + LOSS_LIMIT_LAMPORTS
    if balance <= required:
        raise GuardError("insufficient wallet balance for configured stop reserve and loss limit")
    return {
        "preflight": "ok",
        "cli_version": version,
        "wallet": public_key,
        "balance_lamports": balance,
        "senders": "spam,jito,circular,falcon" if profile == "ab-no-swqos" else "spam,jito,helius_swqos,circular,falcon",
        "loss_limit_lamports": LOSS_LIMIT_LAMPORTS,
        "early_stop_lamports": EARLY_STOP_LAMPORTS,
        "timeout_seconds": DEFAULT_TIMEOUT_SECONDS,
    }


def run_guarded(
    config_path,
    timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
    profile="default",
    test_mode=False,
    workspace_root=None,
    diagnostic_mode=None,
    diagnostic_target=None,
    diagnostic_config_sha256=None,
    diagnostic_tokens_sha256=None,
    token_account_snapshot_reader=None,
    mint_account_validator=None,
    batch_contract_fd=None,
    live_lock_fd=None,
):
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int)
        or not 30 <= timeout_seconds <= DEFAULT_TIMEOUT_SECONDS
    ):
        raise GuardError("timeout must be from 30 through 300 seconds")
    if profile == "auto-filter-live" and test_mode:
        raise GuardError("auto-filter-live does not permit test mode")
    if profile != "auto-filter-live" and (
        batch_contract_fd is not None or live_lock_fd is not None
    ):
        raise GuardError("auto-filter-live launch contract is invalid")
    if (profile == "selector-diagnostic") != test_mode:
        raise GuardError(
            "selector-diagnostic profile and test mode must be provided together"
        )
    if profile == "auto-filter-live" and (
        batch_contract_fd is None or live_lock_fd is None
    ):
        raise GuardError(
            "auto-filter-live descriptors are required"
        )
    _validate_selector_launch_contract(
        profile,
        test_mode,
        diagnostic_mode,
        diagnostic_target,
        diagnostic_config_sha256,
        diagnostic_tokens_sha256,
    )
    diagnostic_config_descriptor = None
    diagnostic_tokens_descriptor = None
    diagnostic_tokens_bytes = None
    diagnostic_root_identity = None
    diagnostic_tokens_identity = None
    input_integrity_checker = None
    starting_token_accounts = None
    auto_filter_contract = None
    auto_log_directory_descriptor = None
    fd = None
    try:
        if profile == "auto-filter-live":
            auto_filter_contract = _open_auto_filter_live_contract(
                workspace_root,
                batch_contract_fd,
                live_lock_fd,
            )
            if (
                timeout_seconds
                != auto_filter_contract["contract"]["timeout_seconds"]
            ):
                raise _auto_filter_error(
                    "timeout does not match the batch contract"
                )
            config = auto_filter_contract["config"]
            config_path = Path(
                f"/proc/self/fd/"
                f"{auto_filter_contract['files']['config']}"
            )
            root = Path(
                f"/proc/self/fd/{auto_filter_contract['stage_fd']}"
            )
            input_integrity_checker = lambda: (
                _auto_filter_integrity_matches(auto_filter_contract)
            )
            full_input_integrity_checker = lambda: (
                _auto_filter_integrity_matches(
                    auto_filter_contract,
                    include_all_inputs=True,
                )
            )
        elif profile == "selector-diagnostic":
            root, diagnostic_config_descriptor = (
                _open_selector_diagnostic_config(
                    workspace_root,
                    config_path,
                )
            )
            config_bytes = _require_descriptor_bytes(
                diagnostic_config_descriptor,
                diagnostic_config_sha256,
            )
            (
                diagnostic_tokens_descriptor,
                diagnostic_tokens_bytes,
                diagnostic_root_identity,
                diagnostic_tokens_identity,
            ) = _open_selector_diagnostic_tokens(
                root,
                diagnostic_target,
                diagnostic_tokens_sha256,
            )
            config_path = Path(
                f"/proc/self/fd/{diagnostic_config_descriptor}"
            )
            config = load_config_bytes(config_bytes)
        else:
            config_path = Path(config_path).resolve()
            root = Path(workspace_root or config_path.parent).resolve()
            config = load_config(config_path)
        preflight_options = {
            "root": root,
            "config": config,
            "profile": profile,
        }
        if auto_filter_contract is not None:
            preflight_options["binary_path"] = Path(
                f"/proc/self/fd/"
                f"{auto_filter_contract['files']['binary']}"
            )
            preflight_options["binary_fd"] = (
                auto_filter_contract["files"]["binary"]
            )
        summary = preflight(config_path, **preflight_options)
        public_key = summary["wallet"]
        rpc_url = _get(config, "rpc", "url")
        start_balance = summary["balance_lamports"]
        batch_start_balance = (
            auto_filter_contract["contract"][
                "batch_start_balance_lamports"
            ]
            if auto_filter_contract is not None
            else start_balance
        )
        if (
            auto_filter_contract is not None
            and batch_start_balance - start_balance
            >= EARLY_STOP_LAMPORTS
        ):
            raise _auto_filter_error(
                "cumulative loss threshold reached"
            )
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        log_name = f"{stamp}-zavod-cli.log"
        log_path = root / "logs" / log_name
        if auto_filter_contract is not None:
            auto_log_directory_descriptor = (
                _open_auto_filter_log_directory(
                    auto_filter_contract["stage_fd"]
                )
            )
            try:
                fd = os.open(
                    log_name,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                    dir_fd=auto_log_directory_descriptor,
                )
                _auto_filter_validate_descriptor(
                    fd,
                    "file",
                    mode=0o600,
                )
            except (GuardError, OSError) as exc:
                if fd is not None:
                    os.close(fd)
                    fd = None
                raise _auto_filter_error(
                    "private log path is invalid"
                ) from exc
        else:
            logs_dir = root / "logs"
            logs_dir.mkdir(mode=0o700, exist_ok=True)
            fd = os.open(
                log_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        started = time.monotonic()
        child = None
        pump = None
        result = None
        finalization_interrupted = False
        pump_alive = False
        pump_started = False
        log_handle = None
        cleanup = {
            "exit_code": None,
            "group_absent": True,
            "interrupted": False,
        }
        operator_signal_event = threading.Event()
        prior_sigint = signal.getsignal(signal.SIGINT)
        prior_sigterm = signal.getsignal(signal.SIGTERM)
    except BaseException:
        if diagnostic_config_descriptor is not None:
            os.close(diagnostic_config_descriptor)
        if diagnostic_tokens_descriptor is not None:
            os.close(diagnostic_tokens_descriptor)
        if fd is not None:
            os.close(fd)
        if auto_log_directory_descriptor is not None:
            os.close(auto_log_directory_descriptor)
        _close_auto_filter_live_contract(auto_filter_contract)
        raise

    def interrupt_handler(signum, frame):
        del signum, frame
        operator_signal_event.set()

    try:
        log_handle = os.fdopen(fd, "w", buffering=1)
        signal.signal(signal.SIGINT, interrupt_handler)
        signal.signal(signal.SIGTERM, interrupt_handler)
        command = [str(root / "zavod-mev-bot-rust-version-cli"), "run"]
        if auto_filter_contract is not None:
            command = [
                f"/proc/self/fd/{auto_filter_contract['files']['binary']}",
                "run",
                "--config",
                str(config_path),
            ]
            if not full_input_integrity_checker():
                raise _auto_filter_error("input integrity violation")
        if test_mode:
            config_bytes = _require_descriptor_bytes(
                diagnostic_config_descriptor,
                diagnostic_config_sha256,
            )
            launch_config = load_config_bytes(config_bytes)
            if validate_selector_diagnostic(launch_config):
                raise _selector_input_integrity_error()
            _require_descriptor_bytes(
                diagnostic_tokens_descriptor,
                diagnostic_tokens_sha256,
                expected_bytes=diagnostic_tokens_bytes,
            )
            input_integrity_checker = lambda: _selector_tokens_path_matches(
                root,
                diagnostic_root_identity,
                diagnostic_tokens_identity,
                diagnostic_tokens_sha256,
                diagnostic_tokens_bytes,
            )
            if not input_integrity_checker():
                raise _selector_input_integrity_error()
            try:
                account_snapshot_reader = (
                    token_account_snapshot_reader
                    or get_token_account_pubkeys
                )
                (mint_account_validator or validate_token_mint_account)(
                    rpc_url,
                    diagnostic_target,
                )
                starting_token_accounts = account_snapshot_reader(
                    rpc_url,
                    public_key,
                )
            except Exception:
                result = {
                    "reason": "rpc_error",
                    "start_balance": start_balance,
                    "end_balance": start_balance,
                    "observed_loss": 0,
                    "child_exit_code": None,
                }
                raise _DiagnosticLaunchSkipped()
            config_bytes = _require_descriptor_bytes(
                diagnostic_config_descriptor,
                diagnostic_config_sha256,
            )
            launch_config = load_config_bytes(config_bytes)
            if validate_selector_diagnostic(launch_config):
                raise _selector_input_integrity_error()
            _require_descriptor_bytes(
                diagnostic_tokens_descriptor,
                diagnostic_tokens_sha256,
                expected_bytes=diagnostic_tokens_bytes,
            )
            if not input_integrity_checker():
                raise _selector_input_integrity_error()
            command.extend(["--config", str(config_path), "--test-mode"])
        popen_arguments = {
            "cwd": root,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "start_new_session": True,
            "env": {
                key: value
                for key, value in os.environ.items()
                if key not in (
                    "ZAVOD_LIVE_LOCK_FD",
                    "ZAVOD_BATCH_CONTRACT_FD",
                )
            },
        }
        if diagnostic_config_descriptor is not None:
            popen_arguments["pass_fds"] = (
                diagnostic_config_descriptor,
                diagnostic_tokens_descriptor,
            )
        elif auto_filter_contract is not None:
            popen_arguments["pass_fds"] = (
                auto_filter_contract["stage_fd"],
                auto_filter_contract["files"]["config"],
                auto_filter_contract["files"]["tokens"],
                auto_filter_contract["files"]["binary"],
            )
        child = subprocess.Popen(command, **popen_arguments)
        if (
            auto_filter_contract is not None
            and not full_input_integrity_checker()
        ):
            raise _auto_filter_error("input integrity violation")
        if operator_signal_event.is_set():
            result = {
                "reason": "operator_signal",
                "start_balance": start_balance,
                "end_balance": start_balance,
                "observed_loss": 0,
                "child_exit_code": None,
            }
        else:
            pump = OutputPump(child.stdout, log_handle, config, test_mode=test_mode)
            try:
                pump.start()
            except Exception:
                pump.output_error_event.set()
                result = {
                    "reason": "output_error",
                    "start_balance": start_balance,
                    "end_balance": start_balance,
                    "observed_loss": 0,
                    "child_exit_code": None,
                }
            else:
                pump_started = True
                result = supervise(
                    child=child,
                    start_balance=batch_start_balance,
                    balance_reader=lambda: get_balance_lamports(
                        rpc_url,
                        public_key,
                    ),
                    monotonic=time.monotonic,
                    sleep=time.sleep,
                    output_error_event=pump.output_error_event,
                    input_integrity_checker=input_integrity_checker,
                    protected_output_event=pump.protected_output_event,
                    starting_token_accounts=starting_token_accounts,
                    test_mode_dispatch_event=pump.test_mode_dispatch_event,
                    token_account_reader=lambda: account_snapshot_reader(
                        rpc_url,
                        public_key,
                    ),
                    timeout_seconds=timeout_seconds,
                    cleanup_child=False,
                    operator_signal_event=operator_signal_event,
                    diagnostic=test_mode,
                    enforce_input_integrity=(
                        auto_filter_contract is not None
                    ),
                )
    except _DiagnosticLaunchSkipped:
        pass
    except KeyboardInterrupt:
        result = {
            "reason": "operator_signal",
            "start_balance": start_balance,
            "end_balance": start_balance,
            "observed_loss": 0,
            "child_exit_code": None,
        }
    finally:
        if diagnostic_config_descriptor is not None:
            os.close(diagnostic_config_descriptor)
            diagnostic_config_descriptor = None
        if diagnostic_tokens_descriptor is not None:
            os.close(diagnostic_tokens_descriptor)
            diagnostic_tokens_descriptor = None
        cleanup = (
            _verified_shutdown(child)
            if child is not None
            else {"exit_code": None, "group_absent": True, "interrupted": False}
        )
        if pump_started:
            try:
                _, interrupted = _retry_keyboard_interrupt(
                    lambda: pump.join(5)
                )
                finalization_interrupted |= interrupted
            except GuardError:
                finalization_interrupted = True
                pump.output_error_event.set()
            pump_alive = pump.is_alive()
            if pump_alive:
                pump.output_error_event.set()
                try:
                    _, interrupted = _retry_keyboard_interrupt(pump.stop)
                    finalization_interrupted |= interrupted
                    _, interrupted = _retry_keyboard_interrupt(
                        lambda: pump.join(1)
                    )
                    finalization_interrupted |= interrupted
                except GuardError:
                    finalization_interrupted = True
                pump_alive = pump.is_alive()
        try:
            if not pump_alive:
                try:
                    close_log = (
                        log_handle.close
                        if log_handle is not None
                        else lambda: os.close(fd)
                    )
                    _, interrupted = _retry_keyboard_interrupt(close_log)
                    finalization_interrupted |= interrupted
                except GuardError:
                    finalization_interrupted = True
                    if pump is not None:
                        pump.output_error_event.set()
                except Exception:
                    if pump is not None:
                        pump.output_error_event.set()
            if auto_filter_contract is None:
                log_path.chmod(0o600)
        finally:
            try:
                try:
                    _, interrupted = _retry_keyboard_interrupt(
                        lambda: signal.signal(
                            signal.SIGINT,
                            prior_sigint,
                        )
                    )
                    finalization_interrupted |= interrupted
                    _, interrupted = _retry_keyboard_interrupt(
                        lambda: signal.signal(
                            signal.SIGTERM,
                            prior_sigterm,
                        )
                    )
                    finalization_interrupted |= interrupted
                except GuardError:
                    finalization_interrupted = True
            finally:
                _close_auto_filter_live_contract(
                    auto_filter_contract
                )
                auto_filter_contract = None
                if auto_log_directory_descriptor is not None:
                    os.close(auto_log_directory_descriptor)
                    auto_log_directory_descriptor = None
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
    elif (
        test_mode
        and pump is not None
        and pump.test_mode_dispatch_event.is_set()
    ):
        result["reason"] = "test_mode_dispatch_violation"
    elif (
        (test_mode or profile == "auto-filter-live")
        and pump is not None
        and pump.protected_output_event.is_set()
    ):
        result["reason"] = "protected_output_violation"
    elif pump is not None and (
        pump.output_error_event.is_set()
        or (pump_started and pump.is_alive())
    ):
        result["reason"] = "output_error"
    elif (
        result.get("reason") not in DIAGNOSTIC_VIOLATION_REASONS
        and (
            operator_signal_event.is_set()
            or cleanup["interrupted"]
            or finalization_interrupted
        )
    ):
        result["reason"] = "operator_signal"
    result["duration_seconds"] = round(time.monotonic() - started, 3)
    result["log_path"] = str(log_path.relative_to(root))
    result["loss_limit_lamports"] = LOSS_LIMIT_LAMPORTS
    result["early_stop_lamports"] = EARLY_STOP_LAMPORTS
    if profile == "auto-filter-live":
        result["batch_start_balance"] = batch_start_balance
        result["stage_start_balance"] = start_balance
    return result


def _print_summary(summary):
    for key in (
        "preflight",
        "cli_version",
        "wallet",
        "balance_lamports",
        "senders",
        "loss_limit_lamports",
        "early_stop_lamports",
        "timeout_seconds",
    ):
        print(f"{key}={summary[key]}")


def _print_run_result(result):
    for key in (
        "reason",
        "start_balance",
        "end_balance",
        "observed_loss",
        "duration_seconds",
        "child_exit_code",
        "loss_limit_lamports",
        "early_stop_lamports",
        "log_path",
    ):
        print(f"{key}={result[key]}")


def _print_auto_filter_run_result(result):
    for key in (
        "reason",
        "duration_seconds",
        "child_exit_code",
        "loss_limit_lamports",
        "early_stop_lamports",
        "log_path",
    ):
        print(f"{key}={result[key]}")


def main(argv=None):
    parser = _GuardArgumentParser(
        description="Secret-safe Zavod operations guard"
    )
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        parser_class=_GuardArgumentParser,
    )
    preflight_parser = subparsers.add_parser("preflight")
    preflight_parser.add_argument("--config", default="config.toml")
    preflight_parser.add_argument("--profile", default="default")
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--config", action="append")
    run_parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    run_parser.add_argument("--profile", default="default")
    run_parser.add_argument("--test-mode", action="store_true")
    run_parser.add_argument("--live-confirmed", action="store_true")
    run_parser.add_argument("--diagnostic-mode", action="append")
    run_parser.add_argument("--diagnostic-target", action="append")
    run_parser.add_argument("--config-sha256", action="append")
    run_parser.add_argument("--tokens-sha256", action="append")
    run_parser.add_argument("--workspace-root", action="append")
    run_parser.add_argument(
        "--batch-contract-fd",
        action="append",
        type=int,
    )
    run_parser.add_argument(
        "--live-lock-fd",
        action="append",
        type=int,
    )
    try:
        args = parser.parse_args(argv)
        if args.command == "preflight":
            _print_summary(preflight(args.config, profile=args.profile))
            return 0
        if args.command == "run":
            if not args.live_confirmed:
                raise GuardError("live confirmation is required")
            if args.config is not None and len(args.config) != 1:
                raise GuardError("invalid command arguments")
            config_path = (
                args.config[0]
                if args.config is not None
                else "config.toml"
            )
            diagnostic_contract = (
                args.diagnostic_mode,
                args.diagnostic_target,
                args.config_sha256,
                args.tokens_sha256,
            )
            if args.profile == "selector-diagnostic" and args.test_mode:
                if any(
                    values is None or len(values) != 1
                    for values in diagnostic_contract
                ):
                    raise GuardError(
                        "selector-diagnostic launch contract is invalid"
                    )
                (
                    diagnostic_mode,
                    diagnostic_target,
                    diagnostic_config_sha256,
                    diagnostic_tokens_sha256,
                ) = tuple(values[0] for values in diagnostic_contract)
            else:
                if any(values is not None for values in diagnostic_contract):
                    raise GuardError(
                        "selector-diagnostic launch contract is invalid"
                    )
                (
                    diagnostic_mode,
                    diagnostic_target,
                    diagnostic_config_sha256,
                    diagnostic_tokens_sha256,
                ) = (None, None, None, None)
            if args.profile == "auto-filter-live":
                if (
                    args.test_mode
                    or args.config is not None
                    or args.workspace_root is None
                    or len(args.workspace_root) != 1
                    or args.batch_contract_fd is None
                    or len(args.batch_contract_fd) != 1
                    or args.live_lock_fd is None
                    or len(args.live_lock_fd) != 1
                ):
                    raise GuardError(
                        "auto-filter-live launch contract is invalid"
                    )
                selected_workspace_root = args.workspace_root[0]
                selected_batch_contract_fd = args.batch_contract_fd[0]
                selected_live_lock_fd = args.live_lock_fd[0]
            else:
                if (
                    args.workspace_root is not None
                    or args.batch_contract_fd is not None
                    or args.live_lock_fd is not None
                ):
                    raise GuardError(
                        "auto-filter-live launch contract is invalid"
                    )
                selected_workspace_root = (
                    Path(__file__).resolve().parents[1]
                    if args.test_mode
                    else None
                )
                selected_batch_contract_fd = None
                selected_live_lock_fd = None
            run_options = {
                "test_mode": args.test_mode,
                "workspace_root": selected_workspace_root,
                "diagnostic_mode": diagnostic_mode,
                "diagnostic_target": diagnostic_target,
                "diagnostic_config_sha256": (
                    diagnostic_config_sha256
                ),
                "diagnostic_tokens_sha256": (
                    diagnostic_tokens_sha256
                ),
            }
            if selected_batch_contract_fd is not None:
                run_options["batch_contract_fd"] = (
                    selected_batch_contract_fd
                )
                run_options["live_lock_fd"] = selected_live_lock_fd
            result = run_guarded(
                config_path,
                args.timeout_seconds,
                args.profile,
                **run_options,
            )
            if args.profile == "auto-filter-live":
                _print_auto_filter_run_result(result)
            else:
                _print_run_result(result)
            if (
                args.test_mode
                and result.get("reason") in DIAGNOSTIC_VIOLATION_REASONS
            ):
                return 1
            return 0
    except GuardError as exc:
        print(f"status=failed\nerror={exc}", file=os.sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
